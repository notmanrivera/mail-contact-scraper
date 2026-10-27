import csv
import io

from mail_contact_scraper import dashboard
from mail_contact_scraper.contacts import FIELDS


def _write(out, contacts):
    with open(out / "contacts.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for c in contacts:
            writer.writerow({k: c.get(k, "") for k in FIELDS})


def test_whatsapp_list_one_row_per_phone_without_opt_outs(tmp_path):
    _write(tmp_path, [
        {"email": "laura@aceros.mx", "name": "Ing. LAURA Martínez", "company": "Aceros", "messages": 3,
         "mobile_phones": "+524422546851", "office_phones": "+524422957070", "last_seen": "2026-10-01T10:00:00"},
        {"email": "pedro@aceros.mx", "name": "Pedro Ruiz", "company": "Aceros", "messages": 9,
         "office_phones": "+524422957070"},                        # same switchboard, writes more
        {"email": "baja@cliente.mx", "name": "Ana", "messages": 50, "mobile_phones": "+5215512345678"},
        {"email": "rebote@cliente.mx", "name": "Luis", "messages": 1, "mobile_phones": "+525598765432"},
        {"email": "sin@tel.mx", "name": "Sin Teléfono", "messages": 1},
    ])
    (tmp_path / "suppression.txt").write_text("# baja 2026-10-20\nbaja@cliente.mx\n# rebote 2026-10-21\nrebote@cliente.mx\n")

    rows = dashboard.whatsapp_rows(tmp_path)
    assert [(r["telefono"], r["tipo"], r["correo"]) for r in rows] == [
        ("+524422546851", "celular", "laura@aceros.mx"),
        ("+525598765432", "celular", "rebote@cliente.mx"),
        ("+524422957070", "oficina", "pedro@aceros.mx"),
    ]
    assert rows[0]["nombre_pila"] == "Laura" and rows[0]["ultimo_correo"] == "2026-10-01"
    assert [r["tipo"] for r in dashboard.whatsapp_rows(tmp_path, only_mobile=True)] == ["celular", "celular"]

    text, name = dashboard.whatsapp_csv(tmp_path, only_mobile=True)
    assert name == "whatsapp-celulares.csv" and text.startswith("﻿telefono,tipo,nombre")
    assert len(list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))) == 2


def test_whatsapp_list_without_contacts_is_just_the_header(tmp_path):
    text, _ = dashboard.whatsapp_csv(tmp_path)
    assert text.strip("﻿\r\n") == ",".join(dashboard.WHATSAPP_FIELDS)
