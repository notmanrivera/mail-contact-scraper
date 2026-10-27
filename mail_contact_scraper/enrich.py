"""Company sector from its website: compras@aceros-del-norte.com.mx -> "Metalmecánica".

After a scan, each new company domain gets one quick visit to its home page (and, if that
says little, its "Nosotros" page). The page title and description are matched against
keyword lists per sector. Answers are kept in output/empresas.json, so a domain is visited
once; failures are tried again after a month. Free-mail domains (gmail.com...) are skipped.

Off with ENRICH_DOMAINS=false.
"""
import html.parser
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .extract import FREE_MAIL

log = logging.getLogger(__name__)

CACHE_FILE = "empresas.json"
TIMEOUT = 6
MAX_BYTES = 400_000
WORKERS = 8
RETRY_FAILED = timedelta(days=30)
RECHECK = timedelta(days=180)
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
ABOUT_PATHS = ("/nosotros", "/quienes-somos", "/empresa", "/about", "/about-us")

# Most specific first: on a tie the earlier sector wins. Words are accent-free stems.
SECTORS = {
    "Aeroespacial": ["aeroespacial", "aerospace", "aeronautic", "aviacion", "aviation", "aircraft"],
    "Automotriz": ["automotri", "automotive", "autopartes", "auto parts", "tier 1", "oem", "vehicul",
                   "carrocer", "llantas", "neumatic"],
    "Farmacéutica": ["farmac", "pharma", "laboratorio", "medicamento", "drug", "biotec", "vacuna"],
    "Médico y salud": ["hospital", "clinica", "medic", "salud", "health", "dental", "dispositivos medicos"],
    "Cosméticos y cuidado personal": ["cosmetic", "belleza", "cuidado personal", "perfum", "beauty"],
    "Alimentos y bebidas": ["aliment", "food", "bebida", "beverage", "carnic", "lacteo", "dairy", "panader",
                            "bakery", "botana", "snack", "cerveza", "brewer", "refresco", "confiter",
                            "chocolate", "cafe", "tortill", "embutido", "agua purificada", "jugos"],
    "Agroindustria": ["agricol", "agro", "agricult", "fertiliz", "semilla", "ganader", "avicol", "invernadero"],
    "Química": ["quimic", "chemical", "resina", "pintura", "paint", "recubrimiento", "coating", "adhesiv",
                "solvente", "petroquimic"],
    "Plásticos y hule": ["plastic", "polimer", "polymer", "inyeccion de plastico", "extrusion", "hule",
                         "rubber", "termoformad", "pet "],
    "Empaque y embalaje": ["empaque", "embalaje", "packaging", "envase", "etiqueta", "label", "carton",
                           "corrugad", "tarima", "pallet", "stretch film", "playo"],
    "Papel e impresión": ["papel", "paper", "imprenta", "impresion", "printing", "litograf", "serigraf"],
    "Textil y confección": ["textil", "textile", "confeccion", "apparel", "ropa", "uniforme", "hilo", "tela"],
    "Metalmecánica": ["metalmecanic", "metal", "acero", "steel", "aluminio", "aluminum", "fundicion",
                      "foundry", "maquinado", "machining", "troquel", "estampado", "soldadura", "welding",
                      "forja", "herreria", "tornilleria", "fastener", "lamina", "tuberia"],
    "Maquinaria y equipo industrial": ["maquinaria", "machinery", "equipo industrial", "industrial equipment",
                                       "automatizacion", "automation", "robot", "hidraulic", "hydraulic",
                                       "neumatic", "pneumatic", "bomba", "pump", "compresor", "valvula",
                                       "valve", "rodamiento", "bearing", "banda transportadora", "conveyor",
                                       "montacarga", "forklift", "refaccion"],
    "Eléctrico y electrónico": ["electric", "electronic", "electronica", "cable", "iluminacion", "lighting",
                                "transformador", "tablero"],
    "Energía": ["energia", "energy", "solar", "fotovoltaic", "petrole", "oil", "gas natural", "combustible"],
    "Minería": ["mineria", "mining", "minera", "mineral"],
    "Construcción": ["construc", "construction", "constructora", "inmobiliar", "real estate", "arquitect",
                     "cemento", "concreto", "concrete", "obra civil", "acabados", "materiales para construccion"],
    "Logística y transporte": ["logistic", "transporte", "transport", "fletes", "freight", "paqueteria",
                               "courier", "almacen", "warehouse", "aduana", "customs", "agente aduanal",
                               "mudanza"],
    "Tecnología y software": ["software", "tecnologia", "technology", "erp", "cloud", "saas",
                              "desarrollo web", "it services", "ciberseguridad", "telecom"],
    "Seguridad industrial": ["seguridad industrial", "safety", "epp", "proteccion personal", "extintor"],
    "Limpieza y mantenimiento": ["limpieza", "cleaning", "mantenimiento", "maintenance", "sanitizacion",
                                 "jarceria", "control de plagas"],
    "Educación": ["universidad", "university", "escuela", "school", "colegio", "educacion", "education"],
    "Gobierno": ["gobierno", "government", "secretaria de", "municipio", "ayuntamiento"],
    "Hotelería y restaurantes": ["hotel", "restaurante", "restaurant", "hospitality", "banquete", "catering"],
    "Finanzas y seguros": ["banco", "bank", "financier", "seguros", "insurance", "fintech",
                           "arrendadora", "leasing"],
    "Servicios profesionales": ["consultor", "consulting", "despacho", "abogad", "contable",
                                "contadores", "accounting", "marketing", "publicidad", "recursos humanos",
                                "reclutamiento", "staffing"],
    "Distribución y comercio": ["distribu", "comercializ", "importador", "exportador", "mayorista",
                                "wholesale", "tienda", "retail", "ferreteri"],
}
_PATTERNS = {sector: [re.compile(r"\b" + re.escape(w)) for w in words] for sector, words in SECTORS.items()}


def _plain(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower())


def classify(title="", description="", body=""):
    """Best sector for the page, or "" when nothing matches. Title counts most."""
    scores = {}
    for weight, text in ((3, title), (2, description), (1, body)):
        text = _plain(text)
        if not text:
            continue
        for sector, patterns in _PATTERNS.items():
            hits = sum(1 for p in patterns if p.search(text))
            if hits:
                scores[sector] = scores.get(sector, 0) + weight * hits
    if not scores:
        return ""
    order = list(SECTORS)
    return max(scores, key=lambda s: (scores[s], -order.index(s)))


class _Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.description, self.site_name, self.keywords = "", "", "", ""
        self.text, self._in, self._skip = [], "", 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description") and not self.description:
                self.description = a.get("content", "").strip()
            elif key == "og:site_name":
                self.site_name = a.get("content", "").strip()
            elif key == "keywords":
                self.keywords = a.get("content", "").strip()
        elif tag == "title":
            self._in = "title"
        elif tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in = ""
        elif tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif not self._skip and data.strip() and sum(map(len, self.text)) < 4000:
            self.text.append(data.strip())


def _get(url, opener):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                                   "Accept-Language": "es-MX,es;q=0.9,en;q=0.5"})
    with opener(request, timeout=TIMEOUT) as res:
        if "html" not in (res.headers.get("Content-Type") or "html"):
            return ""
        raw = res.read(MAX_BYTES)
        charset = res.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _read(url, opener):
    page = _Page()
    page.feed(_get(url, opener))
    return page


def visit(domain, opener=urllib.request.urlopen):
    """{"sector", "title", "description", "url"} for the domain's site; raises OSError if unreachable."""
    page, base, error = None, "", None
    for base in (f"https://{domain}", f"https://www.{domain}", f"http://www.{domain}"):
        try:
            page = _read(base + "/", opener)
            break
        except (OSError, ValueError, urllib.error.URLError) as exc:  # DNS, TLS, 4xx/5xx, timeouts
            error = exc
    if page is None:
        raise OSError(str(error))
    title, description = page.title.strip(), page.description or page.keywords
    body = " ".join(page.text)
    sector = classify(title, description, body)
    if not sector or len(description) < 40:
        for path in ABOUT_PATHS:  # a thin home page: the "Nosotros" page usually says what they do
            try:
                about = _read(base + path, opener)
            except (OSError, ValueError, urllib.error.URLError):
                continue
            body += " " + " ".join(about.text)
            description = description or about.description
            sector = classify(title, description, body)
            break
    return {"sector": sector, "title": re.sub(r"\s+", " ", title)[:200],
            "description": re.sub(r"\s+", " ", description)[:300], "url": base + "/"}


def domain_of(contact):
    """The company domain for a contacts.csv row: its email, else its website."""
    domain = contact.get("email", "").rsplit("@", 1)[-1].lower()
    if domain in FREE_MAIL:
        site = (contact.get("websites") or "").split(";")[0].strip().lower()
        domain = re.sub(r"^(https?://)?(www\.)?", "", site).split("/")[0]
    return "" if not domain or domain in FREE_MAIL or "." not in domain else domain


def load(out_dir):
    try:
        return json.loads((Path(out_dir) / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(out_dir, cache):
    path = Path(out_dir) / CACHE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, path)


def _due(entry, now):
    if not entry:
        return True
    try:
        checked = datetime.fromisoformat(entry.get("checked", ""))
    except ValueError:
        return True
    return now - checked > (RECHECK if entry.get("ok") else RETRY_FAILED)


def pending(domains, cache, now=None):
    now = now or datetime.now()
    return sorted({d for d in domains if d and _due(cache.get(d), now)})


def enrich(out_dir, domains, limit=400, opener=urllib.request.urlopen, now=None):
    """Visit the domains not in empresas.json yet (up to `limit`). Returns how many were visited."""
    cache = load(out_dir)
    todo = pending(domains, cache, now)[:limit]
    if not todo:
        return 0
    stamp = (now or datetime.now()).isoformat(timespec="seconds")

    def one(domain):
        try:
            return domain, {**visit(domain, opener), "ok": True, "checked": stamp}
        except OSError as exc:
            log.debug("No se pudo leer %s: %s", domain, exc)
            return domain, {"ok": False, "checked": stamp, "error": str(exc)[:200]}

    log.info("Revisando la página web de %d empresas para saber su giro...", len(todo))
    visited = 0
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for domain, entry in pool.map(one, todo):
                # A failed retry keeps what an earlier visit found.
                cache[domain] = entry if entry["ok"] else {**cache.get(domain, {}), **entry}
                visited += 1
                if visited % 25 == 0:
                    _save(out_dir, cache)
    finally:
        _save(out_dir, cache)
    return visited


def lookup(cache):
    """domain -> (sector, description) for ContactBook."""
    return {d: (e.get("sector", ""), e.get("description", "")) for d, e in cache.items() if e.get("sector")
            or e.get("description")}
