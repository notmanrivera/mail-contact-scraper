"""Campaign sending from the app's Mailing panel: plan, run in the background, stop, log.

Same rules as `python -m mailing.send --send`: today's limit per mailbox, mailboxes take
turns, nobody gets it twice (envios.csv), unsubscribes and bounces are checked first.
Nothing goes out unless the request carries confirm="ENVIAR", typed by the person.
"""
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import send
from .build import load_campaign

CONFIRM_WORD = "ENVIAR"
RECENT_ROWS = 40


def campaign_path(slug):
    """Path of a saved campaign; only names from the campaigns folder are accepted."""
    paths = {p.stem: p for p in send.CAMPAIGNS.glob("*.toml")}
    if not paths:
        raise ValueError("No hay campañas guardadas.")
    return paths.get(slug) or paths[sorted(paths)[0]]


def paths(out_dir, campaign):
    out_dir = Path(out_dir)
    return {"contacts": out_dir / "contacts.csv", "out": out_dir / "mailing" / campaign["slug"],
            "suppression": out_dir / "suppression.txt"}


def available_senders(env_path, campaign):
    from mail_contact_scraper import accounts, envfile

    found = [m["email"] for m in accounts.list_mailboxes(env_path) if m["has_password"]]
    default = campaign["sender"]["email"]
    if envfile.environ(env_path).get("SMTP_USER") and default.lower() not in (e.lower() for e in found):
        found.insert(0, default)  # a relay can send as any address of the domain
    return found


def _senders(env_path, campaign, chosen, daily_limit):
    available = available_senders(env_path, campaign)
    lowered = {e.lower(): e for e in available}
    chosen = [lowered[c.lower()] for c in (chosen or []) if c and c.lower() in lowered]
    if not chosen:
        default = campaign["sender"]["email"].lower()
        chosen = [lowered[default]] if default in lowered else available[:1]
    if not chosen:
        raise ValueError("Primero agrega un buzón con su contraseña de aplicación en Configuración ▸ Buzones.")
    try:
        return send.load_senders(chosen, campaign, env_path, daily_limit or None)
    except SystemExit as exc:
        raise ValueError(str(exc)) from None


def _limit(value, name):
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name} tiene que ser un número.") from None
    if number < 0:
        raise ValueError(f"{name} no puede ser negativo.")
    return number


def overview(out_dir, env_path, run, slug="", chosen=(), daily_limit=0):
    """Everything the Campaña tab shows: list, today's room per mailbox, log, run."""
    path = campaign_path(slug)
    campaign = load_campaign(path)
    where = paths(out_dir, campaign)
    log = send.read_log(where["out"] / send.LOG_NAME)
    counts = {}
    for row in log:
        counts[row["estado"]] = counts.get(row["estado"], 0) + 1
    info = {
        "campaign": path.stem, "available": available_senders(env_path, campaign),
        "default_sender": campaign["sender"]["email"], "plan": None, "error": "",
        "log": {"counts": counts, "recent": [
            {k: row.get(k, "") for k in ("fecha", "email", "estado", "detalle", "remitente")}
            for row in reversed(log[-RECENT_ROWS:])]},
        "suppression": send.suppression_counts(where["suppression"]),
        "run": run.status(),
    }
    try:
        senders, _ = _senders(env_path, campaign, chosen, _limit(daily_limit, "El límite diario"))
    except ValueError as exc:
        info["error"] = str(exc)
        return info
    info["senders"] = [{"email": s["email"], "daily_limit": s["daily_limit"]} for s in senders]
    if not where["contacts"].exists():
        info["error"] = "Todavía no hay contactos. Corre un escaneo primero."
        return info
    plan = send.send_campaign(path, where["contacts"], where["out"], senders=senders,
                              suppression_path=where["suppression"], say=lambda *a: None)
    info["plan"] = {k: plan[k] for k in ("audience", "already_sent", "pending", "sent_today", "room",
                                         "this_run", "skipped")}
    return info


def check_unsubscribes(out_dir, env_path, slug=""):
    """Read the inboxes for 'Baja de correos' replies and bounces. Returns (new unsubs, new bounces)."""
    from mail_contact_scraper.config import load_settings
    from mail_contact_scraper.imap_source import connect as imap_connect

    campaign = load_campaign(campaign_path(slug))
    boxes = send.inboxes(load_settings(env_path))
    if not boxes:
        raise ValueError("No hay buzones con contraseña para revisar.")
    own = campaign["sender"]["email"].split("@")[-1]
    try:
        return send.sync_suppression(boxes, paths(out_dir, campaign)["suppression"], imap_connect, own_domains=[own])
    except Exception as exc:  # IMAP errors come in many shapes
        raise ValueError(f"No pude revisar bajas y rebotes: {exc}") from None


class SendRun:
    """One campaign run at a time, in a background thread of the app."""

    def __init__(self, check=check_unsubscribes):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._check = check
        self.state = {"running": False}

    def status(self):
        with self._lock:
            return dict(self.state)

    def _set(self, **values):
        with self._lock:
            self.state.update(values)

    def stop(self):
        if not self.status().get("running"):
            return False
        self._stop.set()
        self._set(phase="deteniendo")
        return True

    def start(self, out_dir, env_path, body, pause=4.0):
        if (body.get("confirm") or "").strip() != CONFIRM_WORD:
            raise ValueError(f"Escribe {CONFIRM_WORD} para confirmar el envío.")
        if self.status().get("running"):
            raise ValueError("Ya hay un envío en curso.")
        path = campaign_path(str(body.get("campaign") or ""))
        campaign = load_campaign(path)
        where = paths(out_dir, campaign)
        if not where["contacts"].exists():
            raise ValueError("Todavía no hay contactos. Corre un escaneo primero.")
        senders, _ = _senders(env_path, campaign, body.get("from"), _limit(body.get("daily_limit"), "El límite diario"))
        limit = _limit(body.get("limit"), "El máximo de esta corrida")
        self._stop.clear()
        with self._lock:
            self.state = {"running": True, "phase": "bajas", "campaign": path.stem,
                          "senders": [s["email"] for s in senders], "started_at": _now(),
                          "sent": 0, "total": 0, "last": "", "note": ""}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        args=(out_dir, env_path, path, where, senders, limit, pause))
        self._thread.start()
        return self.status()

    def _run(self, out_dir, env_path, path, where, senders, limit, pause):
        note, awake = "", _keep_awake()
        try:
            try:
                unsub, bounced = self._check(out_dir, env_path, path.stem)
                note = f"Bajas nuevas: {len(unsub)} · rebotes nuevos: {len(bounced)}."
            except ValueError as exc:
                note = f"{exc} Sigo con la lista que ya había."
            if self._stop.is_set():
                self._set(note=note)
                return
            self._set(phase="enviando", note=note)

            def progress(summary, email_addr, who):
                self._set(sent=summary["sent"], total=summary["this_run"], last=f"{email_addr} ({who})")

            result = send.send_campaign(
                path, where["contacts"], where["out"], senders=senders, send=True, limit=limit, pause=pause,
                suppression_path=where["suppression"], sleep=self._stop.wait, stop=self._stop.is_set,
                progress=progress, say=lambda *a: None)
            self._set(sent=result["sent"], total=result["this_run"], rejected=len(result["rejected"]),
                      errors=result["errors"], stopped=result["stopped"])
        except Exception as exc:  # keep the app alive and show what happened
            self._set(error=str(exc) or exc.__class__.__name__)
        finally:
            if awake:
                awake.terminate()
            self._set(running=False, phase="terminado", finished_at=_now())


def _keep_awake():
    """On a Mac, keep it from going to sleep while a run lasts (it can take hours)."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["/usr/bin/caffeinate", "-i"])
    except OSError:
        return None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
