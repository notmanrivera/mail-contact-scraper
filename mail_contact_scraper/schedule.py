"""Daily automatic scan on macOS (launchd).

    python -m mail_contact_scraper.schedule install --at 08:00
    python -m mail_contact_scraper.schedule status
    python -m mail_contact_scraper.schedule uninstall

If the Mac is asleep at that time, launchd runs the scan when it wakes up.
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.dindustrimex.mail-contact-scraper"
PROTECTED = ("Desktop", "Documents", "Downloads")


class ScheduleError(RuntimeError):
    pass


def _agents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(agents_dir=None):
    return Path(agents_dir or _agents_dir()) / f"{LABEL}.plist"


def supported():
    return sys.platform == "darwin"


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def build_plist(hour, minute, out_dir, workdir=None, python=None):
    workdir = Path(workdir or os.getcwd()).resolve()
    out_dir = (workdir / out_dir).resolve()
    root = str(Path(__file__).resolve().parent.parent)
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "mail_contact_scraper", "--output", str(out_dir)],
        "WorkingDirectory": str(workdir),  # where .env lives
        "EnvironmentVariables": {"PYTHONPATH": root, "PYTHONUNBUFFERED": "1"},
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StandardOutPath": str(out_dir / "scheduled.log"),
        "StandardErrorPath": str(out_dir / "scheduled.log"),
        "ProcessType": "Background",
    }


def status(agents_dir=None):
    path = plist_path(agents_dir)
    info = {"supported": supported(), "enabled": False, "hour": 8, "minute": 0, "warning": warning()}
    if path.exists():
        try:
            data = plistlib.loads(path.read_bytes())
            interval = data.get("StartCalendarInterval", {})
            info.update(enabled=True, hour=interval.get("Hour", 8), minute=interval.get("Minute", 0))
        except Exception:
            pass
    return info


def warning(workdir=None):
    """macOS blocks background jobs from Desktop/Documents/Downloads without Full Disk Access."""
    workdir = Path(workdir or os.getcwd()).resolve()
    home = Path.home().resolve()
    for name in PROTECTED:
        if workdir == home / name or home / name in workdir.parents:
            return (f"El proyecto está en ~/{name}. macOS no deja que un proceso automático lea esa "
                    "carpeta: muévelo a otra (por ejemplo ~/mail-contact-scraper) o da Acceso total al "
                    "disco a Python en Configuración del Sistema > Privacidad y seguridad.")
    return ""


def install(hour, minute, out_dir="output", agents_dir=None, run=_launchctl):
    if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
        raise ScheduleError("Hora no válida.")
    if agents_dir is None and not supported():
        raise ScheduleError("El escaneo automático solo está hecho para macOS. En Linux usa cron: "
                            f"0 {int(hour)} * * * cd {os.getcwd()} && {sys.executable} -m mail_contact_scraper")
    path = plist_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    (Path(os.getcwd()) / out_dir).mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    run("bootout", domain, str(path))  # replace an older schedule; fails harmlessly if none
    path.write_bytes(plistlib.dumps(build_plist(hour, minute, out_dir)))
    result = run("bootstrap", domain, str(path))
    if getattr(result, "returncode", 0) not in (0, None):
        raise ScheduleError(f"launchctl no aceptó la tarea: {(result.stderr or '').strip()}")
    return status(agents_dir)


def uninstall(agents_dir=None, run=_launchctl):
    path = plist_path(agents_dir)
    if path.exists():
        run("bootout", f"gui/{os.getuid()}", str(path))
        path.unlink()
    return status(agents_dir)


def main(argv=None):
    p = argparse.ArgumentParser(prog="mail_contact_scraper.schedule")
    sub = p.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install", help="Scan every day at --at HH:MM")
    inst.add_argument("--at", default="08:00")
    inst.add_argument("--output", default=None)
    sub.add_parser("uninstall")
    sub.add_parser("status")
    args = p.parse_args(argv)
    try:
        if args.cmd == "install":
            from .config import load_settings
            hour, _, minute = args.at.partition(":")
            info = install(int(hour), int(minute or 0), args.output or load_settings().output_dir)
        elif args.cmd == "uninstall":
            info = uninstall()
        else:
            info = status()
    except (ScheduleError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Escaneo diario a las {info['hour']:02d}:{info['minute']:02d}" if info["enabled"] else "Sin escaneo automático")
    if info["warning"]:
        print(info["warning"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
