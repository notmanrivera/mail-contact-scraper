import io
import json
import os
import plistlib
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mail_contact_scraper import app, dashboard, envfile, updater


def make_bundle(root, name="Contactos D-Industrimex.app", ident="com.dindustrimex.contactos"):
    bundle = Path(root) / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": ident}))
    return bundle


class FakeGitHub:
    """Stands in for urlopen: answers the releases API and asset downloads."""

    def __init__(self, release=None, code=None, asset=b"zip"):
        self.release, self.code, self.asset = release, code, asset
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.code:
            raise urllib.error.HTTPError(req.full_url, self.code, "error", {}, io.BytesIO(b""))
        body = self.asset if req.get_header("Accept") == "application/octet-stream" else json.dumps(self.release).encode()
        return io.BytesIO(body)


RELEASE = {
    "tag_name": "v1.0.12", "body": "Cambios", "published_at": "2026-10-26T20:00:00Z",
    "assets": [
        {"name": "Contactos-D-Industrimex-1.0.12-x86_64.app.zip", "url": "https://api.github.com/assets/1"},
        {"name": "Contactos-D-Industrimex-1.0.12-arm64.app.zip", "url": "https://api.github.com/assets/2"},
        {"name": "Contactos-D-Industrimex-1.0.12-arm64.dmg", "url": "https://api.github.com/assets/3"},
    ],
}


def make_updater(tmp_path, opener, version="1.0.7", token=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = tmp_path / ".env"
    env.write_text(f"UPDATE_GITHUB_TOKEN={token}\n" if token else "")
    bundle = make_bundle(tmp_path / "Applications")
    return updater.Updater(tmp_path / "data", env_path=env, version=version, repo="notmanrivera/mail-contact-scraper",
                           arch="arm64", bundle=bundle, opener=opener, frozen=True)


def test_versions():
    assert updater.is_newer("v1.0.12", "1.0.7")
    assert not updater.is_newer("v1.0.7", "1.0.7")
    assert not updater.is_newer("v2.0.0", "dev")


def test_check_picks_this_mac_and_sends_token(tmp_path):
    gh = FakeGitHub(RELEASE)
    up = make_updater(tmp_path, gh, token="github_pat_x")
    status = up.check()
    assert status["available"] and status["latest"] == "v1.0.12" and not status["error"]
    assert up.latest["asset_url"].endswith("/2")
    assert gh.requests[0].get_header("Authorization") == "Bearer github_pat_x"
    assert gh.requests[0].full_url.endswith("/repos/notmanrivera/mail-contact-scraper/releases/latest")


def test_check_without_token_on_private_repo(tmp_path):
    up = make_updater(tmp_path, FakeGitHub(code=404))
    status = up.check()
    assert not status["available"] and "token" in status["error"]
    up = make_updater(tmp_path / "b", FakeGitHub(code=401), token="viejo")
    assert "rechazó el token" in up.check()["error"]


def test_up_to_date(tmp_path):
    up = make_updater(tmp_path, FakeGitHub(RELEASE), version="1.0.12")
    assert not up.check()["available"]


def test_disabled_outside_the_app(tmp_path):
    up = updater.Updater(tmp_path, env_path=tmp_path / ".env", version="dev", repo="", frozen=False)
    assert up.status()["enabled"] is False
    assert up.check()["latest"] is None


def test_download_stages_the_new_app(tmp_path, monkeypatch):
    up = make_updater(tmp_path, FakeGitHub(RELEASE, asset=b"PK-fake"))
    up.check()

    def fake_ditto(cmd, **kw):
        assert cmd[:3] == ["ditto", "-x", "-k"] and Path(cmd[3]).read_bytes() == b"PK-fake"
        make_bundle(cmd[4])
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(updater.subprocess, "run", fake_ditto)
    staged = up.download()
    assert staged == tmp_path / "data" / "updates" / "v1.0.12" / "Contactos D-Industrimex.app"
    assert up.staged() == ("v1.0.12", staged)
    assert up.status()["ready"]
    # Nothing left over from the download.
    assert [p.name for p in (tmp_path / "data" / "updates").iterdir()] == ["v1.0.12"]


def test_download_rejects_another_app(tmp_path, monkeypatch):
    up = make_updater(tmp_path, FakeGitHub(RELEASE))
    up.check()
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **kw: (
        make_bundle(cmd[4], ident="com.otra.app"), type("R", (), {"returncode": 0, "stderr": ""})())[1])
    with pytest.raises(updater.UpdateError):
        up.download()
    assert up.staged() is None


def test_install_refuses_from_the_dmg(tmp_path):
    up = make_updater(tmp_path, FakeGitHub(RELEASE))
    up.bundle = Path("/Volumes/Contactos D-Industrimex/Contactos D-Industrimex.app")
    assert "Aplicaciones" in up.install_problem()
    with pytest.raises(updater.UpdateError):
        up.install(tmp_path / "x.app")


def test_swap_script_replaces_and_keeps_old_on_failure(tmp_path):
    script = tmp_path / "swap.sh"
    script.write_text(updater.SWAP_SCRIPT.replace('[ "$relaunch" = "1" ] && open "$app"', ":").replace("xattr", ": xattr"))
    old, new = make_bundle(tmp_path / "Applications"), make_bundle(tmp_path / "updates")
    (new / "nuevo").write_text("1")
    os.system(f'/bin/sh "{script}" 999999 "{old}" "{new}" 0')
    assert (old / "nuevo").exists() and not new.exists()
    assert not list((tmp_path / "Applications").glob("*.old-*"))


def test_redirect_drops_token_for_other_hosts():
    handler = updater._DropAuthOnRedirect()
    req = urllib.request.Request("https://api.github.com/assets/2", headers={"Authorization": "Bearer x"})
    same = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/assets/3")
    other = handler.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/f?sig=1")
    assert same.get_header("Authorization") == "Bearer x"
    assert other.get_header("Authorization") is None


def test_save_token(tmp_path):
    up = make_updater(tmp_path, FakeGitHub(RELEASE))
    up.save_token(" github_pat_abc ")
    assert envfile.read(up.env_path)["UPDATE_GITHUB_TOKEN"] == "github_pat_abc"
    assert up.status()["has_token"]


# ---------- app entry point ----------

def test_prepare_creates_private_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = app.prepare(tmp_path / "Soporte")
    assert Path.cwd() == folder
    env = folder / ".env"
    assert env.exists() and oct(env.stat().st_mode & 0o777) == "0o600"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\n")
    app.prepare(folder)  # never overwrites the user's settings
    assert "pedro" in env.read_text()


def test_bundled_tesseract_goes_first_on_path(tmp_path, monkeypatch):
    res = tmp_path / "Resources"
    (res / "tesseract" / "bin").mkdir(parents=True)
    (res / "tesseract" / "bin" / "tesseract").write_text("")
    monkeypatch.setenv("PATH", "/usr/bin")
    assert app.use_bundled_tesseract(res)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(res / "tesseract" / "bin")
    assert os.environ["TESSDATA_PREFIX"] == str(res / "tesseract" / "tessdata")
    assert not app.use_bundled_tesseract(tmp_path / "nada")


def test_app_runs_the_scraper_when_called_with_dash_m(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTACTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    fixtures = Path(__file__).parent / "fixtures"
    cwd = os.getcwd()
    try:
        code = app.main(["-psn_0_123", "-m", "mail_contact_scraper", "--local", str(fixtures), "--no-ocr",
                         "--output", str(tmp_path / "data" / "output")])
    finally:
        os.chdir(cwd)
    assert code == 0
    assert (tmp_path / "data" / "output" / "contacts.csv").exists()


# ---------- dashboard ----------

def test_dashboard_reports_and_triggers_updates(tmp_path):
    up = make_updater(tmp_path, FakeGitHub(RELEASE))
    out = tmp_path / "output"
    out.mkdir()
    server, url = dashboard.serve(out, port=18900, open_browser=False, env_path=tmp_path / ".env", updater=up)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        data = json.loads(urllib.request.urlopen(url + "data").read())
        assert data["update"]["enabled"] and data["update"]["version"] == "1.0.7"
        req = urllib.request.Request(url + "update", data=json.dumps({"token": "github_pat_nuevo"}).encode(),
                                     headers={"X-Requested-With": "dashboard", "Content-Type": "application/json"})
        result = json.loads(urllib.request.urlopen(req).read())
        assert result["ok"] and result["update"]["latest"] == "v1.0.12" and result["update"]["has_token"]
        assert envfile.read(tmp_path / ".env")["UPDATE_GITHUB_TOKEN"] == "github_pat_nuevo"
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_without_updater(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    server, url = dashboard.serve(out, port=18930, open_browser=False, env_path=tmp_path / ".env")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert json.loads(urllib.request.urlopen(url + "data").read())["update"] is None
        req = urllib.request.Request(url + "update", data=b"{}", headers={"X-Requested-With": "dashboard"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
