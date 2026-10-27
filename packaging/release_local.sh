#!/bin/bash
# Builds the Mac app on this Mac and publishes it as a GitHub release, without GitHub Actions
# (for when the month's Actions minutes run out). The installed apps update to it as usual.
#
#   packaging/release_local.sh
#
# Publishes what is on main at GitHub. It only builds for this Mac's processor (Apple Silicon
# or Intel): Macs of the other kind keep the newest version that has their build. Running it
# on a Mac of the other kind for the same commit adds that build to the same release.
#
# Needs Homebrew; installs tesseract, dylibbundler, gh and Python 3.12 if they are missing, and
# asks for a GitHub login (gh auth login) the first time.
set -euo pipefail
cd "$(dirname "$0")/.."

# All in one function: bash reads it whole before running, so checking out main (which may
# change this file) cannot trip it up halfway.
main() {
  say() { printf '\n== %s\n' "$*"; }

  say "Herramientas"
  command -v brew >/dev/null || { echo "Falta Homebrew: https://brew.sh" >&2; exit 1; }
  for pkg in tesseract dylibbundler gh python@3.12; do
    brew list --versions "$pkg" >/dev/null || brew install "$pkg"
  done
  gh auth status >/dev/null 2>&1 || gh auth login --hostname github.com --git-protocol https --web

  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

  say "Código de main"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Hay cambios sin guardar en esta carpeta. Guárdalos o descártalos primero." >&2
    exit 1
  fi
  START="$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)"
  trap 'git checkout --quiet "$START"' EXIT
  git fetch origin main
  git checkout --quiet --detach origin/main
  SHA="$(git rev-parse HEAD)"

  say "Versión"
  # A release already made for this commit (from the other kind of Mac) gets this build added.
  TAG="$(gh api "repos/$REPO/releases?per_page=30" --jq ".[] | select(.target_commitish == \"$SHA\") | .tag_name" | head -1)"
  if [ -z "$TAG" ]; then
    LATEST="$(gh release view --repo "$REPO" --json tagName -q .tagName 2>/dev/null || echo v1.0.0)"
    TAG="v1.0.$(( ${LATEST##*.} + 1 ))"
    NEW=1
  else
    NEW=0
  fi
  VERSION="${TAG#v}"
  echo "$TAG ($([ "$NEW" = 1 ] && echo nueva || echo "se agrega a la existente")), commit ${SHA:0:7}"

  say "Python"
  VENV=build/venv
  [ -x "$VENV/bin/python" ] || "$(brew --prefix python@3.12)/bin/python3.12" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r requirements.txt -r packaging/requirements-build.txt pytest
  export PATH="$PWD/$VENV/bin:$PATH"
  python -m pytest -q

  say "Armar la app"
  APP_VERSION="$VERSION" APP_REPO="$REPO" packaging/build_mac.sh

  say "Publicar"
  if [ "$NEW" = 1 ]; then
    gh release create "$TAG" dist/*.dmg dist/*.app.zip --repo "$REPO" --target "$SHA" \
      --title "Contactos D-Industrimex $VERSION" --generate-notes
  else
    gh release upload "$TAG" dist/*.dmg dist/*.app.zip --repo "$REPO" --clobber
  fi
  echo
  echo "Listo: https://github.com/$REPO/releases/tag/$TAG"
  echo "Las apps abiertas la encuentran en menos de media hora."
}

main "$@"
