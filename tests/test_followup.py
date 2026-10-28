from datetime import date, datetime, timedelta
from email.message import EmailMessage

from mailing import replies, send
from mailing.build import load_audience, load_campaign
from tests.test_mailing import CAMPAIGN, FakeSMTP, _app_setup, _sender, _send, _wait, write_contacts

TODAY = date(2026, 11, 10)


def _msg(frm, subject, body, in_reply_to="", **headers):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = frm, subject, "Mon, 09 Nov 2026 10:00:00 -0600"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


# ---------- 9. per company and duplicates ----------

def test_same_person_with_two_addresses_is_sent_once(tmp_path):
    write_contacts(tmp_path / "c.csv", [
        {"email": "jperez@aceros.mx", "name": "Juan Pérez", "source": "signature"},
        {"email": "juan.perez@aceros.mx", "name": "juan perez", "source": "signature"},
        {"email": "juan@otra.mx", "name": "Juan Pérez", "source": "signature"},  # other company
        {"email": "ventas@aceros.mx", "name": "", "source": "signature"},
    ])
    people, skipped = load_audience(tmp_path / "c.csv", load_campaign(CAMPAIGN))
    assert [p["email"] for p in people] == ["jperez@aceros.mx", "juan@otra.mx", "ventas@aceros.mx"]
    assert skipped["duplicate"] == 1


def test_company_order_caps_and_mixes_companies():
    people = [{"email": e} for e in ("a@uno.mx", "b@uno.mx", "c@uno.mx", "d@dos.mx", "e@gmail.com", "f@gmail.com")]
    order, held = send.company_order(people, {"dos.mx": 1}, 2)
    assert [p["email"] for p in order] == ["a@uno.mx", "d@dos.mx", "e@gmail.com", "f@gmail.com", "b@uno.mx"]
    assert held == 1  # c@uno.mx waits for tomorrow
    assert send.company_order(people, {}, 0) == (people, 0)


def test_send_respects_the_per_company_cap(tmp_path):
    first = _send(tmp_path, FakeSMTP(), send=True, per_company=2)
    assert first["sent"] == 2 and first["held_by_company"] == 3 and first["pending"] == 5
    again = _send(tmp_path, FakeSMTP(), send=True, per_company=2)  # same day: cliente.mx is full
    assert again["sent"] == 0 and again["this_run"] == 0


# ---------- 6. replies, out of office, no longer works there ----------

def test_classify_replies():
    away = _msg("ana@cliente.mx", "Respuesta automática: Bombeo", "Estoy de vacaciones, regreso el 17 de noviembre.\n"
                "Para urgencias escribe a pedro.lopez@cliente.mx", Auto_Submitted="auto-replied")
    assert replies.classify(away, TODAY) == ("fuera-de-oficina", date(2026, 11, 17), ["pedro.lopez@cliente.mx"])
    gone = _msg("beto@cliente.mx", "RE: Bombeo", "Beto ya no labora en la empresa, favor de escribir a compras@cliente.mx")
    assert replies.classify(gone, TODAY) == ("ya-no-labora", None, ["compras@cliente.mx"])
    lead = _msg("caro@cliente.mx", "RE: Bombeo", "Me interesa, mándame precio de la QC.\n\n"
                "El lun, 2 nov 2026 a las 10:00, Pedro <pedro@dindustrimex.com> escribió:\n> ya no labora")
    assert replies.classify(lead, TODAY) == ("respondió", None, [])
    no_date = _msg("dan@cliente.mx", "Out of office", "I am out of the office.")
    assert replies.classify(no_date, TODAY)[1] == TODAY + timedelta(days=replies.AWAY_DEFAULT_DAYS)


def test_return_dates():
    assert replies.return_date("regreso el 03/12/2026", TODAY) == date(2026, 12, 3)
    assert replies.return_date("estaré de vuelta el 5 de enero", date(2026, 12, 20)) == date(2027, 1, 5)
    assert replies.return_date("a partir del 2 de dic. del 2026", TODAY) == date(2026, 12, 2)
    assert replies.return_date("tel 442 499 8283", TODAY) is None


class FakeIMAP:
    def __init__(self, messages):
        self.messages = {str(i + 1).encode(): m.as_bytes() for i, m in enumerate(messages)}

    def select(self, *a, **k):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        uids, what = args
        out = []
        for uid in uids.split(b","):
            raw = self.messages[uid]
            if b"HEADER" in what.encode():
                raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
            out += [(b"1 (UID " + uid + b" BODY[] {1}", raw), b")"]
        return "OK", out

    def logout(self):
        pass


class Box:
    email = "pedro@dindustrimex.com"


def _sync(tmp_path, messages, sent):
    out = tmp_path / "mailing" / "qc-aro"
    return replies.sync_replies([Box()], lambda b: FakeIMAP(messages), out, sent, tmp_path / "suppression.txt",
                                own_domains=["dindustrimex.com"], today=TODAY), out


def test_sync_replies_records_leads_away_and_referrals(tmp_path):
    sent = [("ana@cliente.mx", "<1@dindustrimex.com>"), ("beto@cliente.mx", "<2@dindustrimex.com>"),
            ("caro@cliente.mx", "<3@dindustrimex.com>")]
    messages = [
        _msg("Ana <ana@cliente.mx>", "Fuera de la oficina", "Regreso el 20/11/2026", Auto_Submitted="auto-replied"),
        _msg("recepcion@cliente.mx", "RE: Bombeo", "Beto ya no trabaja aquí... ya no labora con nosotros. "
             "Escribe a nuevo.comprador@cliente.mx", in_reply_to="<2@dindustrimex.com>"),
        _msg("caro.personal@gmail.com", "RE: Bombeo", "Sí me interesa", in_reply_to="<3@dindustrimex.com>"),
        _msg("otro@x.mx", "Hola", "no tiene que ver"),
        _msg("mailer-daemon@googlemail.com", "Delivery failure", "x", in_reply_to="<1@dindustrimex.com>"),
    ]
    new, out = _sync(tmp_path, messages, sent)
    assert new == {"fuera-de-oficina": ["ana@cliente.mx"], "ya-no-labora": ["beto@cliente.mx"],
                   "respondió": ["caro@cliente.mx"]}
    rows = replies.read_replies(out)
    assert rows["ana@cliente.mx"]["regresa"] == "2026-11-20"
    assert "beto@cliente.mx" in send.read_suppression(tmp_path / "suppression.txt")
    assert [r["email"] for r in replies._rows(out / replies.REFERRALS_NAME)] == ["nuevo.comprador@cliente.mx"]
    assert _sync(tmp_path, messages, sent)[0] == {}  # nothing new the second time


# ---------- 4. follow-up ----------

def _log(out, rows):
    out.mkdir(parents=True, exist_ok=True)
    for when, addr, estado, mid, sender in rows:
        send.append_log(out / send.LOG_NAME, addr, estado, mid, datetime.fromisoformat(when), sender)


def test_followup_goes_in_the_same_thread_only_to_who_did_not_answer(tmp_path):
    out = tmp_path / "mailing" / "qc-aro"
    write_contacts(tmp_path / "contacts.csv", [{"email": f"p{i}@c{i}.mx", "name": f"Persona{i} X",
                                                "source": "signature"} for i in range(6)])
    _log(out, [("2026-11-02 10:00:00", "p0@c0.mx", "enviado", "<a0@dindustrimex.com>", "pedro@dindustrimex.com"),
               ("2026-11-02 10:00:00", "p1@c1.mx", "enviado", "<a1@dindustrimex.com>", "ventas@dindustrimex.com"),
               ("2026-11-02 10:00:00", "p2@c2.mx", "enviado", "<a2@dindustrimex.com>", "pedro@dindustrimex.com"),
               ("2026-11-02 10:00:00", "p3@c3.mx", "enviado", "<a3@dindustrimex.com>", "pedro@dindustrimex.com"),
               ("2026-11-02 10:00:00", "p4@c4.mx", "enviado", "<a4@dindustrimex.com>", "pedro@dindustrimex.com"),
               ("2026-11-08 10:00:00", "p5@c5.mx", "enviado", "<a5@dindustrimex.com>", "pedro@dindustrimex.com")])
    replies._append(out / replies.REPLIES_NAME, replies.REPLY_FIELDS, [
        {"fecha": "2026-11-03 09:00:00", "email": "p2@c2.mx", "tipo": "respondió"},
        {"fecha": "2026-11-03 09:00:00", "email": "p3@c3.mx", "tipo": "fuera-de-oficina", "regresa": "2026-11-20"},
        {"fecha": "2026-11-03 09:00:00", "email": "p4@c4.mx", "tipo": "fuera-de-oficina", "regresa": "2026-11-05"}])
    pedro, ventas = FakeSMTP(), FakeSMTP()
    senders = [_sender(pedro), _sender(ventas, "ventas@dindustrimex.com")]
    now = lambda: datetime(2026, 11, 10, 9, 0)
    summary = _send(tmp_path, None, senders=senders, send=True, followup=True, now=now)
    assert summary["sent"] == 3
    assert summary["followup_waiting"] == {"recent": 1, "replied": 1, "away": 1, "other_mailbox": 0}
    assert sorted(m["To"].split("<")[-1].rstrip(">") for m in pedro.sent) == ["p0@c0.mx", "p4@c4.mx"]
    first = next(m for m in pedro.sent if "p0@c0.mx" in m["To"])
    assert first["In-Reply-To"] == "<a0@dindustrimex.com>" and first["Subject"].startswith("Re: ")
    assert "Hola Persona0," in first.get_body(("plain",)).get_content()
    assert [m["To"] for m in ventas.sent] == ["Persona1 X <p1@c1.mx>"]  # from the mailbox of the first one
    again = _send(tmp_path, None, senders=senders, send=True, followup=True, now=now)
    assert again["sent"] == 0  # nobody gets it twice
    assert send.sent_today(tmp_path / "mailing", date(2026, 11, 10))["pedro@dindustrimex.com"] == 2


def test_referrals_join_the_campaign(tmp_path):
    out = tmp_path / "mailing" / "qc-aro"
    replies._append(out / replies.REFERRALS_NAME, replies.REFERRAL_FIELDS,
                    [{"email": "nuevo@cliente9.mx", "source": "referido", "referido_por": "p0@cliente.mx"}])
    summary = _send(tmp_path, FakeSMTP(), send=False)
    assert summary["pending"] == 6


def test_app_followup_needs_the_replies_checked(tmp_path, monkeypatch):
    from mailing import app as mailing_app
    smtp = FakeSMTP()
    _, env = _app_setup(tmp_path, monkeypatch, smtp)
    _log(tmp_path / "mailing" / "qc-aro", [("2026-10-01 10:00:00", "p0@cliente.mx", "enviado", "<a@d>",
                                            "pedro@dindustrimex.com")])

    def broken(*a):
        raise ValueError("No pude revisar las respuestas: login")
    run = mailing_app.SendRun(check=lambda *a: ([], []), answers=broken)
    run.start(tmp_path, env, {"campaign": "qc-aro", "confirm": "ENVIAR", "mode": "seguimiento"}, pause=0)
    state = _wait(run)
    assert smtp.sent == [] and "No mandé el seguimiento" in state["error"]
    run = mailing_app.SendRun(check=lambda *a: ([], []), answers=lambda *a: {"respondió": ["x@y.mx"]})
    run.start(tmp_path, env, {"campaign": "qc-aro", "confirm": "ENVIAR", "mode": "seguimiento"}, pause=0)
    state = _wait(run)
    assert state["sent"] == 1 and smtp.sent[0]["In-Reply-To"] == "<a@d>" and "1 respuestas nuevas" in state["note"]
    info = mailing_app.overview(tmp_path, env, run, "qc-aro")
    assert info["followup"]["sent"] == 1 and info["followup"]["days"] == 5


def test_export_replies_csv(tmp_path, monkeypatch):
    from mailing import app as mailing_app
    _, env = _app_setup(tmp_path, monkeypatch, FakeSMTP())
    replies._append(tmp_path / "mailing" / "qc-aro" / replies.REPLIES_NAME, replies.REPLY_FIELDS, [
        {"fecha": "2026-11-03 09:00:00", "email": "p1@cliente.mx", "tipo": "fuera-de-oficina", "regresa": "2026-11-20"},
        {"fecha": "2026-11-04 09:00:00", "email": "p0@cliente.mx", "tipo": "respondió"}])
    text, name = mailing_app.export_csv(tmp_path, "qc-aro", "respuestas")
    lines = text.lstrip("﻿").splitlines()
    assert name == "qc-aro-respuestas.csv" and lines[1].startswith("2026-11-04 09:00:00,p0@cliente.mx,Persona0 X")


def test_library_copy_seeded_before_the_followup_gets_it(tmp_path):
    import json
    from mailing import library
    library.ensure(tmp_path)
    info = library.folder(tmp_path, "qc-aro") / library.INFO
    data = json.loads(info.read_text())
    assert data["followup"]["days"] == 5
    del data["followup"]
    info.write_text(json.dumps(data))
    library.ensure(tmp_path)
    assert json.loads(info.read_text())["followup"]["days"] == 5
    data["followup"] = {"days": 3, "body": ["mío"]}
    info.write_text(json.dumps(data))
    library.ensure(tmp_path)
    assert json.loads(info.read_text())["followup"]["days"] == 3


def test_followup_text_is_edited_from_the_app(tmp_path, monkeypatch):
    import pytest
    from mailing import app as mailing_app, library
    _, env = _app_setup(tmp_path, monkeypatch, FakeSMTP())
    info = mailing_app.overview(tmp_path, env, mailing_app.SendRun(), "qc-aro")
    t = info["followup_text"]
    assert t["days"] == 5 and t["editable"] and "Q-Pumps" in t["body"]
    assert t["firma"] == "Pedro Marquina\nDirector de Ventas · D-Industrimex"
    assert t["preview"].startswith("Asunto: Re: ") and "Hola Juan," in t["preview"]
    library.save(tmp_path, "qc-aro", {"followup": {"days": "3", "body": "Primer párrafo.\n\n  Segundo\npárrafo. ",
                                                   "firma": "Rubén\n\nVentas"}})
    t = mailing_app.overview(tmp_path, env, mailing_app.SendRun(), "qc-aro")["followup_text"]
    assert t["days"] == 3 and t["body"] == "Primer párrafo.\n\nSegundo párrafo." and t["firma"] == "Rubén\nVentas"
    assert t["preview"].endswith("Primer párrafo.\n\nSegundo párrafo.\n\nRubén\nVentas")
    with pytest.raises(ValueError, match="60"):
        library.save(tmp_path, "qc-aro", {"followup": {"days": 90, "body": "x"}})
    library.save(tmp_path, "qc-aro", {"followup": {"days": 5, "body": ""}})  # turns it off
    info = mailing_app.overview(tmp_path, env, mailing_app.SendRun(), "qc-aro")
    assert info["followup"] is None and info["followup_text"]["preview"] == ""
