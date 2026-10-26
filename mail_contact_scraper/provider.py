"""Guess the IMAP server of a mailbox from its domain's MX records."""
import logging

log = logging.getLogger(__name__)

# MX hostname fragment -> IMAP host
KNOWN_PROVIDERS = [
    ("google.com", "imap.gmail.com"),
    ("googlemail.com", "imap.gmail.com"),
    ("outlook.com", "outlook.office365.com"),
    ("protection.outlook.com", "outlook.office365.com"),
    ("zoho.com", "imap.zoho.com"),
    ("zoho.eu", "imap.zoho.eu"),
    ("secureserver.net", "imap.secureserver.net"),  # GoDaddy
    ("titan.email", "imap.titan.email"),
    ("hostinger.com", "imap.hostinger.com"),
    ("yahoodns.net", "imap.mail.yahoo.com"),
    ("icloud.com", "imap.mail.me.com"),
    ("ionos", "imap.ionos.com"),
    ("emailsrvr.com", "secure.emailsrvr.com"),  # Rackspace
    ("mailgun.org", None),  # sending only, keep looking
]


def mx_hosts(domain):
    try:
        import dns.resolver
    except ImportError:
        log.warning("dnspython not installed; cannot look up MX records")
        return []
    try:
        answers = dns.resolver.resolve(domain, "MX")
    except Exception as exc:  # NXDOMAIN, timeout, no answer...
        log.warning("MX lookup for %s failed: %s", domain, exc)
        return []
    records = sorted(answers, key=lambda r: r.preference)
    return [str(r.exchange).rstrip(".").lower() for r in records]


def guess_imap_host(email_address):
    """Return (imap_host, reason). Falls back to imap.<domain> / mail.<domain>."""
    domain = email_address.rsplit("@", 1)[-1].lower()
    for mx in mx_hosts(domain):
        for fragment, host in KNOWN_PROVIDERS:
            if fragment in mx and host:
                return host, f"MX {mx} -> {host}"
        # Self-hosted / cPanel style: the MX host itself usually speaks IMAP.
        if mx.endswith(domain):
            return mx, f"MX {mx} is on the same domain"
    return f"mail.{domain}", "no known MX; guessing mail.<domain> (set IMAP_HOST to override)"
