#!/usr/bin/env bash
# File prep for lockstep7.rmmproj.json. Makes the disk images the project's nodes point at and
# starts NO emulator (the Qt console launches every node from the project file).
#   prep_lockstep7.sh [OUT_DIR [CUBEROTATE_DSK]]   defaults: the project's own {out}, the registered CubeRotate build
# Group snap is savevm, so every writable QEMU disk must be qcow2.
set -euo pipefail
SD=/testsrc/sourcedir
OUT=${1:-$SD/86box_remote_monitor/rmm_lockstep5_out}
CUBE=${2:-$SD/86box_remote_monitor/cuberotate/CubeRotate.dsk}
HERE=$(cd "$(dirname "$0")/.." && pwd)

# qemu-img: PATH first, then the usual Windows install dirs (the MSYS shell does not inherit them).
# A host without QEMU only loses the QEMU nodes: warn, skip their disks, the console shows them offline.
if ! command -v qemu-img >/dev/null 2>&1; then
  for d in "/c/Program Files/qemu" "/c/Program Files (x86)/qemu" "/c/qemu" "/d/qemu"; do
    [ -x "$d/qemu-img.exe" ] && { PATH="$d:$PATH"; break; }
  done
fi
HAVE_QEMU=1
command -v qemu-img >/dev/null 2>&1 || { HAVE_QEMU=0; echo "WARNING: qemu-img not found on $(hostname): skipping the w31, sol and m68k disks (those nodes stay offline)" >&2; }
for t in x64sc dgnova; do
  command -v "$t" >/dev/null 2>&1 || echo "WARNING: $t not found on $(hostname): that node will stay offline" >&2
done
NODES=2                                   # a, b (86Box) always; the rest only where their emulator exists
[ "$HAVE_QEMU" = 1 ] && NODES=$((NODES+3))
command -v x64sc >/dev/null 2>&1 && NODES=$((NODES+1))

mkdir -p "$OUT"/{w31,sol,m68k}

# b: floppy for the 86Box nodes (MEMLOOP)
( cd "$OUT"; python3 "$SD/86box_remote_monitor/memwrite_src/mkcom.py" MEMLOOP.COM
  rm -f b_floppy.img; mformat -C -f 1440 -v MEMLOOP -i b_floppy.img ::
  mcopy -o -i b_floppy.img MEMLOOP.COM ::/MEMLOOP.COM )

if [ "$HAVE_QEMU" = 1 ]; then
# m68k: CubeRotate on its own qcow2 disk, and a private qcow2 over the Mac system template
[ -f "$CUBE" ] || { echo "ERROR: $CUBE missing: run the tool: cuberotate build testlist" >&2; exit 2; }
rm -f "$OUT/m68k/cube.qcow2" "$OUT/m68k/hdd.qcow2"
qemu-img convert -f raw -O qcow2 "$CUBE" "$OUT/m68k/cube.qcow2"
qemu-img convert -f raw -O qcow2 /testsrc/templates/mac_q800_761.img "$OUT/m68k/hdd.qcow2"

# sol: private overlays of SCSI units 0..5 (disk0 is a copy: it is the boot disk)
for i in 1 2 3 4 5; do
  rm -f "$OUT/sol/disk-$i.qcow2"
  qemu-img create -q -f qcow2 -b "$SD/solaris9/disk-$i.qcow2" -F qcow2 "$OUT/sol/disk-$i.qcow2"
done
rm -f "$OUT/sol/disk0.qcow2"
qemu-img create -q -f qcow2 -b "$SD/solaris9/disk-0.qcow2" -F qcow2 "$OUT/sol/disk0.qcow2"

# w31: C: from the win31 template with the prebuilt HELLOWIN.EXE, started from AUTOEXEC
W="$OUT/w31"; rm -f "$W/c_raw.img" "$W/c.qcow2"
cp /testsrc/templates/win31_owc.img "$W/c_raw.img"
cat > "$W/mtoolsrc" <<EOM
drive c: file="$W/c_raw.img" offset=32256
EOM
export MTOOLSRC="$W/mtoolsrc"
mmd c:/HELLOWIN 2>/dev/null || true
mcopy -n -o "$SD/win31_training/build/HELLOWIN.EXE" c:/HELLOWIN/HELLOWIN.EXE
mtype c:/AUTOEXEC.BAT | tr -d '\r' | grep -vi hellowin > "$W/autoexec.tmp" || true
printf 'c:\\windows\\win.com c:\\hellowin\\hellowin.exe\n' >> "$W/autoexec.tmp"
sed 's/$/\r/' "$W/autoexec.tmp" > "$W/AUTOEXEC.BAT"
mcopy -n -o "$W/AUTOEXEC.BAT" c:/AUTOEXEC.BAT
unset MTOOLSRC
qemu-img convert -f raw -O qcow2 "$W/c_raw.img" "$W/c.qcow2"
fi
echo "$NODES" > "$OUT/expected_nodes"
echo "prep done: $OUT"
