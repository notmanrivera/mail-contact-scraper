"""Pull structured contact fields out of a signature block."""
import re

import phonenumbers

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"\b(?:https?://)?(?:www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?|https?://[^\s<>]+", re.I)

COMPANY_RE = re.compile(
    r"\b(s\.?\s?a\.?(\s?de\s?c\.?\s?v\.?)?|s\.?\s?de\s?r\.?\s?l\.?(\s?de\s?c\.?\s?v\.?)?|s\.?\s?a\.?\s?p\.?\s?i\.?|"
    r"s\.?\s?c\.?|inc\.?|llc|ltd\.?|l\.?t\.?d\.?a\.?|gmbh|corp\.?|corporation|company|co\.|group|grupo|"
    r"industrias?|international|internacional)\b",
    re.I,
)
TITLE_RE = re.compile(
    r"\b(director|directora|gerente|manager|jefe|jefa|coordinador|coordinadora|supervisor|supervisora|"
    r"ejecutiv[oa]|asesor|asesora|ingenier[oa]|ing\.|lic\.|c\.?p\.|arq\.|dr\.|ceo|cfo|coo|cto|founder|fundador|"
    r"presidente|president|vp|vice|head|lead|engineer|analyst|analista|consultor|consultant|"
    r"ventas|sales|compras|purchasing|marketing|operaciones|operations|administraci[oó]n|"
    r"representante|representative|specialist|especialista|assistant|asistente|account|cuenta)\b",
    re.I,
)
PHONE_LABEL_RE = re.compile(
    r"(cel|m[oó]vil|mobile|whats|cell|fax|ofi|office|oficina|tel|phone|direct|directo|ext)", re.I
)
FREE_MAIL = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.mx", "live.com",
    "live.com.mx", "icloud.com", "aol.com", "prodigy.net.mx", "msn.com", "protonmail.com",
    "proton.me", "hotmail.es", "outlook.es",
}
NAME_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñü'.-]+(\s+[A-ZÁÉÍÓÚÑÜa-z][\wÁÉÍÓÚÑÜáéíóúñü'.-]+){1,4}$")


def find_emails(text):
    seen = []
    for match in EMAIL_RE.findall(text or ""):
        addr = match.strip(".").lower()
        if addr not in seen:
            seen.append(addr)
    return seen


def _signature_region(text, default):
    """If the signature has a number written with +country code, use that country for the rest."""
    for match in phonenumbers.PhoneNumberMatcher(text, None, leniency=phonenumbers.Leniency.VALID):
        region = phonenumbers.region_code_for_number(match.number)
        if region and region != "001":
            return region
    return default


def find_phones(text, region="MX"):
    """Return [(e164, label, raw)] using libphonenumber validation."""
    results = []
    region = _signature_region(text or "", region)
    for line in (text or "").splitlines():
        label_match = PHONE_LABEL_RE.search(line)
        label = _normalize_label(label_match.group(1)) if label_match else ""
        for match in phonenumbers.PhoneNumberMatcher(line, region, leniency=phonenumbers.Leniency.VALID):
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            if match.number.extension:
                e164 += f" ext {match.number.extension}"
            if all(e164 != r[0] for r in results):
                results.append((e164, label, match.raw_string))
    return results


def _normalize_label(raw):
    raw = raw.lower()
    if raw.startswith(("cel", "m", "whats", "cell")):
        return "mobile"
    if raw.startswith("fax"):
        return "fax"
    return "office"


def find_urls(text):
    urls = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;)>")
        if "@" not in url and url.lower() not in (u.lower() for u in urls):
            urls.append(url)
    return urls


def _looks_like_name(line):
    return bool(NAME_RE.match(line)) and not any(ch.isdigit() for ch in line) and "@" not in line


def _best_name(header_name, signature_name):
    """Header display names are often 'pgarcia' or an address; prefer a real full name."""
    header_name = (header_name or "").strip()
    if header_name and "@" not in header_name and " " in header_name:
        return header_name.title() if header_name.isupper() else header_name
    return signature_name or header_name


def company_from_domain(addr_or_url):
    host = addr_or_url.rsplit("@", 1)[-1].lower()
    host = re.sub(r"^(https?://)?(www\.)?", "", host).split("/")[0]
    if not host or host in FREE_MAIL:
        return ""
    return host.split(".")[0].replace("-", " ").title()


def parse_signature(signature, from_name="", from_email="", region="MX"):
    lines = [l.strip() for l in (signature or "").splitlines() if l.strip()]
    name, title, company = "", "", ""

    for i, line in enumerate(lines[:4]):
        if _looks_like_name(line) and not COMPANY_RE.search(line) and not TITLE_RE.search(line):
            name = line
            lines_after = lines[i + 1 : i + 4]
            break
    else:
        lines_after = lines[:4]

    for line in lines_after:
        if not title and TITLE_RE.search(line) and not EMAIL_RE.search(line) and len(line) < 80:
            title = line
    for line in lines:
        if COMPANY_RE.search(line) and not EMAIL_RE.search(line) and len(line) < 80:
            if not TITLE_RE.search(line) or re.search(r"s\.?\s?a\.?|inc|llc|ltd|gmbh|grupo|industria", line, re.I):
                company = line
                break

    emails = find_emails(signature)
    urls = find_urls(signature)
    if not company:
        for source in urls + emails + [from_email]:
            company = company_from_domain(source) if source else ""
            if company:
                break

    return {
        "name": _best_name(from_name, name),
        "signature_name": name,
        "title": title,
        "company": company,
        "emails": emails,
        "phones": find_phones(signature, region),
        "websites": urls,
    }
