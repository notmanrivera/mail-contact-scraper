import csv
import re
from email import message_from_bytes
from email.policy import default

import openpyxl
import pytest
from pathlib import Path

from mailing.build import build, load_campaign, render_html, split_name

CAMPAIGN = "mailing/campaigns/qc-aro.toml"
FIELDS = ["email", "name", "title", "company", "source"]


@pytest.fixture
def local_campaign():
    """qc-aro without its Wix URLs, so images are embedded (cid:/data:)."""
    path = Path(CAMPAIGN).parent / "_test-local.toml"
    text = re.sub(r'(?m)^"[^"]+\.png" = "https://[^"]+"\n', "", Path(CAMPAIGN).read_text())
    path.write_text(text)
    yield str(path)
    path.unlink()


def write_contacts(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def test_split_name_drops_honorifics_and_fixes_case():
    assert split_name("Ing. LAURA Martínez") == ("Laura", "Martínez")
    assert split_name("") == ("", "")
    assert split_name("ventas@x.com") == ("", "")


def test_headline_accent_is_escaped_and_red():
    c = load_campaign(CAMPAIGN)
    c["hero"]["headline"] = "<b>x</b> **rojo**"
    out = render_html(c, "Hola,", "mailto:x")
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert '<span style="color:#A81818;">rojo</span>' in out


def test_build_filters_and_writes_outputs(tmp_path, local_campaign):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [
        {"email": "laura@aceros.mx", "name": "Laura Martínez", "source": "signature"},
        {"email": "pedro@dindustrimex.com", "name": "Pedro", "source": "header"},
        {"email": "baja@cliente.com", "name": "", "source": "header"},
        {"email": "cc@cliente.com", "name": "", "source": "header"},
        {"email": "no-es-correo", "name": "", "source": "header"},
    ])
    supp = tmp_path / "suppression.txt"
    supp.write_text("BAJA@cliente.com\n")
    people, skipped = build(local_campaign, contacts, tmp_path / "out", excel=True, eml=True,
                            suppression_path=supp)
    assert [p["email"] for p in people] == ["laura@aceros.mx", "cc@cliente.com"]
    assert skipped == {"invalid": 1, "internal": 1, "suppressed": 1, "no_signature": 0}

    out = tmp_path / "out"
    preview = (out / "preview.html").read_text()
    assert "Hola Laura," in preview and 'src="data:image/png;base64,' in preview
    assert (out / "imagenes" / "logo-rojo.png").exists()
    rows = list(openpyxl.load_workbook(out / "mailing-qc-aro.xlsx").active.iter_rows(values_only=True))
    assert rows[0][0] == "Correo" and rows[2][2] == "Hola,"
    assert rows[1][6].startswith("<!DOCTYPE html>")
    eml = message_from_bytes((out / "eml" / "laura@aceros.mx.eml").read_bytes(), policy=default)
    assert eml["List-Unsubscribe"].startswith("<mailto:ventas@dindustrimex.com")
    html_part = eml.get_body(("html",))
    assert 'src="cid:logo-rojo.png"' in html_part.get_content()
    cids = {p["Content-ID"] for p in eml.walk() if p.get_content_maintype() == "image"}
    assert "<logo-rojo.png>" in cids and "<qc.png>" in cids


def test_esp_template_needs_hosted_images(tmp_path, local_campaign):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [{"email": "a@x.com", "name": "Ana", "source": "signature"}])
    with pytest.raises(ValueError, match="base_url"):
        build(local_campaign, contacts, tmp_path / "out", esp="brevo")

    hosted = Path(CAMPAIGN).parent / "_test-hosted.toml"
    hosted.write_text(Path(local_campaign).read_text().replace('base_url = ""', 'base_url = "https://cdn.example.com/qc-aro/"'))
    try:
        build(hosted, contacts, tmp_path / "out", esp="brevo")
    finally:
        hosted.unlink()
    tpl = (tmp_path / "out" / "template-brevo.html").read_text()
    assert "{{ contact.SALUDO }}" in tpl and 'src="https://cdn.example.com/qc-aro/hero.png"' in tpl


def test_require_signature_skips_header_only(tmp_path):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [
        {"email": "a@x.com", "name": "Ana", "source": "header+image"},
        {"email": "b@x.com", "name": "", "source": "header"},
    ])
    people, skipped = build(CAMPAIGN, contacts, tmp_path / "out", require_signature=True)
    assert [p["email"] for p in people] == ["a@x.com"]
    assert skipped["no_signature"] == 1


def test_yamm_template_uses_sheet_columns_and_no_own_unsubscribe(tmp_path):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [{"email": "a@x.com", "name": "Ana López", "source": "signature"}])
    hosted = Path(CAMPAIGN).parent / "_test-yamm.toml"
    hosted.write_text(Path(CAMPAIGN).read_text().replace('base_url = ""', 'base_url = "https://cdn.example.com/qc-aro/"'))
    try:
        build(hosted, contacts, tmp_path / "out", esp="yamm")
    finally:
        hosted.unlink()
    tpl = (tmp_path / "out" / "template-yamm.html").read_text()
    assert "{{Saludo}}" in tpl and "Ya no quiero recibir" not in tpl
    rows = list(csv.reader(open(tmp_path / "out" / "audience-yamm.csv", encoding="utf-8-sig")))
    assert rows == [["Email", "Nombre", "Apellido", "Saludo", "Empresa", "Puesto"],
                    ["a@x.com", "Ana", "López", "Hola Ana,", "", ""]]


def test_per_image_urls_for_wix(tmp_path, local_campaign):
    from mailing.build import asset_files, render_html
    c = load_campaign(local_campaign)
    c["assets"]["urls"] = {f.name: f"https://static.wixstatic.com/media/{f.stem}.png" for f in asset_files(c)}
    out = render_html(c, "Hola,", "", images="cid")
    assert 'src="https://static.wixstatic.com/media/logo-rojo.png"' in out and "cid:" not in out
    del c["assets"]["urls"]["qc.png"]
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [{"email": "a@x.com", "name": "Ana", "source": "signature"}])
    with pytest.raises(ValueError, match="qc.png"):
        build(local_campaign, contacts, tmp_path / "out", esp="yamm")


def test_qc_aro_is_ready_for_yamm(tmp_path):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [{"email": "a@x.com", "name": "Ana", "source": "signature"}])
    build(CAMPAIGN, contacts, tmp_path / "out", esp="yamm")
    tpl = (tmp_path / "out" / "template-yamm.html").read_text()
    srcs = re.findall(r'src="([^"]+)"', tpl)
    assert srcs and all(s.startswith("https://static.wixstatic.com/") for s in srcs)
