#!/bin/bash
# Builds "Contactos D-Industrimex.app" for this Mac's architecture, with Tesseract inside,
# checks that it runs, and packs it as a .dmg (to install) and an .app.zip (for updates).
#
#   brew install tesseract dylibbundler
#   pip install -r requirements.txt -r packaging/requirements-build.txt
#   APP_VERSION=1.0.7 APP_REPO=notmanrivera/mail-contact-scraper packaging/build_mac.sh
#
# Without APP_REPO the app never looks for updates. GitHub Actions runs this on every merge
# to main (.github/workflows/mac-app.yml).
set -euo pipefail
cd "$(dirname "$0")/.."

APP="Contactos D-Industrimex"
FILE="Contactos-D-Industrimex"
VERSION="${APP_VERSION:-0.0.0}"
REPO="${APP_REPO:-}"
ARCH="$(uname -m)"
BUILD="build/mac"
TESSDATA_URL="https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0"

rm -rf "$BUILD" dist
mkdir -p "$BUILD" dist

echo "== Versión $VERSION ($ARCH)"
cat > mail_contact_scraper/_build_info.py <<EOF
# Written by packaging/build_mac.sh; read by updater.py.
VERSION = "$VERSION"
REPO = "$REPO"
ARCH = "$ARCH"
EOF

echo "== Íconos"
python packaging/make_icon.py "$BUILD/icon"
iconutil -c icns "$BUILD/icon/AppIcon.iconset" -o "$BUILD/icon/AppIcon.icns"

echo "== Tesseract"
TESS="$BUILD/tesseract"
mkdir -p "$TESS/bin" "$TESS/lib" "$TESS/tessdata"
cp -L "$(brew --prefix tesseract)/bin/tesseract" "$TESS/bin/"
chmod u+w "$TESS/bin/tesseract"
# Copy its Homebrew libraries next to it and point the binary at them.
dylibbundler -of -cd -b -x "$TESS/bin/tesseract" -d "$TESS/lib" -p @executable_path/../lib/ >/dev/null
if otool -L "$TESS/bin/tesseract" "$TESS"/lib/*.dylib | grep -E "^\s+(/opt/homebrew|/usr/local)/"; then
  echo "Tesseract todavía depende de Homebrew" >&2
  exit 1
fi
for lang in eng spa osd; do
  curl -fsSL --retry 3 -o "$TESS/tessdata/$lang.traineddata" "$TESSDATA_URL/$lang.traineddata"
done

echo "== PyInstaller"
APP_VERSION="$VERSION" APP_ICON="$PWD/$BUILD/icon/AppIcon.icns" \
  pyinstaller --noconfirm --clean --log-level WARN --distpath dist --workpath "$BUILD/pyinstaller" packaging/contactos.spec
BUNDLE="dist/$APP.app"
RES="$BUNDLE/Contents/Resources"
cp -R "$TESS" "$RES/tesseract"
cp "$BUILD/icon/menubar.png" "$RES/menubar.png"

echo "== Firma ad hoc"
# No Apple Developer ID: ad hoc signature, so the first open needs "Abrir igualmente" (see LEEME).
find "$RES/tesseract" -type f \( -name "*.dylib" -o -perm -u+x \) -print0 | xargs -0 codesign --force --sign - --timestamp=none
codesign --force --sign - --timestamp=none "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"

echo "== Pruebas"
EXE="$BUNDLE/Contents/MacOS/$APP"
SMOKE="$(mktemp -d)"
"$EXE" -m mail_contact_scraper --help >/dev/null
# HTTPS from inside the app with no certificate settings from this machine: an SSL error here
# means installed apps can't reach GitHub, Google or the mailboxes.
env -u SSL_CERT_FILE -u SSL_CERT_DIR -u REQUESTS_CA_BUNDLE CONTACTOS_DATA_DIR="$SMOKE/upd" \
  "$EXE" -m mail_contact_scraper.updater | tee "$SMOKE/update.json"
if grep -qi "certificate\|SSL" "$SMOKE/update.json"; then
  echo "La app no puede verificar certificados HTTPS" >&2
  exit 1
fi
"$EXE" -m mail_contact_scraper.vision  # Claude SDK for hard image signatures
# OCR with the bundled Tesseract only.
python - "$SMOKE/firma.png" <<'EOF'
import sys
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (900, 160), "white")
ImageDraw.Draw(img).text((20, 40), "Tel. 81 8340 1122", fill="black", font=ImageFont.load_default(size=64))
img.save(sys.argv[1])
EOF
env -i PATH=/usr/bin:/bin TESSDATA_PREFIX="$RES/tesseract/tessdata" "$RES/tesseract/bin/tesseract" "$SMOKE/firma.png" stdout -l spa+eng | tee "$SMOKE/ocr.txt"
grep -q "8340" "$SMOKE/ocr.txt"
# The scraper through the app, reading the test emails.
CONTACTOS_DATA_DIR="$SMOKE/data" "$EXE" -m mail_contact_scraper --local "$PWD/tests/fixtures" --output "$SMOKE/data/output"
test -s "$SMOKE/data/output/contacts.csv"
# The dashboard, without the menu bar or a browser.
CONTACTOS_DATA_DIR="$SMOKE/dash" CONTACTOS_NO_GUI=1 CONTACTOS_NO_BROWSER=1 "$EXE" &
PID=$!
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:8765/data -H "Host: 127.0.0.1" > "$SMOKE/data.json" && break; sleep 1; done
kill "$PID" || true
python -c "import json,sys; u=json.load(open(sys.argv[1]))['update']; assert u['version']=='$VERSION', u; print('Panel OK', u)" "$SMOKE/data.json"
test -f "$SMOKE/dash/.env"

echo "== Empaquetar"
ditto -c -k --keepParent "$BUNDLE" "dist/$FILE-$VERSION-$ARCH.app.zip"
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$BUNDLE" "$STAGE/"
ln -s /Applications "$STAGE/Aplicaciones"
cp packaging/LEEME.txt "$STAGE/Léeme primero.txt"
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "dist/$FILE-$VERSION-$ARCH.dmg" >/dev/null
ls -lh dist/*.dmg dist/*.zip
