"""Merge per-message findings into one row per contact and write CSV."""
import csv
import re
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path

import phonenumbers
from phonenumbers import geocoder

from .enrich import domain_of

AUTOMATED_RE = re.compile(
    r"^(no[-_]?reply|do[-_]?not[-_]?reply|mailer-daemon|postmaster|notifications?|notificaciones|"
    r"bounce[s]?|alerts?|alertas|avisos?|newsletter|news|boletin|info-noreply|automated|system|"
    r"auto-?confirm|marketing|promo(ciones)?|ofertas|mailing|accounts?-?update)[@+._-]",
    re.I,
)
# Anywhere in the local part: catalog-noreply@, avisos.donotreply@.
NOREPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply", re.I)
# reply@, reply-n45hjmcz5keetl4@: the reply-to address a mailing platform hands out.
REPLY_RE = re.compile(r"^(reply|replies|respuesta)([-+._]|$)", re.I)
# Subdomains that only mailing platforms send from: reply@mail.pmmimediagroup.com,
# c.1d48d1@reply.e.sonymusicfans.com.
SENDING_SUBDOMAINS = {
    "reply", "replies", "bounce", "bounces", "e", "em", "email", "mail", "mailer", "mailing", "news",
    "newsletter", "mkt", "marketing", "campaign", "campaigns", "notify", "notifications", "send",
}
NOT_A_NAME = {"com", "net", "org", "gob", "gov", "edu", "co"}
HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)


def sending_subdomain(domain):
    """'reply.' when domain is reply.<something>.<tld>; '' for mail.ru or mail.com.mx."""
    labels = domain.lower().split(".")
    if len(labels) >= 3 and labels[0] in SENDING_SUBDOMAINS and labels[1] not in NOT_A_NAME:
        return labels[0] + "."
    return ""


def _random_code(local):
    # c.1d48d1c7758e42e482e69c18eb87c3ce: a tracking code, not a person.
    for part in re.split(r"[-+._]", local):
        digits = sum(ch.isdigit() for ch in part)
        if HEX_RE.match(part) and digits >= 3:
            return True
        if len(part) >= 20 and digits >= 4 and digits < len(part):
            return True
    return False


def automated_rule(addr):
    """The rule shown in Omitidos when addr belongs to a robot, or ''."""
    local, _, domain = addr.lower().rpartition("@")
    match = AUTOMATED_RE.match(addr)
    if match:
        return match.group(1).lower() + "@"
    if REPLY_RE.match(local):
        return REPLY_RE.match(local).group(1) + "@"
    if NOREPLY_RE.search(local):
        return "*" + NOREPLY_RE.search(local).group(0) + "*@"
    sub = sending_subdomain(domain)
    if sub:
        return "@" + sub
    if _random_code(local):
        return "código"
    return ""

FIELDS = [
    "email", "name", "title", "company", "sector", "location", "mobile_phones", "office_phones", "fax",
    "other_emails", "websites", "source", "messages", "first_seen", "last_seen", "sample_signature",
    "mailboxes", "bounced", "company_web",
]


def _read_list(text):
    return [l.split("#", 1)[0].strip().lower() for l in text.splitlines() if l.split("#", 1)[0].strip()]


@lru_cache(maxsize=1)
def service_patterns():
    """Built-in domains that never are prospects (services.txt), as (entry, regex) pairs."""
    text = resources.files(__package__).joinpath("services.txt").read_text(encoding="utf-8")
    return tuple((entry, _domain_pattern(entry)) for entry in _read_list(text))


@lru_cache(maxsize=1)
def service_catalog():
    """Built-in services as (category, entry), using the "# Category" comments of services.txt."""
    text = resources.files(__package__).joinpath("services.txt").read_text(encoding="utf-8")
    out, category, started = [], "", False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            started = True  # the header ends at the first blank line
        elif line.startswith("#"):
            if started:
                category = line.lstrip("# ").strip()
        else:
            out.append((category, line.split("#", 1)[0].strip().lower()))
    return out


SERVICES_HEADER = ("# Servicios que agregaste a la lista integrada, uno por línea (nombre.com o nombre.*).\n"
                   "# Una línea \"-nombre.*\" apaga un servicio de la lista integrada (sí es cliente).\n")


def read_services_file(path):
    """(added, disabled) from the user's servicios.txt."""
    entries = read_ignore_file(path)
    return [e for e in entries if not e.startswith("-")], [e[1:] for e in entries if e.startswith("-")]


def update_services_file(path, add=None, remove=None, disable=None, enable=None):
    added, disabled = read_services_file(path)
    builtin = {e for _, e in service_catalog()}
    for value in split_entries(add) if add else ():
        if "@" in value:
            raise ValueError("Un servicio es un dominio (uber.com o uber.*). Los correos sueltos van en Tu lista.")
        if value in disabled:
            disabled.remove(value)
        elif value not in builtin and value not in added:
            added.append(value)
    if remove:
        added = [e for e in added if e != remove.strip().lower()]
    if disable and disable.strip().lower() in builtin and disable.strip().lower() not in disabled:
        disabled.append(disable.strip().lower())
    if enable:
        disabled = [e for e in disabled if e != enable.strip().lower()]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(SERVICES_HEADER + "".join(f"{e}\n" for e in added) + "".join(f"-{e}\n" for e in disabled),
                   encoding="utf-8")
    tmp.replace(path)
    return added, disabled


def _domain_pattern(entry):
    entry = entry.lstrip("@")
    if entry.endswith(".*"):  # amazon.* -> amazon.com, amazon.com.mx, mx.amazon.co ...
        base = re.escape(entry[:-2])
        return re.compile(rf"(^|\.){base}\.[a-z]{{2,6}}(\.[a-z]{{2,3}})?$")
    return re.compile(rf"(^|\.){re.escape(entry)}$")


IGNORE_HEADER = ("# Correos o dominios que no son prospectos, uno por línea (uber.com, juan@empresa.mx).\n"
                 "# El dashboard agrega aquí lo que ignoras con el botón \"Ignorar\".\n")
ALLOW_HEADER = ("# Correos o dominios que siempre son contactos, aunque parezcan servicio, aviso automático\n"
                "# o correo masivo (por ejemplo un cliente que también es banco). Uno por línea.\n")


def normalize_ignore(value):
    value = (value or "").strip().lower()
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[^@\s]+@[a-z0-9-]+(\.[a-z0-9-]+)+|[a-z0-9-]+((\.[a-z0-9-]+)+|\.\*)", value):
        raise ValueError(f"\"{value}\" no es un correo (juan@empresa.mx) ni un dominio (uber.com o uber.*).")
    return value


def split_entries(text):
    """'uber.com, juan@x.mx\nrappi.com' -> each one normalized. Raises on the first bad one."""
    return [normalize_ignore(v) for v in re.split(r"[\s,;]+", text or "") if v.strip()]


def update_ignore_file(path, add=None, remove=None, header=IGNORE_HEADER):
    entries = read_ignore_file(path)
    for value in split_entries(add) if add else ():
        if value not in entries:
            entries.append(value)
    if remove:
        entries = [e for e in entries if e != (remove or "").strip().lower()]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(header + "".join(f"{e}\n" for e in entries), encoding="utf-8")
    tmp.replace(path)
    return entries


def read_ignore_file(path):
    """User's own ignore list: one email or domain per line (ignorados.txt)."""
    try:
        return _read_list(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def _rules(entries):
    entries = [i.strip().lower().lstrip("@") for i in entries if i and i.strip()]
    emails = {i for i in entries if "@" in i}
    domains = tuple((i, _domain_pattern(i)) for i in entries if "@" not in i)
    return emails, domains


def _find(addr, emails, domains):
    """The list entry that matches addr (the address itself or its domain rule), or ""."""
    if addr in emails:
        return addr
    domain = addr.rsplit("@", 1)[-1]
    return next((entry for entry, pattern in domains if pattern.search(domain)), "")


class ContactBook:
    def __init__(self, exclude=(), skip_automated=True, exclude_domains=(), skip_services=True, ignore=(),
                 allow=(), services=((), ()), companies=None):
        self.exclude = {e.lower() for e in exclude if e}
        self.skip_automated = skip_automated
        self.exclude_domains = tuple(d.lower() for d in exclude_domains if d)
        added, disabled = services  # the user's own servicios.txt
        self.services = tuple(p for p in service_patterns() if p[0] not in disabled) if skip_services else ()
        self.services += tuple((e, _domain_pattern(e)) for e in added)
        self.ignore_emails, self.ignore_domains = _rules(ignore)
        self.allow_emails, self.allow_domains = _rules(allow)
        self.rows = {}
        self.bounces = {}
        self.omitted = {}  # address -> reason, for the dashboard's count
        self.companies = companies or {}  # domain -> (sector, site description), from enrich.py
        self.omitted_info = {}  # address -> rule, times seen, last date, name: the "Ignorados" list

    def _excluded_domain(self, addr):
        domain = addr.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in self.exclude_domains)

    def allowed(self, addr):
        return bool(_find(addr, self.allow_emails, self.allow_domains))

    def skip_reason(self, addr):
        return self._match(addr)[0]

    def _match(self, addr):
        """(reason, rule): why addr is left out and which entry or pattern decided it."""
        if not addr or addr in self.exclude or self._excluded_domain(addr):
            return "own", ""
        if self.allowed(addr):
            return "", ""
        rule = _find(addr, self.ignore_emails, self.ignore_domains)
        if rule:
            return "ignored", rule
        rule = _find(addr, (), self.services)
        if rule:
            return "service", rule
        if self.skip_automated:
            rule = automated_rule(addr)
            if rule:
                return "automated", rule
        return "", ""

    def _skip(self, addr, date=None, name=""):
        reason, rule = self._match(addr)
        if reason and reason != "own" and addr not in self.rows:
            self._omit(addr, reason, rule, date, name)
        return bool(reason)

    def _omit(self, addr, reason, rule, date=None, name=""):
        self.omitted.setdefault(addr, reason)
        info = self.omitted_info.setdefault(addr, {"rule": rule, "seen": 0, "last_seen": "", "name": ""})
        info["seen"] += 1
        if date and date > info["last_seen"]:
            info["last_seen"] = date
        if name and "@" not in name and not info["name"]:
            info["name"] = name.strip().strip('"')

    def omitted_counts(self):
        return dict(Counter(self.omitted.values()))

    def omitted_list(self):
        """Everyone left out, most seen first, for the dashboard's "Ignorados" window."""
        out = [{"email": a, "reason": r, **self.omitted_info.get(a, {})} for a, r in self.omitted.items()]
        return sorted(out, key=lambda o: (-o.get("seen", 0), o["email"]))

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
        date, name = record.get("date"), record.get("from_name", "")
        if record.get("bulk") and not self.allowed(addr):
            # Newsletters and promos: not a person writing to us, even from a real company.
            if not self._skip(addr, date, name) and addr not in self.rows:
                rule = record["bulk"] if isinstance(record["bulk"], str) else ""
                self._omit(addr, "bulk", rule, date, name)
            return
        if self._skip(addr, date, name):
            return
        info = record["contact"]
        self.omitted.pop(addr, None)
        self.omitted_info.pop(addr, None)
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
        if "@" not in addr or self._skip(addr, date, name):
            return
        self.omitted.pop(addr, None)
        self.omitted_info.pop(addr, None)
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
            websites = "; ".join(row["websites"])
            sector, about = self.companies.get(domain_of({"email": addr, "websites": websites}), ("", ""))
            out.append({
                "email": addr,
                "name": _top(row["names"]),
                "title": _top(row["titles"]),
                "company": _top(row["companies"]),
                "sector": sector,
                "location": _location(row["phones"]),
                "mobile_phones": by_label("mobile"),
                "office_phones": by_label("office", ""),
                "fax": by_label("fax"),
                "other_emails": "; ".join(row["other_emails"]),
                "websites": websites,
                "source": "+".join(sorted(row["sources"])),
                "messages": row["messages"],
                "first_seen": row["first_seen"] or "",
                "last_seen": row["last_seen"] or "",
                "sample_signature": row["signature"].replace("\n", " | "),
                "mailboxes": "; ".join(row["mailboxes"]),
                "bounced": self._bounced(addr, row),
                "company_web": about,
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
