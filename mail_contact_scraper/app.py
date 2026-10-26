"""Entry point of the packaged Mac app ("Contactos D-Industrimex.app", built with PyInstaller).

Double-click: a menu bar icon plus the dashboard in the browser. The settings (.env, with the
mailbox passwords) and the contacts live in ~/Library/Application Support/Contactos
D-Industrimex for each user of the Mac, never inside the app, so updating or deleting the app
keeps them.

The same executable also runs the scraper (`<app> -m mail_contact_scraper ...`): that is how
the dashboard's Start button and the daily launchd scan call it, since sys.executable is the
app itself.

    python -m mail_contact_scraper.app      # try it from the source code (no menu bar icon)
"""
import logging
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "Contactos D-Industrimex"
log = logging.getLogger(__name__)

ENV_TEMPLATE = """# Configuración de Contactos D-Industrimex en esta Mac.
# Los buzones se agregan desde el panel: Configuración ▸ Buzones.
# Este archivo guarda contraseñas: no lo compartas.
IMAP_FOLDERS=INBOX
"""


def data_dir():
    custom = os.environ.get("CONTACTOS_DATA_DIR")
    if custom:
        return Path(custom).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".contactos-dindustrimex"


def resources_dir():
    """Contents/Resources of the bundle (sys.executable is Contents/MacOS/<name>)."""
    return Path(sys.executable).resolve().parent.parent / "Resources"


def use_bundled_tesseract(resources=None):
    """Point OCR at the Tesseract copy shipped inside the app, if there is one."""
    base = Path(resources or resources_dir()) / "tesseract"
    if not (base / "bin" / "tesseract").exists():
        return False
    os.environ["PATH"] = os.pathsep.join([str(base / "bin"), os.environ.get("PATH") or "/usr/bin:/bin"])
    os.environ["TESSDATA_PREFIX"] = str(base / "tessdata")
    return True


def prepare(folder=None):
    folder = Path(folder or data_dir())
    folder.mkdir(parents=True, exist_ok=True)
    os.chdir(folder)  # .env and output/ are relative to here, for the app and its children
    env = folder / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
        os.chmod(env, 0o600)
    use_bundled_tesseract()
    return folder


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [a for a in argv if not a.startswith("-psn_")]  # added by older macOS on launch
    folder = prepare()
    if argv[:1] == ["-m"] and len(argv) > 1 and argv[1].split(".")[0] == "mail_contact_scraper":
        # Run a module of this package the way `python -m` would.
        sys.argv = [sys.executable, *argv[2:]]
        if argv[1] == "mail_contact_scraper":
            from .cli import run
            return run()
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        return 0
    return run_app(folder)


def run_app(folder):
    from . import dashboard
    from .config import load_settings
    from .updater import Updater

    (folder / "output").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(folder / "app.log"), logging.StreamHandler()])
    updater = Updater(folder)
    if updater.install_staged_on_launch():
        return 0  # the swap script reopens the new version in a moment

    out_dir = load_settings().output_dir
    control = dashboard.ScraperControl(out_dir)
    server, url = dashboard.serve(out_dir, open_browser=not os.environ.get("CONTACTOS_NO_BROWSER"), control=control, updater=updater)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()

    def shutdown(install_update=False):
        if control.proc and control.proc.poll() is None and control.stop():
            try:
                control.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                control.proc.kill()
        ready = updater.staged() if install_update and updater.enabled else None
        if ready and not updater.installing and not updater.install_problem():
            updater.install(ready[1], relaunch=False)  # quitting is a good moment to update
        os._exit(0)

    updater.on_exit = shutdown
    updater.start_background()

    try:
        import rumps  # noqa: F401
    except ImportError:
        rumps = None
    if rumps is None or os.environ.get("CONTACTOS_NO_GUI"):
        log.info("Panel: %s (Ctrl+C para cerrar)", url)
        signal.signal(signal.SIGINT, lambda *_: shutdown(install_update=True))
        while True:
            time.sleep(3600)
    run_menu_bar(url, folder, shutdown)
    return 0


def run_menu_bar(url, folder, shutdown):
    import rumps

    icon = resources_dir() / "menubar.png"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(APP_NAME, icon=str(icon) if icon.exists() else None, title=None if icon.exists() else "✉",
                             template=True, quit_button=None)
            self.menu = [
                rumps.MenuItem("Abrir panel", callback=lambda _: webbrowser.open(url)),
                rumps.MenuItem("Abrir carpeta de contactos", callback=lambda _: subprocess.run(["open", str(folder / "output")])),
                None,
                rumps.MenuItem("Salir", callback=lambda _: shutdown(install_update=True)),
            ]

    _open_dashboard_on_reopen(url)
    MenuBarApp().run()


def _open_dashboard_on_reopen(url):
    """Double-clicking the app while it already runs reopens the dashboard instead of doing nothing."""
    try:
        import objc
        import rumps

        def reopen(self, app, has_windows):
            webbrowser.open(url)
            return True

        objc.classAddMethods(rumps.rumps.NSApp, [objc.selector(
            reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception:
        log.debug("Sin reapertura del panel desde el Dock/Finder", exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
