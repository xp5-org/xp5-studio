# linuxpuppet

The Linux "puppet" guest: a tiny i486 Linux 6.1 that an 86Box VM boots, and that the host drives through a RAM
mailbox over RMM (no network, no serial). The host PUTs small static test programs ("personalities") into the guest,
runs them as children of the puppet, and reads back their output. Design and phase status: `puppet_plan.md` in the
86box_remote_monitor container.

## Layout

| path | what |
|---|---|
| `linux/linuxpuppet.c` | the guest `/init` (freestanding, no libc): mailbox loop, loader ops PUT/GET/RUN/KEXEC |
| `linux/puppet_abi.h` | mailbox ABI (anchor at phys 0x80000, offsets, ops). **Mirrored by the host lib, change both together** |
| `linux/puppet_loader.h`, `pp_rt.h`, `sys.h`, `pci.h` | guest runtime: syscalls, port I/O, PCI, loader. Personalities include `pp_rt.h` |
| `linux/personalities/<name>/` | one test personality: `pp_<name>.c` + `manifest.json` (+ optional `include.txt`, one extra `-I` dir per line). Auto-discovered by the build |
| `linux/kernel.frag`, `linuxpuppet.frag` | kernel config fragments on top of `tinyconfig` (base, then the stage) |
| `linux/linuxpuppet.env` | stage knobs: i486 code/kernel, banned instruction regex, floppy image on |
| `linux/linuxpuppet.append` | kernel command line (reserves the mailbox `memmap=64K$0x80000` and the DMA buffer `memmap=32K$0x90000`) |
| `linux/bc_timeconst.py` | stand-in for `bc` (not installed on the build host; Kbuild only uses it for timeconst) |
| `linux/build_linux_guest.sh` | the whole build, see below |
| `host/` | host-side helpers: `mkdisk.py`/`cmp_crc.py` (ide, scsi test disk and CRC check), OFDM/ADC checks and sound session scripts |
| `vmcfg/<cell>/` | the 86Box cells (machine + card configs) the personalities run on |

## Building the guest

    linux/build_linux_guest.sh linuxpuppet

Needs: `gcc` (with `-m32`, i.e. gcc-multilib), `nasm`, `perl`, `python3`, `objdump`, `make`, `mtools` (`mformat`,
`mcopy`), `curl`, `sha256sum`. No root, no `bc`. First build downloads and compiles the kernel (several minutes);
later builds reuse the trees and just rebuild the guest binary, personalities and image.

Environment: `PUPPET_ART` (work and output dir, default `/testsrc/disk_artifacts/linuxpuppet`), `PUPPET_DL_CACHE`
(optional dir holding the pinned tarballs so nothing is downloaded, default `/testsrc/disk_artifacts/linux-r128`).

What it does, in order:

1. **Inputs**, pinned and sha256-checked against the vendors' own sums files, each extracted into its own dir:
   `linux-6.1.189.tar.xz` (cdn.kernel.org) and `syslinux-6.03.tar.xz`. Untrusted downloads never run in place.
2. **Patched kernel tree** (`src/linux-6.1.189-pup`): `relocate_kernel_32.S` saves `%cr4` unconditionally before
   KEXEC, which faults on a CR4-less i486 (no IDT, triple fault, BIOS reset). The build guards it with `cpu_has_pae`
   and aborts if the patch does not apply.
3. **Patched syslinux** (`src/syslinux-6.03-88h`): the 6.03 core sizes INT 15h AH=88h memory from CX instead of AX
   (`core/bios.c`); on a BIOS without E820/E801 (the pb430's 1993 Phoenix) it sees no extended memory and fails
   "Booting kernel failed". The build fixes that one line and rebuilds the core + `ldlinux.c32` together (the
   release `ldlinux.c32` is refused by a rebuilt core). Needs `nasm`; GCC 14 needs the `-Wno-error` set in the script.
4. **Guest `/init`** from `linuxpuppet.c`: `-m32 -march=i486 -nostdlib -static -ffreestanding`. The build then
   disassembles it and fails if it contains an instruction an i486 lacks (`GUEST_BAD` in `linuxpuppet.env`:
   cmov, endbr32, SSE, multi-byte nop, rdtsc, cpuid, cmpxchg8b).
5. **Personalities**: every `personalities/*/` with a `manifest.json` is compiled with the same flags and the same
   instruction check into `out/personalities/<name>/bin/<stem>`. They are *not* in the initramfs; the host PUTs them
   into the running guest (`Puppet.load`), per the `files`/`default_argv` in `manifest.json`.
6. **Kernel**: `tinyconfig` + `kernel.frag` + `linuxpuppet.frag` (re-applied every build), initramfs embedded.
   Checks that the CPU choice (`M486`), VGA console, gzip stuck, that every `=y` in the stage fragment stuck, and that
   the kernel is non-relocatable at 1 MB (`PHYSICAL_START=0x100000`; `RELOCATABLE=y` fails in the decompressor with
   gcc 14).
7. **Boot media**, in `out/linuxpuppet/`: `c_drive.img` (63x16x32 CHS, MBR + FAT partition at LBA 63 + syslinux,
   86Box geometry `63, 16, 32`), `a_drive.img` (1.44 MB FAT12 floppy + syslinux, boots on any machine without a hand-set
   BIOS disk type), `bzImage`, `kernel.config`, `syslinux.cfg`.

Known traps: tinyconfig drops the 32-bit time syscalls (sleeps return at once, so `kernel.frag` re-enables them);
port I/O needs `X86_IOPL_IOPERM`; there is no libgcc, so no 64-bit division or soft-float in guest code.

## Running it

In the test manager the container is `linuxpuppet--i386` (source dir `sourcedir/linuxpuppet`, testlists only; they find
this tree through the token `lp_src`). `linuxpuppet.build` runs the script and registers the images and the
personalities tar as tool outputs; `p0`..`p11` boot a cell and drive it. The host side of the mailbox is
`rmm_86box/linuxpuppet.py` (one copy, in this repo: `../rmm_86box/`, with `puppet_checks.py`, `puppet_vga.py`, `snapfile.py`):

    python3 -m rmm_86box.linuxpuppet --port 56560 wait
    python3 -m rmm_86box.linuxpuppet --port 56560 load <personality dir>
    python3 -m rmm_86box.linuxpuppet --port 56560 run -- /pp/ide/bin/pp_ide run

## Adding a personality

Create `linux/personalities/<name>/pp_<name>.c` (freestanding, `#include "pp_rt.h"`) and `manifest.json`, rebuild,
add a `__testlist__` in the container. Output convention: `<X> CHK <NAME> PASS|FAIL|NA`, `<X> DONE RESULT=...`.
