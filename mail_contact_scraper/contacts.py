"""Merge per-message findings into one row per contact and write CSV."""
import csv
import re
from collections import Counter
from functools import lru_cache

import phonenumbers
from phonenumbers import geocoder

AUTOMATED_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?|bounce[s]?|alerts?|"
    r"newsletter|news|info-noreply|automated|system)[@+.-]",
    re.I,
)

FIELDS = [
    "email", "name", "title", "company", "location", "mobile_phones", "office_phones", "fax",
    "other_emails", "websites", "source", "messages", "first_seen", "last_seen", "sample_signature",
    "mailboxes",
]


class ContactBook:
    def __init__(self, exclude=(), skip_automated=True, exclude_domains=()):
        self.exclude = {e.lower() for e in exclude if e}
        self.skip_automated = skip_automated
        self.exclude_domains = tuple(d.lower() for d in exclude_domains if d)
        self.rows = {}

    def _excluded_domain(self, addr):
        domain = addr.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in self.exclude_domains)

    def _skip(self, addr):
        return (
            not addr
            or addr in self.exclude
            or self._excluded_domain(addr)
            or (self.skip_automated and AUTOMATED_RE.match(addr))
        )

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

    def add_sender(self, record):
        addr = record["from_email"]
        if self._skip(addr):
            return
        info = record["contact"]
        row = self._row(addr)
        self._seen(row, record.get("date"), record.get("mailbox"))
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
            })
        return out

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
