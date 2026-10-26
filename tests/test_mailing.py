import csv
from email import message_from_bytes
from email.policy import default

import openpyxl
import pytest
from pathlib import Path

from mailing.build import build, load_campaign, render_html, split_name

CAMPAIGN = "mailing/campaigns/qc-aro.toml"
FIELDS = ["email", "name", "title", "company", "source"]


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


def test_build_filters_and_writes_outputs(tmp_path):
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
    people, skipped = build(CAMPAIGN, contacts, tmp_path / "out", excel=True, eml=True,
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


def test_esp_template_needs_hosted_images(tmp_path):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [{"email": "a@x.com", "name": "Ana", "source": "signature"}])
    with pytest.raises(ValueError, match="base_url"):
        build(CAMPAIGN, contacts, tmp_path / "out", esp="brevo")

    hosted = Path(CAMPAIGN).parent / "_test-hosted.toml"
    hosted.write_text(Path(CAMPAIGN).read_text().replace('base_url = ""', 'base_url = "https://cdn.example.com/qc-aro/"'))
    try:
        build(hosted, contacts, tmp_path / "out", esp="brevo")
    finally:
        hosted.unlink()
    tpl = (tmp_path / "out" / "template-brevo.html").read_text()
    assert "{{ contact.SALUDO }}" in tpl and 'src="https://cdn.example.com/qc-aro/logo-rojo.png"' in tpl


def test_require_signature_skips_header_only(tmp_path):
    contacts = tmp_path / "contacts.csv"
    write_contacts(contacts, [
        {"email": "a@x.com", "name": "Ana", "source": "header+image"},
        {"email": "b@x.com", "name": "", "source": "header"},
    ])
    people, skipped = build(CAMPAIGN, contacts, tmp_path / "out", require_signature=True)
    assert [p["email"] for p in people] == ["a@x.com"]
    assert skipped["no_signature"] == 1
