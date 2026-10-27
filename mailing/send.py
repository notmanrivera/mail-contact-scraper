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
                    render_text, unsubscribe_mailto)

HERE = Path(__file__).parent
LOG_NAME = "envios.csv"
LOG_FIELDS = ["fecha", "email", "estado", "detalle", "remitente"]
SENT = "enviado"
UNSUBSCRIBE_SUBJECT = "Baja de correos"
# Google Workspace allows 2,000 messages a day per user (Microsoft 365: 10,000); stay under it.
MAILBOX_DAILY_LIMIT = 1500
RELAY_DAILY_LIMIT = 10000

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


def sent_today(mailing_root, day, default_sender=""):
    """Messages sent today per sending mailbox, across every campaign."""
    prefix, counts = day.isoformat(), {}
    for log in Path(mailing_root).glob(f"*/{LOG_NAME}"):
        for row in read_log(log):
            if row["estado"] == SENT and row["fecha"].startswith(prefix):
                sender = (row.get("remitente") or default_sender).lower()
                counts[sender] = counts.get(sender, 0) + 1
    return counts


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

def make_message(campaign, person, subject_prefix="", sender=None):
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
    text = str(err).lower()
    return getattr(err, "smtp_code", 0) == 421 or any(
        k in text for k in ("limit", "quota", "rate", "too many", "blocked", "suspended"))


def send_campaign(campaign_path, contacts_csv, out_dir, *, senders, send=False, test_to=None,
                  limit=0, pause=4.0, suppression_path="output/suppression.txt",
                  now=datetime.now, sleep=time.sleep, say=print, stop=None, progress=None, **audience_opts):
    """Send pending prospects (or one test), taking turns between the senders.

    `senders` are dicts with email, name, daily_limit and connect() (see load_senders).
    `limit` caps this run (0 = whatever today's limits allow). `stop()` returning True ends
    the run between messages; `progress(summary, email, sender)` follows each one sent.
    Returns a summary dict.
    """
    campaign = load_campaign(campaign_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / LOG_NAME
    people, skipped = load_audience(contacts_csv, campaign, suppression_path=suppression_path, **audience_opts)
    done = {r["email"].lower() for r in read_log(log_path) if r["estado"] == SENT}
    pending = [p for p in people if p["email"] not in done]
    today = sent_today(out.parent, now().date(), campaign["sender"]["email"])
    room = {s["email"]: max(0, s["daily_limit"] - today.get(s["email"], 0)) for s in senders}
    capacity = sum(room.values())
    this_run = min(len(pending), capacity, limit or capacity)
    summary = {"audience": len(people), "already_sent": len(people) - len(pending), "pending": len(pending),
               "sent_today": sum(today.get(s["email"], 0) for s in senders), "room": room,
               "this_run": this_run, "sent": 0, "errors": 0, "rejected": [], "stopped": "",
               "skipped": skipped, "by_sender": {}}

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
    queue = list(pending)
    turn = 0
    try:
        while queue and summary["sent"] < this_run:
            if stop and stop():
                summary["stopped"] = "Detenido a mano. Al volver a enviar sigue con los que faltan."
                break
            active = [s for s in senders if room[s["email"]] > 0]
            if not active:
                summary["stopped"] = summary["stopped"] or "Se acabó el cupo de hoy de todos los buzones."
                break
            sender = active[turn % len(active)]
            turn += 1
            person = queue[0]
            msg = make_message(campaign, person, sender=sender)
            who = sender["email"]
            try:
                try:
                    if who not in conns:
                        conns[who] = sender["connect"]()
                    conns[who].send_message(msg)
                except smtplib.SMTPServerDisconnected:
                    conns[who] = sender["connect"]()
                    conns[who].send_message(msg)
            except smtplib.SMTPRecipientsRefused as err:
                detail = "; ".join(f"{c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                                   for c, m in err.recipients.values())
                append_log(log_path, person["email"], "rechazado", detail, now(), who)
                summary["rejected"].append(queue.pop(0)["email"])
                continue
            except (smtplib.SMTPResponseException, OSError, smtplib.SMTPException) as err:
                summary["errors"] += 1
                errors_in_row[who] += 1
                if _is_limit(err) or errors_in_row[who] >= 5 or not isinstance(err, smtplib.SMTPResponseException):
                    # this mailbox is done for today; the prospect stays in the queue for the next one
                    room[who] = 0
                    conns.pop(who, None)
                    note = f"{who}: {err}"
                    say(f"  {who} dejó de enviar: {err}")
                    summary["stopped"] = "; ".join(filter(None, [summary["stopped"], note]))
                    continue
                append_log(log_path, person["email"], "error", str(err)[:300], now(), who)
                queue.pop(0)
                continue
            errors_in_row[who] = 0
            room[who] -= 1
            queue.pop(0)
            append_log(log_path, person["email"], SENT, msg["Message-ID"], now(), who)
            summary["sent"] += 1
            summary["by_sender"][who] = summary["by_sender"].get(who, 0) + 1
            say(f"  {summary['sent']}/{this_run}  {person['email']}  ({who})")
            if progress:
                progress(summary, person["email"], who)
            if queue and summary["sent"] < this_run:
                sleep(pause * random.uniform(0.7, 1.3))  # steady, human pace
    except KeyboardInterrupt:
        summary["stopped"] = "Detenido a mano. Al volver a correrlo sigue con los que faltan."
    finally:
        for conn in conns.values():
            try:
                conn.quit()
            except Exception:
                pass
    if summary["rejected"]:
        add_suppression(suppression_path, summary["rejected"], "rechazado")
    return summary

# --- one test message from pasted HTML (the app's "Envío de prueba") -----------------

CAMPAIGNS = HERE / "campaigns"
# Greeting merge tags of the platforms, filled with a plain "Hola," in a test.
MERGE_GREETING = re.compile(r"\{\{\s*(?:Saludo|contact\.SALUDO)\s*\}\}|\*\|SALUDO\|\*")


def campaigns():
    """Saved campaigns with their subject and ready-to-send HTML, for the app."""
    out = []
    for path in sorted(CAMPAIGNS.glob("*.toml")):
        c = load_campaign(path)
        out.append({"slug": path.stem, "subject": c["subject"], "sender": c["sender"]["email"],
                    "html": render_html(c, greeting(""), unsubscribe_mailto(c, ""))})
    return out


def html_to_text(body):
    body = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li|table)>", "\n", body)
    body = html_lib.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def send_test_html(html_body, subject, to, sender_email, campaign_slug="", env_file=".env"):
    """Send pasted HTML once to one address, marked [PRUEBA]. Raises ValueError with a
    message for the person on anything wrong (bad address, login refused...)."""
    to, sender_email, subject = (to or "").strip(), (sender_email or "").strip(), (subject or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("Escribe un correo válido para recibir la prueba.")
    if not (html_body or "").strip():
        raise ValueError("Pega el HTML del mailing.")
    if not subject:
        raise ValueError("Escribe el asunto.")
    path = CAMPAIGNS / f"{campaign_slug}.toml"
    campaign = load_campaign(path if campaign_slug and path.is_file() else next(iter(sorted(CAMPAIGNS.glob("*.toml")))))
    try:
        sender = load_senders([sender_email], campaign, env_file)[0][0]
    except SystemExit as exc:
        raise ValueError(str(exc)) from None
    html_body = MERGE_GREETING.sub(greeting(""), html_body)
    msg = EmailMessage()
    msg["From"] = formataddr((sender["name"], sender["email"]))
    msg["To"] = to
    msg["Subject"] = "[PRUEBA] " + subject
    if campaign["sender"].get("reply_to"):
        msg["Reply-To"] = campaign["sender"]["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender["email"].split("@")[-1])
    msg.set_content(html_to_text(html_body))
    msg.add_alternative(html_body, subtype="html")
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
    return {"to": to, "from": sender["email"], "subject": msg["Subject"]}


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
