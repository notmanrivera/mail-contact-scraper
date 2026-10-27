"""Self-update for the packaged Mac app from the repository's GitHub Releases.

Every merge to main publishes a release (see .github/workflows/mac-app.yml). The app checks
for it at launch and every half hour while it runs, downloads it in the background and installs it the next
time the app opens, or right away from the dashboard's "Actualizar ahora" button.

The repository is private, so the download needs a read-only GitHub token. It is pasted in the
dashboard's settings and kept in this user's .env, never inside the app.
"""
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import envfile

try:  # written by packaging/build_mac.sh; absent when running from the source code
    from ._build_info import ARCH, REPO, VERSION
except ImportError:
    VERSION, REPO, ARCH = "dev", "", ""

log = logging.getLogger(__name__)
API = "https://api.github.com"
CHECK_EVERY = 30 * 60
TOKEN_KEY = "UPDATE_GITHUB_TOKEN"

# Replaces the bundle once this process has exited, then reopens it if asked.
SWAP_SCRIPT = """#!/bin/sh
pid="$1"; app="$2"; new="$3"; relaunch="$4"
i=0
while kill -0 "$pid" 2>/dev/null && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done
old="$app.old-$$"
if mv "$app" "$old"; then
  if mv "$new" "$app"; then rm -rf "$old"; else mv "$old" "$app"; fi
fi
xattr -dr com.apple.quarantine "$app" 2>/dev/null
[ "$relaunch" = "1" ] && open "$app"
rm -f "$0"
"""


class UpdateError(RuntimeError):
    pass


def parse_version(tag):
    return tuple(int(n) for n in re.findall(r"\d+", tag or ""))


def is_newer(latest, current):
    if not latest or current == "dev":
        return False
    return parse_version(latest) > parse_version(current)


def release_changes(notes, limit=8):
    """The pull request titles in GitHub's generated release notes ("* Title by @user in https://...")."""
    changes = []
    for line in (notes or "").splitlines():
        m = re.match(r"\s*[*-]\s+(.+?)(?:\s+by\s+@\S+)?(?:\s+in\s+https?://\S+)?\s*$", line)
        if m and not m.group(1).lower().startswith("full changelog"):
            changes.append(m.group(1))
    return changes[:limit]


def app_bundle(executable=None):
    """The .app folder this executable runs from, or None outside a bundle."""
    for parent in Path(executable or sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def bundle_id(app):
    try:
        return plistlib.loads((Path(app) / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier")
    except (OSError, ValueError):
        return None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    # GitHub sends asset downloads to a signed storage URL that rejects a second credential.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_DropAuthOnRedirect)


class Updater:
    def __init__(self, data_dir, env_path=".env", version=VERSION, repo=REPO, arch=ARCH,
                 bundle=None, opener=_OPENER.open, frozen=None):
        self.data_dir = Path(data_dir)
        self.env_path = env_path
        self.version, self.repo, self.arch = version, repo, arch
        self.bundle = Path(bundle) if bundle else app_bundle()
        self.opener = opener
        frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.enabled = bool(frozen and repo and version != "dev" and self.bundle)
        self.latest = None   # {"tag", "notes", "asset_url", "published_at"}
        self.error = ""
        self.checked_at = None
        self.downloading = False
        self.installing = False
        self.stage = ""      # what the updater is doing right now, shown live in the dashboard
        self.received = 0    # bytes of the update downloaded so far
        self.total = 0       # its size, 0 if unknown
        self.on_exit = None  # set by the app: stops it so the swap script can replace the bundle
        self.lock = threading.Lock()

    # ---------- GitHub ----------
    @property
    def updates_dir(self):
        return self.data_dir / "updates"

    def token(self):
        return envfile.environ(self.env_path).get(TOKEN_KEY, "").strip()

    def _request(self, url, accept="application/vnd.github+json", timeout=20):
        req = urllib.request.Request(url, headers={
            "Accept": accept, "User-Agent": "contactos-dindustrimex", "X-GitHub-Api-Version": "2022-11-28"})
        token = self.token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        return self.opener(req, timeout=timeout)

    def check(self):
        """Ask GitHub for the newest release. Returns status(); errors land in status()["error"]."""
        if not self.enabled:
            return self.status()
        if not self.downloading:
            self.stage = "checking"
        try:
            with self._request(f"{API}/repos/{self.repo}/releases?per_page=15") as resp:
                releases = json.loads(resp.read().decode("utf-8"))
            # Newest published release with the app for this Mac: one built on a single Mac
            # (packaging/release_local.sh) only carries that Mac's architecture.
            release, asset = self._newest_for_this_mac(releases)
            if not asset:
                raise UpdateError(f"Ninguna versión publicada trae la app para esta Mac ({self.arch}).")
            self.latest = {"tag": release.get("tag_name", ""), "notes": (release.get("body") or "")[:3000],
                           "asset_url": asset["url"], "size": int(asset.get("size") or 0),
                           "published_at": release.get("published_at", "")}
            self.error = ""
        except urllib.error.HTTPError as exc:
            self.error = self._http_message(exc.code)
        except (urllib.error.URLError, OSError) as exc:
            self.error = f"Sin conexión con GitHub ({getattr(exc, 'reason', exc)})."
        except (UpdateError, ValueError, KeyError) as exc:
            self.error = str(exc)
        if self.stage == "checking":
            self.stage = ""
        self.checked_at = datetime.now().isoformat(timespec="seconds")
        return self.status()

    def _newest_for_this_mac(self, releases):
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            for asset in release.get("assets", []):
                if asset.get("name", "").endswith(f"-{self.arch}.app.zip"):
                    return release, asset
        return None, None

    def _http_message(self, code):
        if code == 401:
            return "GitHub rechazó el token. Crea uno nuevo y pégalo en Configuración ▸ Actualizaciones."
        if code in (403, 404) and not self.token():
            return "Para buscar actualizaciones hace falta un token de GitHub (el repositorio es privado)."
        if code == 404:
            return "El token no tiene acceso al repositorio o todavía no hay versiones publicadas."
        return f"GitHub respondió con error {code}."

    # ---------- Download ----------
    def staged(self):
        """(tag, path to the downloaded .app) of the newest update ready to install, or None."""
        best = None
        if self.updates_dir.is_dir():
            for folder in self.updates_dir.iterdir():
                apps = [p for p in folder.glob("*.app") if p.is_dir()] if folder.is_dir() else []
                if apps and is_newer(folder.name, self.version) and (not best or is_newer(folder.name, best[0])):
                    best = (folder.name, apps[0])
        return best

    def download(self):
        """Download and unpack the latest release next to the settings. Returns its .app path."""
        if not self.latest:
            raise UpdateError(self.error or "Todavía no se ha buscado una versión nueva.")
        tag = self.latest["tag"]
        ready = self.staged()
        if ready and ready[0] == tag:
            return ready[1]
        with self.lock:
            self.downloading = True
            self.stage, self.received, self.total = "downloading", 0, self.latest.get("size", 0)
            try:
                self.updates_dir.mkdir(parents=True, exist_ok=True)
                work = Path(tempfile.mkdtemp(prefix=".download-", dir=self.updates_dir))
                try:
                    archive = work / "app.zip"
                    with self._request(self.latest["asset_url"], "application/octet-stream", timeout=120) as resp, \
                            open(archive, "wb") as fh:
                        length = getattr(resp, "headers", None) and resp.headers.get("Content-Length")
                        self.total = int(length) if length and length.isdigit() else self.total
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                            self.received += len(chunk)
                    self.stage = "unpacking"
                    unpacked = work / "app"
                    # ditto keeps the bundle's symlinks and signatures, unlike zipfile.
                    result = subprocess.run(["ditto", "-x", "-k", str(archive), str(unpacked)],
                                            capture_output=True, text=True)
                    if result.returncode != 0:
                        raise UpdateError(f"No se pudo descomprimir la actualización: {result.stderr.strip()}")
                    apps = list(unpacked.glob("*.app"))
                    if not apps or bundle_id(apps[0]) != bundle_id(self.bundle):
                        raise UpdateError("La descarga no es esta app.")
                    target = self.updates_dir / tag
                    shutil.rmtree(target, ignore_errors=True)
                    target.mkdir()
                    final = target / self.bundle.name
                    apps[0].rename(final)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                self._prune(keep=tag)
                log.info("Actualización %s descargada", tag)
                return final
            except urllib.error.HTTPError as exc:
                raise UpdateError(self._http_message(exc.code)) from exc
            except (urllib.error.URLError, OSError) as exc:
                raise UpdateError(f"No se pudo descargar la actualización: {exc}") from exc
            finally:
                self.downloading = False
                self.stage = ""

    def _prune(self, keep):
        for folder in self.updates_dir.iterdir():
            if folder.name != keep:
                shutil.rmtree(folder, ignore_errors=True)

    # ---------- Install ----------
    def install_problem(self):
        """Why the bundle can't be replaced in place, or ''."""
        if not self.bundle:
            return "La app no se está ejecutando desde un paquete .app."
        if str(self.bundle).startswith("/Volumes/"):
            return "Abre la app desde la carpeta Aplicaciones, no desde el disco del instalador."
        if not os.access(self.bundle.parent, os.W_OK):
            return (f"Esta cuenta de la Mac no puede modificar {self.bundle.parent}. Pide a un administrador "
                    "que actualice la app, o pásala a la carpeta Aplicaciones de tu usuario.")
        return ""

    def install(self, new_app, relaunch=True):
        """Start the swap script for new_app. The caller must exit right after."""
        problem = self.install_problem()
        if problem:
            raise UpdateError(problem)
        fd, script = tempfile.mkstemp(prefix="contactos-update-", suffix=".sh")
        with os.fdopen(fd, "w") as fh:
            fh.write(SWAP_SCRIPT)
        subprocess.Popen(["/bin/sh", script, str(os.getpid()), str(self.bundle), str(new_app), "1" if relaunch else "0"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self.installing = True
        log.info("Instalando la actualización desde %s", new_app)

    def install_staged_on_launch(self):
        """At launch: if an update is already downloaded, swap it in and relaunch. True = exit now."""
        ready = self.staged() if self.enabled else None
        if not ready or self.install_problem():
            return False
        try:
            self.install(ready[1], relaunch=True)
        except (UpdateError, OSError) as exc:
            log.warning("No se pudo instalar la actualización: %s", exc)
            return False
        return True

    def apply_now(self, busy=False):
        """The dashboard's "Actualizar ahora": download if needed, install, restart. Runs in a thread."""
        if not self.enabled:
            raise UpdateError("Las actualizaciones solo funcionan en la app instalada.")
        if busy:
            raise UpdateError("Hay un escaneo en curso. Detenlo o espera a que termine para actualizar.")
        problem = self.install_problem()
        if problem:
            raise UpdateError(problem)
        ready = self.staged()
        if not ready:
            if not self.latest:
                self.check()
            if not self.available():
                raise UpdateError(self.error or "Ya tienes la versión más reciente.")
        self.installing = True

        def work():
            try:
                app = self.staged()[1] if self.staged() else self.download()
                self.install(app, relaunch=True)
                if self.on_exit:
                    self.on_exit()
            except (UpdateError, OSError) as exc:
                self.error = str(exc)
                self.installing = False

        threading.Thread(target=work, daemon=True).start()

    # ---------- Background ----------
    def available(self):
        return bool(self.latest and is_newer(self.latest["tag"], self.version))

    def check_and_download(self):
        self.check()
        self.download_quietly()

    def download_quietly(self):
        if self.available() and not self.install_problem() and not self.downloading:
            try:
                self.download()
            except UpdateError as exc:
                self.error = str(exc)

    def start_background(self, every=CHECK_EVERY):
        if not self.enabled:
            return None

        def loop():
            while True:
                try:
                    self.check_and_download()
                except Exception:  # never let the updater take the app down
                    log.exception("Error al buscar actualizaciones")
                time.sleep(every)

        thread = threading.Thread(target=loop, daemon=True, name="updater")
        thread.start()
        return thread

    def save_token(self, token):
        envfile.update(self.env_path, {TOKEN_KEY: token.strip() or None})

    def status(self):
        ready = self.staged() if self.enabled else None
        latest = self.latest["tag"] if self.latest else (ready[0] if ready else None)
        return {
            "enabled": self.enabled,
            "version": self.version,
            "latest": latest,
            "available": self.available() or bool(ready),
            "ready": bool(ready),
            "notes": self.latest["notes"] if self.latest else "",
            "changes": release_changes(self.latest["notes"]) if self.latest else [],
            "error": self.error,
            "checked_at": self.checked_at,
            "has_token": bool(self.token()),
            "downloading": self.downloading,
            "installing": self.installing,
            # apply_now() downloads after setting installing: show the download until it's done.
            "stage": ((self.stage if self.stage in ("downloading", "unpacking") else "installing")
                      if self.installing else self.stage or ("ready" if ready else "")),
            "received": self.received,
            "total": self.total,
            "problem": self.install_problem() if self.enabled else "",
        }


def main(argv=None):
    """`<app> -m mail_contact_scraper.updater`: check GitHub once and print the result (diagnostics)."""
    from .app import data_dir

    print(json.dumps(Updater(data_dir()).check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
