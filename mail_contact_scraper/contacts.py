"""Merge per-message findings into one row per contact and write CSV."""
import csv
import re
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path

import phonenumbers
from phonenumbers import geocoder

AUTOMATED_RE = re.compile(
    r"^(no[-_]?reply|do[-_]?not[-_]?reply|mailer-daemon|postmaster|notifications?|notificaciones|"
    r"bounce[s]?|alerts?|alertas|avisos?|newsletter|news|boletin|info-noreply|automated|system|"
    r"auto-?confirm|marketing|promo(ciones)?|ofertas|mailing|accounts?-?update)[@+._-]",
    re.I,
)

FIELDS = [
    "email", "name", "title", "company", "location", "mobile_phones", "office_phones", "fax",
    "other_emails", "websites", "source", "messages", "first_seen", "last_seen", "sample_signature",
    "mailboxes", "bounced",
]


def _read_list(text):
    return [l.split("#", 1)[0].strip().lower() for l in text.splitlines() if l.split("#", 1)[0].strip()]


@lru_cache(maxsize=1)
def service_patterns():
    """Built-in domains that never are prospects (services.txt), as compiled regexes."""
    text = resources.files(__package__).joinpath("services.txt").read_text(encoding="utf-8")
    return tuple(_domain_pattern(entry) for entry in _read_list(text))


def _domain_pattern(entry):
    entry = entry.lstrip("@")
    if entry.endswith(".*"):  # amazon.* -> amazon.com, amazon.com.mx, mx.amazon.co ...
        base = re.escape(entry[:-2])
        return re.compile(rf"(^|\.){base}\.[a-z]{{2,6}}(\.[a-z]{{2,3}})?$")
    return re.compile(rf"(^|\.){re.escape(entry)}$")


IGNORE_HEADER = ("# Correos o dominios que no son prospectos, uno por línea (uber.com, juan@empresa.mx).\n"
                 "# El dashboard agrega aquí lo que ignoras con el botón \"Ignorar\".\n")


def normalize_ignore(value):
    value = (value or "").strip().lower()
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"([^@\s]+@)?[a-z0-9-]+(\.[a-z0-9-]+)+", value):
        raise ValueError("Escribe un correo (juan@empresa.mx) o un dominio (uber.com).")
    return value


def update_ignore_file(path, add=None, remove=None):
    entries = read_ignore_file(path)
    if add:
        add = normalize_ignore(add)
        if add not in entries:
            entries.append(add)
    if remove:
        entries = [e for e in entries if e != (remove or "").strip().lower()]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(IGNORE_HEADER + "".join(f"{e}\n" for e in entries), encoding="utf-8")
    tmp.replace(path)
    return entries


def read_ignore_file(path):
    """User's own ignore list: one email or domain per line (ignorados.txt)."""
    try:
        return _read_list(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


class ContactBook:
    def __init__(self, exclude=(), skip_automated=True, exclude_domains=(), skip_services=True, ignore=()):
        self.exclude = {e.lower() for e in exclude if e}
        self.skip_automated = skip_automated
        self.exclude_domains = tuple(d.lower() for d in exclude_domains if d)
        self.services = service_patterns() if skip_services else ()
        ignore = [i.strip().lower() for i in ignore if i and i.strip()]
        self.ignore_emails = {i for i in ignore if "@" in i.lstrip("@")}
        self.ignore_domains = tuple(_domain_pattern(i) for i in ignore if "@" not in i.lstrip("@"))
        self.rows = {}
        self.bounces = {}
        self.omitted = {}  # address -> reason, for the dashboard's count

    def _excluded_domain(self, addr):
        domain = addr.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in self.exclude_domains)

    def skip_reason(self, addr):
        if not addr or addr in self.exclude or self._excluded_domain(addr):
            return "own"
        domain = addr.rsplit("@", 1)[-1]
        if addr in self.ignore_emails or any(p.search(domain) for p in self.ignore_domains):
            return "ignored"
        if any(p.search(domain) for p in self.services):
            return "service"
        if self.skip_automated and AUTOMATED_RE.match(addr):
            return "automated"
        return ""

    def _skip(self, addr):
        reason = self.skip_reason(addr)
        if reason and reason != "own" and addr not in self.rows:
            self.omitted.setdefault(addr, reason)
        return bool(reason)

    def omitted_counts(self):
        return dict(Counter(self.omitted.values()))

    def _row(self, addr):
        return self.rows.setdefault(addr, {
            "names": Counter(), "titles": Counter(), "companies": Counter(),
            "phones": {}, "other_emails": [], "websites": [], "sources": set(),
            "messages": 0, "first_seen": None, "last_seen": None, "signature": "",
            "mailboxes": [],
        })

    def _seen(self, row, date, mailbox=None):
        row["messages"] += 1
        if mailbox and mailbox not in row["mailboxes"]:
            row["mailboxes"].append(mailbox)
        if date:
            row["first_seen"] = min(filter(None, [row["first_seen"], date]))
            row["last_seen"] = max(filter(None, [row["last_seen"], date]))

    def add_bounce(self, addr, date):
        addr = (addr or "").lower()
        if date and (addr not in self.bounces or date > self.bounces[addr]):
            self.bounces[addr] = date
        elif addr and addr not in self.bounces:
            self.bounces[addr] = ""

    def add_sender(self, record):
        for addr in record.get("bounced") or []:
            self.add_bounce(addr, record.get("date"))
        addr = record["from_email"]
        if record.get("bulk"):
            # Newsletters and promos: not a person writing to us, even from a real company.
            if not self._skip(addr) and addr not in self.rows:
                self.omitted.setdefault(addr, "bulk")
            return
        if self._skip(addr):
            return
        info = record["contact"]
        self.omitted.pop(addr, None)
        row = self._row(addr)
        self._seen(row, record.get("date"), record.get("mailbox"))
        date = record.get("date")
        if date and (not row.get("last_from") or date > row["last_from"]):
            row["last_from"] = date
        if record.get("signature_from_image"):
            row["sources"].add("image")
        row["sources"].add("signature" if record["signature"] else "header")
        if info["name"]:
            row["names"][info["name"]] += 1
        if info["title"]:
            row["titles"][info["title"]] += 1
        if info["company"]:
            row["companies"][info["company"]] += 1
        for e164, label, _raw in info["phones"]:
            row["phones"].setdefault(e164, label or "office")
        for other in info["emails"]:
            if not same_address(other, addr) and not self._skip(other) and other not in row["other_emails"]:
                row["other_emails"].append(other)
        for url in info["websites"]:
            if url not in row["websites"]:
                row["websites"].append(url)
        if record["signature"] and len(record["signature"]) > len(row["signature"]):
            row["signature"] = record["signature"]

    def add_header_contact(self, name, addr, date=None, mailbox=None):
        addr = (addr or "").lower()
        if "@" not in addr or self._skip(addr):
            return
        self.omitted.pop(addr, None)
        row = self._row(addr)
        self._seen(row, date, mailbox)
        row["sources"].add("header")
        if name and "@" not in name:
            row["names"][name.strip().strip('"')] += 1

    def to_rows(self):
        out = []
        for addr, row in sorted(self.rows.items()):
            def by_label(*labels):
                return "; ".join(p for p, l in row["phones"].items() if l in labels)
            out.append({
                "email": addr,
                "name": _top(row["names"]),
                "title": _top(row["titles"]),
                "company": _top(row["companies"]),
                "location": _location(row["phones"]),
                "mobile_phones": by_label("mobile"),
                "office_phones": by_label("office", ""),
                "fax": by_label("fax"),
                "other_emails": "; ".join(row["other_emails"]),
                "websites": "; ".join(row["websites"]),
                "source": "+".join(sorted(row["sources"])),
                "messages": row["messages"],
                "first_seen": row["first_seen"] or "",
                "last_seen": row["last_seen"] or "",
                "sample_signature": row["signature"].replace("\n", " | "),
                "mailboxes": "; ".join(row["mailboxes"]),
                "bounced": self._bounced(addr, row),
            })
        return out

    def _bounced(self, addr, row):
        """Date of the last delivery failure, unless they wrote to us after it."""
        if addr not in self.bounces:
            return ""
        when = self.bounces[addr]
        if row.get("last_from") and when and row["last_from"] > when:
            return ""
        return when or "sí"

    def write_csv(self, path):
        rows = self.to_rows()
        # utf-8-sig so Excel opens accents correctly
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)


def same_address(a, b):
    """Same mailbox even if OCR lost a dot, dash or underscore (malberto@ vs m.alberto@).
    A different letter is a different person (jgarcia@ vs agarcia@), so that one stays."""
    (la, _, da), (lb, _, db) = a.partition("@"), b.partition("@")
    return da == db and re.sub(r"[._-]", "", la) == re.sub(r"[._-]", "", lb)


@lru_cache(maxsize=None)
def phone_location(e164):
    """City/state for a phone from its area code, e.g. +5281... -> "Monterrey, NL"."""
    try:
        number = phonenumbers.parse(e164.split(" ")[0])
    except phonenumbers.NumberParseException:
        return "", ""
    place = geocoder.description_for_number(number, "es")
    country = geocoder.country_name_for_number(number, "es")
    return ("" if place == country else place), country


def _location(phones):
    # Office lines first: they say where the company is; mobiles say where the person bought it.
    ordered = sorted(phones, key=lambda p: phones[p] == "mobile")
    found = [phone_location(p) for p in ordered]
    return next((place for place, _ in found if place), next((c for _, c in found if c), ""))


def _top(counter):
    return counter.most_common(1)[0][0] if counter else ""
