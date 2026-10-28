"""Command line entry point: python -m mail_contact_scraper [options]."""
import argparse
import csv
import json
import logging
import mailbox
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import enrich
from .config import load_settings
from .contacts import ContactBook, read_ignore_file, read_services_file
from .extract import parse_signature
from .parse import parse_message
from .signature import IMG_TOKEN_RE, extract_signature

log = logging.getLogger("mail_contact_scraper")

# Bump when reading a message gets better (OCR, signature parsing, bulk detection), so
# --refresh knows which records were read by an older version.
READER_VERSION = 2


def resolve_images(signature, msg, ocr):
    """Replace [[IMG:n]] tokens in the signature with the image's OCR text."""
    used = False

    def substitute(match):
        nonlocal used
        idx = int(match.group(1))
        if not ocr or idx >= len(msg["image_srcs"]):
            return ""
        text = ocr.read(ocr.load(msg["image_srcs"][idx], msg["images"]))
        used = used or bool(text)
        return text

    text = IMG_TOKEN_RE.sub(substitute, signature)
    return "\n".join(l for l in text.splitlines() if l.strip()), used


def analyze(raw_bytes, region="MX", ocr=None):
    msg = parse_message(raw_bytes)
    signature = extract_signature(msg["body"], msg["from_name"])
    signature, from_image = resolve_images(signature, msg, ocr)
    contact = parse_signature(signature, msg["from_name"], msg["from_email"], region)
    return {
        "message_id": msg["message_id"],
        "date": msg["date"],
        "subject": msg["subject"],
        "from_name": msg["from_name"],
        "from_email": msg["from_email"],
        "recipients": msg["recipients"],
        "signature": signature,
        "signature_from_image": from_image,
        "contact": contact,
        "bulk": msg["bulk"],
        "bounced": msg["bounced"],
        "v": READER_VERSION,
    }


def record_key(rec):
    """What identifies a message across runs: its IMAP uid, or the local file it came from."""
    if rec.get("uid"):
        return ("imap", (rec.get("mailbox") or "").lower(), rec.get("folder"), rec.get("uidvalidity"), rec["uid"])
    if rec.get("source"):
        return ("file", rec["source"])
    return None


def _load_done(jsonl_path, primary="", min_version=0):
    """Previous records plus, per mailbox, the (folder, uidvalidity, uid) already read.

    A message read twice (by --refresh) keeps only its last record. With min_version, a
    record read by an older version doesn't count as done, so it is fetched again.
    """
    done, by_key, loose = {}, {}, []
    if jsonl_path.exists():
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("uid") and not rec.get("mailbox") and primary:
                    rec["mailbox"] = primary  # written before multi-mailbox support
                key = record_key(rec)
                if key:
                    by_key.pop(key, None)  # re-insert so the order follows the last read
                    by_key[key] = rec
                else:
                    loose.append(rec)
    for rec in by_key.values():
        if rec.get("uid") and rec.get("v", 1) >= min_version:
            key = (rec.get("mailbox") or "").lower()
            done.setdefault(key, set()).add((rec["folder"], rec["uidvalidity"], rec["uid"]))
    return done, loose + list(by_key.values())


def refresh_progress(jsonl_path, primary=""):
    """(read by this version, total) for the dashboard's "Volver a leer todo"."""
    _, records = _load_done(jsonl_path, primary)
    return sum(1 for r in records if r.get("v", 1) >= READER_VERSION), len(records)


def _compact(jsonl_path, records):
    """Rewrite messages.jsonl without the older copies --refresh left behind."""
    tmp = jsonl_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    os.replace(tmp, jsonl_path)


def _forget_image_signatures(jsonl_path):
    """Drop records whose signature came from an image, so the scan fetches them again."""
    if not jsonl_path.exists():
        return 0
    kept, dropped = [], 0
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                image = json.loads(line).get("signature_from_image")
            except (json.JSONDecodeError, AttributeError):
                image = False
            if image:
                dropped += 1
            else:
                kept.append(line)
    tmp = jsonl_path.with_suffix(".tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    os.replace(tmp, jsonl_path)
    log.info("Se volverán a leer %d correos con firma en imagen.", dropped)
    return dropped


def iter_local(paths):
    """Yield (source, raw_bytes) from .eml files, directories of them, or .mbox files."""
    for path in map(Path, paths):
        if path.is_dir():
            yield from iter_local(sorted(p for p in path.rglob("*") if p.is_file()))
        elif path.suffix.lower() in (".mbox", ".mbx") or path.name.lower() == "mbox":
            for i, msg in enumerate(mailbox.mbox(str(path))):
                yield f"{path}#{i}", msg.as_bytes()
        elif path.suffix.lower() in (".eml", ".msg", ".txt", ""):
            yield str(path), path.read_bytes()


def build_contacts(records, settings, out_dir):
    """Write contacts.csv from the parsed messages. Returns (count, omitted)."""
    book = ContactBook(
        exclude=[b.email for b in settings.mailboxes] if settings.exclude_self else [],
        skip_automated=settings.skip_automated,
        exclude_domains=settings.exclude_domains,
        skip_services=settings.skip_services,
        # Read every time: the dashboard's "Ignorar" button edits it while a scan runs.
        ignore=read_ignore_file(settings.ignore_file),
        allow=read_ignore_file(settings.allow_file),
        competitors=read_ignore_file(settings.competitors_file),
        services=read_services_file(settings.services_file),
        companies=enrich.lookup(enrich.load(out_dir)) if settings.enrich_domains else None,
    )
    for rec in records:
        book.add_sender(rec)
        if settings.include_header_contacts and not rec.get("bulk"):
            for name, addr in rec.get("recipients", []):
                book.add_header_contact(name, addr, rec.get("date"), rec.get("mailbox"))
    count = book.write_csv(out_dir / "contacts.csv.tmp")
    os.replace(out_dir / "contacts.csv.tmp", out_dir / "contacts.csv")
    tmp = out_dir / "omitted.json.tmp"
    tmp.write_text(json.dumps(book.omitted_list(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_dir / "omitted.json")
    return count, book.omitted_counts()


def rebuild(out_dir, settings=None):
    """Re-apply filters to everything already read, without scanning (dashboard "Ignorar")."""
    settings = settings or load_settings()
    _, records = _load_done(Path(out_dir) / "messages.jsonl",
                            settings.mailboxes[0].email if settings.mailboxes else "")
    count, omitted = build_contacts(records, settings, Path(out_dir))
    status_path = Path(out_dir) / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if status and not status.get("running"):
        status.update(contacts=count, omitted=omitted)
        tmp = Path(out_dir) / "status.json.tmp"
        tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, status_path)
    return count


def write_outputs(records, settings, out_dir, status, running=True):
    """Rebuild contacts.csv and status.json; called every batch so the dashboard stays live."""
    count, omitted = build_contacts(records, settings, out_dir)
    status.update(running=running, contacts=count, messages_total=len(records), omitted=omitted,
                  updated_at=datetime.now().isoformat(timespec="seconds"))
    tmp = out_dir / "status.json.tmp"
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_dir / "status.json")
    return count


def build_parser():
    p = argparse.ArgumentParser(
        prog="mail_contact_scraper",
        description="Extract contacts (names, companies, phones, emails) from email signatures.",
    )
    p.add_argument("--folders", help="Comma separated IMAP folders, or ALL (default: IMAP_FOLDERS or INBOX)")
    p.add_argument("--list-folders", action="store_true", help="Print the mailbox folders and exit")
    p.add_argument("--detect", action="store_true", help="Print the IMAP host guessed from MX records and exit")
    p.add_argument("--mailbox", help="Only these mailboxes (comma separated emails). With --local: "
                   "the mailbox the files came from")
    p.add_argument("--local", nargs="+", metavar="PATH", help="Read .eml files / folders / .mbox instead of IMAP")
    p.add_argument("--output", help="Output directory (default: OUTPUT_DIR or ./output)")
    p.add_argument("--fresh", action="store_true", help="Ignore previous progress and start over")
    p.add_argument("--no-ocr", action="store_true", help="Skip OCR of image signatures")
    p.add_argument("--refresh", action="store_true",
                   help="Read again every message read by an older version (better OCR and filters). "
                        "Current contacts stay until each message is re-read; resumes if stopped")
    p.add_argument("--reocr", action="store_true",
                   help="Read again the messages whose signature was an image (after improving OCR)")
    p.add_argument("--dashboard", action="store_true", help="Open the live dashboard while scraping")
    p.add_argument("--limit", type=int, help="Stop after N new messages (handy for a test run)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    if args.dashboard:
        # The dashboard owns the scraper as a child process, so Start/Stop work from the page
        # and the page stays up after the run ends.
        from .dashboard import main as dashboard_main
        dash_args = ["--autostart", "--"] + _passthrough(argv)
        if args.output:
            dash_args = ["--output", args.output] + dash_args
        return dashboard_main(dash_args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    settings = load_settings()
    if args.output:
        settings.output_dir = args.output
    if args.no_ocr:
        settings.ocr_images = False
    out_dir = Path(settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "messages.jsonl"
    if not args.detect and not args.list_folders and not _take_lock(out_dir):
        log.error("Ya hay un escaneo en curso en %s (el del dashboard o el automático).", out_dir)
        return 2
    if args.fresh and jsonl_path.exists():
        jsonl_path.unlink()
    done, records = _load_done(jsonl_path, settings.mailboxes[0].email if settings.mailboxes else "")
    wanted = [m.strip().lower() for m in (args.mailbox or "").split(",") if m.strip()]

    boxes = []
    if not args.local:
        boxes = [b for b in settings.mailboxes if not wanted or b.email.lower() in wanted]
        if not settings.mailboxes:
            sys.exit("Set IMAP_USER (and IMAP_PASSWORD or IMAP_OAUTH_TOKEN) in .env")
        if not boxes:
            sys.exit("No configured mailbox matches --mailbox. Configured: "
                     + ", ".join(b.email for b in settings.mailboxes))
        for box in boxes:
            if not box.imap_host:
                from .provider import guess_imap_host
                box.imap_host, reason = guess_imap_host(box.email)
                log.info("%s IMAP host: %s (%s)", box.email, box.imap_host, reason)
        if args.detect:
            print("\n".join(b.imap_host if len(boxes) == 1 else f"{b.email}: {b.imap_host}" for b in boxes))
            return 0

    primary = settings.mailboxes[0].email if settings.mailboxes else ""
    if args.reocr:  # only now that the scan can start, so a bad .env never loses records
        _forget_image_signatures(jsonl_path)
        done, records = _load_done(jsonl_path, primary)
    if args.refresh:
        done, records = _load_done(jsonl_path, primary, min_version=READER_VERSION)
        _compact(jsonl_path, records)
        pending = sum(1 for r in records if r.get("v", 1) < READER_VERSION)
        log.info("Volviendo a leer %d correos leídos con una versión anterior.", pending)
    index = {record_key(r): i for i, r in enumerate(records) if record_key(r)}

    ocr = None
    if settings.ocr_images:
        from .ocr import SignatureOCR
        from .vision import from_settings
        ocr = SignatureOCR(settings.ocr_lang, settings.ocr_remote_images, from_settings(settings, out_dir))

    local_box = wanted[0] if args.local and len(wanted) == 1 else ""
    status = {"started_at": datetime.now().isoformat(timespec="seconds"),
              "mailbox": boxes[0].email if boxes else local_box,
              "mailboxes": [{"email": b.email, "state": "pending", "new": 0} for b in boxes],
              "pid": os.getpid(), "folder": "", "folder_total": 0, "folder_pending": 0, "new_messages": 0,
              "refresh": bool(args.refresh)}
    write_outputs(records, settings, out_dir, status)

    def on_folder(folder, total, pending):
        status.update(folder=folder, folder_total=total, folder_pending=pending, folder_done=0)

    try:
        new = 0
        with open(jsonl_path, "a", encoding="utf-8") as sink:
            def handle(raw, meta, entry=None):
                nonlocal new
                try:
                    rec = analyze(raw, settings.default_region, ocr)
                except Exception as exc:
                    log.warning("Could not parse %s: %s", meta, exc)
                    return
                rec.update(meta)
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                key = record_key(rec)
                if key and key in index:  # --refresh: the new reading replaces the old one
                    records[index[key]] = rec
                else:
                    if key:
                        index[key] = len(records)
                    records.append(rec)
                new += 1
                status["new_messages"] = new
                status["folder_done"] = status.get("folder_done", 0) + 1
                if entry is not None:
                    entry["new"] += 1
                if new % settings.batch_size == 0:
                    sink.flush()
                    write_outputs(records, settings, out_dir, status)

            if args.local:
                seen = {r.get("source") for r in records if not args.refresh or r.get("v", 1) >= READER_VERSION}
                status.update(folder="archivos locales", folder_pending=0, folder_done=0)
                meta = {"mailbox": local_box} if local_box else {}
                for source, raw in iter_local(args.local):
                    if source in seen:
                        continue
                    handle(raw, {"source": source, **meta})
                    if args.limit and new >= args.limit:
                        break
            else:
                from . import imap_source
                for box, entry in zip(boxes, status["mailboxes"]):
                    if args.limit and new >= args.limit:
                        break
                    entry["state"] = "running"
                    previous = {k: status.get(k) for k in ("mailbox", "folder", "folder_total", "folder_pending", "folder_done")}
                    status.update(mailbox=box.email, folder="", folder_total=0, folder_pending=0, folder_done=0)
                    write_outputs(records, settings, out_dir, status)
                    try:
                        scan_mailbox(imap_source, box, args, settings, done.get(box.email.lower(), set()),
                                     on_folder, lambda raw, meta: handle(raw, {"mailbox": box.email, **meta}, entry),
                                     lambda: bool(args.limit and new >= args.limit))
                    except Exception as exc:
                        # One bad password should not stop the other mailboxes.
                        log.error("%s: %s", box.email, exc)
                        entry.update(state="error", error=str(exc) or exc.__class__.__name__)
                        if not status["folder"] and previous["folder"]:
                            status.update(previous)  # keep showing the last mailbox that was read
                        continue
                    entry["state"] = "done"
                failed = [e for e in status["mailboxes"] if e["state"] == "error"]
                if failed and len(failed) == len(boxes):
                    raise RuntimeError(failed[0]["error"] if len(failed) == 1 else
                                       "; ".join(f"{e['email']}: {e['error']}" for e in failed))

    except BaseException as exc:
        for entry in status["mailboxes"]:
            if entry["state"] == "running":
                entry["state"] = "stopped" if isinstance(exc, KeyboardInterrupt) else "error"
        status["error"] = "Detenido" if isinstance(exc, KeyboardInterrupt) else (str(exc) or exc.__class__.__name__)
        write_outputs(records, settings, out_dir, status, running=False)
        raise
    if args.refresh:
        _compact(jsonl_path, records)
    if settings.enrich_domains:
        try:
            enrich_companies(records, settings, out_dir, status)
        except KeyboardInterrupt:  # Stop while visiting websites: keep what was read, finish the rest next scan
            log.info("Revisión de sitios web detenida.")
        except Exception as exc:  # never lose a scan over a website
            log.warning("No se pudo revisar el giro de las empresas: %s", exc)
    count = write_outputs(records, settings, out_dir, status, running=False)
    log.info("Processed %d new messages (%d total). Wrote %d contacts to %s",
             new, len(records), count, out_dir / "contacts.csv")
    _auto_sheets(out_dir)
    return 0


def enrich_companies(records, settings, out_dir, status=None):
    """Visit the websites of the companies not classified yet, then rebuild contacts.csv."""
    build_contacts(records, settings, out_dir)  # the contacts of this scan's last batch too
    with open(out_dir / "contacts.csv", newline="", encoding="utf-8-sig") as fh:
        domains = {enrich.domain_of(row) for row in csv.DictReader(fh)}
    todo = enrich.pending(domains, enrich.load(out_dir))[: settings.enrich_max]
    if not todo:
        return 0
    if status is not None:
        status["enriching"] = len(todo)
        write_outputs(records, settings, out_dir, status)
    try:
        visited = enrich.enrich(out_dir, todo, limit=settings.enrich_max)
    finally:
        if status is not None:
            status.pop("enriching", None)
    return visited


_LOCKS = {}


def _take_lock(out_dir):
    """One scan per output folder: the dashboard's and the scheduled one must not interleave."""
    key = str(Path(out_dir).resolve())
    if key in _LOCKS:
        return True
    try:
        import fcntl
    except ImportError:  # Windows
        return True
    fh = open(out_dir / ".scan.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCKS[key] = fh  # held until the process exits
    return True


def _auto_sheets(out_dir):
    from . import sheets

    cfg = sheets.config()
    if not (cfg["url"] and cfg["auto"]):
        return
    try:
        state = sheets.push(out_dir)
        log.info("Google Sheets: %d contactos en la pestaña %s", state["rows"], state["tab"])
    except sheets.SheetsError as exc:
        log.warning("Google Sheets: %s", exc)


def scan_mailbox(imap_source, box, args, settings, done, on_folder, handle, reached_limit):
    conn = imap_source.connect(box)
    try:
        if args.list_folders:
            names = imap_source.list_folders(conn)
            if len(settings.mailboxes) > 1:
                print(f"{box.email}:")
                names = ["  " + n for n in names]
            print("\n".join(names))
            return
        folders = args.folders.split(",") if args.folders else box.folders
        if [f.upper() for f in folders] == ["ALL"]:
            folders = imap_source.list_folders(conn)
        for folder in folders:
            for fold, uidv, uid, raw in imap_source.iter_messages(
                conn, folder.strip(), settings.batch_size, settings.batch_delay, done, on_folder
            ):
                handle(raw, {"folder": fold, "uidvalidity": uidv, "uid": uid})
                if reached_limit():
                    return
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _passthrough(argv):
    """Arguments to forward from `--dashboard` to the scraper the dashboard launches."""
    return [a for a in argv if a != "--dashboard"]


def run():
    # A parent started from a background shell may hand us SIGINT ignored; the dashboard's
    # Stop button relies on it, so always restore Python's Ctrl+C behaviour.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return main()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
