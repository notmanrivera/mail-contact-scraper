"""Send contacts.csv to a Google Sheet through a small Apps Script web app (no Google Cloud
project needed). See README, "Google Sheets".

    python -m mail_contact_scraper.sheets setup     # token in .env + the script to paste
    python -m mail_contact_scraper.sheets push      # send output/contacts.csv now
"""
import argparse
import csv
import json
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import envfile

STATE_FILE = "sheets.json"


class SheetsError(RuntimeError):
    pass


def config(env_path=".env"):
    env = envfile.environ(env_path)
    return {
        "url": env.get("SHEETS_WEBAPP_URL", "").strip(),
        "token": env.get("SHEETS_TOKEN", "").strip(),
        "tab": env.get("SHEETS_TAB", "").strip() or "Contactos",
        "auto": env.get("SHEETS_AUTO", "true").strip().lower() not in ("0", "false", "no", "off"),
    }


def script(token):
    return resources.files(__package__).joinpath("sheets_apps_script.gs").read_text(encoding="utf-8").replace(
        "__TOKEN__", token)


def ensure_token(env_path=".env"):
    token = config(env_path)["token"]
    if not token:
        token = secrets.token_urlsafe(24)
        envfile.update(env_path, {"SHEETS_TOKEN": token})
    return token


def last_sync(out_dir):
    try:
        return json.loads((Path(out_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def push(out_dir, env_path=".env", url=None, opener=urllib.request.urlopen):
    """Replace the sheet's tab with contacts.csv. Records the result in output/sheets.json."""
    cfg = config(env_path)
    url = url or cfg["url"]
    state = {"at": datetime.now().isoformat(timespec="seconds"), "ok": False}
    try:
        if not url.startswith("https://script.google.com/"):
            raise SheetsError("Falta SHEETS_WEBAPP_URL (la URL de la app web, https://script.google.com/...).")
        if not cfg["token"]:
            raise SheetsError("Falta SHEETS_TOKEN. Corre: python -m mail_contact_scraper.sheets setup")
        with open(Path(out_dir) / "contacts.csv", newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
        if not rows:
            raise SheetsError("contacts.csv está vacío.")
        body = json.dumps({"token": cfg["token"], "tab": cfg["tab"], "header": rows[0], "rows": rows[1:]}).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=120) as res:
                text = res.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise SheetsError(f"No se pudo llegar a Google: {exc}") from exc
        try:
            answer = json.loads(text)
        except ValueError:
            # An HTML page here means the web app isn't public or needs a Google sign-in.
            raise SheetsError("Google no respondió como app web. Revisa que esté publicada con acceso "
                              "\"Cualquier usuario\" y que la URL termine en /exec.") from None
        if not answer.get("ok"):
            raise SheetsError(f"La hoja rechazó los datos: {answer.get('error', 'sin detalle')}")
        state.update(ok=True, rows=answer.get("rows", len(rows) - 1), tab=cfg["tab"])
        if url != cfg["url"]:
            envfile.update(env_path, {"SHEETS_WEBAPP_URL": url})
    except (SheetsError, OSError) as exc:
        state["error"] = str(exc)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if not state["ok"]:
        raise SheetsError(state["error"])
    return state


def main(argv=None):
    p = argparse.ArgumentParser(prog="mail_contact_scraper.sheets")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="Create SHEETS_TOKEN and print the Apps Script to paste")
    push_p = sub.add_parser("push", help="Send contacts.csv to the sheet")
    push_p.add_argument("--output", default=None)
    args = p.parse_args(argv)
    if args.cmd == "setup":
        print(script(ensure_token()))
        return 0
    from .config import load_settings
    try:
        state = push(args.output or load_settings().output_dir)
    except SheetsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{state['rows']} contactos enviados a la pestaña {state['tab']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
