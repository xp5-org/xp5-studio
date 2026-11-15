#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Freezes xp5-studio (pyside6-deploy / Nuitka, standalone) and assembles the one-click Linux release folder
# with its .zip and .7z:   build_bundle_linux.sh OUT_DIR 86BOX_BINARY ROMS_DIR [IMAGE ...]
# Needs only python3 -m venv, pip (network for the first run), a C compiler and ccache-less patience (~15 min).
set -euo pipefail
OUT=$(readlink -f "$1"); BIN=$(readlink -f "$2"); ROMS=$(readlink -f "$3"); shift 3
SRC=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
VENV=$OUT/venv
if [ ! -x "$VENV/bin/pyside6-deploy" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q "PySide6_Essentials==6.12.0" "Nuitka==4.3.1" patchelf py7zr ordered-set
fi
STAGE=$OUT/stage
rm -rf "$STAGE" "$OUT/rel"; mkdir -p "$STAGE/rmm_demo"
cp "$SRC/xp5_studio.py" "$STAGE/"
cp -r "$SRC/rmm_86box" "$STAGE/"
# the GUI package and its data; tests, mockups and projects are not frozen (projects ship beside the program)
(cd "$SRC/rmm_demo" && cp -r rmmgui rmm_demo.py domains groups "$STAGE/rmm_demo/")
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
ICON=$("$VENV/bin/python" -c "import PySide6,os;print(os.path.join(os.path.dirname(PySide6.__file__),'scripts','deploy_lib','pyside_icon.jpg'))")
sed "s#@ICON@#$ICON#" "$SRC/packaging/pysidedeploy.spec" > "$STAGE/pysidedeploy.spec"
(cd "$STAGE" && PYTHONPATH="$STAGE/rmm_demo" PATH="$VENV/bin:$PATH" "$VENV/bin/pyside6-deploy" -c pysidedeploy.spec --force --no-install)
DIST=$STAGE/xp5-studio.dist
[ -d "$DIST" ] || { echo "pyside6-deploy left no $DIST" >&2; exit 1; }
IMG=(); for i in "$@"; do IMG+=(--image "$i"); done
"$VENV/bin/python" "$SRC/make_bundle.py" --dist "$DIST" --binary "$BIN" --roms "$ROMS" --out "$OUT/rel" --platform linux "${IMG[@]}"
ls -l "$OUT/rel"
