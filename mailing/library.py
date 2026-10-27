"""The app's campaign library: each person's own mailings, kept in their data folder.

Nothing here needs GitHub or a new version of the app: a campaign is created, edited,
imported or shared from the Envíos panel, and each Mac keeps its own.

    <out_dir>/campanas/<slug>/campana.json   name, subject, sender (name, email, reply_to)
    <out_dir>/campanas/<slug>/mailing.html   the HTML that goes out, with merge tags
    <out_dir>/campanas/<slug>/imagenes/      images the HTML names (sent attached inline)
    <out_dir>/campanas/biblioteca.json       the campaign the Campaña tab sends, and the
                                             built-in .toml campaigns already copied in

Merge tags, filled per person: {{Saludo}} ("Hola Laura,"), {{Nombre}}, {{Empresa}} and
{{Baja}} (the unsubscribe link). The tags of YAMM, Brevo and Mailchimp work too.

Images can be links (https://...), files of the campaign named in the HTML (src="hero.png")
or pasted data: images. Files and data: images are attached inline (cid:), which Gmail and
Outlook show without hosting anything. The send log stays in <out_dir>/mailing/<slug>/.
"""
import base64
import binascii
import html as html_lib
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import unicodedata
import zipfile
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import unquote

from markupsafe import Markup

from .build import greeting, load_campaign, render_html, unsubscribe_mailto

BUILT_IN = Path(__file__).parent / "campaigns"
INFO, BODY, IMAGES, INDEX, TRASH = "campana.json", "mailing.html", "imagenes", "biblioteca.json", ".papelera"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE = 5 * 1024 * 1024
MAX_IMPORT = 30 * 1024 * 1024  # unpacked size of an imported .zip
# Extra settings of a campaign that travel as they are (seed, export, import): the follow-up text.
KEEP = ("followup",)
GMAIL_CLIPS = 100_000  # Gmail hides the rest of a message whose HTML passes ~102 KB
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
DEFAULT_SENDER = {"name": "Pedro Marquina", "email": "pedro@dindustrimex.com", "reply_to": "ventas@dindustrimex.com"}

GREETING_TAGS = re.compile(r"\{\{\s*(?:Saludo|contact\.SALUDO)\s*\}\}|\*\|SALUDO\|\*")
UNSUB_TAGS = re.compile(r"\{\{\s*(?:Baja|unsubscribe)\s*\}\}|\*\|UNSUB\|\*")
NAME_TAGS = re.compile(r"\{\{\s*Nombre\s*\}\}|\*\|FNAME\|\*")
COMPANY_TAGS = re.compile(r"\{\{\s*Empresa\s*\}\}")
# Where an HTML email points at an image: src="", background="" and CSS url().
IMAGE_REFS = re.compile(r"""(?P<pre>\b(?:src|background)\s*=\s*)(?P<q>["'])(?P<ref>.*?)(?P=q)"""
                        r"""|(?P<cpre>url\(\s*)(?P<cq>["']?)(?P<cref>[^"')]*?)(?P=cq)(?P<cpost>\s*\))""", re.I)
FOOTER = ('<p style="margin:0;padding:18px 12px;font:12px/1.5 Arial,sans-serif;color:#8a8a8a;text-align:center;">'
          '¿Prefieres no recibir más correos? <a href="{{Baja}}" style="color:#8a8a8a;">Darte de baja</a></p>')


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:50] or "campana"


def safe_name(name):
    """File name an image is kept (and referenced) as: no folders, no odd characters."""
    name = unquote(str(name or "")).split("?")[0].split("#")[0].replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode())
    return f"{stem.strip('-')[:60] or 'imagen'}.{ext.lower()}" if ext else ""


def root(out_dir):
    return Path(out_dir) / "campanas"


def _read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path, data):
    # the app serves requests in threads: each write gets its own temp file
    tmp = Path(path).with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _index(out_dir):
    return _read_json(root(out_dir) / INDEX, {"active": "", "seeded": []})


def _folders(out_dir):
    base = root(out_dir)
    return sorted(p for p in base.iterdir() if p.is_dir() and SLUG_RE.match(p.name) and (p / INFO).exists()) \
        if base.exists() else []


def ensure(out_dir):
    """Create the library and copy in the built-in campaigns it hasn't copied yet (once:
    after that the person's copy is theirs to edit or delete)."""
    with _LOCK:
        return _ensure(out_dir)


_LOCK = threading.RLock()


def _ensure(out_dir):
    base = root(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    index = _index(out_dir)
    before = json.dumps(index, sort_keys=True)
    for toml in sorted(BUILT_IN.glob("*.toml")):
        if toml.stem.startswith("_") or toml.stem in index["seeded"]:
            continue
        if not (base / toml.stem).exists():
            _seed(base / toml.stem, load_campaign(toml))
        index["seeded"].append(toml.stem)
        index["active"] = index["active"] or toml.stem
    if json.dumps(index, sort_keys=True) != before or not (base / INDEX).exists():
        _write_json(base / INDEX, index)
    return index


def _seed(folder, c):
    """A built-in campaign as plain HTML with merge tags; images it doesn't link go in imagenes/."""
    (folder / IMAGES).mkdir(parents=True)
    html = render_html(c, Markup("{{Saludo}}"), Markup("{{Baja}}"))
    assets = c["_dir"] / c["assets"]["dir"]
    for ref in set(re.findall(r'cid:([^"\')\s]+)', html)):
        if (assets / ref).is_file():
            shutil.copy2(assets / ref, folder / IMAGES / safe_name(ref))
    html = re.sub(r'cid:([^"\')\s]+)', lambda m: safe_name(m.group(1)), html)
    (folder / BODY).write_text(html, encoding="utf-8")
    sender = {k: c["sender"].get(k, "") for k in ("name", "email", "reply_to")}
    name = c.get("name") or c["subject"]
    info = {"name": name, "subject": c["subject"], "sender": sender,
            "created": _now(), "updated": _now(), "origin": f"{folder.name}.toml"}
    for key in KEEP:
        if key in c:
            info[key] = c[key]
    _write_json(folder / INFO, info)


def folder(out_dir, slug):
    """Folder of a saved campaign; ValueError for anything that isn't one."""
    slug = str(slug or "")
    path = root(out_dir) / slug
    if not SLUG_RE.match(slug) or not (path / INFO).exists():
        raise ValueError("Esa campaña ya no existe.")
    return path


def pick(out_dir, slug=""):
    """The campaign asked for, else the active one, else the first. Ensures the library."""
    index = ensure(out_dir)
    for wanted in (slug, index.get("active")):
        try:
            return folder(out_dir, wanted)
        except ValueError:
            pass
    found = _folders(out_dir)
    if not found:
        raise ValueError("No hay campañas guardadas. Crea una en Envíos ▸ Editar.")
    return found[0]


def load(path):
    """A library campaign in the shape the sender uses (see send.make_message)."""
    path = Path(path)
    info = _read_json(path / INFO, {})
    sender = {**DEFAULT_SENDER, **{k: v for k, v in (info.get("sender") or {}).items() if v}}
    c = {"slug": path.name, "name": info.get("name") or path.name, "subject": info.get("subject") or "",
         "sender": sender, "html": (path / BODY).read_text(encoding="utf-8") if (path / BODY).exists() else "",
         "_folder": path, "created": info.get("created", ""), "updated": info.get("updated", "")}
    c.update({k: info[k] for k in KEEP if k in info})
    return c


def _sent_count(out_dir, slug):
    log = Path(out_dir) / "mailing" / slug / "envios.csv"
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8-sig").splitlines()[1:] if ",enviado," in line)


def list_campaigns(out_dir):
    index = ensure(out_dir)
    out = []
    for path in _folders(out_dir):
        c = load(path)
        out.append({"slug": c["slug"], "name": c["name"], "subject": c["subject"], "sender": c["sender"]["email"],
                    "updated": c["updated"], "sent": _sent_count(out_dir, c["slug"])})
    out.sort(key=lambda c: c["updated"], reverse=True)
    active = index.get("active") if any(c["slug"] == index.get("active") for c in out) else (out[0]["slug"] if out else "")
    return out, active


def images(path):
    pics = Path(path) / IMAGES
    return [{"name": p.name, "size": p.stat().st_size}
            for p in sorted(pics.iterdir()) if p.suffix.lower() in IMAGE_TYPES] if pics.exists() else []


def details(out_dir, slug):
    """Everything the editor shows for one campaign."""
    path = folder(out_dir, slug)
    c = load(path)
    return {"slug": c["slug"], "name": c["name"], "subject": c["subject"], "sender": c["sender"],
            "html": c["html"], "images": images(path), "updated": c["updated"],
            "sent": _sent_count(out_dir, c["slug"]), "checks": checks(c)}


def set_active(out_dir, slug):
    path = folder(out_dir, slug)
    with _LOCK:
        index = ensure(out_dir)
        index["active"] = path.name
        _write_json(root(out_dir) / INDEX, index)
    return path.name


def _new_slug(out_dir, name):
    base, n = slugify(name), 1
    slug = base
    # a slug that ever had a send log is never reused, so a new campaign never inherits it
    while (root(out_dir) / slug).exists() or (Path(out_dir) / "mailing" / slug).exists():
        n += 1
        slug = f"{base[:46]}-{n}"
    return slug


def _clean_sender(sender, fallback=None):
    sender = sender if isinstance(sender, dict) else {}
    out = dict(fallback or DEFAULT_SENDER)
    for key in ("name", "email", "reply_to"):
        value = str(sender.get(key) or "").strip()
        if value:
            out[key] = value[:200]
    for key in ("email", "reply_to"):
        if out[key] and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", out[key], re.I):
            raise ValueError(f"“{out[key]}” no es un correo válido.")
    return out


def create(out_dir, name, subject="", html="", sender=None, pictures=None, extra=None):
    """A new campaign; returns its slug. `pictures` maps file name -> bytes; `extra` carries KEEP keys."""
    ensure(out_dir)
    name = (name or "").strip()[:120] or "Campaña nueva"
    slug = _new_slug(out_dir, name)
    path = root(out_dir) / slug
    (path / IMAGES).mkdir(parents=True)
    for pic_name, data in (pictures or {}).items():
        _store_image(path, pic_name, data)
    (path / BODY).write_text(html or "", encoding="utf-8")
    info = {"name": name, "subject": (subject or "").strip()[:250],
            "sender": _clean_sender(sender), "created": _now(), "updated": _now()}
    info.update({k: v for k, v in (extra or {}).items() if k in KEEP})
    _write_json(path / INFO, info)
    return slug


def duplicate(out_dir, slug):
    c = load(folder(out_dir, slug))
    pictures = {p.name: p.read_bytes() for p in (c["_folder"] / IMAGES).glob("*") if p.is_file()}
    return create(out_dir, f"{c['name']} (copia)", c["subject"], c["html"], c["sender"], pictures, c)


def save(out_dir, slug, body):
    """Save the editor's fields. Only what the body carries changes."""
    path = folder(out_dir, slug)
    info = _read_json(path / INFO, {})
    if "name" in body:
        info["name"] = str(body.get("name") or "").strip()[:120] or info.get("name") or path.name
    if "subject" in body:
        subject = str(body.get("subject") or "").strip()
        if not subject:
            raise ValueError("Escribe el asunto.")
        info["subject"] = subject[:250]
    if "sender" in body:
        info["sender"] = _clean_sender(body.get("sender"), {**DEFAULT_SENDER, **(info.get("sender") or {})})
    if "html" in body:
        html = str(body.get("html") or "")
        if not html.strip():
            raise ValueError("El HTML está vacío.")
        (path / BODY).write_text(html, encoding="utf-8")
    info["updated"] = _now()
    _write_json(path / INFO, info)
    return details(out_dir, slug)


def delete(out_dir, slug, running=""):
    """Move a campaign to campanas/.papelera (not erased). Its send log stays, so today's
    limits still count what it sent."""
    path = folder(out_dir, slug)
    if running and running == path.name:
        raise ValueError("Esa campaña se está enviando ahora. Detén el envío antes de borrarla.")
    trash = root(out_dir) / TRASH
    trash.mkdir(exist_ok=True)
    shutil.move(str(path), str(trash / f"{path.name}-{datetime.now():%Y%m%d-%H%M%S}"))
    index = ensure(out_dir)
    if index.get("active") == path.name:
        rest = _folders(out_dir)
        index["active"] = rest[0].name if rest else ""
        _write_json(root(out_dir) / INDEX, index)


def _store_image(path, name, data):
    name = safe_name(name)
    if Path(name).suffix.lower() not in IMAGE_TYPES:
        raise ValueError(f"{name or 'Ese archivo'} no es una imagen (png, jpg, gif o webp).")
    if len(data) > MAX_IMAGE:
        raise ValueError(f"{name} pesa más de 5 MB. Redúcela antes de subirla.")
    (path / IMAGES).mkdir(exist_ok=True)
    (path / IMAGES / name).write_bytes(data)
    return name


def add_image(out_dir, slug, name, data):
    path = folder(out_dir, slug)
    stored = _store_image(path, name, data)
    _touch(path)
    return stored


def remove_image(out_dir, slug, name):
    path = folder(out_dir, slug)
    target = path / IMAGES / safe_name(name)
    if target.is_file():
        target.unlink()
        _touch(path)


def _touch(path):
    info = _read_json(path / INFO, {})
    info["updated"] = _now()
    _write_json(path / INFO, info)


def image_file(out_dir, slug, name):
    path = folder(out_dir, slug) / IMAGES / safe_name(name)
    if not path.is_file() or path.suffix.lower() not in IMAGE_TYPES:
        raise ValueError("No existe esa imagen.")
    return path


# --- sharing: export / import -------------------------------------------------------

def export_zip(out_dir, slug):
    """The campaign as a .zip to pass to someone else (they import it in their app)."""
    c = load(folder(out_dir, slug))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = {"name": c["name"], "subject": c["subject"], "sender": c["sender"], **{k: c[k] for k in KEEP if k in c}}
        z.writestr(INFO, json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr(BODY, c["html"])
        for p in sorted((c["_folder"] / IMAGES).glob("*")):
            if p.suffix.lower() in IMAGE_TYPES:
                z.write(p, f"{IMAGES}/{p.name}")
    return buf.getvalue(), f"{c['slug']}.zip"


def import_file(out_dir, filename, data):
    """A .zip (an exported campaign, or any HTML with its images) or a single .html file."""
    filename = str(filename or "")
    if filename.lower().endswith((".html", ".htm")):
        html = data.decode("utf-8", errors="replace")
        return create(out_dir, _title(html) or Path(filename).stem, _title(html), html)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError("Sube un .zip (HTML con sus imágenes) o un archivo .html.")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [m for m in z.infolist() if not m.is_dir() and "__MACOSX" not in m.filename
                   and not Path(m.filename).name.startswith(".")]
        if sum(m.file_size for m in members) > MAX_IMPORT:
            raise ValueError("El .zip es demasiado grande (más de 30 MB descomprimido).")
        pages = sorted((m for m in members if m.filename.lower().endswith((".html", ".htm"))),
                       key=lambda m: (Path(m.filename).name != BODY, -m.file_size))
        if not pages:
            raise ValueError("El .zip no trae ningún archivo .html.")
        html = z.read(pages[0]).decode("utf-8", errors="replace")
        info_member = next((m for m in members if Path(m.filename).name == INFO), None)
        info = {}
        if info_member:
            try:
                info = json.loads(z.read(info_member))
            except ValueError:
                info = {}
        pictures = {Path(m.filename).name: z.read(m) for m in members
                    if Path(m.filename).suffix.lower() in IMAGE_TYPES and m.file_size <= MAX_IMAGE}
    name = info.get("name") or _title(html) or Path(filename).stem
    try:
        sender = _clean_sender(info.get("sender"))
    except ValueError:
        sender = None
    return create(out_dir, name, info.get("subject") or _title(html), html, sender, pictures, info)


def _title(html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:120] if m else ""


# --- rendering one message ----------------------------------------------------------

def _decode_data_uri(ref):
    m = re.match(r"data:(image/[a-z0-9.+-]+);base64,(.*)$", ref.strip(), re.I | re.S)
    if not m:
        return None, None
    try:
        return m.group(1).lower(), base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True)
    except (binascii.Error, ValueError):
        return None, None


def _local_name(ref):
    """The campaign file an image reference means (src="imagenes/hero.png", "cid:hero.png"), or ''."""
    ref = ref.strip()
    if not ref or re.match(r"(?i)^(https?:|mailto:|tel:|#|data:|\{\{|\*\|)", ref):
        return ""
    return safe_name(ref[4:] if ref.lower().startswith("cid:") else ref)


def merge(html, person, campaign):
    """Fill the merge tags for one person. Values are HTML-escaped."""
    first = person.get("first_name") or ""
    unsub = unsubscribe_mailto(campaign, person.get("email") or "")
    out = GREETING_TAGS.sub(lambda m: html_lib.escape(person.get("greeting") or greeting(first)), html)
    out = NAME_TAGS.sub(lambda m: html_lib.escape(first), out)
    out = COMPANY_TAGS.sub(lambda m: html_lib.escape(person.get("company") or ""), out)
    return UNSUB_TAGS.sub(lambda m: html_lib.escape(unsub), out)


def with_unsubscribe(html):
    """Every mailing carries an unsubscribe link: one is added at the end if the HTML has none."""
    if UNSUB_TAGS.search(html or ""):
        return html
    if re.search(r"(?i)</body>", html):
        return re.sub(r"(?i)</body>", lambda m: FOOTER + m.group(0), html, count=1)
    return html + FOOTER


def inline_images(html, folder_path):
    """Point local and data: images at cid: attachments. Returns (html, [(cid, mime, bytes)], missing)."""
    pics = Path(folder_path) / IMAGES if folder_path else None
    attached, by_key, missing = [], {}, []

    def attach(key, mime, data, cid):
        if key not in by_key:
            by_key[key] = cid
            attached.append((cid, mime, data))
        return "cid:" + by_key[key]

    def swap(ref):
        if ref.strip().lower().startswith("data:"):
            mime, data = _decode_data_uri(ref)
            if data is None:
                return ref
            ext = {".jpe": ".jpg", None: ".png"}.get(mimetypes.guess_extension(mime), mimetypes.guess_extension(mime))
            n = 1
            while f"pegada-{n}{ext}" in by_key.values() or (pics and (pics / f"pegada-{n}{ext}").exists()):
                n += 1
            return attach(ref, mime, data, f"pegada-{n}{ext}")
        name = _local_name(ref)
        if not name:
            return ref
        if pics and (pics / name).is_file():
            return attach(name, mimetypes.guess_type(name)[0] or "image/png", (pics / name).read_bytes(), name)
        missing.append(name)
        return ref

    def repl(m):
        if m.group("pre") is not None:
            return f"{m.group('pre')}{m.group('q')}{swap(m.group('ref'))}{m.group('q')}"
        return f"{m.group('cpre')}{m.group('cq')}{swap(m.group('cref'))}{m.group('cq')}{m.group('cpost')}"
    return IMAGE_REFS.sub(repl, html), attached, sorted(set(missing))


def preview(html, slug, person, campaign):
    """HTML for the editor's preview: merge tags filled, campaign files served by the app."""
    def repl(m):
        ref = m.group("ref") if m.group("pre") is not None else m.group("cref")
        name = _local_name(ref)
        if name:
            ref = f"/mailing/library/img?campaign={slug}&name={name}"
        if m.group("pre") is not None:
            return f"{m.group('pre')}{m.group('q')}{ref}{m.group('q')}"
        return f"{m.group('cpre')}{m.group('cq')}{ref}{m.group('cq')}{m.group('cpost')}"
    return IMAGE_REFS.sub(repl, merge(with_unsubscribe(html), person, campaign))


def checks(campaign, html=None):
    """Warnings for the editor: what would go wrong in the inbox."""
    html = campaign["html"] if html is None else html
    out = []
    if not (html or "").strip():
        return ["Todavía no tiene HTML: pégalo o importa un .zip."]
    final, attached, missing = inline_images(with_unsubscribe(html), campaign.get("_folder"))
    if missing:
        out.append("Faltan estas imágenes en la campaña: " + ", ".join(missing)
                   + ". Súbelas en Imágenes o cambia el src por una liga https://.")
    if not UNSUB_TAGS.search(html):
        out.append("No tenía liga de baja: se agrega una al final del correo ({{Baja}}).")
    size = len(final.encode("utf-8"))
    if size > GMAIL_CLIPS:
        out.append(f"El HTML pesa {size // 1024} KB: Gmail corta los correos de más de ~100 KB "
                   "(“Mensaje recortado”). Quita estilos o texto sobrante.")
    weight = sum(len(d) for _, _, d in attached)
    if weight > 3 * 1024 * 1024:
        out.append(f"Las imágenes adjuntas pesan {weight / 1048576:.1f} MB por correo; redúcelas a menos "
                   "de ~1 MB en total para que llegue rápido y no caiga en spam.")
    if re.search(r"(?is)<script\b", html):
        out.append("Trae <script>: los correos no corren JavaScript y Gmail puede marcarlo como peligroso.")
    if not campaign.get("subject"):
        out.append("Falta el asunto.")
    return out


def build_message(campaign, person, subject=None, html=None):
    """The email for one person (From is the campaign's sender; send.make_message adjusts it)."""
    from .send import html_to_text  # send imports this module

    s = campaign["sender"]
    body, attached, _ = inline_images(merge(with_unsubscribe(campaign["html"] if html is None else html),
                                            person, campaign), campaign.get("_folder"))
    msg = EmailMessage()
    msg["From"] = formataddr((s["name"], s["email"]))
    msg["To"] = formataddr((person["name"], person["email"])) if person.get("name") else person["email"]
    msg["Subject"] = subject if subject is not None else campaign["subject"]
    if s.get("reply_to"):
        msg["Reply-To"] = s["reply_to"]
    msg["List-Unsubscribe"] = f"<{unsubscribe_mailto(campaign, person['email'])}>"
    msg.set_content(html_to_text(body))
    msg.add_alternative(body, subtype="html")
    if attached:
        html_part = msg.get_payload()[1]
        for cid, mime, data in attached:
            maintype, subtype = mime.split("/", 1)
            html_part.add_related(data, maintype, subtype, cid=f"<{cid}>", filename=cid, disposition="inline")
    return msg
