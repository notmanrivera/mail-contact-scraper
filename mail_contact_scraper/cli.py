"""Command line entry point: python -m mail_contact_scraper [options]."""
import argparse
import json
import logging
import mailbox
import sys
from pathlib import Path

from .config import load_settings
from .contacts import ContactBook
from .extract import parse_signature
from .parse import parse_message
from .signature import IMG_TOKEN_RE, extract_signature

log = logging.getLogger("mail_contact_scraper")


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
    }


def _load_done(jsonl_path):
    done, records = set(), []
    if jsonl_path.exists():
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                records.append(rec)
                if rec.get("uid"):
                    done.add((rec["folder"], rec["uidvalidity"], rec["uid"]))
    return done, records


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


def build_parser():
    p = argparse.ArgumentParser(
        prog="mail_contact_scraper",
        description="Extract contacts (names, companies, phones, emails) from email signatures.",
    )
    p.add_argument("--folders", help="Comma separated IMAP folders, or ALL (default: IMAP_FOLDERS or INBOX)")
    p.add_argument("--list-folders", action="store_true", help="Print the mailbox folders and exit")
    p.add_argument("--detect", action="store_true", help="Print the IMAP host guessed from MX records and exit")
    p.add_argument("--local", nargs="+", metavar="PATH", help="Read .eml files / folders / .mbox instead of IMAP")
    p.add_argument("--output", help="Output directory (default: OUTPUT_DIR or ./output)")
    p.add_argument("--fresh", action="store_true", help="Ignore previous progress and start over")
    p.add_argument("--no-ocr", action="store_true", help="Skip OCR of image signatures")
    p.add_argument("--limit", type=int, help="Stop after N new messages (handy for a test run)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    settings = load_settings()
    if args.output:
        settings.output_dir = args.output
    if args.no_ocr:
        settings.ocr_images = False
    out_dir = Path(settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "messages.jsonl"
    if args.fresh and jsonl_path.exists():
        jsonl_path.unlink()
    done, records = _load_done(jsonl_path)

    if not args.local:
        if not settings.email:
            sys.exit("Set IMAP_USER (and IMAP_PASSWORD or IMAP_OAUTH_TOKEN) in .env")
        if not settings.imap_host:
            from .provider import guess_imap_host
            settings.imap_host, reason = guess_imap_host(settings.email)
            log.info("IMAP host: %s (%s)", settings.imap_host, reason)
        if args.detect:
            print(settings.imap_host)
            return 0

    ocr = None
    if settings.ocr_images:
        from .ocr import SignatureOCR
        ocr = SignatureOCR(settings.ocr_lang, settings.ocr_remote_images)

    new = 0
    with open(jsonl_path, "a", encoding="utf-8") as sink:
        def handle(raw, meta):
            nonlocal new
            try:
                rec = analyze(raw, settings.default_region, ocr)
            except Exception as exc:
                log.warning("Could not parse %s: %s", meta, exc)
                return
            rec.update(meta)
            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
            records.append(rec)
            new += 1

        if args.local:
            seen = {r.get("source") for r in records}
            for source, raw in iter_local(args.local):
                if source in seen:
                    continue
                handle(raw, {"source": source})
                if args.limit and new >= args.limit:
                    break
        else:
            from . import imap_source
            conn = imap_source.connect(settings)
            try:
                if args.list_folders:
                    print("\n".join(imap_source.list_folders(conn)))
                    return 0
                folders = (args.folders.split(",") if args.folders else settings.folders)
                if [f.upper() for f in folders] == ["ALL"]:
                    folders = imap_source.list_folders(conn)
                for folder in folders:
                    for fold, uidv, uid, raw in imap_source.iter_messages(
                        conn, folder.strip(), settings.batch_size, settings.batch_delay, done
                    ):
                        handle(raw, {"folder": fold, "uidvalidity": uidv, "uid": uid})
                        if args.limit and new >= args.limit:
                            break
                    else:
                        continue
                    break
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

    book = ContactBook(
        exclude=[settings.email] if settings.exclude_self else [],
        skip_automated=settings.skip_automated,
    )
    for rec in records:
        book.add_sender(rec)
        if settings.include_header_contacts:
            for name, addr in rec.get("recipients", []):
                book.add_header_contact(name, addr, rec.get("date"))
    csv_path = out_dir / "contacts.csv"
    count = book.write_csv(csv_path)
    log.info("Processed %d new messages (%d total). Wrote %d contacts to %s", new, len(records), count, csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
