"""Render a D-Industrimex mailing for every prospect in contacts.csv.

Nothing here sends mail. It writes files to review and then import into an
email platform (Brevo, Mailchimp) or open as drafts (.eml).
"""
import base64
import csv
import html
import mimetypes
import shutil
import re
import tomllib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

TEMPLATES = Path(__file__).parent / "templates"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
HONORIFICS = {"ing", "lic", "dr", "dra", "mtro", "mtra", "arq", "c.p", "cp", "sr", "sra", "srita", "mr", "mrs", "ms"}

# Merge tags each platform replaces at send time, and the CSV headers it imports.
ESP = {
    # Yet Another Mail Merge (Google Sheets + Gmail): {{Columna}} del Sheet; YAMM agrega su propia liga de baja.
    "yamm": {
        "greeting": "{{Saludo}}",
        "unsubscribe": "",
        "columns": {"email": "Email", "first_name": "Nombre", "last_name": "Apellido",
                    "greeting": "Saludo", "company": "Empresa", "title": "Puesto"},
    },
    "brevo": {
        "greeting": "{{ contact.SALUDO }}",
        "unsubscribe": "{{ unsubscribe }}",
        "columns": {"email": "EMAIL", "first_name": "FIRSTNAME", "last_name": "LASTNAME",
                    "greeting": "SALUDO", "company": "EMPRESA", "title": "PUESTO"},
    },
    "mailchimp": {
        "greeting": "*|SALUDO|*",
        "unsubscribe": "*|UNSUB|*",
        "columns": {"email": "Email Address", "first_name": "First Name", "last_name": "Last Name",
                    "greeting": "SALUDO", "company": "Company", "title": "Title"},
    },
}

AUDIENCE_FIELDS = ["email", "first_name", "last_name", "greeting", "name", "title", "company", "source"]


def accent(text):
    """Escape, then turn **words** into the brand red keyword."""
    escaped = html.escape(text or "", quote=False)
    return Markup(re.sub(r"\*\*(.+?)\*\*", r'<span style="color:#A81818;">\1</span>', escaped))


def plain(text):
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")


_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True, keep_trailing_newline=True)
_env.filters["accent"] = accent


def load_campaign(path):
    with open(path, "rb") as fh:
        campaign = tomllib.load(fh)
    campaign["_dir"] = Path(path).resolve().parent.parent
    return campaign


def asset_files(campaign):
    """Image files of the campaign, in the order the template first uses them."""
    folder = campaign["_dir"] / campaign["assets"]["dir"]
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif"))


def hosted_url(campaign, name):
    """Public URL of an image: its own entry in [assets.urls] (e.g. Wix) or base_url + name."""
    assets = campaign["assets"]
    if name in assets.get("urls", {}):
        return assets["urls"][name]
    if assets.get("base_url"):
        return assets["base_url"].rstrip("/") + "/" + name
    return None


def is_hosted(campaign):
    return all(hosted_url(campaign, f.name) for f in asset_files(campaign))


def _asset_resolver(campaign, mode):
    """How images are referenced: hosted URL, cid: (attached inline) or data: (local preview)."""
    hosted = is_hosted(campaign)
    folder = campaign["_dir"] / campaign["assets"]["dir"]

    def resolve(name):
        if hosted:
            return hosted_url(campaign, name)
        if mode == "data":
            mime = mimetypes.guess_type(name)[0] or "image/png"
            return f"data:{mime};base64," + base64.b64encode((folder / name).read_bytes()).decode()
        return "cid:" + name
    return resolve


def split_name(name):
    """'Ing. LAURA Martínez' -> ('Laura', 'Martínez'). Empty when there is no usable name."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    while parts and parts[0].lower().rstrip(".") in HONORIFICS:
        parts.pop(0)
    if not parts or "@" in parts[0] or not re.match(r"^[^\W\d_]", parts[0]):
        return "", ""
    fix = lambda w: w.capitalize() if w.isupper() or w.islower() else w
    return fix(parts[0]), " ".join(fix(p) for p in parts[1:])


def greeting(first_name):
    return f"Hola {first_name}," if first_name else "Hola,"


def load_audience(csv_path, campaign, exclude_domains=(), suppression_path=None, require_signature=False):
    """Read the scraper CSV and keep valid, non-internal, non-suppressed prospects."""
    own = campaign["sender"]["email"].split("@")[-1].lower()
    domains = {own, *(d.lower().lstrip("@") for d in exclude_domains)}
    suppressed = set()
    if suppression_path and Path(suppression_path).exists():
        suppressed = {l.strip().lower() for l in Path(suppression_path).read_text().splitlines()
                      if l.strip() and not l.startswith("#")}
    out, seen, skipped = [], set(), {"invalid": 0, "internal": 0, "suppressed": 0, "no_signature": 0}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            email = (row.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email) or email in seen:
                skipped["invalid"] += 1
                continue
            if email.split("@")[-1] in domains:
                skipped["internal"] += 1
                continue
            if email in suppressed:
                skipped["suppressed"] += 1
                continue
            if require_signature and "signature" not in (row.get("source") or "") and "image" not in (row.get("source") or ""):
                skipped["no_signature"] += 1
                continue
            seen.add(email)
            first, last = split_name(row.get("name"))
            out.append({
                "email": email, "first_name": first, "last_name": last, "greeting": greeting(first),
                "name": row.get("name", ""), "title": row.get("title", ""),
                "company": row.get("company", ""), "source": row.get("source", ""),
            })
    return out, skipped


def unsubscribe_mailto(campaign, email):
    to = campaign["sender"].get("reply_to") or campaign["sender"]["email"]
    return f"mailto:{to}?subject={quote('Baja de correos')}&body={quote(email)}"


def render_html(campaign, greeting_text, unsubscribe_url, images="cid"):
    return _env.get_template("mailing.html").render(
        c=campaign, greeting=greeting_text, unsubscribe_url=unsubscribe_url,
        asset=_asset_resolver(campaign, images))


def render_text(campaign, greeting_text, unsubscribe_url):
    c = campaign
    lines = [greeting_text, "", plain(c["hero"]["headline"]), "", c["hero"]["body"],
             f"{c['hero']['cta_label']}: {c['hero']['cta_url']}", ""]
    for b in c["blocks"]:
        lines += [f"[{b['brand']}] {plain(b['title'])}", b["body"]]
        lines += [f"  ✓ {check}" for check in b["checks"]]
        lines += [f"{b['cta_label'].rstrip(' →')}: {b['cta_url']}", ""]
    lines += [plain(c["closing"]["question"]), c["closing"].get("intro", "")]
    lines += [f"  {ch['label']}: va {ch['pick']}" for ch in c["closing"].get("choices", [])]
    lines += [*c["closing"]["body"], c["closing"]["contact"], ""]
    s = c["signature"]
    lines += [s["name"], s["title"], s["address"], c["footer"]["site"], "",
              c["footer"]["reason"], f"Para darte de baja: {unsubscribe_url}"]
    return "\n".join(lines) + "\n"


def build_eml(campaign, person, html_body, text_body):
    s = campaign["sender"]
    msg = EmailMessage()
    msg["From"] = formataddr((s["name"], s["email"]))
    msg["To"] = formataddr((person["name"], person["email"])) if person["name"] else person["email"]
    msg["Subject"] = campaign["subject"]
    if s.get("reply_to"):
        msg["Reply-To"] = s["reply_to"]
    msg["List-Unsubscribe"] = f"<{unsubscribe_mailto(campaign, person['email'])}>"
    msg["X-Unsent"] = "1"  # Outlook / Apple Mail open it as a draft
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if not is_hosted(campaign):
        html_part = msg.get_payload()[1]
        for f in asset_files(campaign):
            maintype, subtype = (mimetypes.guess_type(f.name)[0] or "image/png").split("/")
            html_part.add_related(f.read_bytes(), maintype, subtype, cid=f"<{f.name}>",
                                  filename=f.name, disposition="inline")
    return msg


def write_csv(path, rows, fields, header=None):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header or fields)
        for r in rows:
            w.writerow([r.get(f, "") for f in fields])


EXCEL_COLUMNS = ["Correo", "Nombre", "Saludo", "Empresa", "Puesto", "Asunto", "CuerpoHTML", "Enviado"]


def write_excel(path, campaign, people):
    """One row per prospect with the finished HTML body, ready for the Outlook macro."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Mailing"
    ws.append(EXCEL_COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="A81818")
    for p in people:
        body = render_html(campaign, p["greeting"], unsubscribe_mailto(campaign, p["email"]))
        if len(body) > 32_000:  # Excel cell limit is 32,767 characters
            raise ValueError(f"El HTML para {p['email']} excede el límite de una celda de Excel")
        ws.append([p["email"], p["name"], p["greeting"], p["company"], p["title"], campaign["subject"], body, ""])
    for col, width in zip("ABCDEFGH", (34, 28, 18, 30, 26, 40, 20, 18)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.save(path)
    if not is_hosted(campaign):
        # the Outlook macro attaches these inline from the folder next to the workbook
        img_dir = Path(path).parent / "imagenes"
        img_dir.mkdir(exist_ok=True)
        for f in asset_files(campaign):
            shutil.copy2(f, img_dir / f.name)


def build(campaign_path, contacts_csv, out_dir, esp=None, eml=False, excel=False, **audience_opts):
    campaign = load_campaign(campaign_path)
    people, skipped = load_audience(contacts_csv, campaign, **audience_opts)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    write_csv(out / "audience.csv", people, AUDIENCE_FIELDS)
    sample = people[0] if people else {"email": "prospecto@ejemplo.com", "greeting": greeting("Laura")}
    (out / "preview.html").write_text(
        render_html(campaign, sample["greeting"], unsubscribe_mailto(campaign, sample["email"]), images="data"),
        encoding="utf-8")
    (out / "preview.txt").write_text(
        render_text(campaign, sample["greeting"], unsubscribe_mailto(campaign, sample["email"])), encoding="utf-8")

    if esp:
        if not is_hosted(campaign):
            raise ValueError(f"Para {esp} las imágenes deben estar en línea: llena [assets] base_url en la campaña")
        tags = ESP[esp]
        (out / f"template-{esp}.html").write_text(
            render_html(campaign, Markup(tags["greeting"]), Markup(tags["unsubscribe"])), encoding="utf-8")
        cols = tags["columns"]
        write_csv(out / f"audience-{esp}.csv", people, list(cols), header=list(cols.values()))

    if excel:
        write_excel(out / f"mailing-{campaign['slug']}.xlsx", campaign, people)

    if eml:
        eml_dir = out / "eml"
        eml_dir.mkdir(exist_ok=True)
        for p in people:
            unsub = unsubscribe_mailto(campaign, p["email"])
            msg = build_eml(campaign, p, render_html(campaign, p["greeting"], unsub),
                            render_text(campaign, p["greeting"], unsub))
            (eml_dir / f"{p['email']}.eml").write_bytes(bytes(msg))

    return people, skipped
