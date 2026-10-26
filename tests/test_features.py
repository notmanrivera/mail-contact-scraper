import csv
import io
import json
import plistlib
import threading
import urllib.request
from pathlib import Path

import pytest

from mail_contact_scraper import accounts, cli, dashboard, envfile, schedule, sheets
from mail_contact_scraper.contacts import _location

FIXTURES = Path(__file__).parent / "fixtures"


def test_location_from_area_code():
    assert _location({"+528183401122 ext 12": "office", "+523312345678": "mobile"}) == "Monterrey, NL"
    assert _location({"+523312345678": "mobile"}) == "Guadalajara, JAL"
    assert _location({"+12145551234": "office"}) == "Texas"
    assert _location({}) == ""


def test_csv_has_location(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    cli.main(["--local", str(FIXTURES), "--output", str(tmp_path), "--no-ocr"])
    rows = {r["email"]: r for r in csv.DictReader(open(tmp_path / "contacts.csv", encoding="utf-8-sig"))}
    assert rows["lmartinez@aceroslaguna.com.mx"]["location"] == "Coahuila"


def test_envfile_keeps_comments_and_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Buzón\nIMAP_USER=pedro@dindustrimex.com\nIMAP_PASSWORD=\nBATCH_SIZE=50 # bloques\n")
    envfile.update(env, {"IMAP_PASSWORD": "abcd efgh ijkl", "IMAP_USER_2": "ventas@dindustrimex.com"})
    text = env.read_text()
    assert text.startswith("# Buzón\nIMAP_USER=pedro@dindustrimex.com\nIMAP_PASSWORD=\"abcd efgh ijkl\"\n")
    values = envfile.read(env)
    assert values["IMAP_PASSWORD"] == "abcd efgh ijkl" and values["BATCH_SIZE"] == "50"
    assert values["IMAP_USER_2"] == "ventas@dindustrimex.com"
    envfile.update(env, {"IMAP_USER_2": None})
    assert "IMAP_USER_2" not in envfile.read(env)
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_save_and_remove_mailboxes(tmp_path, monkeypatch):
    for key in ("IMAP_USER", "IMAP_PASSWORD", "IMAP_HOST", "IMAP_USER_2", "IMAP_USER_3"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    accounts.save_mailbox(env, "pedro@dindustrimex.com", "p1", "imap.gmail.com")
    accounts.save_mailbox(env, "ventas@dindustrimex.com", "p2", "imap.gmail.com")
    accounts.save_mailbox(env, "compras@dindustrimex.com", "p3", "imap.gmail.com")
    accounts.remove_mailbox(env, "ventas@dindustrimex.com")
    accounts.save_mailbox(env, "almacen@dindustrimex.com", "p4", "imap.gmail.com")  # reuses slot 2
    accounts.save_mailbox(env, "COMPRAS@dindustrimex.com", "nueva", "imap.gmail.com")  # update
    values = envfile.read(env)
    assert values["IMAP_USER_2"] == "almacen@dindustrimex.com" and "IMAP_HOST_2" not in values
    assert values["IMAP_USER_3"] == "compras@dindustrimex.com" and values["IMAP_PASSWORD_3"] == "nueva"
    assert [m["email"] for m in accounts.list_mailboxes(env)] == [
        "pedro@dindustrimex.com", "almacen@dindustrimex.com", "compras@dindustrimex.com"]
    with pytest.raises(accounts.AccountError):
        accounts.remove_mailbox(env, "pedro@dindustrimex.com")
    with pytest.raises(accounts.AccountError):
        accounts.save_mailbox(env, "no-es-correo", "x")


def test_dashboard_adds_mailbox_after_testing_it(tmp_path, monkeypatch):
    for key in ("IMAP_USER", "IMAP_PASSWORD", "IMAP_HOST", "IMAP_USER_2", "IMAP_PASSWORD_2"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\nIMAP_PASSWORD=p1\nIMAP_HOST=imap.gmail.com\n")
    tried = []

    def fake_test(email, password, host="", port=993, timeout=20, env_path=".env"):
        tried.append((email, password))
        if password == "mala":
            raise accounts.AccountError("El servidor rechazó el usuario o la contraseña")
        return accounts.resolve_host(email, host, env_path)

    monkeypatch.setattr(accounts, "test_connection", fake_test)
    server, url = dashboard.serve(tmp_path / "out", port=8795, open_browser=False, env_path=env)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body, header="dashboard"):
        req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Requested-With": header, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}") if exc.code != 403 else {}

    try:
        assert post("/settings/mailbox", {"email": "ventas@dindustrimex.com", "password": "x"}, header="evil")[0] == 403
        code, data = post("/settings/mailbox", {"email": "ventas@dindustrimex.com", "password": "mala"})
        assert code == 400 and "rechazó" in data["error"]
        assert "IMAP_USER_2" not in envfile.read(env)
        code, data = post("/settings/mailbox", {"email": "ventas@dindustrimex.com", "password": "buena"})
        assert code == 200 and [m["email"] for m in data["mailboxes"]][-1] == "ventas@dindustrimex.com"
        assert envfile.read(env)["IMAP_PASSWORD_2"] == "buena"
        settings = json.loads(urllib.request.urlopen(url + "settings").read())
        assert "buena" not in json.dumps(settings)  # passwords never go back to the page
    finally:
        server.shutdown()


def test_schedule_writes_launch_agent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.chdir(tmp_path)
    info = schedule.install(7, 30, "output", agents_dir=tmp_path / "agents", run=lambda *a: calls.append(a))
    data = plistlib.loads(schedule.plist_path(tmp_path / "agents").read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert data["ProgramArguments"][1:3] == ["-m", "mail_contact_scraper"]
    assert data["WorkingDirectory"] == str(tmp_path.resolve())
    assert [c[0] for c in calls] == ["bootout", "bootstrap"]
    assert info["enabled"] and info["hour"] == 7
    assert not schedule.uninstall(agents_dir=tmp_path / "agents", run=lambda *a: None)["enabled"]
    with pytest.raises(schedule.ScheduleError):
        schedule.install(25, 0, agents_dir=tmp_path / "agents", run=lambda *a: None)


def test_schedule_warns_about_protected_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "Documents" in schedule.warning(tmp_path / "Documents" / "mail-contact-scraper")
    assert schedule.warning(tmp_path / "mail-contact-scraper") == ""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sheets_push(tmp_path, monkeypatch):
    for key in ("SHEETS_WEBAPP_URL", "SHEETS_TOKEN", "SHEETS_TAB"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    out = tmp_path / "out"
    out.mkdir()
    (out / "contacts.csv").write_text("email,name\nlaura@x.mx,Laura\n", encoding="utf-8-sig")
    token = sheets.ensure_token(env)
    assert token in sheets.script(token) and "__TOKEN__" not in sheets.script(token)
    sent = []

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return FakeResponse(json.dumps({"ok": True, "rows": len(sent[-1]["rows"])}).encode())

    url = "https://script.google.com/macros/s/abc/exec"
    state = sheets.push(out, env, url, opener=opener)
    assert state["ok"] and state["rows"] == 1
    assert sent[0] == {"token": token, "tab": "Contactos", "header": ["email", "name"], "rows": [["laura@x.mx", "Laura"]]}
    assert envfile.read(env)["SHEETS_WEBAPP_URL"] == url  # saved once it worked
    assert sheets.last_sync(out)["ok"]

    def html(req, timeout):
        return FakeResponse(b"<html>Sign in</html>")

    with pytest.raises(sheets.SheetsError, match="app web"):
        sheets.push(out, env, opener=html)
    assert not sheets.last_sync(out)["ok"]


def test_second_scan_on_same_output_is_refused(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    tmp_path.mkdir(exist_ok=True)
    holder = open(tmp_path / ".scan.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cli.main(["--local", str(FIXTURES), "--output", str(tmp_path), "--no-ocr"]) == 2
    finally:
        holder.close()


def test_new_mailbox_uses_the_server_of_its_domain(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAP_HOST", raising=False)
    env = tmp_path / ".env"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\nIMAP_HOST=imap.gmail.com\n")
    assert accounts.resolve_host("ventas@dindustrimex.com", "", env) == "imap.gmail.com"
    assert accounts.resolve_host("x@otro.mx", "imap.otro.mx", env) == "imap.otro.mx"
