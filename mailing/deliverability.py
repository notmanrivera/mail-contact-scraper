"""Checks that keep a campaign from hurting the sending mailboxes.

- domain_health(): SPF, DKIM and DMARC of a sending domain, as a traffic light.
- clean_list(): addresses whose domain can't receive mail (no MX, no A) or looks
  like a typo (gmal.com, empresa.con), found before they turn into hard bounces.

MX / A answers are cached for an hour (TXT is always asked again); `resolve(name, rtype)` can be swapped in tests.
It returns a list of strings, [] when the name has no such record, and raises
LookupError when the name does not exist and TimeoutError when DNS can't answer.
"""
import time
from concurrent.futures import ThreadPoolExecutor

CACHE_SECONDS = 3600
_cache = {}

# Free mail providers people mistype, and TLD slips.
KNOWN_DOMAINS = (
    "gmail.com", "hotmail.com", "hotmail.es", "outlook.com", "outlook.es", "live.com", "live.com.mx",
    "yahoo.com", "yahoo.com.mx", "icloud.com", "prodigy.net.mx", "msn.com",
)
BAD_TLDS = {"con": "com", "cmo": "com", "cpm": "com", "vom": "com", "xom": "com", "comm": "com",
            "ocm": "com", "co": None, "mz": "mx", "mxx": "mx", "nx": "mx"}
# DKIM selectors of Google Workspace, Microsoft 365 and common hosts.
DKIM_SELECTORS = ("google", "selector1", "selector2", "default", "k1", "mail", "dkim", "s1")
PROVIDER_SPF = {"google": "_spf.google.com", "microsoft": "spf.protection.outlook.com"}


def _dns(name, rtype):
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rtype, lifetime=5)
    except dns.resolver.NXDOMAIN:
        raise LookupError(name) from None
    except dns.resolver.NoAnswer:
        return []
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        raise TimeoutError(name) from None
    return [b"".join(r.strings).decode(errors="replace") if rtype == "TXT" else r.to_text() for r in answer]


resolve = _dns


def _lookup(name, rtype, cached=True):
    key = (name.lower(), rtype)
    hit = _cache.get(key) if cached else None
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        if isinstance(hit[1], Exception):
            raise hit[1]
        return hit[1]
    try:
        value = resolve(name, rtype)
    except LookupError as exc:
        _cache[key] = (time.time(), exc)
        raise
    _cache[key] = (time.time(), value)
    return value


def dns_works():
    """A known domain answers; without this a network that answers 'no such domain' for
    everything would make every address look dead."""
    try:
        return bool(_lookup("gmail.com", "MX"))
    except (LookupError, TimeoutError):
        return False


# --- sending domain health ------------------------------------------------------------

def _txt(name):
    try:
        return _lookup(name, "TXT", cached=False)  # a fix in the DNS shows up on the next look
    except LookupError:
        return []


def domain_health(domain, provider=None):
    """SPF / DKIM / DMARC of a sending domain. Each item and the whole get a light:
    'ok' (green), 'warn' (yellow) or 'bad' (red), with a line saying why in Spanish.
    `provider` ('google', 'microsoft' or '') is read from the MX when not given."""
    domain = domain.lower()
    checks = {}
    try:
        if not dns_works():
            raise TimeoutError(domain)
        if provider is None:
            provider = mail_provider(domain)
        spf = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
        wanted = PROVIDER_SPF.get(provider, "")
        if not spf:
            checks["spf"] = ("bad", "No hay registro SPF: Gmail y Outlook pueden mandar todo a spam.")
        elif len(spf) > 1:
            checks["spf"] = ("bad", "Hay dos registros SPF; deben ser uno solo.")
        elif wanted and wanted not in spf[0]:
            checks["spf"] = ("warn", f"El SPF no incluye a {wanted} (el servidor que envía).")
        else:
            checks["spf"] = ("ok", "SPF correcto.")

        dkim = next((s for s in DKIM_SELECTORS
                     if any("p=" in t for t in _txt(f"{s}._domainkey.{domain}"))), "")
        checks["dkim"] = (("ok", f"DKIM activo (selector {dkim}).") if dkim else
                          ("bad", "No encontré la firma DKIM. En Google Workspace: Admin ▸ Gmail ▸ "
                                  "Autenticar correo electrónico ▸ Generar registro y Comenzar autenticación."))

        dmarc = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
        policy = ""
        if dmarc:
            policy = next((p.split("=", 1)[1].strip().lower() for p in dmarc[0].split(";")
                           if p.strip().lower().startswith("p=")), "")
        if not dmarc:
            checks["dmarc"] = ("bad", "No hay registro DMARC (_dmarc). Desde 2024 Gmail y Yahoo lo piden "
                                      "para envíos masivos.")
        elif policy == "none":
            checks["dmarc"] = ("warn", "DMARC en modo p=none: sirve, pero conviene subirlo a quarantine "
                                       "cuando todo esté en verde.")
        else:
            checks["dmarc"] = ("ok", f"DMARC activo (p={policy or '?'}).")
    except TimeoutError:
        return {"domain": domain, "light": "unknown", "checks": {},
                "summary": "No pude consultar el DNS ahora; vuelve a intentar."}
    lights = [c[0] for c in checks.values()]
    light = "bad" if "bad" in lights else "warn" if "warn" in lights else "ok"
    return {"domain": domain, "light": light,
            "checks": {k: {"light": v[0], "text": v[1]} for k, v in checks.items()},
            "summary": {"ok": "Listo para enviar.", "warn": "Se puede enviar, con detalles por mejorar.",
                        "bad": "Arregla esto antes de enviar en volumen."}[light]}


def mail_provider(domain):
    """'google' or 'microsoft' when the domain's mail is hosted there (by its MX), else ''."""
    try:
        mx = " ".join(_lookup(domain, "MX")).lower()
    except LookupError:
        return ""
    if "google" in mx:
        return "google"
    if "outlook" in mx:
        return "microsoft"
    return ""


# --- list cleaning ----------------------------------------------------------------------

def _distance_one(a, b):
    """True when a and b differ by one edit (or two swapped letters)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def typo_suggestion(domain):
    """'gmal.com' -> 'gmail.com', 'empresa.con' -> 'empresa.com'; '' when it looks fine."""
    domain = domain.lower()
    if domain in KNOWN_DOMAINS:
        return ""
    for known in KNOWN_DOMAINS:
        if _distance_one(domain, known):
            return known
    head, _, tld = domain.rpartition(".")
    if tld in BAD_TLDS and BAD_TLDS[tld] and head:
        return f"{head}.{BAD_TLDS[tld]}"
    return ""


def domain_receives_mail(domain):
    """True / False, or None when DNS couldn't tell (then the address is kept)."""
    try:
        if _lookup(domain, "MX"):
            return True
        return bool(_lookup(domain, "A"))  # no MX: mail goes to the A record (RFC 5321)
    except LookupError:
        return False
    except TimeoutError:
        return None


def clean_list(addresses, workers=16):
    """Addresses that would bounce for sure, each with why and a suggested fix.
    Returns {"bad": [{email, motivo, sugerencia}], "checked_domains": n, "unknown_domains": n}.
    Raises ValueError when DNS can't be used right now."""
    if not dns_works():
        raise ValueError("No pude consultar los dominios (¿hay internet?). No saqué a nadie de la lista.")
    by_domain = {}
    for a in addresses:
        by_domain.setdefault(a.rsplit("@", 1)[-1].lower(), []).append(a)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        alive = dict(zip(by_domain, pool.map(domain_receives_mail, by_domain)))
    bad = []
    for domain, emails in sorted(by_domain.items()):
        suggestion = typo_suggestion(domain)
        if suggestion:
            reason = "error de dedo en el dominio"
        elif alive[domain] is False:
            reason = "el dominio no recibe correo (no existe o no tiene servidor de correo)"
        else:
            continue
        for e in emails:
            bad.append({"email": e, "motivo": reason,
                        "sugerencia": e.rsplit("@", 1)[0] + "@" + suggestion if suggestion else ""})
    return {"bad": bad, "checked_domains": len(by_domain),
            "unknown_domains": sum(1 for v in alive.values() if v is None)}
