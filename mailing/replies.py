"""Who answered a campaign, read from the inboxes (nothing is marked as read).

Each answer to a campaign message is one of:
- respondió:        a person wrote back. A lead: no follow-up for them.
- fuera-de-oficina: an automatic reply. The follow-up waits until the date they return.
- ya-no-labora:     "ya no labora aquí". The address goes to the suppression list and any
                    other address in the reply (who took over) is added to the campaign.

Kept in output/mailing/<campaña>/respuestas.csv; referrals in referidos.csv next to it.
"""
import csv
import email
import re
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

REPLIES_NAME = "respuestas.csv"
REFERRALS_NAME = "referidos.csv"
REPLY_FIELDS = ["fecha", "email", "tipo", "regresa", "nuevo_contacto", "asunto", "buzon"]
REFERRAL_FIELDS = ["email", "name", "company", "title", "source", "referido_por"]
REPLIED, AWAY, GONE = "respondió", "fuera-de-oficina", "ya-no-labora"

AUTO_SUBJECT = re.compile(r"fuera de (la )?oficina|respuesta autom|autorespuesta|automatic reply|auto.?reply|"
                          r"out of (the )?office|ausencia|vacaciones|autoresponder", re.I)
GONE_TEXT = re.compile(
    r"ya no (labora|trabaja|colabora|forma parte|pertenece|se encuentra (laborando|en la empresa|con nosotros)|"
    r"est[aá] (laborando|en la empresa|con nosotros))|dej[oó] de (laborar|trabajar|colaborar)|"
    r"ha dejado (la empresa|de (laborar|trabajar|colaborar))|no longer (with|works|employed)|"
    r"(has )?left (the|our) (company|organi[sz]ation)",
    re.I)
NOT_PEOPLE = re.compile(r"mailer-daemon|postmaster|no-?reply|noreply", re.I)
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
     "noviembre", "diciembre"))}
MONTHS.update({"setiembre": 9, "sept": 9, "sep": 9, "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
               "jul": 7, "ago": 8, "oct": 10, "nov": 11, "dic": 12})
AWAY_DEFAULT_DAYS = 7  # no return date in the reply: wait a week


def _text(msg):
    """Plain text of a message (HTML stripped when there is no text part), without the quoted part."""
    from .send import html_to_text

    plain, rich = "", ""
    for part in msg.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html") or part.get_filename():
            continue
        raw = part.get_payload(decode=True) or b""
        text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if kind == "text/plain" and not plain:
            plain = text
        elif kind == "text/html" and not rich:
            rich = html_to_text(text)
    text = plain or rich
    # stop at the quoted original ("El lun, 27 oct ... escribió:", "-----Original Message-----", "> ")
    cut = re.search(r"^(El .{5,120} escribi[oó]:|On .{5,120} wrote:|-+ ?(Original Message|Mensaje original) ?-+|De: .+|From: .+)$",
                    text, re.I | re.M)
    return text[:cut.start()] if cut else text


def is_automatic(msg):
    auto = (msg.get("Auto-Submitted") or "").lower()
    return ((auto and auto != "no") or bool(msg.get("X-Autoreply") or msg.get("X-Autorespond"))
            or (msg.get("Precedence") or "").lower() == "auto_reply"
            or bool(AUTO_SUBJECT.search(msg.get("Subject") or "")))


def return_date(text, today):
    """The date an out-of-office reply says the person is back, or None."""
    text = text.lower()
    for d, m, y in re.findall(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", text):
        found = _date(int(d), int(m), y, today)
        if found:
            return found
    names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for d, m, y in re.findall(rf"\b(\d{{1,2}}) de ({names})\.?(?: (?:de|del) (\d{{4}}))?", text):
        found = _date(int(d), MONTHS[m], y, today)
        if found:
            return found
    return None


def _date(d, m, y, today):
    try:
        year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
        found = date(year, m, d)
    except ValueError:
        return None
    if not y and found < today - timedelta(days=30):  # "regreso el 5 de enero", said in December
        found = found.replace(year=found.year + 1)
    return found if today - timedelta(days=30) <= found <= today + timedelta(days=365) else None


def classify(msg, today):
    """(tipo, return date or None, other addresses in the reply)."""
    text = _text(msg)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    others = []
    for a in ADDRESS.findall(text):
        a = a.lower().strip(".")
        if a != sender and a not in others and not NOT_PEOPLE.search(a):
            others.append(a)
    if GONE_TEXT.search(text) or GONE_TEXT.search(msg.get("Subject") or ""):
        return GONE, None, others
    if is_automatic(msg):
        return AWAY, return_date(text, today) or today + timedelta(days=AWAY_DEFAULT_DAYS), others
    return REPLIED, None, []


def read_replies(out_dir):
    """email -> the row that counts: a person's reply, else 'ya no labora', else the newest
    out-of-office (it holds the return date)."""
    by_email = {}
    for row in _rows(Path(out_dir) / REPLIES_NAME):
        by_email.setdefault(row["email"], []).append(row)
    out = {}
    for addr, rows in by_email.items():
        for tipo in (REPLIED, GONE, AWAY):
            match = [r for r in rows if r["tipo"] == tipo]
            if match:
                out[addr] = max(match, key=lambda r: r["fecha"])
                break
    return out


def waiting_until(row):
    """The day an out-of-office person is back, as a date, or None."""
    try:
        return date.fromisoformat(row.get("regresa") or "")
    except ValueError:
        return None


def sync_replies(mailboxes, connect, out_dir, sent, suppression_path, own_domains=(), today=None, since_days=60,
                 seen_path=None, progress=None, stop=None):
    """Read the inboxes for answers to what we sent: `sent` is (email, Message-ID) pairs.

    Writes the new ones to respuestas.csv, puts 'ya no labora' addresses in the suppression
    list and their replacements in referidos.csv. Returns {tipo: [emails]} of what is new.
    seen_path, progress and stop work as in send.sync_suppression."""
    from .send import Seen, Stopped, add_suppression

    today = today or date.today()
    out = Path(out_dir)
    known = {(r["email"], r["fecha"], r["tipo"]) for r in _rows(out / REPLIES_NAME)}
    sent = list(sent)
    ids = {mid.strip().strip("<>").lower(): addr.lower() for addr, mid in sent if mid}
    sent = {addr.lower() for addr, _ in sent}
    own = {d.lower() for d in own_domains}
    since = (today - timedelta(days=since_days)).strftime("%d-%b-%Y")
    found, seen = [], Seen(seen_path)
    for box in mailboxes:
        if progress:
            progress(f"Conectando con {box.email}…")
        conn = connect(box)
        try:
            conn.select("INBOX", readonly=True)
            validity = seen.validity(conn)
            typ, data = conn.uid("SEARCH", None, f"(SINCE {since})")
            uids = seen.new(f"{box.email}|{validity}|respuestas", data[0].split() if data and data[0] else [])
            for chunk in range(0, len(uids), 200):
                if stop and stop():
                    raise Stopped("Detenido.")
                if progress:
                    progress(f"Respuestas en {box.email}: {min(chunk + 200, len(uids))} de {len(uids)} correos nuevos")
                batch = b",".join(uids[chunk:chunk + 200])
                typ, parts = conn.uid("FETCH", batch, "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES)])")
                for uid, head in _uid_parts(parts):
                    h = email.message_from_bytes(head)
                    who = parseaddr(h.get("From", ""))[1].lower()
                    refs = re.findall(r"<([^>]+)>", f"{h.get('In-Reply-To', '')} {h.get('References', '')}")
                    person = next((ids[r.lower()] for r in refs if r.lower() in ids), None)
                    if not person and who in sent:
                        person = who
                    if not person or NOT_PEOPLE.search(who):
                        continue
                    typ, full = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                    raw = next((p[1] for p in full if isinstance(p, tuple)), b"")
                    found.append((person, email.message_from_bytes(raw), box.email))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    new, rows, referrals, gone = {}, [], [], set()
    for person, msg, mailbox in found:
        tipo, back, others = classify(msg, today)
        try:
            when = email.utils.parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if (person, when, tipo) in known:
            continue
        known.add((person, when, tipo))
        others = [a for a in others if a.split("@")[-1] not in own and a not in sent]
        rows.append({"fecha": when, "email": person, "tipo": tipo, "regresa": back.isoformat() if back else "",
                     "nuevo_contacto": " ".join(others[:3]), "asunto": (msg.get("Subject") or "")[:200],
                     "buzon": mailbox})
        new.setdefault(tipo, []).append(person)
        if tipo == GONE:
            gone.add(person)
            referrals += [{"email": a, "source": "referido", "referido_por": person} for a in others[:3]]
    _append(out / REPLIES_NAME, REPLY_FIELDS, rows)
    if referrals:
        have = {r["email"] for r in _rows(out / REFERRALS_NAME)}
        _append(out / REFERRALS_NAME, REFERRAL_FIELDS, [r for r in referrals if r["email"] not in have])
    if gone:
        add_suppression(suppression_path, gone, GONE)
    seen.save()
    return new


def _uid_parts(parts):
    for p in parts or []:
        if isinstance(p, tuple):
            m = re.search(rb"UID (\d+)", p[0])
            if m:
                yield m.group(1), p[1]


def _rows(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _append(path, fields, rows):
    if not rows:
        return
    new = not Path(path).exists()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
