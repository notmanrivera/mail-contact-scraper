"""Settings loaded from environment variables (and a local .env file)."""
from dataclasses import dataclass, field

from . import envfile


def _bool(value, default=False):
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


@dataclass
class Mailbox:
    email: str = ""
    password: str = ""
    oauth_token: str = ""
    imap_host: str = ""
    imap_port: int = 993
    folders: list = field(default_factory=lambda: ["INBOX"])


MAX_MAILBOXES = 50


@dataclass
class Settings:
    email: str = ""
    password: str = ""
    oauth_token: str = ""
    imap_host: str = ""
    imap_port: int = 993
    folders: list = field(default_factory=lambda: ["INBOX"])
    batch_size: int = 50
    batch_delay: float = 1.0
    default_region: str = "MX"
    include_header_contacts: bool = True
    exclude_self: bool = True
    skip_automated: bool = True
    skip_services: bool = True
    ignore_file: str = "ignorados.txt"
    allow_file: str = "permitidos.txt"
    services_file: str = "servicios.txt"
    exclude_domains: list = field(default_factory=list)
    ocr_images: bool = True
    ocr_remote_images: bool = False
    ocr_lang: str = "spa+eng"
    ocr_engine: str = "tesseract"       # tesseract | auto (Claude when unsure) | claude
    anthropic_api_key: str = ""
    ocr_claude_model: str = ""
    output_dir: str = "output"
    mailboxes: list = field(default_factory=list)


def load_settings(env_file=".env", base=None):
    # Read the file every time instead of loading it into os.environ, so a dashboard that
    # edits .env starts its next scan with the new values.
    env = envfile.environ(env_file, base).get
    folders = _folders(env("IMAP_FOLDERS")) or ["INBOX"]
    user = env("IMAP_USER", "")
    port = int(env("IMAP_PORT", "993"))
    mailboxes = []
    if user:
        mailboxes.append(Mailbox(user, env("IMAP_PASSWORD", ""), env("IMAP_OAUTH_TOKEN", ""),
                                 env("IMAP_HOST", ""), port, folders))
    # Extra mailboxes: IMAP_USER_2 / IMAP_PASSWORD_2 / ..., each falling back to the main
    # host, port and folders when its own value is empty.
    for n in range(2, MAX_MAILBOXES + 1):
        extra = env(f"IMAP_USER_{n}", "").strip()
        if not extra or extra.lower() in {b.email.lower() for b in mailboxes}:
            continue
        mailboxes.append(Mailbox(
            extra, env(f"IMAP_PASSWORD_{n}", ""), env(f"IMAP_OAUTH_TOKEN_{n}", ""),
            env(f"IMAP_HOST_{n}", "") or env("IMAP_HOST", ""),
            int(env(f"IMAP_PORT_{n}", "") or port),
            _folders(env(f"IMAP_FOLDERS_{n}")) or folders,
        ))
    # Coworkers are not prospects: by default drop everyone on the mailboxes' own domains.
    own_domains = []
    for box in mailboxes:
        domain = box.email.rsplit("@", 1)[-1].lower() if "@" in box.email else ""
        if domain and domain not in own_domains:
            own_domains.append(domain)
    raw_domains = (env("EXCLUDE_DOMAINS") or "").strip()
    if not raw_domains:
        exclude_domains = own_domains
    elif raw_domains.lower() == "none":
        exclude_domains = []
    else:
        exclude_domains = [d.strip().lower().lstrip("@") for d in raw_domains.split(",") if d.strip()]
    return Settings(
        email=user,
        password=env("IMAP_PASSWORD", ""),
        oauth_token=env("IMAP_OAUTH_TOKEN", ""),
        imap_host=env("IMAP_HOST", ""),
        imap_port=port,
        folders=folders,
        batch_size=int(env("BATCH_SIZE", "50")),
        batch_delay=float(env("BATCH_DELAY", "1.0")),
        default_region=env("DEFAULT_REGION", "MX").upper(),
        include_header_contacts=_bool(env("INCLUDE_HEADER_CONTACTS"), True),
        exclude_self=_bool(env("EXCLUDE_SELF"), True),
        skip_automated=_bool(env("SKIP_AUTOMATED"), True),
        skip_services=_bool(env("SKIP_SERVICES"), True),
        ignore_file=env("IGNORE_FILE", "") or "ignorados.txt",
        allow_file=env("ALLOW_FILE", "") or "permitidos.txt",
        services_file=env("SERVICES_FILE", "") or "servicios.txt",
        exclude_domains=exclude_domains,
        ocr_images=_bool(env("OCR_IMAGES"), True),
        ocr_remote_images=_bool(env("OCR_REMOTE_IMAGES"), False),
        ocr_lang=env("OCR_LANG", "spa+eng"),
        ocr_engine=(env("OCR_ENGINE", "") or "tesseract").strip().lower(),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        ocr_claude_model=env("OCR_CLAUDE_MODEL", ""),
        output_dir=env("OUTPUT_DIR", "output"),
        mailboxes=mailboxes,
    )


def _folders(value):
    return [f.strip() for f in (value or "").split(",") if f.strip()]
