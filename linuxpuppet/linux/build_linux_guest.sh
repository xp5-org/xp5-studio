#!/bin/bash
# Host-side build of the Linux puppet guest: kernel (+ embedded initramfs with the stage's /init), a bootable raw HDD
# image (MBR + one FAT partition + syslinux) and a 1.44 MB floppy.   build_linux_guest.sh [STAGE]   (default linuxpuppet)
# Started as a copy of sourcedir/r128_mesa/linux/build_linux_guest.sh (the Rage 128 L-stages), made card-neutral.
# Outputs live in /testsrc/disk_artifacts/linuxpuppet. Inputs (pinned, sha256-checked tarballs; untrusted, built in their
# own dirs) are copied from /testsrc/disk_artifacts/linux-r128/dl when present; kernel + syslinux trees are our own (patched).
set -euo pipefail
STAGE=${1:-linuxpuppet}
HERE=$(cd "$(dirname "$0")" && pwd)
ART=${PUPPET_ART:-/testsrc/disk_artifacts/linuxpuppet}   # work + output dir (override to build outside the test tree)
SHARED=${PUPPET_DL_CACHE:-/testsrc/disk_artifacts/linux-r128}   # optional cache of the pinned tarballs; never written; else downloaded
KVER=6.1.189; SVER=6.03
# the kernel tree is our own too (src/linux-$KVER-pup): relocate_kernel_32.S saves %cr4 unconditionally before KEXEC,
# which #UDs on a CR4-less 486 (86Box i486dx, real i486DX) with no IDT -> triple fault -> BIOS reset instead of kexec.
# The patch guards that save with cpu_has_pae, the same test the file already uses before writing %cr4.
KSRC=$ART/src/linux-$KVER-pup; KOUT=$ART/build/kernel-$STAGE
# syslinux is always our own: the 6.03 release core sizes INT 15h AH=88h memory from CX instead of AX (core/bios.c),
# so on a BIOS without E820/E801 (pb430's 1993 Phoenix) it sees 0 KB above 1 MB and "Booting kernel failed: Invalid
# argument". We rebuild the core (ldlinux.sys/.bss) with that one fix; ldlinux.c32 stays the release binary.
SDIR=$ART/src/syslinux-$SVER-88h; SINST=$ART/build/syslinux-inst-88h; SLX=$SINST/syslinux-mtools
# 0. inputs: pinned tarballs, sha256 from the vendors' own sums files, each extracted into its own dir
mkdir -p "$ART/dl" "$ART/src" "$ART/build"
fetch() {  # url sumsurl file
  [ -f "$ART/dl/$3" ] || cp "$SHARED/dl/$3" "$ART/dl/$3" 2>/dev/null || curl -sSfL "$1/$3" -o "$ART/dl/$3"
  [ -f "$ART/dl/$3.sums" ] || cp "$SHARED/dl/$3.sums" "$ART/dl/$3.sums" 2>/dev/null || curl -sSfL "$2" -o "$ART/dl/$3.sums"
  (cd "$ART/dl" && grep " $3\$" "$3.sums" | sha256sum -c -)
}
if [ ! -f "$KSRC/Makefile" ]; then
  fetch https://cdn.kernel.org/pub/linux/kernel/v6.x https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc linux-$KVER.tar.xz
  rm -rf "$KSRC" "$KOUT"; mkdir -p "$KSRC"; tar -xf "$ART/dl/linux-$KVER.tar.xz" -C "$KSRC" --strip-components=1
  RK=$KSRC/arch/x86/kernel/relocate_kernel_32.S
  perl -0pi -e 's/(\tmovl\t%cr3, %eax\n\tmovl\t%eax, CR3\(%edi\)\n)\tmovl\t%cr4, %eax\n\tmovl\t%eax, CR4\(%edi\)\n/$1\t\/* linuxpuppet: no CR4 on an i486DX; cpu_has_pae is the CR4 guard used below *\/\n\tcmpl\t\$0, 20+16(%esp)\n\tje\t9f\n\tmovl\t%cr4, %eax\n\tmovl\t%eax, CR4(%edi)\n9:\n/' "$RK"
  grep -q 'linuxpuppet: no CR4' "$RK" || { echo "relocate_kernel_32 CR4 patch did not apply"; rm -rf "$KSRC"; exit 1; }
fi
if [ ! -x "$SLX" ]; then
  command -v nasm >/dev/null || { echo "nasm is needed to rebuild the syslinux core (apt-get install nasm gcc-multilib)"; exit 1; }
  fetch https://cdn.kernel.org/pub/linux/utils/boot/syslinux https://cdn.kernel.org/pub/linux/utils/boot/syslinux/sha256sums.asc syslinux-$SVER.tar.xz
  rm -rf "$SDIR" "$SINST"; mkdir -p "$SDIR" "$SINST"; tar -xf "$ART/dl/syslinux-$SVER.tar.xz" -C "$SDIR" --strip-components=1
  S=$SDIR
  sed -i '/Finally try INT 15h AH=88h/,/SMT_FREE/ s/oreg\.ecx\.w\[0\] << 10/oreg.eax.w[0] << 10/' "$S/core/bios.c"
  grep -A5 'Finally try INT 15h AH=88h' "$S/core/bios.c" | grep -q 'oreg.eax.w\[0\] << 10' || { echo "88h patch did not apply"; exit 1; }
  # the tarball ships a prebuilt bios/core and ldlinux.c32 (make would just re-dd the release ldlinux.bin): drop both;
  # a rebuilt core refuses the release ldlinux.c32 ("Failed to load ldlinux.c32"), so they are built together. com32
  # builds only lib + elflink/ldlinux (the core links libcom32core.a; the other com32 dirs fail under GCC 14)
  cp "$S/bios/core/ldlinux.sys" "$SINST/ldlinux.sys.release"; rm -rf "$S/bios/core" "$S/bios/com32/elflink/ldlinux"
  sed -i -e '1i SUBDIRS = lib elflink/ldlinux' -e '/^SUBDIRS = /,/[^\\]$/d' "$S/com32/Makefile"
  # GCC 14 / new binutils need these relaxations
  W="-fcommon -Wno-error=implicit-function-declaration -Wno-error=incompatible-pointer-types -Wno-error=int-conversion"
  W="$W -Wno-error=implicit-int -Wno-error=return-mismatch -Wno-error=declaration-missing-parameter-type"
  (cd "$S" && make bios CC="gcc $W" LD="ld --no-dynamic-linker" ISUBDIRS= ITARGET= \
     IOBJECTS= BSUBDIRS="codepage com32 lzo core" > "$SINST/core-build.log" 2>&1) || { tail -20 "$SINST/core-build.log"; exit 1; }
  cmp -s "$S/bios/core/ldlinux.sys" "$SINST/ldlinux.sys.release" && { echo "syslinux core was not rebuilt"; exit 1; }
  (cd "$SINST" && P="perl $S/libinstaller/bin2c.pl" &&
    $P syslinux_bootsect < $S/bios/core/ldlinux.bss > bootsect_bin.c &&
    $P syslinux_ldlinux 512 < $S/bios/core/ldlinux.sys > ldlinux_bin.c &&
    $P syslinux_ldlinuxc32 < $S/bios/com32/elflink/ldlinux/ldlinux.c32 > ldlinuxc32_bin.c &&
    gcc -O1 -w -D_FILE_OFFSET_BITS=64 -I$S/mtools -I$S/libfat -I$S/libinstaller -I$S/bios -I. -o syslinux-mtools \
      $S/mtools/syslinux.c $S/libinstaller/{fs,syslxmod,syslxopt,setadv}.c bootsect_bin.c ldlinux_bin.c ldlinuxc32_bin.c $S/libfat/*.c)
fi
OUT=$ART/out/$STAGE; mkdir -p "$KOUT" "$OUT" "$ART/build/initramfs-$STAGE"
# per-stage knobs (optional $STAGE.env): GUEST_MARCH/GUEST_MTUNE (guest code), GUEST_BAD (objdump regex of insns the
# target CPU lacks), GUEST_CFLAGS (extra), KCPU (the CONFIG_ CPU choice that must stick), FLOPPY=1 (also a 1.44 MB image)
GUEST_MARCH=i586; GUEST_MTUNE=pentium; GUEST_BAD='cmov[a-z]*|endbr32|xmm[0-9]|nop[wl]'; GUEST_CFLAGS=; KCPU=M586TSC; FLOPPY=0
[ -f "$HERE/$STAGE.env" ] && . "$HERE/$STAGE.env"

# 1. guest binary (freestanding, no libc)
CF="-m32 -march=$GUEST_MARCH -mtune=$GUEST_MTUNE $GUEST_CFLAGS -nostdlib -static -ffreestanding -fno-builtin -fno-stack-protector -fno-pie -no-pie -fcf-protection=none -fno-asynchronous-unwind-tables -O2 -Wall -Wl,-e,_start"
IR=$ART/build/initramfs-$STAGE
gcc $CF -o "$IR/init" "$HERE/$STAGE.c"
# refuse a binary with instructions the target CPU lacks (it would SIGILL); GUEST_BAD comes from $STAGE.env
if objdump -d "$IR/init" | grep -E -q "\\b($GUEST_BAD)\\b"; then echo "guest binary has insns the $GUEST_MARCH target lacks"; objdump -d "$IR/init" | grep -E "\\b($GUEST_BAD)\\b" | head; exit 1; fi
# personalities (puppet_plan.md 2.3): personalities/<name>/*.c -> out/personalities/<name>/bin/<stem>, same flags and
# instruction check as /init; the host PUTs them into the running puppet (Puppet.load), they are not in the initramfs
PPOUT=$ART/out/personalities
for pd in "$HERE"/personalities/*/; do
  [ -f "$pd/manifest.json" ] || continue
  pn=$(basename "$pd"); rm -rf "$PPOUT/$pn"; mkdir -p "$PPOUT/$pn/bin"
  for src in "$pd"*.c; do
    bin=$PPOUT/$pn/bin/$(basename "$src" .c)
    # a personality may build from sources outside this tree: include.txt lists extra -I dirs, one per line
    INC=; [ -f "$pd/include.txt" ] && INC=$(sed 's/^/-I/' "$pd/include.txt" | tr '\n' ' ')
    gcc $CF -U_FORTIFY_SOURCE -I"$HERE" $INC -o "$bin" "$src"
    if objdump -d "$bin" | grep -E -q "\\b($GUEST_BAD)\\b"; then echo "personality $pn: $bin has insns the $GUEST_MARCH target lacks"; exit 1; fi
  done
  cp "$pd/manifest.json" "$PPOUT/$pn/manifest.json"
  echo "personality $pn: $(ls "$PPOUT/$pn/bin" | tr '\n' ' ')"
done
# extra payload (firmware etc.) listed in $HERE/$STAGE.cpio.list, appended to the generated list
{
  echo "dir /dev 755 0 0"; echo "dir /proc 755 0 0"; echo "dir /sys 755 0 0"
  echo "nod /dev/console 600 0 0 c 5 1"; echo "nod /dev/null 666 0 0 c 1 3"
  echo "file /init $IR/init 755 0 0"
  [ -f "$HERE/$STAGE.cpio.list" ] && sed "s|@ART@|$ART|g" "$HERE/$STAGE.cpio.list"
} > "$IR/list"

# 2. kernel. No bc on the box (and no root): Kbuild's only bc use, timeconst.bc, goes through a stand-in on PATH.
BT=$ART/build/tools; mkdir -p "$BT"
printf '#!/bin/sh\nexec python3 %s "$@"\n' "$HERE/bc_timeconst.py" > "$BT/bc"; chmod +x "$BT/bc"
export PATH="$BT:$PATH"
[ -f "$KOUT/.config" ] || make -C "$KSRC" O="$KOUT" ARCH=i386 tinyconfig >/dev/null
# the fragment is reapplied on every build, so editing kernel.frag always takes effect
apply_frag() {
  grep -v '^#' "$1" | while IFS== read -r k v; do
    [ -z "$k" ] && continue
    if [ "$v" = n ]; then "$KSRC/scripts/config" --file "$KOUT/.config" -d "${k#CONFIG_}"
    elif [ "$v" = y ]; then "$KSRC/scripts/config" --file "$KOUT/.config" -e "${k#CONFIG_}"
    else "$KSRC/scripts/config" --file "$KOUT/.config" --set-val "${k#CONFIG_}" "$v"; fi
  done
}
apply_frag "$HERE/kernel.frag"
[ -f "$HERE/$STAGE.frag" ] && apply_frag "$HERE/$STAGE.frag"   # stage-specific options (e.g. framebuffer for L2)
"$KSRC/scripts/config" --file "$KOUT/.config" --set-str INITRAMFS_SOURCE "$IR/list"
make -C "$KSRC" O="$KOUT" ARCH=i386 olddefconfig >/dev/null
make -C "$KSRC" O="$KOUT" ARCH=i386 -j"$(nproc)" bzImage 2>&1 | tail -5
for k in $KCPU VGA_CONSOLE KERNEL_GZIP; do
  grep -q "^CONFIG_$k=y" "$KOUT/.config" || { echo "kernel config: CONFIG_$k did not stick"; exit 1; }
done
if [ -f "$HERE/$STAGE.frag" ]; then   # every =y in the stage fragment must have stuck
  for k in $(grep '=y$' "$HERE/$STAGE.frag" | grep -v '^#' | cut -d= -f1); do
    grep -q "^$k=y" "$KOUT/.config" || { echo "kernel config: $k did not stick"; exit 1; }
  done
fi
grep -q "^CONFIG_PHYSICAL_START=0x100000" "$KOUT/.config" && ! grep -q "^CONFIG_RELOCATABLE=y" "$KOUT/.config" || { echo "kernel must be non-relocatable at 1 MB"; exit 1; }
cp "$KOUT/arch/x86/boot/bzImage" "$OUT/bzImage"
cp "$KOUT/.config" "$OUT/kernel.config"

# 3. boot disk: 63 spt x 16 heads x 32 cyl (86Box "63, 16, 32"), MBR + FAT partition at LBA 63, syslinux
IMG=$OUT/c_drive.img; SEC=$((63*16*32)); PSEC=$((SEC-63)); OFF=$((63*512))
rm -f "$IMG"; truncate -s $((SEC*512)) "$IMG"
mformat -i "$IMG@@$OFF" -T $PSEC -h 16 -s 63 -v LPUPPET ::
cat > "$OUT/syslinux.cfg" <<CFG
DEFAULT linux
PROMPT ${KPROMPT:-0}
TIMEOUT ${KTIMEOUT:-1}
LABEL linux
  KERNEL bzImage
  APPEND ${KAPPEND:-$( [ -f "$HERE/$STAGE.append" ] && cat "$HERE/$STAGE.append" || echo "console=ttyS0,115200n8 console=tty0 quiet loglevel=3")}
CFG
mcopy -i "$IMG@@$OFF" "$OUT/bzImage" ::bzImage
mcopy -i "$IMG@@$OFF" "$OUT/syslinux.cfg" ::syslinux.cfg
"$SLX" -i -t $OFF "$IMG"  # install ldlinux into the FAT partition
python3 - "$IMG" "$SDIR/bios/mbr/mbr.bin" <<'PY'
import sys,struct
img,mbr=sys.argv[1],sys.argv[2]
spt,hd,cyl=63,16,32
total=spt*hd*cyl
def chs(lba):
    c=lba//(spt*hd); h=(lba//spt)%hd; s=lba%spt+1
    return bytes([h,(s&63)|((c>>8)&3)<<6,c&255])
ent=bytes([0x80])+chs(63)+bytes([0x0e])+chs(total-1)+struct.pack('<II',63,total-63)
with open(img,'r+b') as f:
    f.write(open(mbr,'rb').read()[:440]); f.seek(446); f.write(ent+bytes(48)+b'\x55\xaa')
PY
# 4. optional 1.44 MB floppy (FAT12 + syslinux): boots on any machine without a hand-set BIOS hard disk type
if [ "$FLOPPY" = 1 ]; then
  FIMG=$OUT/a_drive.img; rm -f "$FIMG"
  mformat -i "$FIMG" -C -f 1440 -v PUPPET ::
  mcopy -i "$FIMG" "$OUT/bzImage" ::bzImage
  mcopy -i "$FIMG" "$OUT/syslinux.cfg" ::syslinux.cfg
  "$SLX" -i "$FIMG"
fi
ls -l "$OUT"
