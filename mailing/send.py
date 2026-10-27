"""Send a mailing from the company's own mailbox, without YAMM or a paid platform.

Uses SMTP with the same mailboxes and app passwords the scraper already has in .env, so
every message leaves from pedro@ (and shows up in its Sent folder). For more volume it
spreads the run across several mailboxes (--from), or goes through a bulk relay such as
Amazon SES or Brevo (SMTP_USER / SMTP_PASSWORD in .env). Nothing goes out unless you
pass --test or --send:

    python -m mailing.send                       # what would be sent, nothing goes out
    python -m mailing.send --test tu@correo.com  # one sample to you
    python -m mailing.send --send                # the real run, asks to type ENVIAR

Every message is logged in output/mailing/<campaña>/envios.csv, so a run can stop and
continue later without repeating anyone. Before each run it reads the inbox for
"Baja de correos" replies and bounces and adds them to output/suppression.txt.
"""
import argparse
import csv
import email
import html as html_lib
import imaplib
import os
import random
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from .build import (EMAIL_RE, build_eml, greeting, load_audience, load_campaign, render_html,
                    render_text, split_name, unsubscribe_mailto)

HERE = Path(__file__).parent
LOG_NAME = "envios.csv"
LOG_FIELDS = ["fecha", "email", "estado", "detalle", "remitente"]
SENT = "enviado"
FOLLOWUP = "seguimiento"
FOLLOWUP_DAYS = 5
# Prospects of one company (same domain) a day, so a whole office doesn't get it at once.
# Free mail domains (gmail.com, hotmail.com...) don't count as a company.
PER_COMPANY_DAILY = 3
UNSUBSCRIBE_SUBJECT = "Baja de correos"
# Google Workspace allows 2,000 messages a day per user (Microsoft 365: 10,000); stay under it.
MAILBOX_DAILY_LIMIT = 1500
RELAY_DAILY_LIMIT = 10000
# Gmail drops a long SMTP session ("421 4.7.0 Connection expired"); open a fresh one this often.
RECONNECT_EVERY = 50
# A dropped or expired connection is retried this many times (after these pauses, in seconds)
# before that mailbox is given up for the run; the prospect is never marked as failed for it.
RETRY_PAUSES = (10, 30, 60)
# Every so often a longer break, so the pace doesn't look machine-made.
BREAK_EVERY = (40, 60)
BREAK_SECONDS = (30, 90)
# Circuit breaker: the whole run pauses when this share of the attempts fail (after at least
# BREAKER_MIN_ATTEMPTS), or after this many failures in a row. Every CHECK_EVERY sent, the
# `checkpoint` hook (the app reads the bounces in the inboxes) may pause it too.
BREAKER_RATE = 0.05
BREAKER_MIN_ATTEMPTS = 20
BREAKER_IN_ROW = 8
CHECK_EVERY = 50

# IMAP host the scraper uses -> (SMTP host, port)
SMTP_FOR_IMAP = {
    "imap.gmail.com": ("smtp.gmail.com", 465),
    "outlook.office365.com": ("smtp.office365.com", 587),
    "imap.zoho.com": ("smtp.zoho.com", 465),
    "imap.secureserver.net": ("smtpout.secureserver.net", 465),
    "imap.titan.email": ("smtp.titan.email", 465),
    "imap.hostinger.com": ("smtp.hostinger.com", 465),
}


class StopSending(Exception):
    """The provider refused more mail for now (daily limit, rate limit, blocked)."""


# --- mailbox and SMTP ---------------------------------------------------------------

def load_senders(addresses, campaign, env_file=".env", daily_limit=None):
    """Mailboxes to send from, each with its SMTP login and daily limit.

    With SMTP_USER / SMTP_PASSWORD in .env (Amazon SES, Brevo, Mailgun...) every message
    goes through that relay instead, so any verified address of the domain can be a sender.
    """
    from mail_contact_scraper import envfile
    from mail_contact_scraper.config import load_settings
    from mail_contact_scraper.provider import guess_imap_host

    settings = load_settings(env_file)
    env = envfile.environ(env_file).get
    relay_user, relay_password = env("SMTP_USER", ""), env("SMTP_PASSWORD", "")
    names = {s["email"].lower(): s.get("name", "") for s in [campaign["sender"], *campaign.get("senders", [])]}
    senders = []
    for address in addresses:
        box = next((b for b in settings.mailboxes if b.email.lower() == address.lower()), None)
        if relay_user:
            from mail_contact_scraper.config import Mailbox
            login = Mailbox(relay_user, relay_password)
            host, port = env("SMTP_HOST", ""), int(env("SMTP_PORT", "") or 587)
            if not host:
                raise SystemExit("Con SMTP_USER pon también SMTP_HOST (ej. email-smtp.us-east-1.amazonaws.com).")
            limit = daily_limit or int(env("SMTP_DAILY_LIMIT", "") or RELAY_DAILY_LIMIT)
        else:
            if not box or not (box.password or box.oauth_token):
                raise SystemExit(f"Para enviar desde {address} ponlo en .env (IMAP_USER o IMAP_USER_2...) "
                                 "con su contraseña de aplicación, la misma que usa el scraper.")
            if not box.imap_host:
                box.imap_host = guess_imap_host(box.email)[0]
            host, port = SMTP_FOR_IMAP.get(box.imap_host.lower(), (f"smtp.{box.email.split('@')[-1]}", 465))
            host = env("SMTP_HOST", "") or host
            port = int(env("SMTP_PORT", "") or port)
            login = box
            limit = daily_limit or MAILBOX_DAILY_LIMIT
        senders.append({
            "email": address.lower(), "name": names.get(address.lower(), campaign["sender"]["name"]),
            "host": host, "daily_limit": limit,
            "connect": (lambda login=login, host=host, port=port: open_smtp(login, host, port)),
        })
    return senders, settings


def open_smtp(box, host, port, timeout=60):
    conn = smtplib.SMTP_SSL(host, port, timeout=timeout) if port == 465 else smtplib.SMTP(host, port, timeout=timeout)
    if port != 465:
        conn.starttls()
    if box.oauth_token:
        auth = f"user={box.email}\x01auth=Bearer {box.oauth_token}\x01\x01"
        conn.auth("XOAUTH2", lambda challenge=None: auth)
    else:
        conn.login(box.email, box.password)
    return conn


# --- log and suppression list --------------------------------------------------------

def read_log(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def append_log(path, email_addr, status, detail="", now=None, sender=""):
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_FIELDS)
        w.writerow([(now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), email_addr, status, detail, sender])
        fh.flush()
        os.fsync(fh.fileno())  # on disk before the next message, so a crash never resends it


def sent_today(mailing_root, day, default_sender="", by="remitente"):
    """Messages sent today (campaign and follow-ups) per sending mailbox, across every
    campaign; with by="dominio", per recipient domain instead."""
    prefix, counts = day.isoformat(), {}
    for log in Path(mailing_root).glob(f"*/{LOG_NAME}"):
        for row in read_log(log):
            if row["estado"] in (SENT, FOLLOWUP) and row["fecha"].startswith(prefix):
                key = (row["email"].rsplit("@", 1)[-1] if by == "dominio"
                       else row.get("remitente") or default_sender).lower()
                counts[key] = counts.get(key, 0) + 1
    return counts


def load_people(contacts_csv, out_dir, campaign, suppression_path=None, **audience_opts):
    """The campaign's audience: the scraper's contacts plus the referrals found in replies
    ('ya no labora, escribe a ...'), in referidos.csv of the campaign folder."""
    from .replies import REFERRALS_NAME

    people, skipped = load_audience(contacts_csv, campaign, suppression_path=suppression_path, **audience_opts)
    referrals = Path(out_dir) / REFERRALS_NAME
    if referrals.exists():
        have = {p["email"] for p in people}
        extra, _ = load_audience(referrals, campaign, suppression_path=suppression_path)
        people += [p for p in extra if p["email"] not in have]
    return people, skipped


def followup_due(campaign, log, people, replies, today, days=None):
    """Who gets the follow-up now: got the campaign `days` ago or more, didn't answer, isn't
    away (out of office until a later date), and hasn't had it yet. Each one with the
    mailbox and Message-ID of the first message, to answer in the same thread."""
    from .replies import AWAY, waiting_until

    days = days or int(campaign.get("followup", {}).get("days") or FOLLOWUP_DAYS)
    by_email = {p["email"]: p for p in people}
    followed = {r["email"].lower() for r in log if r["estado"] == FOLLOWUP}
    due, waiting = [], {"recent": 0, "replied": 0, "away": 0}
    for row in log:
        addr = row["email"].lower()
        if row["estado"] != SENT or addr in followed or addr not in by_email:
            continue
        answer = replies.get(addr)
        if answer and answer["tipo"] != AWAY:
            waiting["replied"] += 1
            continue
        back = waiting_until(answer) if answer else None
        if back and back >= today:
            waiting["away"] += 1
            continue
        if row["fecha"][:10] > (today - timedelta(days=days)).isoformat():
            waiting["recent"] += 1
            continue
        due.append(dict(by_email[addr], sender=(row.get("remitente") or campaign["sender"]["email"]).lower(),
                        original_id=row.get("detalle", "")))
    return due, waiting


def company_order(people, today_by_domain, per_company):
    """Up to `per_company` a day from each company domain, mixed so the same company never
    goes twice in a row. Returns (who can go today, how many wait for another day)."""
    from .deliverability import KNOWN_DOMAINS

    if not per_company:
        return list(people), 0
    count, ranked, held = dict(today_by_domain), [], 0
    for i, p in enumerate(people):
        domain = p["email"].rsplit("@", 1)[-1]
        if domain in KNOWN_DOMAINS:
            ranked.append((0, i, p))
            continue
        n = count.get(domain, 0)
        if n >= per_company:
            held += 1
            continue
        count[domain] = n + 1
        ranked.append((n - today_by_domain.get(domain, 0), i, p))
    return [p for _, _, p in sorted(ranked, key=lambda r: (r[0], r[1]))], held


def read_suppression(path):
    if not Path(path).exists():
        return set()
    return {l.strip().lower() for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")}


def add_suppression(path, addresses, reason):
    known = read_suppression(path)
    new = sorted({a.lower() for a in addresses} - known)
    if new:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"# {reason} {date.today().isoformat()}\n" + "".join(a + "\n" for a in new))
    return new


def sync_suppression(mailboxes, suppression_path, connect, own_domains=(), days=90):
    """Add 'Baja de correos' requests and bounced recipients from the inboxes to the list.

    Reads only (BODY.PEEK), nothing is marked as read.
    """
    from mail_contact_scraper.parse import bounced_addresses

    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    unsubscribed, bounced = set(), set()
    for box in mailboxes:
        conn = connect(box)
        try:
            conn.select("INBOX", readonly=True)
            for criteria, bucket in ((f'(SINCE {since} SUBJECT "{UNSUBSCRIBE_SUBJECT}")', unsubscribed),
                                     (f'(SINCE {since} FROM "mailer-daemon")', bounced),
                                     (f'(SINCE {since} FROM "postmaster")', bounced)):
                typ, data = conn.uid("SEARCH", None, criteria)
                for uid in (data[0].split() if data and data[0] else []):
                    typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                    msg = email.message_from_bytes(raw)
                    if bucket is bounced:
                        bucket.update(bounced_addresses(msg))
                        continue
                    bucket.add(parseaddr(msg.get("From", ""))[1].lower())
                    # the unsubscribe link puts the address that got the mailing in the body
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            text = part.get_payload(decode=True) or b""
                            bucket.update(m.lower() for m in re.findall(r"[^\s<>@]+@[^\s<>@]+\.[a-z]{2,}",
                                                                        text.decode(errors="replace"), re.I))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    own = {d.lower() for d in own_domains}
    keep = lambda addrs: {a for a in addrs if EMAIL_RE.match(a) and a.split("@")[-1] not in own}
    return (add_suppression(suppression_path, keep(unsubscribed), "baja"),
            add_suppression(suppression_path, keep(bounced), "rebote"))


def inboxes(settings):
    """Mailboxes of .env that can be read, for the unsubscribe and bounce check."""
    from mail_contact_scraper.provider import guess_imap_host

    boxes = [b for b in settings.mailboxes if b.password or b.oauth_token]
    for b in boxes:
        if not b.imap_host:
            b.imap_host = guess_imap_host(b.email)[0]
    return boxes


def suppression_counts(path):
    """How many addresses the list holds per reason (baja, rebote, rechazado...)."""
    counts, reason = {}, "otro"
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                reason = (line[1:].split() or ["otro"])[0]
            elif line.strip():
                counts[reason] = counts.get(reason, 0) + 1
    return counts


# --- sending -------------------------------------------------------------------------

def make_followup(campaign, person, original_id, sender):
    """The follow-up: a short note answering the first message, in the same thread."""
    from .build import plain

    f = campaign.get("followup") or {}
    paragraphs = f.get("body") or []
    if isinstance(paragraphs, str):
        paragraphs = [paragraphs]
    paragraphs = [p.replace("{{Saludo}}", person["greeting"]) for p in paragraphs]
    sig = campaign.get("signature", {})
    signature = [sender.get("name") or sig.get("name", ""), sig.get("title", "")]
    unsub = unsubscribe_mailto(campaign, person["email"])
    text = "\n\n".join([person["greeting"], *paragraphs]) + "\n\n" + "\n".join(filter(None, signature))
    text += f"\n\nPara no recibir más correos: {unsub}\n"
    esc = html_lib.escape
    body = "".join(f'<p style="margin:0 0 14px">{esc(p)}</p>' for p in [person["greeting"], *paragraphs])
    body += '<p style="margin:0 0 14px">' + "<br>".join(esc(x) for x in signature if x) + "</p>"
    body += (f'<p style="margin:18px 0 0;font-size:11px;color:#888">Si no quieres recibir más correos, '
             f'<a href="{esc(unsub)}" style="color:#888">escríbenos aquí</a>.</p>')
    html_body = f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">{body}</div>'
    msg = EmailMessage()
    msg["From"] = formataddr((sender.get("name") or campaign["sender"]["name"], sender["email"]))
    msg["To"] = formataddr((person["name"], person["email"])) if person.get("name") else person["email"]
    subject = f.get("subject") or plain(campaign["subject"])
    msg["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    if campaign["sender"].get("reply_to"):
        msg["Reply-To"] = campaign["sender"]["reply_to"]
    if original_id:
        msg["In-Reply-To"] = original_id
        msg["References"] = original_id
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender["email"].split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def make_message(campaign, person, subject_prefix="", sender=None):
    if "html" in campaign:  # a campaign of the app's library (mailing/library.py)
        from .library import build_message
        msg = build_message(campaign, person)
    else:
        unsub = unsubscribe_mailto(campaign, person["email"])
        msg = build_eml(campaign, person, render_html(campaign, person["greeting"], unsub),
                        render_text(campaign, person["greeting"], unsub))
    del msg["X-Unsent"]
    if sender:
        msg.replace_header("From", formataddr((sender["name"], sender["email"])))
    if subject_prefix:
        msg.replace_header("Subject", subject_prefix + msg["Subject"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender or campaign["sender"])["email"].split("@")[-1])
    return msg


def _is_limit(err):
    """The provider refuses more mail from this mailbox for now (daily quota, rate, block)."""
    text = str(err).lower()
    return any(k in text for k in ("limit", "quota", "rate", "too many", "blocked", "suspended", "5.4.5"))


def _is_transient(err):
    """A dropped, expired or timed-out connection, or a 4xx reply: reconnect and try again."""
    if isinstance(err, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(err, smtplib.SMTPResponseException):
        return 400 <= err.smtp_code < 500
    return isinstance(err, OSError) and not isinstance(err, smtplib.SMTPException)  # socket errors


def _close(conn):
    try:
        conn.quit()
    except Exception:
        pass


def send_campaign(campaign_path, contacts_csv, out_dir, *, senders, send=False, test_to=None,
                  limit=0, pause=4.0, suppression_path="output/suppression.txt",
                  now=datetime.now, sleep=time.sleep, say=print, stop=None, progress=None, checkpoint=None,
                  followup=False, per_company=0, **audience_opts):
    """Send pending prospects (or one test), taking turns between the senders.

    `senders` are dicts with email, name, daily_limit and connect() (see load_senders).
    `limit` caps this run (0 = whatever today's limits allow). `stop()` returning True ends
    the run between messages; `progress(summary, email, sender)` follows each one sent.
    `checkpoint(sent_emails)` runs every CHECK_EVERY sent and returns why to pause, or "".
    A pause sets summary["paused"]; the ones not reached stay pending for the next run.
    `followup=True` sends the campaign's follow-up instead (see followup_due), each from the
    mailbox that sent the first message. `per_company` caps prospects per company a day.
    Returns a summary dict.
    """
    campaign = load_campaign(campaign_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / LOG_NAME
    people, skipped = load_people(contacts_csv, out, campaign, suppression_path, **audience_opts)
    log = read_log(log_path)
    done = {r["email"].lower() for r in log if r["estado"] == (FOLLOWUP if followup else SENT)}
    waiting = {}
    if followup:
        from .replies import read_replies
        if not campaign.get("followup", {}).get("body"):
            raise ValueError("Esta campaña no tiene texto de seguimiento.")
        pending, waiting = followup_due(campaign, log, people, read_replies(out), now().date())
        known = {s["email"] for s in senders}
        waiting["other_mailbox"] = sum(1 for p in pending if p["sender"] not in known)
        pending = [p for p in pending if p["sender"] in known]
    else:
        pending = [p for p in people if p["email"] not in done]
    pending, held = company_order(pending, sent_today(out.parent, now().date(), by="dominio"), per_company)
    today = sent_today(out.parent, now().date(), campaign["sender"]["email"])
    room = {s["email"]: max(0, s["daily_limit"] - today.get(s["email"], 0)) for s in senders}
    capacity = sum(room.values())
    this_run = min(len(pending), capacity, limit or capacity)
    summary = {"audience": len(people), "already_sent": len(done & {p["email"] for p in people}),
               "pending": len(pending) + held, "held_by_company": held, "followup_waiting": waiting,
               "sent_today": sum(today.get(s["email"], 0) for s in senders), "room": room,
               "this_run": this_run, "sent": 0, "errors": 0, "rejected": [], "stopped": "",
               "paused": "", "skipped": skipped, "by_sender": {}}

    if test_to:
        sample = dict(pending[0] if pending else people[0] if people else
                      {"email": test_to, "name": "", "greeting": greeting("")})
        sample.update(email=test_to, name="")
        for s in senders:  # one sample from each mailbox, to check they all deliver
            conn = s["connect"]()
            try:
                conn.send_message(make_message(campaign, sample, "[PRUEBA] ", s))
            finally:
                conn.quit()
            summary["sent"] += 1
        return summary
    if not send or not this_run:
        return summary

    conns, errors_in_row = {}, {s["email"]: 0 for s in senders}
    used = {}  # messages sent on each open connection
    queue = list(pending)
    turn = 0
    next_break = random.randint(*BREAK_EVERY)
    failed, failed_in_row, sent_now = 0, 0, []

    def breaker():
        """Why the run should pause, or ''."""
        attempts = summary["sent"] + failed
        if failed_in_row >= BREAKER_IN_ROW:
            return f"{failed_in_row} envíos seguidos fallaron."
        if attempts >= BREAKER_MIN_ATTEMPTS and failed / attempts > BREAKER_RATE:
            return (f"{failed} de {attempts} envíos fallaron ({failed / attempts:.0%}); "
                    f"el máximo sano es {BREAKER_RATE:.0%}.")
        return ""

    def pause_run(reason):
        summary["paused"] = reason
        summary["stopped"] = "; ".join(filter(None, [summary["stopped"], "Pausado: " + reason]))
        say(f"  Pausado: {reason}")

    def deliver(sender, msg):
        """Send on this mailbox's connection, opening a fresh one every RECONNECT_EVERY
        messages; a dropped or expired connection is reopened and the same message retried."""
        who = sender["email"]
        for attempt in range(len(RETRY_PAUSES) + 1):
            if stop and stop():
                raise StopSending("Detenido a mano.")
            try:
                if who in conns and used[who] >= RECONNECT_EVERY:
                    _close(conns.pop(who))
                if who not in conns:
                    conns[who], used[who] = sender["connect"](), 0
                conns[who].send_message(msg)
                used[who] += 1
                return
            except smtplib.SMTPRecipientsRefused:
                raise
            except (smtplib.SMTPException, OSError) as err:
                if who in conns:
                    _close(conns.pop(who))
                if _is_limit(err) or not _is_transient(err) or attempt == len(RETRY_PAUSES):
                    raise
                say(f"  {who}: {err}; reconecto y reintento en {RETRY_PAUSES[attempt]} s")
                sleep(RETRY_PAUSES[attempt])
    try:
        while queue and summary["sent"] < this_run:
            if stop and stop():
                summary["stopped"] = "Detenido a mano. Al volver a enviar sigue con los que faltan."
                break
            active = [s for s in senders if room[s["email"]] > 0]
            if not active:
                summary["stopped"] = summary["stopped"] or "Se acabó el cupo de hoy de todos los buzones."
                break
            if followup:  # in the same thread, so from the mailbox that sent the first one
                by_email = {s["email"]: s for s in active}
                person = next((p for p in queue if p["sender"] in by_email), None)
                if not person:
                    summary["stopped"] = summary["stopped"] or "Se acabó el cupo de hoy de los buzones que faltan."
                    break
                sender = by_email[person["sender"]]
                msg = make_followup(campaign, person, person["original_id"], sender)
            else:
                sender = active[turn % len(active)]
                turn += 1
                person = queue[0]
                msg = make_message(campaign, person, sender=sender)
            who = sender["email"]
            try:
                deliver(sender, msg)
            except StopSending:
                summary["stopped"] = "Detenido a mano. Al volver a enviar sigue con los que faltan."
                break
            except smtplib.SMTPRecipientsRefused as err:
                detail = "; ".join(f"{c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                                   for c, m in err.recipients.values())
                append_log(log_path, person["email"], "rechazado", detail, now(), who)
                queue.remove(person)
                summary["rejected"].append(person["email"])
                failed, failed_in_row = failed + 1, failed_in_row + 1
                if breaker():
                    pause_run(breaker())
                    break
                continue
            except (smtplib.SMTPResponseException, OSError, smtplib.SMTPException) as err:
                summary["errors"] += 1
                errors_in_row[who] += 1
                if (_is_limit(err) or _is_transient(err) or errors_in_row[who] >= 5
                        or isinstance(err, smtplib.SMTPAuthenticationError)):
                    # this mailbox is done for now (retries spent); the prospect stays in the queue
                    room[who] = 0
                    note = f"{who}: {err}"
                    say(f"  {who} dejó de enviar: {err}")
                    summary["stopped"] = "; ".join(filter(None, [summary["stopped"], note]))
                    continue
                append_log(log_path, person["email"], "error", str(err)[:300], now(), who)
                queue.remove(person)
                failed, failed_in_row = failed + 1, failed_in_row + 1
                if breaker():
                    pause_run(breaker())
                    break
                continue
            errors_in_row[who] = 0
            failed_in_row = 0
            room[who] -= 1
            queue.remove(person)
            append_log(log_path, person["email"], FOLLOWUP if followup else SENT, msg["Message-ID"], now(), who)
            summary["sent"] += 1
            summary["by_sender"][who] = summary["by_sender"].get(who, 0) + 1
            sent_now.append(person["email"])
            say(f"  {summary['sent']}/{this_run}  {person['email']}  ({who})")
            if progress:
                progress(summary, person["email"], who)
            if checkpoint and summary["sent"] % CHECK_EVERY == 0 and queue and summary["sent"] < this_run:
                reason = checkpoint(list(sent_now))
                if reason:
                    pause_run(reason)
                    break
            if queue and summary["sent"] < this_run:
                if summary["sent"] >= next_break:  # now and then a longer break
                    next_break = summary["sent"] + random.randint(*BREAK_EVERY)
                    sleep(random.uniform(*BREAK_SECONDS))
                else:
                    sleep(pause * random.uniform(0.7, 1.6))  # uneven, human pace
    except KeyboardInterrupt:
        summary["stopped"] = "Detenido a mano. Al volver a correrlo sigue con los que faltan."
    finally:
        for conn in conns.values():
            _close(conn)
    if summary["rejected"]:
        add_suppression(suppression_path, summary["rejected"], "rechazado")
    return summary

# --- one test message from pasted HTML (the app's "Envío de prueba") -----------------

CAMPAIGNS = HERE / "campaigns"
# Greeting merge tags of the platforms, filled with the recipient's name in a test.
GREETING_TAG = "{{Saludo}}"
MERGE_GREETING = re.compile(r"\{\{\s*(?:Saludo|contact\.SALUDO)\s*\}\}|\*\|SALUDO\|\*")


def campaigns():
    """Saved campaigns with their subject and HTML (greeting left as {{Saludo}}), for the app."""
    out = []
    for path in sorted(CAMPAIGNS.glob("*.toml")):
        c = load_campaign(path)
        out.append({"slug": path.stem, "subject": c["subject"], "sender": c["sender"]["email"],
                    "html": render_html(c, GREETING_TAG, unsubscribe_mailto(c, ""))})
    return out


def html_to_text(body):
    body = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li|table)>", "\n", body)
    body = html_lib.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _contact_field(contacts_csv, address, field):
    if not contacts_csv or not Path(contacts_csv).exists():
        return ""
    with open(contacts_csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if (row.get("email") or "").strip().lower() == address.lower():
                return row.get(field) or ""
    return ""


def contact_name(contacts_csv, address):
    """Name the scraper found for an address, or ''."""
    return _contact_field(contacts_csv, address, "name")


def send_test_html(html_body, subject, to, sender_email, campaign_slug="", env_file=".env",
                   name="", contacts_csv=None, out_dir=None):
    """Send pasted HTML once to one address, marked [PRUEBA]. {{Saludo}} becomes "Hola <name>,"
    with the name typed, or else the one in contacts.csv, like the real run. With `out_dir`
    the campaign comes from the app's library, so its images go attached like in the real
    run. Raises ValueError with a message for the person on anything wrong."""
    from .library import build_message, folder, load

    to, sender_email, subject = (to or "").strip(), (sender_email or "").strip(), (subject or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("Escribe un correo válido para recibir la prueba.")
    if not (html_body or "").strip():
        raise ValueError("Pega el HTML del mailing.")
    if not subject:
        raise ValueError("Escribe el asunto.")
    campaign = None
    if out_dir is not None:
        try:
            campaign = load(folder(out_dir, campaign_slug))
        except ValueError:
            campaign = None
    if campaign is None:
        path = CAMPAIGNS / f"{campaign_slug}.toml"
        toml = load_campaign(path if campaign_slug and path.is_file() else next(iter(sorted(CAMPAIGNS.glob("*.toml")))))
        campaign = {"slug": campaign_slug, "sender": toml["sender"], "subject": subject, "_folder": None}
    campaign = {**campaign, "html": html_body, "subject": subject}
    try:
        sender = load_senders([sender_email], campaign, env_file)[0][0]
    except SystemExit as exc:
        raise ValueError(str(exc)) from None
    first = split_name(name or contact_name(contacts_csv, to))[0]
    person = {"email": to, "name": "", "first_name": first, "greeting": greeting(first),
              "company": _contact_field(contacts_csv, to, "company")}
    msg = build_message(campaign, person, subject="[PRUEBA] " + subject)
    msg.replace_header("From", formataddr((sender["name"], sender["email"])))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender["email"].split("@")[-1])
    try:
        conn = sender["connect"]()
    except smtplib.SMTPAuthenticationError:
        raise ValueError(f"{sender['email']} no aceptó su contraseña de aplicación para enviar. "
                         "Revísala en Configuración ▸ Buzones.") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise ValueError(f"No se pudo conectar a {sender['host']}: {exc}") from None
    try:
        conn.send_message(msg)
    except smtplib.SMTPException as exc:
        raise ValueError(f"El servidor rechazó la prueba: {exc}") from None
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return {"to": to, "from": sender["email"], "subject": msg["Subject"], "greeting": greeting(first)}


def main(argv=None):
    from mail_contact_scraper.imap_source import connect as imap_connect

    ap = argparse.ArgumentParser(
        prog="python -m mailing.send",
        description="Envía el mailing desde el buzón de la empresa (SMTP). Sin --test o --send no envía nada.")
    ap.add_argument("campaign", nargs="?", default=HERE / "campaigns" / "qc-aro.toml")
    ap.add_argument("--contacts", default="output/contacts.csv", help="CSV del scraper")
    ap.add_argument("--out", help="carpeta de la campaña (default: output/mailing/<slug>)")
    ap.add_argument("--test", metavar="CORREO", help="manda una sola prueba a este correo")
    ap.add_argument("--send", action="store_true", help="envía de verdad a los prospectos pendientes")
    ap.add_argument("--yes", action="store_true", help="no pedir confirmación")
    ap.add_argument("--from", dest="senders", action="append", metavar="CORREO",
                    help="buzón desde el que se envía (repetible: se turnan). Default: el sender de la campaña")
    ap.add_argument("--limit", type=int, default=0, help="máximo de correos en esta corrida (default: lo que dé el cupo de hoy)")
    ap.add_argument("--daily-limit", type=int,
                    help=f"máximo por día y por buzón (default {MAILBOX_DAILY_LIMIT}; Google Workspace permite 2,000; "
                         f"con un servidor de envío SMTP_DAILY_LIMIT o {RELAY_DAILY_LIMIT})")
    ap.add_argument("--pause", type=float, default=4.0, help="segundos entre correos (default 4)")
    ap.add_argument("--no-bajas", action="store_true", help="no revisar bajas y rebotes antes de enviar")
    ap.add_argument("--bajas", action="store_true", help="solo revisar bajas y rebotes, sin enviar")
    ap.add_argument("--suppression", default="output/suppression.txt")
    ap.add_argument("--exclude-domain", action="append", default=[])
    ap.add_argument("--require-signature", action="store_true")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)

    campaign = load_campaign(args.campaign)
    own_domain = campaign["sender"]["email"].split("@")[-1]
    addresses = args.senders or [campaign["sender"]["email"]]
    senders, settings = load_senders(addresses, campaign, args.env, args.daily_limit)

    if args.bajas or (args.send and not args.no_bajas):
        try:
            unsub, bounced = sync_suppression(inboxes(settings), args.suppression, imap_connect, own_domains=[own_domain])
            print(f"Bajas nuevas: {len(unsub)} · rebotes nuevos: {len(bounced)} → {args.suppression}")
        except Exception as err:
            print(f"No pude revisar bajas y rebotes ({err}). Sigo con la lista que ya había.")
        if args.bajas:
            return

    if not Path(args.contacts).exists():
        sys.exit(f"No existe {args.contacts}. Corre primero: python -m mail_contact_scraper")
    out = args.out or Path(settings.output_dir) / "mailing" / campaign["slug"]
    opts = dict(senders=senders, limit=args.limit, pause=args.pause, suppression_path=args.suppression,
                exclude_domains=args.exclude_domain, require_signature=args.require_signature)

    plan = send_campaign(args.campaign, args.contacts, out, **opts)
    print(f"Campaña {campaign['slug']}")
    for s in senders:
        print(f"  {s['email']} ({s['host']}): quedan {plan['room'][s['email']]} de {s['daily_limit']} hoy")
    print(f"Prospectos: {plan['audience']} · ya enviados: {plan['already_sent']} · pendientes: {plan['pending']}")
    print(f"En esta corrida: {plan['this_run']}")

    if args.test:
        send_campaign(args.campaign, args.contacts, out, test_to=args.test, **opts)
        print(f"Prueba enviada a {args.test} desde " + ", ".join(s["email"] for s in senders) + ".")
        return
    if not args.send:
        print("No se envió nada. Usa --test tu@correo.com para una prueba o --send para enviar.")
        return
    if not plan["this_run"]:
        print("No hay nada que enviar hoy.")
        return
    hours = plan["this_run"] * args.pause / 3600
    if not args.yes and input(f"Se van a enviar {plan['this_run']} correos (~{hours:.1f} h). "
                              "Escribe ENVIAR para seguir: ").strip() != "ENVIAR":
        print("Cancelado, no se envió nada.")
        return
    result = send_campaign(args.campaign, args.contacts, out, send=True, **opts)
    print(f"Enviados: {result['sent']} · rechazados: {len(result['rejected'])} · errores: {result['errors']}")
    for who, n in result["by_sender"].items():
        print(f"  {who}: {n}")
    if result["stopped"]:
        print(result["stopped"])
    print(f"Registro: {Path(out) / LOG_NAME}")


if __name__ == "__main__":
    main()
