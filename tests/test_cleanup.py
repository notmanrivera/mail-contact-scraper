"""Leaving out what is not a prospect: services, mass mailings, ignored senders, bounces."""
import csv
import json
import threading
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from mail_contact_scraper import cli, dashboard
from mail_contact_scraper.contacts import ContactBook, read_ignore_file, update_ignore_file
from mail_contact_scraper.parse import parse_message

SIG = "Saludos,\nLaura Martínez\nAceros La Laguna S.A. de C.V.\nTel: +52 871 712 3399\n"


def mail(sender, body=SIG, date="Mon, 06 Oct 2025 10:00:00 -0600", **headers):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "pedro@dindustrimex.com"
    msg["Subject"] = "Hola"
    msg["Date"] = date
    msg["Message-ID"] = f"<{abs(hash((sender, body, date)))}@x>"
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content("Buen día, les escribo por la cotización.\n\n" + body)
    return msg


def dsn(failed, date="Tue, 07 Oct 2025 10:00:00 -0600"):
    msg = EmailMessage()
    msg["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    msg["To"] = "pedro@dindustrimex.com"
    msg["Subject"] = "Delivery Status Notification (Failure)"
    msg["Date"] = date
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(f"No se encontró la dirección {failed}.")
    report = EmailMessage()
    report.set_content(f"Reporting-MTA: dns; googlemail.com\n\nFinal-Recipient: rfc822; {failed}\n"
                       "Action: failed\nStatus: 5.1.1\n")
    msg.make_mixed()
    msg.attach(_delivery_status(failed))
    return msg


def _delivery_status(failed):
    from email.mime.base import MIMEBase
    from email import message_from_string

    part = MIMEBase("message", "delivery-status")
    part.set_payload([message_from_string("Reporting-MTA: dns; googlemail.com\n"),
                      message_from_string(f"Final-Recipient: rfc822; {failed}\nAction: failed\nStatus: 5.1.1\n")])
    return part


def test_detects_mass_mailings_but_not_people():
    assert parse_message(mail("promo@aceros.mx", List_Unsubscribe_Post="List-Unsubscribe=One-Click").as_bytes())["bulk"]
    assert parse_message(mail("a@b.mx", Precedence="bulk").as_bytes())["bulk"]
    # A Google Group (ventas@...) adds List-Id and List-Unsubscribe; that is still a person writing.
    group = mail("laura@aceroslaguna.com.mx", List_Id="<ventas.dindustrimex.com>",
                 List_Unsubscribe="<mailto:googlegroups-manage+1@googlegroups.com>", Precedence="list")
    assert parse_message(group.as_bytes())["bulk"] == ""


def test_reads_bounce_reports():
    rec = parse_message(dsn("juan@viejo.mx").as_bytes())
    assert rec["bounced"] == ["juan@viejo.mx"]


def _record(sender, date="2025-10-06T16:00:00", **extra):
    name, _, addr = sender.partition(" <")
    return {"from_email": addr.rstrip(">") if addr else sender, "date": date, "signature": SIG, "bulk": "",
            "contact": {"name": "", "title": "", "company": "", "phones": [], "emails": [], "websites": []},
            **extra}


def test_contact_book_leaves_out_services_bulk_and_ignored():
    book = ContactBook(ignore=["proveedor-spam.mx", "juan@aceros.mx"])
    for addr in ("noreply@uber.com", "recibos@ubereats.com.mx", "avisos@bbva.mx", "hola@rappi.com",
                 "ventas@proveedor-spam.mx", "juan@aceros.mx", "laura@aceroslaguna.com.mx",
                 "compras@amazon.com.mx", "notificaciones@cliente.mx"):
        book.add_sender(_record(addr))
    book.add_sender(_record("eventos@aceroslaguna.com.mx", bulk="list-unsubscribe"))
    emails = [r["email"] for r in book.to_rows()]
    # Amazon is not on the built-in list on purpose: big companies can also be clients.
    assert emails == ["compras@amazon.com.mx", "laura@aceroslaguna.com.mx"]
    assert book.omitted_counts() == {"service": 4, "ignored": 2, "automated": 1, "bulk": 1}


def test_bounce_is_cleared_when_they_write_again():
    book = ContactBook()
    book.add_sender(_record("laura@aceroslaguna.com.mx", date="2025-10-01T10:00:00"))
    book.add_sender(_record("juan@viejo.mx", date="2025-09-01T10:00:00"))
    book.add_sender({**_record("mailer-daemon@googlemail.com", date="2025-10-02T10:00:00"),
                     "bounced": ["laura@aceroslaguna.com.mx", "juan@viejo.mx"]})
    book.add_sender(_record("laura@aceroslaguna.com.mx", date="2025-10-05T10:00:00"))
    rows = {r["email"]: r for r in book.to_rows()}
    assert rows["juan@viejo.mx"]["bounced"] == "2025-10-02T10:00:00"
    assert rows["laura@aceroslaguna.com.mx"]["bounced"] == ""


def test_ignore_file_round_trip(tmp_path):
    path = tmp_path / "ignorados.txt"
    update_ignore_file(path, add="@Uber.com")
    update_ignore_file(path, add="juan@x.mx")
    update_ignore_file(path, add="uber.com")
    assert read_ignore_file(path) == ["uber.com", "juan@x.mx"]
    update_ignore_file(path, remove="uber.com")
    assert read_ignore_file(path) == ["juan@x.mx"]
    assert path.read_text().startswith("#")


def test_dashboard_ignore_button_rebuilds_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    (tmp_path / "in").mkdir()
    for i, m in enumerate([mail("Laura <laura@aceroslaguna.com.mx>"), mail("Uber <recibos@uber.com>"),
                           mail("Rafa <rafa@ruido.mx>"), mail("Ana <ana@ruido.mx>"), dsn("juan@viejo.mx"),
                           mail("Juan <juan@viejo.mx>", date="Mon, 01 Sep 2025 10:00:00 -0600")]):
        (tmp_path / "in" / f"{i}.eml").write_bytes(m.as_bytes())
    cli.main(["--local", "in", "--output", "out", "--no-ocr"])
    read = lambda: {r["email"]: r for r in csv.DictReader(open(tmp_path / "out" / "contacts.csv", encoding="utf-8-sig"))}
    rows = read()
    assert "recibos@uber.com" not in rows and "rafa@ruido.mx" in rows
    assert rows["juan@viejo.mx"]["bounced"]
    status = json.loads((tmp_path / "out" / "status.json").read_text())
    assert status["omitted"]["service"] == 1

    server, url = dashboard.serve(tmp_path / "out", port=8797, open_browser=False, env_path=tmp_path / ".env")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(url + "settings/ignore", data=json.dumps({"add": "ruido.mx"}).encode(),
                                     method="POST", headers={"X-Requested-With": "dashboard"})
        data = json.loads(urllib.request.urlopen(req).read())
        assert data["ignored"] == ["ruido.mx"]
    finally:
        server.shutdown()
    rows = read()
    assert "rafa@ruido.mx" not in rows and "ana@ruido.mx" not in rows and "laura@aceroslaguna.com.mx" in rows
    assert json.loads((tmp_path / "out" / "status.json").read_text())["omitted"]["ignored"] == 2


def test_omitted_list_says_why_and_allow_wins():
    book = ContactBook(ignore=["ruido.mx"], allow=["banorte.com"])
    book.add_sender(_record("recibos@uber.com", date="2025-10-01T10:00:00"))
    book.add_sender(_record("recibos@uber.com", date="2025-10-03T10:00:00"))
    book.add_sender(_record("rafa@ruido.mx"))
    book.add_sender(_record("noreply@aceros.mx"))
    book.add_sender({**_record("ventas@aceros.mx"), "bulk": "list-unsubscribe"})
    book.add_sender(_record("compras@banorte.com"))             # a bank in services.txt, but a client
    book.add_sender({**_record("boletin@banorte.com"), "bulk": "esp"})
    by = {o["email"]: o for o in book.omitted_list()}
    assert by["recibos@uber.com"] == {"email": "recibos@uber.com", "reason": "service", "rule": "uber.*",
                                      "seen": 2, "last_seen": "2025-10-03T10:00:00", "name": ""}
    assert (by["rafa@ruido.mx"]["reason"], by["rafa@ruido.mx"]["rule"]) == ("ignored", "ruido.mx")
    assert (by["noreply@aceros.mx"]["reason"], by["noreply@aceros.mx"]["rule"]) == ("automated", "noreply@")
    assert (by["ventas@aceros.mx"]["reason"], by["ventas@aceros.mx"]["rule"]) == ("bulk", "list-unsubscribe")
    assert {r["email"] for r in book.to_rows()} == {"compras@banorte.com", "boletin@banorte.com"}
    assert list(by)[0] == "recibos@uber.com"  # most seen first


def test_full_ignored_window(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMAP_USER", "pedro@dindustrimex.com")
    (tmp_path / "in").mkdir()
    for i, m in enumerate([mail("Laura <laura@aceroslaguna.com.mx>"), mail("Uber <recibos@uber.com>"),
                           mail("Rafa <rafa@ruido.mx>"), mail("Ana <ana@ruido.mx>")]):
        (tmp_path / "in" / f"{i}.eml").write_bytes(m.as_bytes())
    cli.main(["--local", "in", "--output", "out", "--no-ocr"])
    out, env = tmp_path / "out", tmp_path / ".env"
    (out / "suppression.txt").write_text("# baja 2026-10-20\nx@cliente.mx\n# rebote 2026-10-21\ny@viejo.mx\n")

    code, data = dashboard.settings_action("/settings/ignore", {"add": "ruido.mx, @Spam.com\njuan@x.mx"}, out, env)
    assert code == 200 and data["added"] == ["ruido.mx", "spam.com", "juan@x.mx"]
    lists = data["lists"]
    assert lists["ignored"][0] == {"value": "ruido.mx", "hides": 2}
    assert {o["email"]: o["reason"] for o in lists["omitted"]} == {
        "recibos@uber.com": "service", "rafa@ruido.mx": "ignored", "ana@ruido.mx": "ignored"}
    assert lists["suppression"] == [{"email": "x@cliente.mx", "reason": "baja", "date": "2026-10-20"},
                                    {"email": "y@viejo.mx", "reason": "rebote", "date": "2026-10-21"}]
    assert dashboard.settings_action("/settings/ignore", {"add": "uber.com, no es dominio"}, out, env)[0] == 400

    # "Permitir" from the omitted list brings Uber back, and takes a value out of the other list.
    code, data = dashboard.settings_action("/settings/allow", {"add": "recibos@uber.com ruido.mx"}, out, env)
    assert data["lists"]["allowed"] == ["recibos@uber.com", "ruido.mx"]
    assert [i["value"] for i in data["lists"]["ignored"]] == ["spam.com", "juan@x.mx"]
    rows = {r["email"] for r in csv.DictReader(open(out / "contacts.csv", encoding="utf-8-sig"))}
    assert {"recibos@uber.com", "rafa@ruido.mx", "laura@aceroslaguna.com.mx"} <= rows
    assert dashboard.ignored_payload(out, env)["omitted"] == []
