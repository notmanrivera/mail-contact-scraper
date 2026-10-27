"""The app's campaign library: each person's campaigns in their data folder."""
import base64
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from mailing import library, send
from tests.test_mailing import FakeSMTP, _sender, write_contacts

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
       b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
PAGE = ('<html><head><title>Promo ARO</title></head><body><img src="images/Logo%20Rojo.png">'
        '<p>{{Saludo}} {{Nombre}} de {{Empresa}}</p><img src="data:image/png;base64,' + base64.b64encode(PNG).decode()
        + '"><div style="background:url(\'fondo.png\')"></div><a href="{{Baja}}">baja</a></body></html>')


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_built_in_campaign_is_copied_in_once(tmp_path):
    found, active = library.list_campaigns(tmp_path)
    assert [c["slug"] for c in found] == ["qc-aro"] and active == "qc-aro"
    qc = library.details(tmp_path, "qc-aro")
    assert qc["name"] == "Q-Pumps QC + ARO · octubre 2026" and qc["sender"]["reply_to"] == "ventas@dindustrimex.com"
    assert "{{Saludo}}" in qc["html"] and "{{Baja}}" in qc["html"] and "https://static.wixstatic.com/" in qc["html"]
    assert qc["checks"] == []
    library.delete(tmp_path, "qc-aro")  # deleted stays deleted (it went to the trash)
    assert library.list_campaigns(tmp_path) == ([], "")
    assert list((tmp_path / "campanas" / ".papelera").iterdir())


def test_import_zip_and_send_with_images_attached(tmp_path):
    slug = library.import_file(tmp_path, "promo.zip", _zip({
        "promo/index.html": PAGE, "promo/images/Logo Rojo.png": PNG, "promo/fondo.png": PNG,
        "__MACOSX/._index.html": "x"}))
    c = library.details(tmp_path, slug)
    assert slug == "promo-aro" and c["subject"] == "Promo ARO"
    assert {i["name"] for i in c["images"]} == {"Logo-Rojo.png", "fondo.png"} and c["checks"] == []
    msg = send.make_message(library.load(library.folder(tmp_path, slug)),
                            {"email": "laura@lacteos.mx", "name": "Laura Pérez", "first_name": "Laura",
                             "greeting": "Hola Laura,", "company": "Lácteos & Co"})
    html = msg.get_body(("html",)).get_content()
    assert "Hola Laura, Laura de Lácteos &amp; Co" in html
    assert 'src="cid:Logo-Rojo.png"' in html and "url('cid:fondo.png')" in html and "data:image" not in html
    assert "mailto:ventas@dindustrimex.com?subject=Baja%20de%20correos&amp;body=laura%40lacteos.mx" in html
    cids = sorted(p["Content-ID"] for p in msg.walk() if p.get_content_maintype() == "image")
    assert cids == ["<Logo-Rojo.png>", "<fondo.png>", "<pegada-1.png>"]
    assert "Hola Laura" in msg.get_body(("plain",)).get_content() and msg["List-Unsubscribe"]


def test_checks_warn_before_it_goes_out(tmp_path):
    slug = library.create(tmp_path, "Sin baja", "Asunto", '<body><img src="no-esta.jpg">' + "x" * 120_000 + "</body>")
    checks = " ".join(library.details(tmp_path, slug)["checks"])
    assert "no-esta.jpg" in checks and "liga de baja" in checks and "Gmail corta" in checks
    msg = send.make_message(library.load(library.folder(tmp_path, slug)),
                            {"email": "a@b.com", "name": "", "greeting": "Hola,"})
    assert "Darte de baja" in msg.get_body(("html",)).get_content()  # added for them


def test_export_import_roundtrip_and_duplicates_start_fresh(tmp_path):
    other = tmp_path / "companero"
    slug = library.import_file(tmp_path, "promo.zip", _zip({"index.html": PAGE, "images/Logo Rojo.png": PNG}))
    info = tmp_path / "campanas" / slug / "campana.json"
    data = json.loads(info.read_text())
    info.write_text(json.dumps({**data, "followup": {"days": 5, "body": ["{{Saludo}} ¿lo viste?"]}}))
    library.save(tmp_path, slug, {"sender": {"name": "Rubén Rivera", "reply_to": "ruben@dindustrimex.com"}})
    zipped, name = library.export_zip(tmp_path, slug)
    assert name == f"{slug}.zip"
    theirs = library.import_file(other, name, zipped)
    copy = library.load(library.folder(other, theirs))
    assert copy["sender"]["name"] == "Rubén Rivera" and copy["followup"]["days"] == 5
    assert copy["html"] == library.load(library.folder(tmp_path, slug))["html"]
    assert [i["name"] for i in library.images(copy["_folder"])] == ["Logo-Rojo.png"]
    # each Mac keeps its own: editing one doesn't touch the other
    library.save(other, theirs, {"subject": "Otro asunto"})
    assert library.load(library.folder(tmp_path, slug))["subject"] == "Promo ARO"
    # a copy never inherits a send log, and a slug that had one is never reused
    (tmp_path / "mailing" / "promo-aro-copia").mkdir(parents=True)
    dup = library.duplicate(tmp_path, slug)
    assert dup == "promo-aro-copia-2" and library.load(library.folder(tmp_path, dup))["followup"]


@pytest.mark.parametrize("slug", ["../x", "qc-aro/../../etc", "", "A B"])
def test_bad_names_are_refused(tmp_path, slug):
    library.ensure(tmp_path)
    with pytest.raises(ValueError):
        library.folder(tmp_path, slug)
    assert library.safe_name("../../secret.png") == "secret.png"
    with pytest.raises(ValueError):
        library.add_image(tmp_path, "qc-aro", "virus.exe", b"x")


def test_zip_without_html_or_too_big(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="html"):
        library.import_file(tmp_path, "x.zip", _zip({"a.png": PNG}))
    monkeypatch.setattr(library, "MAX_IMPORT", 10)
    with pytest.raises(ValueError, match="grande"):
        library.import_file(tmp_path, "x.zip", _zip({"a.html": PAGE}))


def _app(tmp_path, monkeypatch, smtp):
    from mailing import app as mailing_app
    env = tmp_path / ".env"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\nIMAP_PASSWORD=x\n")
    write_contacts(tmp_path / "contacts.csv", [{"email": f"p{i}@cliente.mx", "name": f"Persona{i} X",
                                                "source": "signature", "company": "Cliente"} for i in range(3)])
    monkeypatch.setattr(send, "load_senders", lambda addrs, campaign, env, limit=None: (
        [_sender(smtp, a, daily_limit=limit or 1500) for a in addrs], None))
    return mailing_app, env


def test_campaign_tab_sends_the_active_library_campaign(tmp_path, monkeypatch):
    from tests.test_mailing import _wait
    smtp = FakeSMTP()
    mailing_app, env = _app(tmp_path, monkeypatch, smtp)
    slug = library.create(tmp_path, "Diciembre", "Feliz diciembre", "<p>{{Saludo}} de {{Empresa}}</p>")
    library.set_active(tmp_path, slug)
    info = mailing_app.overview(tmp_path, env, mailing_app.SendRun(), "")
    assert info["campaign"] == slug and info["subject"] == "Feliz diciembre" and info["plan"]["pending"] == 3
    run = mailing_app.SendRun(check=lambda *a: ([], []))
    run.start(tmp_path, env, {"confirm": "ENVIAR"}, pause=0)
    assert _wait(run)["sent"] == 3
    assert {m["Subject"] for m in smtp.sent} == {"Feliz diciembre"}
    assert "Hola Persona0, de Cliente" in smtp.sent[0].get_body(("html",)).get_content()
    assert (tmp_path / "mailing" / slug / "envios.csv").exists()
    # the built-in one keeps its own log and its own pending list
    assert mailing_app.overview(tmp_path, env, run, "qc-aro")["plan"]["pending"] == 3
    with pytest.raises(ValueError, match="Detén"):
        library.delete(tmp_path, slug, running=slug)


def test_test_send_uses_the_edited_html_and_campaign_images(tmp_path, monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(send, "load_senders", lambda addrs, campaign, env: ([_sender(smtp, addrs[0])], None))
    slug = library.import_file(tmp_path, "p.zip", _zip({"index.html": PAGE, "Logo Rojo.png": PNG}))
    send.send_test_html('<p>{{Saludo}}</p><img src="Logo Rojo.png">', "Sin guardar", "ruben@test.com",
                        "pedro@dindustrimex.com", slug, out_dir=tmp_path, name="Rubén")
    msg = smtp.sent[0]
    assert msg["Subject"] == "[PRUEBA] Sin guardar" and msg["Reply-To"] == "ventas@dindustrimex.com"
    html = msg.get_body(("html",)).get_content()
    assert "<p>Hola Rubén,</p>" in html and 'src="cid:Logo-Rojo.png"' in html
    assert any(p.get("Content-ID") == "<Logo-Rojo.png>" for p in msg.walk())


def test_dashboard_library_endpoints(tmp_path, monkeypatch):
    from mail_contact_scraper import dashboard

    smtp = FakeSMTP()
    _, env = _app(tmp_path, monkeypatch, smtp)
    server, url = dashboard.serve(tmp_path, port=18995, open_browser=False, env_path=env)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body, headers={"X-Requested-With": "dashboard"}):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST", headers=headers)
        return json.loads(urllib.request.urlopen(req).read())
    get = lambda path: urllib.request.urlopen(url + path).read()
    try:
        panel = json.loads(get("mailing"))
        assert panel["active"] == "qc-aro" and panel["campaigns"][0]["name"].startswith("Q-Pumps")
        new = post("mailing/library/new", {"name": "Noviembre", "subject": "Hola", "html": "<p>{{Saludo}}</p>"})
        slug = new["slug"]
        assert slug == "noviembre" and new["active"] == "qc-aro"
        added = post("mailing/library/image", {"campaign": slug, "files": [
            {"name": "foto.png", "data": base64.b64encode(PNG).decode()}]})
        assert added["added"] == ["foto.png"]
        assert get(f"mailing/library/img?campaign={slug}&name=foto.png") == PNG
        saved = post("mailing/library/save", {"campaign": slug, "html": '<img src="foto.png"><p>{{Saludo}}</p>'})
        assert saved["campaign"]["checks"] == ["No tenía liga de baja: se agrega una al final del correo ({{Baja}})."]
        prev = post("mailing/library/preview", {"campaign": slug, "html": '<img src="foto.png">{{Saludo}}', "name": "Ana"})
        assert f'/mailing/library/img?campaign={slug}&name=foto.png' in prev["html"] and "Hola Ana," in prev["html"]
        big = base64.b64encode(_zip({"index.html": PAGE + " " * 300_000})).decode()  # larger than settings bodies
        imported = post("mailing/library/import", {"filename": "promo.zip", "data": big})
        assert imported["slug"] == "promo-aro"
        assert post("mailing/library/active", {"campaign": slug})["active"] == slug
        assert json.loads(get("mailing/campaign"))["campaign"] == slug
        zipped = zipfile.ZipFile(io.BytesIO(get(f"mailing/library/export?campaign={slug}")))
        assert sorted(zipped.namelist()) == ["campana.json", "imagenes/foto.png", "mailing.html"]
        after = post("mailing/library/delete", {"campaign": slug})
        assert slug not in [c["slug"] for c in after["campaigns"]] and after["active"] != slug
        with pytest.raises(urllib.error.HTTPError) as err:
            post("mailing/library/save", {"campaign": "../qc-aro", "html": "x"})
        assert err.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as err:  # other websites can't change campaigns
            post("mailing/library/delete", {"campaign": "qc-aro"}, headers={})
        assert err.value.code == 403
        assert smtp.sent == []
    finally:
        server.shutdown()
