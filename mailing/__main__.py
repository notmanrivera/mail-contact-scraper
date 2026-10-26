import argparse
import sys
from pathlib import Path

from .build import ESP, build

HERE = Path(__file__).parent


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m mailing",
        description="Genera el mailing D-Industrimex personalizado para cada prospecto. No envía nada.")
    ap.add_argument("campaign", nargs="?", default=HERE / "campaigns" / "qc-aro.toml",
                    help="archivo .toml de la campaña (default: qc-aro)")
    ap.add_argument("--contacts", default="output/contacts.csv", help="CSV del scraper")
    ap.add_argument("--out", help="carpeta de salida (default: output/mailing/<slug>)")
    ap.add_argument("--esp", choices=sorted(ESP), help="genera plantilla y CSV para importar en esa plataforma")
    ap.add_argument("--excel", action="store_true",
                    help="Excel con el HTML listo por prospecto, para enviar con la macro de Outlook")
    ap.add_argument("--eml", action="store_true", help="un borrador .eml por prospecto")
    ap.add_argument("--exclude-domain", action="append", default=[], help="dominio a excluir (repetible)")
    ap.add_argument("--suppression", default="output/suppression.txt",
                    help="correos dados de baja, uno por línea (default: output/suppression.txt)")
    ap.add_argument("--require-signature", action="store_true",
                    help="solo contactos con firma (no los que solo aparecen en De/Para/CC)")
    args = ap.parse_args(argv)

    if not Path(args.contacts).exists():
        sys.exit(f"No existe {args.contacts}. Corre primero: python -m mail_contact_scraper")

    from .build import load_campaign
    out = args.out or Path("output") / "mailing" / load_campaign(args.campaign)["slug"]
    try:
        people, skipped = build(args.campaign, args.contacts, out, esp=args.esp, eml=args.eml, excel=args.excel,
                                exclude_domains=args.exclude_domain, suppression_path=args.suppression,
                                require_signature=args.require_signature)
    except ValueError as err:
        sys.exit(str(err))
    print(f"{len(people)} prospectos → {out}")
    print("Omitidos: " + ", ".join(f"{k} {v}" for k, v in skipped.items() if v) if any(skipped.values()) else "Omitidos: 0")
    print(f"Revisa {Path(out) / 'preview.html'} antes de importar o enviar.")


if __name__ == "__main__":
    main()
