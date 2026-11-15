#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Freezes xp5-studio (pyside6-deploy / Nuitka, standalone .app) and assembles the one-click macOS release folder
# with its .zip and .7z:   build_bundle_macos.sh OUT_DIR 86BOX_APP ROMS_DIR [IMAGE ...]
# Needs python3 (3.12 from Homebrew; the system 3.9 is too old for PySide6 6.12), Xcode command line tools and
# network for the first run (build venv in OUT/venv). Apple Silicon or Intel: the .app is built for the host.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
abspath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }
OUT=$(abspath "$1"); BIN=$(abspath "$2"); ROMS=$(abspath "$3"); shift 3
SRC=$(cd "$(dirname "$0")/.." && pwd)
PY=$(command -v python3.12 || command -v python3.13 || command -v python3.11)
mkdir -p "$OUT"
VENV=$OUT/venv
if [ ! -x "$VENV/bin/pyside6-deploy" ]; then
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q "PySide6_Essentials==6.12.0" "Nuitka==4.3.1" py7zr ordered-set
fi
STAGE=$OUT/stage
rm -rf "$STAGE" "$OUT/rel"; mkdir -p "$STAGE/rmm_demo"
cp "$SRC/xp5_studio.py" "$STAGE/"
cp -r "$SRC/rmm_86box" "$STAGE/"
(cd "$SRC/rmm_demo" && cp -r rmmgui rmm_demo.py domains groups "$STAGE/rmm_demo/")
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
ICON=$("$VENV/bin/python" -c "import PySide6,os;print(os.path.join(os.path.dirname(PySide6.__file__),'scripts','deploy_lib','pyside_icon.icns'))")
sed "s#@ICON@#$ICON#" "$SRC/packaging/pysidedeploy.spec" > "$STAGE/pysidedeploy.spec"
(cd "$STAGE" && PYTHONPATH="$STAGE/rmm_demo" PATH="$VENV/bin:$PATH" "$VENV/bin/pyside6-deploy" -c pysidedeploy.spec --force --no-install)
APP=$(find "$STAGE" -maxdepth 2 -name "xp5*.app" | head -1)
[ -d "$APP" ] || { echo "pyside6-deploy left no .app in $STAGE" >&2; ls -l "$STAGE" >&2; exit 1; }
# make the 86Box.app self-contained: Qt frameworks and plugins, SDL3, openal, ... are copied in and re-pointed
# (a user's mac has no Homebrew), then ad-hoc re-signed because rewriting the load commands voids the signature
BOXAPP=$OUT/86Box.app
rm -rf "$BOXAPP"; cp -R "$BIN" "$BOXAPP"
QTBIN=$( (ls -d /opt/homebrew/opt/qt@5/bin /usr/local/opt/qt@5/bin 2>/dev/null || true) | head -1)
"$QTBIN/macdeployqt" "$BOXAPP" -always-overwrite
codesign --force --deep --sign - "$BOXAPP"
LEAK=$(find "$BOXAPP" -type f -perm +111 | while read -r f; do otool -L "$f" 2>/dev/null | tail -n +2 | grep -v "/$(basename "$f") " | grep '/opt/homebrew\|/usr/local/' | sed "s#^#$f: #"|| true; done)
if [ -n "$LEAK" ]; then echo "$LEAK" >&2; echo "86Box.app still links outside the bundle" >&2; exit 1; fi
IMG=(); for i in "$@"; do IMG+=(--image "$i"); done
"$VENV/bin/python" "$SRC/make_bundle.py" --dist "$APP" --binary "$BOXAPP" --roms "$ROMS" --out "$OUT/rel" --platform macos ${IMG[@]+"${IMG[@]}"}
ls -l "$OUT/rel"
