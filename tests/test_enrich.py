import csv
import io
import json
import urllib.error
from datetime import datetime
from email.message import Message

from mail_contact_scraper import cli, enrich
from mail_contact_scraper.config import load_settings
from mail_contact_scraper.contacts import ContactBook

PAGES = {
    "https://aceros-del-norte.com.mx/": """<html><head><title>Aceros del Norte | Lámina y perfiles de acero</title>
        <meta name="description" content="Distribuidor de acero, lámina galvanizada y tubería para la industria.">
        </head><body><script>var x="hospital";</script><p>Corte y doblez de lámina</p></body></html>""",
    "https://www.lacteos-qro.mx/": """<html><head><title>Lácteos Querétaro</title></head><body>Inicio</body></html>""",
    "https://www.lacteos-qro.mx/nosotros": """<html><body><p>Somos una empresa de alimentos lácteos:
        quesos, crema y yogur para todo el Bajío.</p></body></html>""",
    "https://nada.mx/": "<html><head><title>Bienvenidos</title></head><body>Próximamente</body></html>",
}


class _Res(io.BytesIO):
    def __init__(self, body):
        super().__init__(body.encode())
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(request, timeout=None):
    url = request.full_url
    opener.calls.append(url)
    if url not in PAGES:
        raise urllib.error.URLError("no hay")
    return _Res(PAGES[url])


opener.calls = []


def test_classify_reads_title_description_and_about_page():
    assert enrich.visit("aceros-del-norte.com.mx", opener)["sector"] == "Metalmecánica"
    lacteos = enrich.visit("lacteos-qro.mx", opener)  # bare domain fails, www works, home page is thin
    assert lacteos["sector"] == "Alimentos y bebidas" and lacteos["url"] == "https://www.lacteos-qro.mx/"
    assert enrich.visit("nada.mx", opener)["sector"] == ""
    assert enrich.classify("Grupo Farmacéutico del Centro", "Laboratorio de medicamentos genéricos") == "Farmacéutica"
    assert enrich.classify("Aviso legal | Bolsa de trabajo | Sistemas") == ""


def test_domain_of_skips_free_mail():
    assert enrich.domain_of({"email": "compras@aceros-del-norte.com.mx"}) == "aceros-del-norte.com.mx"
    assert enrich.domain_of({"email": "juan@gmail.com", "websites": "https://www.lacteos-qro.mx/contacto"}) == "lacteos-qro.mx"
    assert enrich.domain_of({"email": "juan@gmail.com", "websites": ""}) == ""


def test_enrich_caches_and_retries_failures_later(tmp_path):
    opener.calls.clear()
    now = datetime(2026, 10, 27, 12, 0)
    domains = ["aceros-del-norte.com.mx", "caido.mx"]
    assert enrich.enrich(tmp_path, domains, opener=opener, now=now) == 2
    cache = enrich.load(tmp_path)
    assert cache["aceros-del-norte.com.mx"]["sector"] == "Metalmecánica" and cache["aceros-del-norte.com.mx"]["ok"]
    assert cache["caido.mx"]["ok"] is False
    assert enrich.enrich(tmp_path, domains, opener=opener, now=now) == 0           # nothing visited twice
    assert enrich.pending(domains, cache, datetime(2026, 12, 1)) == ["caido.mx"]   # failures retried after a month
    assert enrich.lookup(cache) == {"aceros-del-norte.com.mx": (
        "Metalmecánica", "Distribuidor de acero, lámina galvanizada y tubería para la industria.")}


def test_sector_column_in_contacts_csv(tmp_path, monkeypatch):
    (tmp_path / enrich.CACHE_FILE).write_text(json.dumps({"aceros-del-norte.com.mx": {
        "ok": True, "sector": "Metalmecánica", "description": "Acero", "checked": "2026-10-27T12:00:00"}}))
    monkeypatch.setenv("ENRICH_DOMAINS", "true")
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    record = {"from_email": "compras@aceros-del-norte.com.mx", "from_name": "Laura", "signature": "",
              "date": "2026-10-01", "contact": {"name": "", "title": "", "company": "", "phones": [],
                                                "websites": [], "emails": []}}
    cli.build_contacts([record], load_settings(tmp_path / ".env"), tmp_path)
    row = next(csv.DictReader(open(tmp_path / "contacts.csv", encoding="utf-8-sig")))
    assert (row["sector"], row["company_web"]) == ("Metalmecánica", "Acero")
    assert ContactBook().to_rows() == []


def test_scan_visits_new_company_sites_at_the_end(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setenv("ENRICH_DOMAINS", "true")
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    seen = []
    monkeypatch.setattr(enrich, "visit", lambda d, opener=None: seen.append(d) or {"sector": "Química", "title": "",
                                                                                   "description": "", "url": ""})
    fixtures = Path(__file__).parent / "fixtures"
    cli.main(["--local", str(fixtures), "--output", str(tmp_path), "--fresh", "--no-ocr"])
    rows = list(csv.DictReader(open(tmp_path / "contacts.csv", encoding="utf-8-sig")))
    assert seen and all("gmail.com" not in d for d in seen)
    assert {r["sector"] for r in rows if enrich.domain_of(r) in seen} == {"Química"}
    assert "enriching" not in json.loads((tmp_path / "status.json").read_text())
