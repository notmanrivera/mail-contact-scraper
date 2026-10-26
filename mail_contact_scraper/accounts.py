"""Add, update and remove mailboxes in .env (used by the dashboard's settings)."""
import re

from . import envfile
from .config import MAX_MAILBOXES, Mailbox

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUFFIX_KEYS = ("IMAP_USER", "IMAP_PASSWORD", "IMAP_OAUTH_TOKEN", "IMAP_HOST", "IMAP_PORT", "IMAP_FOLDERS")


class AccountError(ValueError):
    pass


def _slots(values):
    """{slot: email}; slot "" is the main mailbox, "_2", "_3"... the extra ones."""
    slots = {}
    if values.get("IMAP_USER", "").strip():
        slots[""] = values["IMAP_USER"].strip()
    for n in range(2, MAX_MAILBOXES + 1):
        email = values.get(f"IMAP_USER_{n}", "").strip()
        if email:
            slots[f"_{n}"] = email
    return slots


def list_mailboxes(env_path=".env"):
    values = envfile.environ(env_path)
    out = []
    for slot, email in _slots(values).items():
        out.append({
            "email": email,
            "main": slot == "",
            "has_password": bool(values.get(f"IMAP_PASSWORD{slot}") or values.get(f"IMAP_OAUTH_TOKEN{slot}")),
            "host": values.get(f"IMAP_HOST{slot}", "") or values.get("IMAP_HOST", ""),
            "folders": values.get(f"IMAP_FOLDERS{slot}", "") or values.get("IMAP_FOLDERS", "") or "INBOX",
        })
    return out


def resolve_host(email, host="", env_path=".env"):
    if host:
        return host
    # Same domain as a mailbox already set up: same server (no DNS lookup needed).
    values = envfile.environ(env_path)
    domain = email.rsplit("@", 1)[-1].lower()
    for slot, other in _slots(values).items():
        known = values.get(f"IMAP_HOST{slot}", "") or values.get("IMAP_HOST", "")
        if known and other.rsplit("@", 1)[-1].lower() == domain:
            return known
    from .provider import guess_imap_host
    return guess_imap_host(email)[0]


def test_connection(email, password, host="", port=993, timeout=20, env_path=".env"):
    """Log in and out. Returns the host used; raises AccountError with a readable reason."""
    from . import imap_source

    host = resolve_host(email, host, env_path)
    try:
        conn = imap_source.connect(Mailbox(email, password, "", host, int(port or 993)), timeout=timeout)
    except imap_source.imaplib.IMAP4.error as exc:
        text = exc.args[0].decode(errors="replace") if exc.args and isinstance(exc.args[0], bytes) else str(exc)
        raise AccountError(f"El servidor rechazó el usuario o la contraseña ({host}): {text}") from exc
    except OSError as exc:
        raise AccountError(f"No se pudo conectar a {host}: {exc}") from exc
    try:
        conn.logout()
    except Exception:
        pass
    return host


def save_mailbox(env_path, email, password, host="", folders=""):
    """Add a mailbox, or update the password/host/folders of one that is already there."""
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise AccountError("Escribe un correo válido.")
    if not password:
        raise AccountError("Falta la contraseña de aplicación.")
    values = envfile.read(env_path)
    slots = _slots(values)
    slot = next((s for s, e in slots.items() if e.lower() == email.lower()), None)
    if slot is not None:
        email = slots[slot]  # keep how it was written
    else:
        if "" not in slots:
            slot = ""
        else:
            free = [n for n in range(2, MAX_MAILBOXES + 1) if f"_{n}" not in slots]
            if not free:
                raise AccountError(f"Ya hay {MAX_MAILBOXES} buzones.")
            slot = f"_{free[0]}"
    changes = {f"IMAP_USER{slot}": email, f"IMAP_PASSWORD{slot}": password}
    main_host = values.get("IMAP_HOST", "")
    if slot == "" or (host and host != main_host):
        changes[f"IMAP_HOST{slot}"] = host
    elif slot:
        changes[f"IMAP_HOST{slot}"] = None  # same as the main one
    if folders:
        changes[f"IMAP_FOLDERS{slot}"] = folders
    envfile.update(env_path, changes)
    return slot


def remove_mailbox(env_path, email):
    values = envfile.read(env_path)
    slot = next((s for s, e in _slots(values).items() if e.lower() == (email or "").strip().lower()), None)
    if slot is None:
        raise AccountError("Ese buzón no está en .env.")
    if slot == "":
        raise AccountError("Es el buzón principal. Para cambiarlo, edita IMAP_USER en .env.")
    envfile.update(env_path, {f"{k}{slot}": None for k in SUFFIX_KEYS})
