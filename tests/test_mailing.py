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
    assert skipped == {"invalid": 1, "internal": 1, "suppressed": 1, "bounced": 0, "no_signature": 0}

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


def test_bounced_contacts_get_no_mailing(tmp_path):
    contacts = tmp_path / "contacts.csv"
    with open(contacts, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS + ["bounced"])
        w.writeheader()
        w.writerows([{"email": "a@x.com", "name": "Ana", "source": "signature", "bounced": ""},
                     {"email": "b@x.com", "name": "Beto", "source": "signature", "bounced": "2025-10-02T10:00:00"}])
    people, skipped = build(CAMPAIGN, contacts, tmp_path / "out")
    assert [p["email"] for p in people] == ["a@x.com"]
    assert skipped["bounced"] == 1


class FakeSMTP:
    def __init__(self, refuse=(), fail_after=None):
        self.sent, self.refuse, self.fail_after = [], set(refuse), fail_after

    def send_message(self, msg):
        import smtplib
        to = msg["To"].split("<")[-1].rstrip(">")
        if to in self.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise smtplib.SMTPDataError(550, b"Daily user sending limit exceeded")
        self.sent.append(msg)

    def quit(self):
        pass


def _sender(smtp, email="pedro@dindustrimex.com", daily_limit=1500):
    return {"email": email, "name": "Pedro Marquina", "host": "smtp.test", "daily_limit": daily_limit,
            "connect": lambda: smtp}


def _send(tmp_path, smtp, senders=None, daily_limit=1500, **kw):
    from mailing.send import send_campaign
    contacts = tmp_path / "contacts.csv"
    if not contacts.exists():
        write_contacts(contacts, [{"email": f"p{i}@cliente.mx", "name": f"Persona{i} X", "source": "signature"}
                                  for i in range(5)])
    return send_campaign(CAMPAIGN, contacts, tmp_path / "mailing" / "qc-aro",
                         senders=senders or [_sender(smtp, daily_limit=daily_limit)],
                         sleep=lambda s: None, say=lambda *a: None,
                         suppression_path=tmp_path / "suppression.txt", **kw)


def test_send_dry_run_sends_nothing(tmp_path):
    smtp = FakeSMTP()
    plan = _send(tmp_path, smtp)
    assert plan["pending"] == 5 and plan["this_run"] == 5 and smtp.sent == []


def test_send_logs_and_never_repeats(tmp_path):
    from mailing.send import read_log
    smtp = FakeSMTP()
    first = _send(tmp_path, smtp, send=True, limit=3)
    assert first["sent"] == 3
    msg = smtp.sent[0]
    assert msg["From"] == "Pedro Marquina <pedro@dindustrimex.com>" and "X-Unsent" not in msg
    assert msg["List-Unsubscribe"].startswith("<mailto:ventas@dindustrimex.com")
    assert "Hola Persona0," in msg.get_body(("html",)).get_content()
    second = _send(tmp_path, smtp, send=True, limit=10)
    assert second["sent"] == 2 and second["already_sent"] == 3
    assert len({m["To"] for m in smtp.sent}) == 5
    assert len(read_log(tmp_path / "mailing" / "qc-aro" / "envios.csv")) == 5


def test_send_respects_daily_limit_across_runs(tmp_path):
    smtp = FakeSMTP()
    _send(tmp_path, smtp, send=True, daily_limit=2)
    again = _send(tmp_path, smtp, send=True, daily_limit=2)
    assert len(smtp.sent) == 2 and again["this_run"] == 0


def test_rejected_go_to_suppression_and_limit_stops(tmp_path):
    from mailing.send import read_suppression
    smtp = FakeSMTP(refuse={"p1@cliente.mx"}, fail_after=2)
    result = _send(tmp_path, smtp, send=True)
    assert result["rejected"] == ["p1@cliente.mx"] and result["sent"] == 2
    assert "Daily user sending limit" in result["stopped"]
    assert "p1@cliente.mx" in read_suppression(tmp_path / "suppression.txt")


def test_test_send_goes_only_to_tester(tmp_path):
    smtp = FakeSMTP()
    result = _send(tmp_path, smtp, test_to="ruben@ejemplo.com")
    assert result["sent"] == 1 and smtp.sent[0]["To"] == "ruben@ejemplo.com"
    assert smtp.sent[0]["Subject"].startswith("[PRUEBA] ")
    assert not (tmp_path / "mailing" / "qc-aro" / "envios.csv").exists()


def test_sync_suppression_reads_unsubscribes_and_bounces(tmp_path):
    from mailing.send import read_suppression, sync_suppression
    unsub = (b"From: Ana <ana@cliente.mx>\r\nSubject: Baja de correos\r\nContent-Type: text/plain\r\n\r\n"
             b"ana.vieja@cliente.mx\r\n")
    bounce = (b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nX-Failed-Recipients: nadie@cliente.mx\r\n"
              b"Subject: Delivery Status Notification (Failure)\r\n\r\nNo existe\r\n")

    class FakeIMAP:
        def select(self, *a, **k):
            return "OK", [b"2"]

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                crit = args[-1]
                return "OK", [b"1" if "Baja" in crit else b"2" if "mailer-daemon" in crit else b""]
            return "OK", [(b"1 (BODY[] {10}", unsub if args[0] == b"1" else bounce)]

        def logout(self):
            pass

    supp = tmp_path / "suppression.txt"
    unsubscribed, bounced = sync_suppression([object()], supp, lambda box: FakeIMAP(),
                                             own_domains=["dindustrimex.com"])
    assert set(unsubscribed) == {"ana@cliente.mx", "ana.vieja@cliente.mx"} and bounced == ["nadie@cliente.mx"]
    assert read_suppression(supp) == {"ana@cliente.mx", "ana.vieja@cliente.mx", "nadie@cliente.mx"}
    assert sync_suppression([object()], supp, lambda box: FakeIMAP()) == ([], [])


def test_senders_take_turns_and_one_hitting_its_limit_hands_over(tmp_path):
    from mailing.send import read_log
    pedro, ventas = FakeSMTP(fail_after=1), FakeSMTP()
    result = _send(tmp_path, None, send=True, senders=[
        _sender(pedro), _sender(ventas, "ventas@dindustrimex.com")])
    assert result["sent"] == 5 and len(pedro.sent) == 1 and len(ventas.sent) == 4
    assert ventas.sent[0]["From"] == "Pedro Marquina <ventas@dindustrimex.com>"
    assert {m["To"] for m in pedro.sent + ventas.sent} == {f"Persona{i} X <p{i}@cliente.mx>" for i in range(5)}
    rows = read_log(tmp_path / "mailing" / "qc-aro" / "envios.csv")
    assert [r["remitente"] for r in rows if r["estado"] == "enviado"].count("ventas@dindustrimex.com") == 4


def test_daily_limit_is_per_mailbox(tmp_path):
    a, b = FakeSMTP(), FakeSMTP()
    senders = lambda: [_sender(a, daily_limit=1), _sender(b, "ventas@dindustrimex.com", daily_limit=2)]
    first = _send(tmp_path, None, send=True, senders=senders())
    assert first["sent"] == 3 and len(a.sent) == 1 and len(b.sent) == 2
    again = _send(tmp_path, None, send=True, senders=senders())
    assert again["this_run"] == 0


def test_load_senders_uses_mailboxes_or_a_relay(tmp_path):
    from mailing.send import load_senders
    campaign = load_campaign(CAMPAIGN)
    env = tmp_path / ".env"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\nIMAP_PASSWORD=abcd\nIMAP_HOST=imap.gmail.com\n")
    senders, _ = load_senders(["pedro@dindustrimex.com"], campaign, env)
    assert senders[0]["host"] == "smtp.gmail.com" and senders[0]["daily_limit"] == 1500
    with pytest.raises(SystemExit, match="ventas@"):
        load_senders(["ventas@dindustrimex.com"], campaign, env)
    env.write_text(env.read_text() + "SMTP_HOST=email-smtp.us-east-1.amazonaws.com\nSMTP_USER=AKIA\n"
                   "SMTP_PASSWORD=secret\nSMTP_DAILY_LIMIT=20000\n")
    senders, _ = load_senders(["pedro@dindustrimex.com", "ventas@dindustrimex.com"], campaign, env)
    assert [s["daily_limit"] for s in senders] == [20000, 20000]
    assert senders[1]["host"] == "email-smtp.us-east-1.amazonaws.com"
