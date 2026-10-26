# PyInstaller spec for "Contactos D-Industrimex.app". Run through packaging/build_mac.sh.
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

APP = "Contactos D-Industrimex"
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("APP_VERSION", "0.0.0")

datas = collect_data_files("mail_contact_scraper")  # dashboard.html, static/, Apps Script
binaries = []
hiddenimports = collect_submodules("mail_contact_scraper") + collect_submodules("dns")
for package in ("phonenumbers",):  # loads its metadata modules lazily
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mailing"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP)
app = BUNDLE(
    coll,
    name=f"{APP}.app",
    icon=os.environ.get("APP_ICON"),
    bundle_identifier="com.dindustrimex.contactos",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": APP,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu bar icon only, no Dock icon
        "NSHumanReadableCopyright": "D-Industrimex",
    },
)
