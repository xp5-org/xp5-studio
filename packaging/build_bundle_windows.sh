#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Freezes xp5-studio (pyside6-deploy / Nuitka, standalone) and assembles the one-click Windows release folder
# with its .zip and .7z:   build_bundle_windows.sh OUT_DIR 86BOX_EXE ROMS_DIR [IMAGE ...]
# Run from MSYS2 (bash) on a Windows host. OUT_DIR should be a local disk (the R: RAM disk), never an NFS share.
# Needs a native CPython 3.11-3.13 (python.org build, found via $XP5_PYTHON, the py launcher or C:/Python3*; the
# MSYS2 python cannot install the PySide6 wheels) and network for the first run (build venv in OUT/venv).
set -euo pipefail
export PATH="/usr/bin:$PATH"
win() { cygpath -m "$1"; }
mkdir -p "$1"
OUT=$(win "$(cd "$1" && pwd)"); BIN=$(win "$(readlink -f "$2")"); ROMS=$(win "$(readlink -f "$3")"); shift 3
SRC=$(win "$(cd "$(dirname "$0")/.." && pwd)")

native_python() {   # first CPython that reports sys.platform == win32 and 3.11..3.13
  local c
  for c in "${XP5_PYTHON:-}" /c/Python3{13,12,11}/python.exe \
           /c/Users/*/AppData/Local/Programs/Python/Python3{13,12,11}/python.exe \
           "/c/Program Files/Python3"{13,12,11}/python.exe; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys;sys.exit(0 if sys.platform=="win32" and (3,11)<=sys.version_info[:2]<=(3,13) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  if command -v py >/dev/null 2>&1; then
    for v in 3.13 3.12 3.11; do
      if p=$(py -$v -c 'import sys;print(sys.executable)' 2>/dev/null); then echo "$(cygpath -u "$p" | tr -d '\r')"; return 0; fi
    done
  fi
  return 1
}
PY=$(native_python) || { echo "no native Windows CPython 3.11-3.13 (set XP5_PYTHON)" >&2; exit 1; }
echo "python: $PY"
VENV=$OUT/venv
if [ ! -x "$VENV/Scripts/pyside6-deploy.exe" ]; then
  "$PY" -m venv "$VENV"
  "$VENV/Scripts/python.exe" -m pip install -q --no-cache-dir "PySide6_Essentials==6.12.0" "Nuitka==4.3.1" py7zr ordered-set zstandard
fi
VPY=$VENV/Scripts/python.exe
STAGE=$OUT/stage
rm -rf "$STAGE" "$OUT/rel"; mkdir -p "$STAGE/rmm_demo"
cp "$SRC/xp5_studio.py" "$STAGE/"
cp -r "$SRC/rmm_86box" "$STAGE/"
# the GUI package and its data; tests, mockups and projects are not frozen (projects ship beside the program)
(cd "$SRC/rmm_demo" && cp -r rmmgui rmm_demo.py domains groups "$STAGE/rmm_demo/")
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
ICON=$("$VPY" -c "import PySide6,os;d=os.path.join(os.path.dirname(PySide6.__file__),'scripts','deploy_lib');print((os.path.join(d,'pyside_icon.ico') if os.path.exists(os.path.join(d,'pyside_icon.ico')) else os.path.join(d,'pyside_icon.jpg')).replace(os.sep,'/'))" | tr -d '\r')
sed "s#@ICON@#$ICON#" "$SRC/packaging/pysidedeploy.spec" > "$STAGE/pysidedeploy.spec"
(cd "$STAGE" && PYTHONPATH="$STAGE/rmm_demo" PATH="$VENV/Scripts:$PATH" "$VENV/Scripts/pyside6-deploy.exe" -c pysidedeploy.spec --force --no-install)
DIST=$(find "$STAGE" -maxdepth 1 -name '*.dist' | head -1)
[ -d "$DIST" ] || { echo "pyside6-deploy left no .dist in $STAGE" >&2; ls -l "$STAGE" >&2; exit 1; }
IMG=(); for i in "$@"; do IMG+=(--image "$(win "$(readlink -f "$i")")"); done
"$VPY" "$SRC/make_bundle.py" --dist "$(win "$DIST")" --binary "$BIN" --roms "$ROMS" --out "$OUT/rel" --platform windows ${IMG[@]+"${IMG[@]}"}
ls -l "$OUT/rel"
