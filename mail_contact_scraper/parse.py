"""Turn a raw RFC 822 message into headers + readable body text."""
import email
import html
import re
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser


def decode_str(value):
    if not value:
        return ""
    try:
        text = str(make_header(decode_header(str(value))))
    except Exception:
        text = str(value)
    # Raw 8-bit (non RFC 2047) headers come back as surrogate escapes.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raw = text.encode("utf-8", "surrogateescape")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    return text


# Gmail, Outlook, Apple Mail and Yahoo wrappers around the quoted previous message.
QUOTE_DIV_RE = re.compile(r"gmail_quote|divRplyFwdMsg|OutlookMessageHeader|yahoo_quoted|AppleMailQuote|moz-cite-prefix", re.I)


class _HTMLToText(HTMLParser):
    BREAK_TAGS = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP_TAGS = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []  # <img src> values, referenced as [[IMG:n]] in the text
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip += 1
        elif tag == "div" and QUOTE_DIV_RE.search(" ".join(v or "" for k, v in attrs if k in ("id", "class"))):
            self.parts.append("\n[[QUOTE]]\n")
        elif tag in self.BREAK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag == "img":
            src = dict(attrs).get("src") or ""
            if src:
                self.parts.append(f"\n[[IMG:{len(self.images)}]]\n")
                self.images.append(src)
        elif tag == "blockquote":
            self.parts.append("\n[[QUOTE]]\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("mailto:", "tel:")):
                self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in self.BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup, with_images=False):
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html.unescape(markup))
        return (text, []) if with_images else text
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (text, parser.images) if with_images else text


def _part_text(part):
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def body_text(msg):
    """Return (text, image_srcs, images).

    Uses the HTML part when there is one, because image signatures only exist
    there; falls back to text/plain. `images` maps Content-ID / file name to bytes.
    """
    plain, rich, images = None, None, {}
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype.startswith("image/"):
            data = part.get_payload(decode=True)
            if data:
                for key in (part.get("Content-ID"), part.get("Content-Location"), part.get_filename()):
                    if key:
                        images[decode_str(key).strip().strip("<>").lower()] = data
            continue
        if part.get_content_disposition() == "attachment":
            continue
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and rich is None:
            rich = _part_text(part)
    if rich and rich.strip():
        text, srcs = html_to_text(rich, with_images=True)
        if text.strip() or srcs:
            return text, srcs, images
    if plain:
        return plain.replace("\r\n", "\n"), [], images
    return "", [], images


def bulk_reason(msg):
    """Why this looks like a mass mailing (newsletter, promo, notification), or ""."""
    get = lambda name: (msg.get(name) or "").strip().lower()
    if msg.get("List-Unsubscribe-Post"):  # one-click unsubscribe: required for bulk senders
        return "list-unsubscribe"
    if get("Precedence") in ("bulk", "junk"):
        return "precedence"
    if get("Auto-Submitted") not in ("", "no"):
        return "auto-submitted"
    if msg.get("Feedback-ID") or msg.get("X-Mailgun-Sid") or msg.get("X-SG-EID") or msg.get("X-MC-User"):
        return "esp"  # Gmail feedback loop, Mailgun, SendGrid, Mailchimp
    return ""


BLOCKED = re.compile(r"spam|blocked|bloque|blacklist|block ?list|reputation|policy|rejected for policy|"
                     r"not accepted|unsolicited|5\.7\.\d", re.I)


def delivery_failures(msg):
    """[(address, "rebote" | "bloqueado")] that a delivery report (DSN) says failed for good.

    "rebote": the address does not exist or cannot receive mail. "bloqueado": the recipient's
    server refused the message itself (spam filter, policy, reputation): the address may be fine.
    Delay reports ("problema temporal", Action: delayed, Status 4.x.x) are not failures: the
    sending server keeps trying and sends a failure report later if it gives up."""
    failed = {}
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        for block in part.get_payload() or []:
            if not hasattr(block, "get"):
                continue
            action = (block.get("Action") or "").strip().lower()
            status = (block.get("Status") or "").strip()
            recipient = block.get("Final-Recipient") or block.get("Original-Recipient") or ""
            addr = recipient.split(";", 1)[-1].strip().strip("<>").lower()
            if "@" in addr and (action == "failed" or status.startswith("5")) and addr not in failed:
                why = f"{status} {block.get('Diagnostic-Code') or ''}"
                blocked = not status.startswith(("5.1", "5.2")) and (status.startswith("5.7") or BLOCKED.search(why))
                failed[addr] = "bloqueado" if blocked else "rebote"
    subject = decode_str(str(msg.get("Subject") or ""))
    if not failed and "delay" in subject.lower():
        return []
    for value in msg.get_all("X-Failed-Recipients") or []:
        for _, addr in getaddresses([value]):
            addr = addr.lower()
            if "@" in addr and addr not in failed:
                failed[addr] = "bloqueado" if re.search(r"bloque|blocked", subject, re.I) else "rebote"
    return list(failed.items())


def bounced_addresses(msg):
    """Recipients a delivery-failure report (DSN) says could not be reached."""
    return [addr for addr, _ in delivery_failures(msg)]


def _raw_headers(msg, name):
    """Header values as original strings (compat32's .get() mangles raw 8-bit headers)."""
    return [v for k, v in msg.raw_items() if k.lower() == name.lower()]


def parse_message(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=policy.compat32)
    header = lambda name: decode_str((_raw_headers(msg, name) or [""])[0])
    from_name, from_addr = parseaddr(header("From"))
    date = None
    try:
        if msg.get("Date"):
            dt = parsedate_to_datetime(msg.get("Date"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            date = dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        pass
    body, image_srcs, images = body_text(msg)
    recipients = []
    for name in ("To", "Cc", "Reply-To"):
        values = [decode_str(v) for v in _raw_headers(msg, name)]
        recipients += [(n, a) for n, a in getaddresses(values) if a]
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "subject": header("Subject"),
        "date": date,
        "from_name": from_name.strip().strip('"'),
        "from_email": from_addr.lower(),
        "recipients": recipients,
        "body": body,
        "image_srcs": image_srcs,
        "images": images,
        "bulk": bulk_reason(msg),
        "bounced": bounced_addresses(msg),
    }
