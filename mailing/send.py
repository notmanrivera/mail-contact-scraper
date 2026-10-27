"""Send a mailing from the company's own mailbox, without YAMM or a paid platform.

Uses SMTP with the same mailbox and app password the scraper already has in .env, so
every message leaves from pedro@ (and shows up in its Sent folder). Nothing goes out
unless you pass --test or --send:

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
import imaplib
import random
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.utils import make_msgid, formatdate, parseaddr
from pathlib import Path

from .build import (EMAIL_RE, build_eml, greeting, load_audience, load_campaign, render_html,
                    render_text, unsubscribe_mailto)

HERE = Path(__file__).parent
LOG_NAME = "envios.csv"
LOG_FIELDS = ["fecha", "email", "estado", "detalle"]
SENT = "enviado"
UNSUBSCRIBE_SUBJECT = "Baja de correos"

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

def sender_mailbox(sender_email, env_file=".env"):
    """The .env mailbox (address + app password) the campaign sends from, and its SMTP host."""
    from mail_contact_scraper import envfile
    from mail_contact_scraper.config import load_settings
    from mail_contact_scraper.provider import guess_imap_host

    settings = load_settings(env_file)
    box = next((b for b in settings.mailboxes if b.email.lower() == sender_email.lower()), None)
    if not box or not (box.password or box.oauth_token):
        raise SystemExit(f"Para enviar desde {sender_email} ponlo en .env (IMAP_USER o IMAP_USER_2...) "
                         "con su contraseña de aplicación, la misma que usa el scraper.")
    if not box.imap_host:
        box.imap_host = guess_imap_host(box.email)[0]
    env = envfile.environ(env_file).get
    host, port = SMTP_FOR_IMAP.get(box.imap_host.lower(), (f"smtp.{box.email.split('@')[-1]}", 465))
    host = env("SMTP_HOST", "") or host
    port = int(env("SMTP_PORT", "") or port)
    return box, host, port, settings


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


def append_log(path, email_addr, status, detail="", now=None):
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_FIELDS)
        w.writerow([(now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), email_addr, status, detail])


def sent_today(mailing_root, day):
    """Messages sent today across every campaign, to respect the mailbox's daily limit."""
    prefix = day.isoformat()
    return sum(1 for log in Path(mailing_root).glob(f"*/{LOG_NAME}") for row in read_log(log)
               if row["estado"] == SENT and row["fecha"].startswith(prefix))


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


# --- sending -------------------------------------------------------------------------

def make_message(campaign, person, subject_prefix=""):
    unsub = unsubscribe_mailto(campaign, person["email"])
    msg = build_eml(campaign, person, render_html(campaign, person["greeting"], unsub),
                    render_text(campaign, person["greeting"], unsub))
    del msg["X-Unsent"]
    if subject_prefix:
        msg.replace_header("Subject", subject_prefix + msg["Subject"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=campaign["sender"]["email"].split("@")[-1])
    return msg


def _is_limit(err):
    text = str(err).lower()
    return getattr(err, "smtp_code", 0) == 421 or any(
        k in text for k in ("limit", "quota", "rate", "too many", "blocked", "suspended"))


def send_campaign(campaign_path, contacts_csv, out_dir, *, smtp_connect, send=False, test_to=None,
                  limit=200, daily_limit=500, pause=8.0, suppression_path="output/suppression.txt",
                  now=datetime.now, sleep=time.sleep, say=print, **audience_opts):
    """Send pending prospects (or one test). Returns a summary dict."""
    campaign = load_campaign(campaign_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / LOG_NAME
    people, skipped = load_audience(contacts_csv, campaign, suppression_path=suppression_path, **audience_opts)
    done = {r["email"].lower() for r in read_log(log_path) if r["estado"] == SENT}
    pending = [p for p in people if p["email"] not in done]
    today = sent_today(out.parent, now().date())
    room = max(0, min(limit, daily_limit - today))
    summary = {"audience": len(people), "already_sent": len(people) - len(pending), "pending": len(pending),
               "sent_today": today, "this_run": min(room, len(pending)), "sent": 0, "errors": 0,
               "rejected": [], "stopped": "", "skipped": skipped}

    if test_to:
        sample = dict(pending[0] if pending else people[0] if people else
                      {"email": test_to, "name": "", "greeting": greeting("")})
        sample.update(email=test_to, name="")
        conn = smtp_connect()
        try:
            conn.send_message(make_message(campaign, sample, "[PRUEBA] "))
        finally:
            conn.quit()
        summary["sent"] = 1
        return summary
    if not send or not summary["this_run"]:
        return summary

    batch = pending[:summary["this_run"]]
    conn = smtp_connect()
    consecutive_errors = 0
    try:
        for i, person in enumerate(batch):
            msg = make_message(campaign, person)
            try:
                try:
                    conn.send_message(msg)
                except smtplib.SMTPServerDisconnected:
                    conn = smtp_connect()
                    conn.send_message(msg)
            except smtplib.SMTPRecipientsRefused as err:
                detail = "; ".join(f"{c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                                   for c, m in err.recipients.values())
                append_log(log_path, person["email"], "rechazado", detail, now())
                summary["rejected"].append(person["email"])
                continue
            except (smtplib.SMTPDataError, smtplib.SMTPSenderRefused, smtplib.SMTPResponseException) as err:
                append_log(log_path, person["email"], "error", str(err)[:300], now())
                summary["errors"] += 1
                if _is_limit(err):
                    summary["stopped"] = f"El servidor no acepta más correos por ahora: {err}"
                    break
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    summary["stopped"] = "5 errores seguidos, se detuvo el envío"
                    break
                continue
            except (OSError, smtplib.SMTPException) as err:
                append_log(log_path, person["email"], "error", str(err)[:300], now())
                summary["errors"] += 1
                summary["stopped"] = f"Se perdió la conexión: {err}"
                break
            consecutive_errors = 0
            append_log(log_path, person["email"], SENT, msg["Message-ID"], now())
            summary["sent"] += 1
            say(f"  {summary['sent']}/{len(batch)}  {person['email']}")
            if i < len(batch) - 1:
                sleep(pause * random.uniform(0.7, 1.3))  # steady, human pace
    except KeyboardInterrupt:
        summary["stopped"] = "Detenido a mano. Al volver a correrlo sigue con los que faltan."
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    if summary["rejected"]:
        add_suppression(suppression_path, summary["rejected"], "rechazado")
    return summary


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
    ap.add_argument("--limit", type=int, default=200, help="máximo de correos en esta corrida (default 200)")
    ap.add_argument("--daily-limit", type=int, default=500,
                    help="máximo por día sumando todas las campañas (default 500; Google Workspace permite 2,000)")
    ap.add_argument("--pause", type=float, default=8.0, help="segundos entre correos (default 8)")
    ap.add_argument("--no-bajas", action="store_true", help="no revisar bajas y rebotes antes de enviar")
    ap.add_argument("--bajas", action="store_true", help="solo revisar bajas y rebotes, sin enviar")
    ap.add_argument("--suppression", default="output/suppression.txt")
    ap.add_argument("--exclude-domain", action="append", default=[])
    ap.add_argument("--require-signature", action="store_true")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)

    campaign = load_campaign(args.campaign)
    sender = campaign["sender"]["email"]
    box, host, port, settings = sender_mailbox(sender, args.env)

    if args.bajas or (args.send and not args.no_bajas):
        boxes = [b for b in settings.mailboxes if b.password or b.oauth_token]
        for b in boxes:
            if not b.imap_host:
                b.imap_host = box.imap_host
        try:
            unsub, bounced = sync_suppression(boxes, args.suppression, imap_connect,
                                              own_domains=[sender.split("@")[-1]])
            print(f"Bajas nuevas: {len(unsub)} · rebotes nuevos: {len(bounced)} → {args.suppression}")
        except Exception as err:
            print(f"No pude revisar bajas y rebotes ({err}). Sigo con la lista que ya había.")
        if args.bajas:
            return

    if not Path(args.contacts).exists():
        sys.exit(f"No existe {args.contacts}. Corre primero: python -m mail_contact_scraper")
    out = args.out or Path(settings.output_dir) / "mailing" / campaign["slug"]
    opts = dict(smtp_connect=lambda: open_smtp(box, host, port), limit=args.limit,
                daily_limit=args.daily_limit, pause=args.pause, suppression_path=args.suppression,
                exclude_domains=args.exclude_domain, require_signature=args.require_signature)

    plan = send_campaign(args.campaign, args.contacts, out, **opts)
    print(f"Campaña {campaign['slug']} desde {sender} ({host})")
    print(f"Prospectos: {plan['audience']} · ya enviados: {plan['already_sent']} · pendientes: {plan['pending']}")
    print(f"Enviados hoy: {plan['sent_today']} de {args.daily_limit} · en esta corrida: {plan['this_run']}")

    if args.test:
        send_campaign(args.campaign, args.contacts, out, test_to=args.test, **opts)
        print(f"Prueba enviada a {args.test}.")
        return
    if not args.send:
        print("No se envió nada. Usa --test tu@correo.com para una prueba o --send para enviar.")
        return
    if not plan["this_run"]:
        print("No hay nada que enviar hoy.")
        return
    minutes = plan["this_run"] * args.pause / 60
    if not args.yes and input(f"Se van a enviar {plan['this_run']} correos (~{minutes:.0f} min). "
                              "Escribe ENVIAR para seguir: ").strip() != "ENVIAR":
        print("Cancelado, no se envió nada.")
        return
    result = send_campaign(args.campaign, args.contacts, out, send=True, **opts)
    print(f"Enviados: {result['sent']} · rechazados: {len(result['rejected'])} · errores: {result['errors']}")
    if result["stopped"]:
        print(result["stopped"])
    print(f"Registro: {Path(out) / LOG_NAME}")


if __name__ == "__main__":
    main()
