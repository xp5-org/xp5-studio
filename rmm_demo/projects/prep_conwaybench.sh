#!/usr/bin/env bash
# Writes the conwaybench project into a local VM root and prints nothing else but its path.
#   prep_conwaybench.sh OUT_FILE_DIR
# VM root: R:/ (RAM disk) on the Win10 host, because NFS under Windows is unreliable for a running VM; /tmp elsewhere.
# The 86Box binary: build_out_win/86Box.exe on Windows, the A build on Linux.
set -euo pipefail
SD=/testsrc/sourcedir/86box_remote_monitor
if [ -n "${MSYSTEM:-}" ]; then
  [ -d /r ] || { echo "MISSING RAM disk R: -- mount one (ImDisk: imdisk -a -s 2G -m R: -p \"/fs:ntfs /q /y\")" >&2; exit 1; }
  VMROOT=/r/rmm_groups/conwaybench
  BIN=$SD/build_out_win/86Box.exe
else
  VMROOT=/tmp/rmm_groups/conwaybench
  BIN=$SD/GIT_SRC_ROOT/A/86Box/build/regular/src/86Box
fi
[ -f "$BIN" ] || { echo "MISSING 86Box build: $BIN" >&2; exit 1; }
rm -rf "$VMROOT"; mkdir -p "$VMROOT"
# the console and 86Box are native Windows programs on the MSYS host: they need R:/..., not /r/...
NAT=$VMROOT; [ -n "${MSYSTEM:-}" ] && NAT=$(cygpath -m "$VMROOT")
# copy the binary beside the VM so a running 86Box never reads the NFS share
cp -f "$BIN" "$VMROOT/$(basename "$BIN")"
python3 - "$SD/rmm_demo/projects/conwaybench.rmmproj.json" "$NAT" "$NAT/$(basename "$BIN")" /opt/86box/roms > "$VMROOT/conwaybench.rmmproj.json" <<'P'
import json, sys
src, vmroot, binary, roms = sys.argv[1:5]
p = json.load(open(src)); g = p["group"]
g["vars"].update(vmroot=vmroot, binary=binary)
g["nodes"]["box1"]["launch"]["roms"] = roms
print(json.dumps(p, indent=1))
P
echo "$VMROOT"
