import pytest

from mailing import deliverability as dv
from mailing import send
from tests.test_mailing import FakeSMTP, _app_setup, _send, _wait, write_contacts

GOOD = {
    ("gmail.com", "MX"): ["5 gmail-smtp-in.l.google.com."],
    ("dindustrimex.com", "MX"): ["1 smtp.google.com."],
    ("dindustrimex.com", "TXT"): ["v=spf1 include:_spf.google.com ~all", "google-site-verification=x"],
    ("google._domainkey.dindustrimex.com", "TXT"): ["v=DKIM1; k=rsa; p=MIIB"],
    ("_dmarc.dindustrimex.com", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@dindustrimex.com"],
    ("cliente.mx", "MX"): ["10 mx.cliente.mx."],
    ("sinmx.mx", "MX"): [], ("sinmx.mx", "A"): ["1.2.3.4"],
    ("lento.mx", "MX"): TimeoutError("lento.mx"),
}


@pytest.fixture
def dns(monkeypatch):
    records = dict(GOOD)

    def resolve(name, rtype):
        value = records.get((name, rtype))
        if isinstance(value, Exception):
            raise value
        if value is None:
            if any(n == name for n, _ in records):
                return []
            raise LookupError(name)
        return value
    monkeypatch.setattr(dv, "resolve", resolve)
    monkeypatch.setattr(dv, "_cache", {})
    return records


def test_domain_health_green_when_spf_dkim_dmarc_are_set(dns):
    h = dv.domain_health("dindustrimex.com")
    assert h["light"] == "ok" and {c["light"] for c in h["checks"].values()} == {"ok"}
    assert "google" in h["checks"]["dkim"]["text"]


def test_domain_health_flags_what_is_missing(dns):
    dns[("dindustrimex.com", "TXT")] = ["v=spf1 include:mailgun.org ~all"]
    dns[("_dmarc.dindustrimex.com", "TXT")] = ["v=DMARC1; p=none"]
    del dns[("google._domainkey.dindustrimex.com", "TXT")]
    h = dv.domain_health("dindustrimex.com")
    assert h["light"] == "bad"
    assert h["checks"]["spf"]["light"] == "warn" and "_spf.google.com" in h["checks"]["spf"]["text"]
    assert h["checks"]["dkim"]["light"] == "bad" and h["checks"]["dmarc"]["light"] == "warn"
    dns[("dindustrimex.com", "TXT")] = ["v=spf1 a ~all", "v=spf1 mx ~all"]
    assert "dos registros" in dv.domain_health("dindustrimex.com", "")["checks"]["spf"]["text"]


def test_domain_health_unknown_when_dns_does_not_answer(dns):
    dns[("dindustrimex.com", "MX")] = TimeoutError("x")
    assert dv.domain_health("dindustrimex.com")["light"] == "unknown"


def test_nothing_is_dropped_when_dns_answers_no_for_everything(dns):
    dns.clear()
    with pytest.raises(ValueError, match="internet"):
        dv.clean_list(["a@cliente.mx"])
    assert dv.domain_health("dindustrimex.com")["light"] == "unknown"


def test_typo_suggestions():
    assert dv.typo_suggestion("gmal.com") == "gmail.com"
    assert dv.typo_suggestion("hotmial.com") == "hotmail.com"
    assert dv.typo_suggestion("empresa.con") == "empresa.com"
    assert dv.typo_suggestion("empresa.com.mz") == "empresa.com.mx"
    assert dv.typo_suggestion("gmail.com") == "" and dv.typo_suggestion("cliente.mx") == ""
    assert dv.typo_suggestion("empresa.co") == ""  # .co is a real TLD


def test_clean_list_finds_dead_domains_and_typos_but_keeps_unknown(dns):
    found = dv.clean_list(["a@cliente.mx", "b@noexiste.mx", "c@gmal.com", "d@sinmx.mx", "e@lento.mx"])
    bad = {b["email"]: b for b in found["bad"]}
    assert set(bad) == {"b@noexiste.mx", "c@gmal.com"}
    assert bad["c@gmal.com"]["sugerencia"] == "c@gmail.com" and "no recibe" in bad["b@noexiste.mx"]["motivo"]
    assert found["checked_domains"] == 5 and found["unknown_domains"] == 1


# ---------- circuit breaker ----------

def _contacts(tmp_path, n):
    write_contacts(tmp_path / "contacts.csv", [{"email": f"p{i}@cliente.mx", "name": f"Persona{i} X",
                                                "source": "signature"} for i in range(n)])


def test_breaker_pauses_when_too_many_are_refused(tmp_path):
    from mailing.send import read_log
    _contacts(tmp_path, 40)
    smtp = FakeSMTP(refuse={f"p{i}@cliente.mx" for i in range(0, 40, 4)})  # 1 in 4 refused
    summary = _send(tmp_path, smtp, send=True)
    assert summary["paused"] and "fallaron" in summary["paused"]
    attempts = summary["sent"] + len(summary["rejected"])
    assert send.BREAKER_MIN_ATTEMPTS <= attempts < 25
    assert len(read_log(tmp_path / "mailing" / "qc-aro" / "envios.csv")) == attempts  # rest still pending


def test_breaker_pauses_on_a_streak_of_failures(tmp_path):
    _contacts(tmp_path, 12)
    smtp = FakeSMTP(refuse={f"p{i}@cliente.mx" for i in range(1, 12)})
    summary = _send(tmp_path, smtp, send=True)
    assert summary["sent"] == 1 and len(summary["rejected"]) == send.BREAKER_IN_ROW
    assert "seguidos" in summary["paused"]


def test_healthy_run_is_not_paused(tmp_path):
    _contacts(tmp_path, 40)
    summary = _send(tmp_path, FakeSMTP(refuse={"p3@cliente.mx"}), send=True)
    assert summary["sent"] == 39 and not summary["paused"]


def test_checkpoint_can_pause_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(send, "CHECK_EVERY", 5)
    _contacts(tmp_path, 20)
    seen = []

    def checkpoint(sent):
        seen.append(len(sent))
        return "rebotes" if len(sent) >= 10 else ""
    summary = _send(tmp_path, FakeSMTP(), send=True, checkpoint=checkpoint)
    assert seen == [5, 10] and summary["sent"] == 10 and summary["paused"] == "rebotes"


def test_bounce_checkpoint_counts_only_this_runs_bounces():
    from mailing import app as mailing_app
    bounced = []
    check = mailing_app.bounce_checkpoint(lambda *a: ([], list(bounced)), "out", ".env", "qc-aro")
    sent = [f"p{i}@x.mx" for i in range(50)]
    bounced[:] = ["otro@viejo.mx", "P1@x.mx", "p2@x.mx"]  # 2 of 50 = 4%
    assert check(sent) == ""
    bounced[:] = ["p3@x.mx"]  # now 3 of 50
    assert "3 de 50" in check(sent)


def test_app_run_pauses_on_bounces_and_notifies(tmp_path, monkeypatch):
    from mailing import app as mailing_app
    monkeypatch.setattr(send, "CHECK_EVERY", 2)
    notes = []
    monkeypatch.setattr(mailing_app, "notify", lambda title, text: notes.append(title))
    smtp = FakeSMTP()
    _, env = _app_setup(tmp_path, monkeypatch, smtp)
    run = mailing_app.SendRun(check=lambda *a: ([], ["p0@cliente.mx"]))
    run.start(tmp_path, env, {"campaign": "qc-aro", "confirm": "ENVIAR"}, pause=0)
    state = _wait(run)
    assert state["sent"] == 2 and "rebotaron" in state["paused"] and notes == ["Envío en pausa"]


# ---------- the app ----------

def test_app_clean_list_and_apply(tmp_path, monkeypatch, dns):
    from mailing import app as mailing_app
    _, env = _app_setup(tmp_path, monkeypatch, FakeSMTP())
    write_contacts(tmp_path / "contacts.csv", [
        {"email": e, "name": "Ana X", "source": "signature"}
        for e in ("a@cliente.mx", "b@noexiste.mx", "c@gmal.com")])
    found = mailing_app.clean_list(tmp_path, "qc-aro")
    assert len(found["bad"]) == 2 and found["applied"] == 0 and "c@gmail.com" in found["csv"]
    assert not (tmp_path / "suppression.txt").exists()
    applied = mailing_app.clean_list(tmp_path, "qc-aro", apply=True)
    assert applied["applied"] == 2
    assert send.suppression_counts(tmp_path / "suppression.txt") == {"error-de-dedo": 1, "dominio-sin-correo": 1}
    assert mailing_app.clean_list(tmp_path, "qc-aro")["bad"] == []  # already out of the list


def test_dashboard_health_and_clean(tmp_path, monkeypatch, dns):
    import json
    import threading
    import urllib.request
    from mail_contact_scraper import dashboard

    _, env = _app_setup(tmp_path, monkeypatch, FakeSMTP())
    server, url = dashboard.serve(tmp_path, port=18998, open_browser=False, env_path=env)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        req = urllib.request.Request(url + path, json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Requested-With": "dashboard"})
        return json.loads(urllib.request.urlopen(req).read())
    try:
        health = post("mailing/health", {"campaign": "qc-aro"})
        assert [d["domain"] for d in health["domains"]] == ["dindustrimex.com"]
        assert health["domains"][0]["light"] == "ok" and len(health["domains"][0]["senders"]) == 2
        cleaned = post("mailing/clean", {"campaign": "qc-aro", "apply": "yes"})  # only true applies
        assert cleaned["ok"] and cleaned["bad"] == [] and cleaned["applied"] == 0
    finally:
        server.shutdown()
