import csv
import io
import shutil
from pathlib import Path

import pytest

from mail_contact_scraper import cli, imap_source
from mail_contact_scraper.extract import find_phones, parse_signature
from mail_contact_scraper.signature import extract_signature

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return cli.analyze((FIXTURES / name).read_bytes(), "MX")


def test_spanish_signature_ignores_body_phone_and_quoted_reply():
    rec = load("01_plain_es.eml")
    c = rec["contact"]
    assert c["name"] == "Laura Martínez"
    assert c["title"] == "Gerente de Ventas"
    assert c["company"] == "Aceros La Laguna S.A. de C.V."
    phones = {p[0]: p[1] for p in c["phones"]}
    assert phones == {"+528717123344 ext 210": "office", "+528715550199": "mobile"}
    assert "AVISO" not in rec["signature"]
    assert "Pedro" not in rec["signature"]


def test_html_signature_uses_us_region_from_explicit_country_code():
    c = load("02_html_en.eml")["contact"]
    assert c["company"] == "Midwest Valves Inc."
    assert {p[0] for p in c["phones"]} == {"+13125550142", "+13125550187"}
    assert c["websites"] == ["www.midwestvalves.com"]


def test_dash_delimiter_and_outlook_quote():
    rec = load("03_dashdash_outlook.eml")
    assert rec["signature"].startswith("Ing. José Hernández")
    assert "Enviado" not in rec["signature"]
    assert rec["contact"]["company"] == ""  # gmail.com is not a company
    assert rec["contact"]["phones"][0][:2] == ("+523319876543", "mobile")


def test_one_line_body_is_not_a_signature():
    assert load("04_no_signature.eml")["signature"] == ""


def test_sign_off_must_be_short():
    body = "Gracias por tu correo de ayer, lo reviso.\nNos vemos."
    assert extract_signature(body) == ""


def test_phone_formats():
    found = [p[0] for p in find_phones("Tel. (55) 5123-4567 / 01 800 123 4567\nFax: +52 81 8333 2211", "MX")]
    assert "+525551234567" in found
    assert "+528183332211" in found


def test_company_from_domain_when_missing():
    c = parse_signature("Ana López\nTel 55 1234 5678", "", "ana@grupo-ferrex.com.mx")
    assert c["company"] == "Grupo Ferrex"


def test_local_run_writes_merged_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    assert cli.main(["--local", str(FIXTURES), "--output", str(tmp_path), "--fresh"]) == 0
    rows = {r["email"]: r for r in csv.DictReader(open(tmp_path / "contacts.csv", encoding="utf-8-sig"))}
    assert "pedro@dindustrimex.com" not in rows
    assert not any(e.startswith("noreply") for e in rows)
    laura = rows["lmartinez@aceroslaguna.com.mx"]
    assert laura["messages"] == "2"
    assert "+528717123399" in laura["office_phones"]
    assert rows["compras@aceroslaguna.com.mx"]["source"] == "header"


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for iter_messages / list_folders."""

    def __init__(self, messages):
        self.messages = messages  # uid -> bytes
        self.fetched = []

    def select(self, folder, readonly=False):
        assert readonly, "must open folders read-only"
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"777"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        uids, query = args
        assert "PEEK" in query, "must not mark messages as read"
        self.fetched.append(uids)
        data = []
        for uid in uids.split(","):
            data += [(b"1 (UID " + uid.encode() + b" BODY[] {10}", self.messages[uid.encode()]), b")"]
        return "OK", data

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "Elementos enviados"',
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\HasNoChildren) "." Archive',
        ]


def test_imap_batches_skip_done_and_peek():
    raw = (FIXTURES / "05_repeat_laura.eml").read_bytes()
    conn = FakeIMAP({str(i).encode(): raw for i in range(1, 6)})
    got = list(imap_source.iter_messages(conn, "INBOX", batch_size=2, delay=0, skip={("INBOX", "777", "1")}))
    assert [g[2] for g in got] == ["2", "3", "4", "5"]
    assert conn.fetched == ["2,3", "4,5"]


def test_list_folders():
    assert imap_source.list_folders(FakeIMAP({})) == ["INBOX", "Elementos enviados", "Archive"]


def _signature_png():
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("DejaVuSans.ttf", 15) if _has_dejavu() else ImageFont.load_default()
    img = Image.new("RGB", (420, 110), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 70, 110], fill=(20, 60, 140))  # logo block
    lines = ["Roberto Salinas", "Jefe de Compras", "Tel: +52 81 8340 1122", "rsalinas@plasticosnorte.mx"]
    for i, line in enumerate(lines):
        draw.text((85, 8 + i * 25), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _has_dejavu():
    from PIL import ImageFont
    try:
        ImageFont.truetype("DejaVuSans.ttf", 10)
        return True
    except OSError:
        return False


def _image_signature_eml(png):
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("related")
    msg["From"] = "Roberto Salinas <rsalinas@plasticosnorte.mx>"
    msg["To"] = "pedro@dindustrimex.com"
    msg["Subject"] = "Pedido de resina"
    msg["Date"] = "Tue, 20 Oct 2026 10:00:00 -0600"
    html = (
        "<p>Pedro, confirmo el pedido.</p><p>Saludos,</p><img src='cid:firma001'>"
        "<div class='gmail_quote'>El lun escribió:<img src='cid:other'></div>"
    )
    msg.attach(MIMEText(html, "html", "utf-8"))
    image = MIMEImage(png, "png")
    image.add_header("Content-ID", "<firma001>")
    image.add_header("Content-Disposition", "inline", filename="firma.png")
    msg.attach(image)
    return msg.as_bytes()


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_image_signature_is_ocrd():
    from mail_contact_scraper.ocr import SignatureOCR

    rec = cli.analyze(_image_signature_eml(_signature_png()), "MX", SignatureOCR())
    assert rec["signature_from_image"]
    c = rec["contact"]
    assert [p[0] for p in c["phones"]] == ["+528183401122"]
    assert "rsalinas@plasticosnorte.mx" in c["emails"]
    assert "Compras" in c["title"]


def test_image_tokens_dropped_without_ocr():
    rec = cli.analyze(_image_signature_eml(b"not really a png"), "MX", None)
    assert "[[IMG" not in rec["signature"]
    assert not rec["signature_from_image"]


def test_status_and_csv_written_during_run(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    monkeypatch.setenv("BATCH_SIZE", "2")
    snapshots = []
    real = cli.write_outputs

    def spy(records, settings, out_dir, status, running=True):
        count = real(records, settings, out_dir, status, running)
        snapshots.append(json.loads((out_dir / "status.json").read_text()))
        return count

    monkeypatch.setattr(cli, "write_outputs", spy)
    cli.main(["--local", str(FIXTURES), "--output", str(tmp_path), "--fresh", "--no-ocr"])
    assert [s["running"] for s in snapshots] == [True, True, True, False]
    assert snapshots[-1]["messages_total"] == 5
    assert (tmp_path / "contacts.csv").exists()


def test_dashboard_data_endpoint(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.request

    from mail_contact_scraper import dashboard

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    cli.main(["--local", str(FIXTURES), "--output", str(tmp_path), "--fresh", "--no-ocr"])
    server, url = dashboard.serve(tmp_path, port=8790, open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert "Contactos de clientes".encode() in urllib.request.urlopen(url).read()
        data = json.loads(urllib.request.urlopen(url + "data").read())
        assert data["status"]["running"] is False
        assert len(data["contacts"]) == 4
        again = json.loads(urllib.request.urlopen(f"{url}data?since={data['version']}").read())
        assert "contacts" not in again  # unchanged CSV is not resent
    finally:
        server.shutdown()


def test_own_domain_contacts_are_excluded(tmp_path, monkeypatch):
    from mail_contact_scraper.config import load_settings

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    monkeypatch.delenv("EXCLUDE_DOMAINS", raising=False)
    assert load_settings(env_file="missing.env").exclude_domains == ["dindustrimex.com"]
    coworker = (FIXTURES / "05_repeat_laura.eml").read_bytes().replace(
        b"lmartinez@aceroslaguna.com.mx", b"ventas@mx.dindustrimex.com")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "coworker.eml").write_bytes(coworker)
    (tmp_path / "in" / "laura.eml").write_bytes((FIXTURES / "01_plain_es.eml").read_bytes())
    cli.main(["--local", str(tmp_path / "in"), "--output", str(tmp_path / "out"), "--no-ocr"])
    rows = list(csv.DictReader(open(tmp_path / "out" / "contacts.csv", encoding="utf-8-sig")))
    emails = {r["email"] for r in rows}
    assert not any("dindustrimex.com" in e for e in emails)
    assert "lmartinez@aceroslaguna.com.mx" in emails

    monkeypatch.setenv("EXCLUDE_DOMAINS", "none")
    assert load_settings(env_file="missing.env").exclude_domains == []


def test_dashboard_start_button_runs_scraper(tmp_path, monkeypatch):
    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    from mail_contact_scraper import dashboard

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    control = dashboard.ScraperControl(tmp_path, ["--local", str(FIXTURES), "--no-ocr", "--fresh"])
    assert "--fresh" not in control.scrape_args  # the button resumes, never wipes
    control.scrape_args = control.first_args
    server, url = dashboard.serve(tmp_path, port=8800, open_browser=False, control=control)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Without the custom header (e.g. a form on another website) the button is refused.
        try:
            urllib.request.urlopen(urllib.request.Request(url + "start", method="POST"))
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        req = urllib.request.Request(url + "start", method="POST", headers={"X-Requested-With": "dashboard"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        control.proc.wait(timeout=60)
        for _ in range(20):
            data = json.loads(urllib.request.urlopen(url + "data").read())
            if not data["control"]["alive"]:
                break
            time.sleep(0.2)
        assert data["status"]["running"] is False
        assert len(data["contacts"]) == 4
    finally:
        server.shutdown()


def test_extra_mailboxes_from_env(monkeypatch):
    from mail_contact_scraper.config import load_settings

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER_2", "ventas@dindustrimex.com")
    monkeypatch.setenv("IMAP_PASSWORD_2", "abcd efgh")
    monkeypatch.setenv("IMAP_USER_3", "PEDRO@dindustrimex.com")  # duplicate, ignored
    monkeypatch.setenv("IMAP_USER_5", "compras@grupoaliado.mx")
    monkeypatch.setenv("IMAP_HOST_5", "imap.zoho.com")
    monkeypatch.setenv("IMAP_FOLDERS_5", "INBOX,Archivo")
    monkeypatch.delenv("EXCLUDE_DOMAINS", raising=False)
    s = load_settings(env_file="missing.env")
    assert [b.email for b in s.mailboxes] == ["pedro@dindustrimex.com", "ventas@dindustrimex.com",
                                              "compras@grupoaliado.mx"]
    assert s.mailboxes[1].password == "abcd efgh" and s.mailboxes[1].imap_host == "imap.gmail.com"
    assert s.mailboxes[2].imap_host == "imap.zoho.com" and s.mailboxes[2].folders == ["INBOX", "Archivo"]
    assert s.exclude_domains == ["dindustrimex.com", "grupoaliado.mx"]


def test_scans_every_mailbox_and_skips_a_failing_one(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER_2", "ventas@dindustrimex.com")
    monkeypatch.setenv("IMAP_USER_3", "malo@dindustrimex.com")
    boxes = {
        "pedro@dindustrimex.com": {b"1": (FIXTURES / "01_plain_es.eml").read_bytes(),
                                   b"2": (FIXTURES / "05_repeat_laura.eml").read_bytes()},
        "ventas@dindustrimex.com": {b"1": (FIXTURES / "05_repeat_laura.eml").read_bytes()},
    }
    connected = []

    def fake_connect(box):
        connected.append(box.email)
        if box.email not in boxes:
            raise imap_source.imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")
        conn = FakeIMAP(boxes[box.email])
        conn.logout = lambda: None
        return conn

    monkeypatch.setattr(imap_source, "connect", fake_connect)
    # A record from before multi-mailbox support belongs to the main mailbox.
    tmp_path.joinpath("messages.jsonl").write_text(json.dumps(
        {**cli.analyze(boxes["pedro@dindustrimex.com"][b"1"]), "folder": "INBOX", "uidvalidity": "777", "uid": "1"}) + "\n")
    assert cli.main(["--output", str(tmp_path), "--no-ocr"]) == 0
    assert connected == ["pedro@dindustrimex.com", "ventas@dindustrimex.com", "malo@dindustrimex.com"]
    recs = [json.loads(l) for l in tmp_path.joinpath("messages.jsonl").read_text().splitlines()]
    assert [(r.get("mailbox"), r["uid"]) for r in recs[1:]] == [
        ("pedro@dindustrimex.com", "2"), ("ventas@dindustrimex.com", "1")]
    status = json.loads(tmp_path.joinpath("status.json").read_text())
    assert [b["state"] for b in status["mailboxes"]] == ["done", "done", "error"]
    assert "AUTHENTICATIONFAILED" in status["mailboxes"][2]["error"] and "error" not in status
    rows = {r["email"]: r for r in csv.DictReader(open(tmp_path / "contacts.csv", encoding="utf-8-sig"))}
    assert rows["lmartinez@aceroslaguna.com.mx"]["mailboxes"] == "pedro@dindustrimex.com; ventas@dindustrimex.com"

    # Only one mailbox, and everything there is already read.
    connected.clear()
    assert cli.main(["--output", str(tmp_path), "--no-ocr", "--mailbox", "VENTAS@dindustrimex.com"]) == 0
    assert connected == ["ventas@dindustrimex.com"]
    assert len(tmp_path.joinpath("messages.jsonl").read_text().splitlines()) == 3


def test_all_mailboxes_failing_is_an_error(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    monkeypatch.setenv("IMAP_HOST", "imap.test")

    def fail(box):
        raise imap_source.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imap_source, "connect", fail)
    with pytest.raises(RuntimeError):
        cli.main(["--output", str(tmp_path), "--no-ocr"])
    assert json.loads(tmp_path.joinpath("status.json").read_text())["error"] == "AUTHENTICATIONFAILED"
