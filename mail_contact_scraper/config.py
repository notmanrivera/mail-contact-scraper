"""Settings loaded from environment variables (and a local .env file)."""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional
    load_dotenv = None


def _bool(value, default=False):
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


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
    exclude_domains: list = field(default_factory=list)
    ocr_images: bool = True
    ocr_remote_images: bool = False
    ocr_lang: str = "spa+eng"
    output_dir: str = "output"


def load_settings(env_file=".env"):
    if load_dotenv and os.path.exists(env_file):
        load_dotenv(env_file)
    env = os.environ.get
    folders = [f.strip() for f in env("IMAP_FOLDERS", "INBOX").split(",") if f.strip()]
    user = env("IMAP_USER", "")
    # Coworkers are not prospects: by default drop everyone on the mailbox's own domain.
    own_domain = user.rsplit("@", 1)[-1].lower() if "@" in user else ""
    raw_domains = (env("EXCLUDE_DOMAINS") or "").strip()
    if not raw_domains:
        exclude_domains = [own_domain] if own_domain else []
    elif raw_domains.lower() == "none":
        exclude_domains = []
    else:
        exclude_domains = [d.strip().lower().lstrip("@") for d in raw_domains.split(",") if d.strip()]
    return Settings(
        email=user,
        password=env("IMAP_PASSWORD", ""),
        oauth_token=env("IMAP_OAUTH_TOKEN", ""),
        imap_host=env("IMAP_HOST", ""),
        imap_port=int(env("IMAP_PORT", "993")),
        folders=folders or ["INBOX"],
        batch_size=int(env("BATCH_SIZE", "50")),
        batch_delay=float(env("BATCH_DELAY", "1.0")),
        default_region=env("DEFAULT_REGION", "MX").upper(),
        include_header_contacts=_bool(env("INCLUDE_HEADER_CONTACTS"), True),
        exclude_self=_bool(env("EXCLUDE_SELF"), True),
        skip_automated=_bool(env("SKIP_AUTOMATED"), True),
        exclude_domains=exclude_domains,
        ocr_images=_bool(env("OCR_IMAGES"), True),
        ocr_remote_images=_bool(env("OCR_REMOTE_IMAGES"), False),
        ocr_lang=env("OCR_LANG", "spa+eng"),
        output_dir=env("OUTPUT_DIR", "output"),
    )
