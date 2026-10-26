"""Read every message from IMAP folders without marking anything as read."""
import imaplib
import logging
import re
import time

log = logging.getLogger(__name__)


def connect(box, timeout=60):
    conn = imaplib.IMAP4_SSL(box.imap_host, box.imap_port, timeout=timeout)
    if box.oauth_token:
        # Gmail / Microsoft 365 OAuth2 (XOAUTH2). Basic auth is disabled on M365.
        auth = f"user={box.email}\x01auth=Bearer {box.oauth_token}\x01\x01"
        conn.authenticate("XOAUTH2", lambda _: auth.encode())
    else:
        conn.login(box.email, box.password)
    return conn


def list_folders(conn):
    typ, data = conn.list()
    folders = []
    for line in data or []:
        if not line:
            continue
        match = LIST_RE.match(line.decode(errors="replace"))
        if not match or "\\noselect" in match.group("flags").lower():
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        folders.append(name)
    return folders


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


def _quote(folder):
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def iter_messages(conn, folder, batch_size=50, delay=1.0, skip=None, on_folder=None):
    """Yield (folder, uidvalidity, uid, raw_bytes). `skip` is a set of (folder, uidvalidity, uid)."""
    typ, _ = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        log.warning("Cannot open folder %s", folder)
        return
    uidvalidity = ""
    typ, resp = conn.response("UIDVALIDITY")
    if resp and resp[0]:
        uidvalidity = resp[0].decode() if isinstance(resp[0], bytes) else str(resp[0])

    typ, data = conn.uid("SEARCH", None, "ALL")
    uids = data[0].split() if data and data[0] else []
    pending = [u.decode() for u in uids if (folder, uidvalidity, u.decode()) not in (skip or set())]
    log.info("%s: %d messages, %d to process", folder, len(uids), len(pending))
    if on_folder:
        on_folder(folder, len(uids), len(pending))

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        # BODY.PEEK[] keeps the \Seen flag untouched.
        typ, data = conn.uid("FETCH", ",".join(batch), "(UID BODY.PEEK[])")
        if typ != "OK":
            log.warning("Fetch failed for batch starting at UID %s", batch[0])
            continue
        for item in data:
            if not isinstance(item, tuple):
                continue
            match = re.search(rb"UID (\d+)", item[0])
            if match:
                yield folder, uidvalidity, match.group(1).decode(), item[1]
        log.info("%s: %d/%d", folder, min(start + batch_size, len(pending)), len(pending))
        if delay and start + batch_size < len(pending):
            time.sleep(delay)
