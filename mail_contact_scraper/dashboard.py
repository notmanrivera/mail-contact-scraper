"""Local live dashboard for output/contacts.csv, with Start / Stop for the scraper.

    python -m mail_contact_scraper --dashboard          # dashboard + start scraping right away
    python -m mail_contact_scraper.dashboard            # dashboard only; start from the page

Serves on 127.0.0.1 only, so the contact data never leaves this computer.
"""
import argparse
import importlib.util
import csv
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)
DEFAULT_PORT = 8765
MAX_HTML = 5 * 1024 * 1024  # pasted mailing HTML can carry base64 images
STATIC_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_contacts(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _tail(path, lines=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""


class ScraperControl:
    """Starts the scraper as a child process and reports whether a run is alive."""

    def __init__(self, out_dir, scrape_args=()):
        self.out_dir = Path(out_dir)
        # A button click resumes; it must never wipe previous progress.
        self.scrape_args = [a for a in scrape_args if a != "--fresh"]
        self.first_args = list(scrape_args)
        self.proc = None
        self.started_at = None
        self.lock = threading.Lock()

    @property
    def log_path(self):
        return self.out_dir / "scraper.log"

    def alive(self, status):
        if self.proc and self.proc.poll() is None:
            return True
        return bool(status.get("running")) and _pid_alive(status.get("pid"))

    def busy(self):
        return self.alive(_read_json(self.out_dir / "status.json"))

    def start(self, first=False, extra=()):
        with self.lock:
            if self.alive(_read_json(self.out_dir / "status.json")):
                return False
            args = (self.first_args if first else self.scrape_args) + list(extra)
            if "--output" not in args:
                args = ["--output", str(self.out_dir)] + args
            self.out_dir.mkdir(parents=True, exist_ok=True)
            logfile = open(self.log_path, "w", encoding="utf-8")
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            # Find this package even if the dashboard was started from another folder.
            root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "mail_contact_scraper", *args],
                stdout=logfile, stderr=subprocess.STDOUT, env=env,
                # Own session: Ctrl+C in the dashboard's terminal reaches it only via stop().
                start_new_session=os.name != "nt",
            )
            logfile.close()
            self.started_at = datetime.now().isoformat(timespec="seconds")
            log.info("Scraper iniciado (pid %s)", self.proc.pid)
            return True

    def stop(self):
        status = _read_json(self.out_dir / "status.json")
        pid = self.proc.pid if self.proc and self.proc.poll() is None else status.get("pid")
        if not self.alive(status) or not pid:
            return False
        try:
            # SIGINT lets the scraper save progress and mark the run as stopped.
            os.kill(int(pid), signal.CTRL_C_EVENT if os.name == "nt" else signal.SIGINT)
        except OSError:
            if self.proc:
                self.proc.terminate()
        return True

    def report(self, status):
        info = {"alive": self.alive(status), "can_control": True}
        if self.proc and self.proc.poll() not in (None, 0):
            info["exit_code"] = self.proc.poll()
            # Failed before it could write status.json (e.g. missing .env): surface the log.
            if (status.get("started_at") or "") < (self.started_at or ""):
                info["failure"] = _tail(self.log_path)
        return info


def settings_payload(out_dir, env_path):
    from . import accounts, schedule, sheets

    from .config import load_settings
    from .contacts import read_ignore_file

    cfg = sheets.config(env_path)
    return {
        "ignored": read_ignore_file(load_settings(env_path).ignore_file),
        "mailboxes": accounts.list_mailboxes(env_path),
        "schedule": schedule.status(),
        "sheets": {"configured": bool(cfg["url"] and cfg["token"]), "tab": cfg["tab"], "auto": cfg["auto"],
                   "last": sheets.last_sync(out_dir)},
        "ocr": _ocr_settings(env_path),
        "refresh": _refresh_progress(out_dir, env_path),
    }


def _refresh_progress(out_dir, env_path):
    from .cli import refresh_progress
    from .config import load_settings

    boxes = load_settings(env_path).mailboxes
    done, total = refresh_progress(Path(out_dir) / "messages.jsonl", boxes[0].email if boxes else "")
    return {"done": done, "total": total}


def ignored_payload(out_dir, env_path):
    """GET /ignored: the user's two lists, everyone left out and why, and the mailing's
    unsubscribes and bounces (read only: those are the mailing's to keep)."""
    from .config import load_settings
    from .contacts import read_ignore_file, read_services_file, service_catalog

    settings = load_settings(env_path)
    omitted = _read_json_list(Path(out_dir) / "omitted.json")
    hides = {"ignored": {}, "service": {}}
    for o in omitted:
        if o.get("reason") in hides and o.get("rule"):
            hides[o["reason"]][o["rule"]] = hides[o["reason"]].get(o["rule"], 0) + 1
    added, disabled = read_services_file(settings.services_file)
    services = [{"entry": e, "category": "Agregados por ti", "builtin": False, "disabled": False,
                 "hides": hides["service"].get(e, 0)} for e in added]
    services += [{"entry": e, "category": c, "builtin": True, "disabled": e in disabled,
                  "hides": hides["service"].get(e, 0)} for c, e in service_catalog()]
    return {
        "services": services,
        "services_on": settings.skip_services,
        "ignored": [{"value": v, "hides": hides["ignored"].get(v, 0)} for v in read_ignore_file(settings.ignore_file)],
        "allowed": read_ignore_file(settings.allow_file),
        "omitted": omitted,
        "suppression": _read_suppression(Path(out_dir) / "suppression.txt"),
    }


def _read_json_list(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _read_suppression(path):
    """mailing's suppression.txt: '# baja 2026-10-27' headers, then one address per line."""
    out, reason, when = [], "otro", ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.startswith("#"):
            words = line[1:].split()
            reason, when = (words[0] if words else "otro"), (words[1] if len(words) > 1 else "")
        elif line.strip():
            out.append({"email": line.strip().lower(), "reason": reason, "date": when})
    return out


OCR_ENGINES = ("tesseract", "auto", "claude")


def _ocr_settings(env_path):
    from . import envfile

    values = envfile.read(env_path)
    engine = (values.get("OCR_ENGINE") or "tesseract").lower()
    installed = importlib.util.find_spec("anthropic") is not None  # importing it takes ~1 s
    # Never send the key back to the page; only whether there is one.
    return {"engine": engine if engine in OCR_ENGINES else "tesseract",
            "has_key": bool(values.get("ANTHROPIC_API_KEY")), "available": installed}


def _save_ocr(env_path, body):
    from . import envfile

    engine = (body.get("engine") or "").lower()
    if engine not in OCR_ENGINES:
        raise ValueError("Elige cómo leer las firmas en imagen.")
    key = (body.get("api_key") or "").strip()
    if key and not key.startswith("sk-ant-"):
        raise ValueError("La clave de Anthropic empieza con sk-ant-.")
    if engine != "tesseract" and not key and not envfile.read(env_path).get("ANTHROPIC_API_KEY"):
        raise ValueError("Para usar Claude pega tu clave de API de Anthropic.")
    envfile.update(env_path, {"OCR_ENGINE": engine, **({"ANTHROPIC_API_KEY": key} if key else {})})


def settings_action(path, body, out_dir, env_path, control=None):
    """POST /settings/<action>. Returns (status code, payload)."""
    from . import accounts, schedule, sheets

    try:
        if path == "/settings/services":
            from . import cli
            from .config import load_settings
            from .contacts import update_services_file

            settings = load_settings(env_path)
            update_services_file(settings.services_file, **{k: body.get(k) for k in ("add", "remove", "disable", "enable")})
            if not (control and control.alive(_read_json(Path(out_dir) / "status.json"))):
                cli.rebuild(out_dir, settings)
            return 200, {"ok": True, **settings_payload(out_dir, env_path), "lists": ignored_payload(out_dir, env_path)}
        if path in ("/settings/ignore", "/settings/allow"):
            from . import cli
            from .config import load_settings
            from .contacts import ALLOW_HEADER, IGNORE_HEADER, read_ignore_file, split_entries, update_ignore_file

            settings = load_settings(env_path)
            lists = [(settings.ignore_file, IGNORE_HEADER), (settings.allow_file, ALLOW_HEADER)]
            (this, header), (other, other_header) = lists if path == "/settings/ignore" else lists[::-1]
            added = split_entries(body.get("add"))
            update_ignore_file(this, add=" ".join(added), remove=body.get("remove"), header=header)
            # One entry lives in one list: ignoring something takes it off "permitidos" and back.
            if set(added) & set(read_ignore_file(other)):
                for value in added:
                    update_ignore_file(other, remove=value, header=other_header)
            # A running scan re-reads the list on its next batch; otherwise apply it now.
            if not (control and control.alive(_read_json(Path(out_dir) / "status.json"))):
                cli.rebuild(out_dir, settings)
            return 200, {"ok": True, "added": added, **settings_payload(out_dir, env_path),
                         "lists": ignored_payload(out_dir, env_path)}
        if path == "/settings/mailbox":
            email, password = (body.get("email") or "").strip(), body.get("password") or ""
            if not accounts.EMAIL_RE.match(email) or not password:
                raise accounts.AccountError("Escribe el correo y su contraseña de aplicación.")
            host = accounts.test_connection(email, password, (body.get("host") or "").strip(), env_path=env_path)
            accounts.save_mailbox(env_path, email, password, host)
        elif path == "/settings/mailbox/remove":
            accounts.remove_mailbox(env_path, body.get("email"))
        elif path == "/settings/schedule":
            if body.get("enabled"):
                hour, _, minute = str(body.get("time") or "08:00").partition(":")
                schedule.install(int(hour), int(minute or 0), str(Path(out_dir).resolve()))
            else:
                schedule.uninstall()
        elif path == "/settings/ocr":
            _save_ocr(env_path, body)
        elif path == "/settings/sheets/script":
            return 200, {"ok": True, "script": sheets.script(sheets.ensure_token(env_path))}
        elif path == "/settings/sheets/push":
            url = (body.get("url") or "").strip() or None
            return 200, {"ok": True, "result": sheets.push(out_dir, env_path, url), **settings_payload(out_dir, env_path)}
        else:
            return 404, {"ok": False, "error": "not found"}
    except (accounts.AccountError, schedule.ScheduleError, sheets.SheetsError, ValueError) as exc:
        return 400, {"ok": False, "error": str(exc), **settings_payload(out_dir, env_path)}
    return 200, {"ok": True, **settings_payload(out_dir, env_path)}


def _mailing_send():
    try:
        from mailing import send
    except ImportError:  # a pip install without the mailing folder
        return None
    return send


def mailing_payload(env_path):
    """Campaigns and the mailboxes a test can go out from, for the "Envío de prueba" panel."""
    from . import accounts, envfile

    send = _mailing_send()
    if not send:
        return {"available": False, "campaigns": [], "senders": []}
    senders = [m["email"] for m in accounts.list_mailboxes(env_path) if m["has_password"]]
    found = send.campaigns()
    default = found[0]["sender"] if found else ""
    relay = bool(envfile.environ(env_path).get("SMTP_USER"))
    if default and default.lower() not in (e.lower() for e in senders) and relay:
        senders.insert(0, default)  # a relay can send as any address of the domain
    return {"available": True, "campaigns": found, "senders": senders, "default_sender": default}


def mailing_action(path, body, out_dir, env_path, run):
    """POST /mailing/<action>: a test, the campaign run, or the unsubscribe check.
    Returns (status code, payload)."""
    send = _mailing_send()
    if not send:
        return 404, {"ok": False, "error": "Esta versión no incluye los mailings."}
    from mailing import app as mailing_app

    slug = str(body.get("campaign") or "")
    try:
        if path == "/mailing/test":
            return 200, {"ok": True, **send.send_test_html(
                body.get("html"), body.get("subject"), body.get("to"), body.get("from"), slug, env_path,
                name=str(body.get("name") or ""), contacts_csv=Path(out_dir) / "contacts.csv")}
        if path == "/mailing/campaign/start":
            return 200, {"ok": True, "run": run.start(out_dir, env_path, body)}
        if path == "/mailing/campaign/stop":
            return 200, {"ok": run.stop(), "run": run.status()}
        if path == "/mailing/bajas":
            unsub, bounced = mailing_app.check_unsubscribes(out_dir, env_path, slug)
            return 200, {"ok": True, "unsubscribed": len(unsub), "bounced": len(bounced)}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 404, {"ok": False, "error": "not found"}


def mailing_overview(query, out_dir, env_path, run):
    """GET /mailing/campaign: the Campaña tab (list, room per mailbox, log, current run)."""
    if not _mailing_send():
        return 404, {"available": False}
    from mailing import app as mailing_app

    try:
        return 200, mailing_app.overview(out_dir, env_path, run, query.get("campaign", [""])[0],
                                         query.get("from", []), query.get("daily_limit", ["0"])[0])
    except ValueError as exc:
        return 400, {"error": str(exc)}


def update_action(updater, body, control, out_dir):
    """POST /update/<...> from the packaged app. Returns (status code, payload)."""
    from .updater import UpdateError

    try:
        if "token" in body:
            updater.save_token(str(body.get("token") or ""))
        if body.get("apply"):
            updater.apply_now(busy=control.alive(_read_json(Path(out_dir) / "status.json")))
        else:
            updater.check()
            threading.Thread(target=updater.download_quietly, daemon=True).start()
    except (UpdateError, OSError) as exc:
        return 400, {"ok": False, "error": str(exc), "update": updater.status()}
    return 200, {"ok": True, "update": updater.status()}


def make_handler(out_dir, control, env_path=".env", updater=None):
    out_dir = Path(out_dir)
    package = resources.files(__package__)
    page = package.joinpath("dashboard.html").read_bytes()
    send_run = None
    if _mailing_send():
        from mailing.app import SendRun
        send_run = SendRun()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        def _send(self, body, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200):
            self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json", status)

        def _local_host(self):
            # Blocks DNS-rebinding pages from reading the contacts.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local_host():
                return self._send(b"forbidden", "text/plain", 403)
            url = urlparse(self.path)
            csv_path = out_dir / "contacts.csv"
            if url.path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif url.path == "/data":
                since = parse_qs(url.query).get("since", [""])[0]
                version = str(csv_path.stat().st_mtime_ns) if csv_path.exists() else "0"
                status = _read_json(out_dir / "status.json")
                payload = {"status": status, "version": version, "control": control.report(status),
                           "update": updater.status() if updater else None}
                if since != version:
                    payload["contacts"] = _read_contacts(csv_path)
                self._json(payload)
            elif url.path == "/settings":
                self._json(settings_payload(out_dir, env_path))
            elif url.path == "/ignored":
                self._json(ignored_payload(out_dir, env_path))
            elif url.path == "/mailing":
                self._json(mailing_payload(env_path))
            elif url.path == "/mailing/name":
                send = _mailing_send()
                address = parse_qs(url.query).get("email", [""])[0].strip()
                name = send.contact_name(csv_path, address) if send and address else ""
                first = send.split_name(name)[0] if name else ""
                self._json({"name": name, "first": first})
            elif url.path == "/mailing/export" and _mailing_send():
                from mailing import app as mailing_app
                q = parse_qs(url.query)
                try:
                    text, name = mailing_app.export_csv(out_dir, q.get("campaign", [""])[0], q.get("which", [""])[0])
                except ValueError as exc:
                    return self._send(str(exc).encode(), "text/plain; charset=utf-8", 400)
                self.send_response(200)
                body = text.encode("utf-8")
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/mailing/campaign":
                code, payload = mailing_overview(parse_qs(url.query), out_dir, env_path, send_run)
                self._json(payload, code)
            elif url.path == "/contacts.csv" and csv_path.exists():
                self._send(csv_path.read_bytes(), "text/csv; charset=utf-8")
            elif url.path.startswith("/static/"):
                name = url.path.rsplit("/", 1)[-1]
                asset = package.joinpath("static", name)
                if name and asset.is_file() and Path(name).suffix in STATIC_TYPES:
                    self._send(asset.read_bytes(), STATIC_TYPES[Path(name).suffix])
                else:
                    self._send(b"not found", "text/plain", 404)
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            # A custom header can't be sent cross-site without a CORS preflight, which this
            # server never grants, so other websites can't press these buttons.
            if not self._local_host() or self.headers.get("X-Requested-With") != "dashboard":
                return self._send(b"forbidden", "text/plain", 403)
            if self.path == "/start":
                started = control.start()
                self._json({"ok": started}, 200 if started else 409)
            elif self.path == "/refresh":
                started = control.start(extra=["--refresh"])
                self._json({"ok": started}, 200 if started else 409)
            elif self.path == "/reocr":
                started = control.start(extra=["--reocr"])
                self._json({"ok": started}, 200 if started else 409)
            elif self.path == "/stop":
                stopped = control.stop()
                self._json({"ok": stopped}, 200 if stopped else 409)
            elif self.path.startswith(("/settings/", "/mailing/")) or (self.path == "/update" and updater):
                length = int(self.headers.get("Content-Length") or 0)
                if length > (MAX_HTML if self.path == "/mailing/test" else 65536):
                    return self._send(b"too large", "text/plain", 413)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                body = body if isinstance(body, dict) else {}
                if self.path == "/update":
                    code, payload = update_action(updater, body, control, out_dir)
                elif self.path.startswith("/mailing/"):
                    code, payload = mailing_action(self.path, body, out_dir, env_path, send_run)
                else:
                    code, payload = settings_action(self.path, body, out_dir, env_path, control)
                self._json(payload, code)
            else:
                self._send(b"not found", "text/plain", 404)

    return Handler


def serve(out_dir, port=DEFAULT_PORT, open_browser=True, control=None, env_path=".env", updater=None):
    control = control or ScraperControl(out_dir)
    handler = make_handler(out_dir, control, env_path, updater)  # outside the loop: its errors aren't port errors
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    else:
        raise OSError(f"No free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log.info("Dashboard: %s", url)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    return server, url


def main(argv=None):
    p = argparse.ArgumentParser(prog="mail_contact_scraper.dashboard")
    p.add_argument("--output", default=None, help="Folder with contacts.csv (default: OUTPUT_DIR or output)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--env", default=".env", help="Settings file the page edits (default: .env)")
    p.add_argument("--autostart", action="store_true", help="Start scraping as soon as the dashboard opens")
    p.add_argument("scrape_args", nargs=argparse.REMAINDER, help="Arguments for the scraper, after --")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.output is None:
        from .config import load_settings
        args.output = load_settings(args.env).output_dir
    scrape_args = args.scrape_args[1:] if args.scrape_args[:1] == ["--"] else args.scrape_args
    control = ScraperControl(args.output, scrape_args)
    server, _ = serve(args.output, args.port, not args.no_browser, control, args.env)
    if args.autostart:
        control.start(first=True)
    log.info("Ctrl+C para cerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Only the run this dashboard started; a run from another terminal keeps going.
        if control.proc and control.proc.poll() is None and control.stop():
            log.info("Deteniendo el scraper y guardando el avance…")
            try:
                control.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                control.proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
