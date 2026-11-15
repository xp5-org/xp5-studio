# SPDX-License-Identifier: MIT
"""Machine profiles: one per QEMU machine the backend knows, and the argv that starts it.

A profile is the machine's fixed part (binary, -M, CPU, memory, framebuffer, what the input
devices are). Media and firmware are options the caller gives per VM:

    argv("isapc", run_dir, fda="/img/dos.img", hda="/img/c.qcow2")
    argv("q800", run_dir, bios="Quadra-650.ROM", scsi=["pram.img", "hd.qcow2"], cdrom="sys7.iso")
    argv("ss5", run_dir, hda="sol.qcow2", cdrom="solaris.iso")

Headless and local only: -display none, QMP on a unix socket in run_dir, VNC (when asked
for) on a unix socket next to it, nothing listening on TCP. The guest starts paused (-S) so
the client sees it from the first instruction; connect() resumes it, as with 86Box.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    binary: str                    # qemu-system-*
    machine: str                   # -M
    cpu: str = ""                  # -cpu; "" = the machine's default
    memory_mib: int = 16
    arch: str = "i386"             # for the domain and the gdbstub
    keyboard: str = "?"            # what input.devices reports
    mouse: str = "?"
    mouse_buttons: int = 2
    extra: tuple = ()              # always-on args (framebuffer size, firmware path, ...)
    options: tuple = ()            # per-VM options argv() accepts
    doc: str = ""
    aliases: tuple = field(default=())


PROFILES = {p.name: p for p in (
    Profile("isapc", "qemu-system-i386", "isapc", cpu="486", memory_mib=16, arch="i386",
            keyboard="at", mouse="ps2", mouse_buttons=3, extra=("-vga", "std"),
            options=("fda", "fdb", "hda", "hdb", "cdrom", "boot"),
            doc="ISA-only PC: i8042 keyboard and PS/2 mouse (relative only), ISA VGA",
            aliases=("i386-isapc",)),
    Profile("q800", "qemu-system-m68k", "q800", memory_mib=64, arch="m68k",
            keyboard="adb", mouse="adb", mouse_buttons=1, extra=("-g", "800x600x8"),
            options=("bios", "scsi", "cdrom", "boot"),
            doc="Macintosh Quadra 800: ADB keyboard and mouse (relative only), NuBus macfb; "
                "needs a Quadra ROM (bios=) and a PRAM/boot disk",
            aliases=("m68k", "m68k-q800")),
    Profile("ss5", "qemu-system-sparc", "SS-5", memory_mib=64, arch="sparc",
            keyboard="sun", mouse="sun", mouse_buttons=3, extra=("-g", "1024x768x8"),
            options=("hda", "hdb", "cdrom", "boot", "bios"),
            doc="SPARCstation 5 (sun4m): Sun keyboard and mouse on the ESCC (relative only), TCX "
                "framebuffer, OpenBIOS",
            aliases=("sparc", "sparc-ss5")),
)}


def get(name):
    for p in PROFILES.values():
        if name == p.name or name in p.aliases:
            return p
    raise KeyError(f"no QEMU profile {name!r}; known: {', '.join(sorted(PROFILES))}")


def sockets(run_dir):
    """Where a VM in run_dir listens: {"qmp": path, "vnc": path}."""
    return {"qmp": os.path.join(run_dir, "qmp.sock"), "vnc": os.path.join(run_dir, "vnc.sock")}


def argv(profile, run_dir, binary=None, memory_mib=None, vnc=False, gdb_port=None, paused=True,
         args=(), **opts):
    """The command line that starts `profile` headless with its sockets in run_dir."""
    p = get(profile) if isinstance(profile, str) else profile
    bad = set(opts) - set(p.options)
    if bad:
        raise ValueError(f"{p.name}: unknown options {sorted(bad)}; takes {', '.join(p.options)}")
    s = sockets(run_dir)
    a = [binary or p.binary, "-M", p.machine, "-m", str(memory_mib or p.memory_mib),
         "-display", "none", "-monitor", "none", "-audio", "none",     # q800's ASC fails with no host audio
         "-qmp", f"unix:{s['qmp']},server=on,wait=off",
         "-name", os.path.basename(os.path.normpath(run_dir)), *p.extra]
    if p.cpu:
        a += ["-cpu", p.cpu]
    if vnc:
        a += ["-vnc", f"unix:{s['vnc']}"]
    if gdb_port:
        a += ["-gdb", f"tcp:127.0.0.1:{int(gdb_port)}"]
    if paused:
        a.append("-S")
    a += _media(p, opts)
    return a + list(args)


def _media(p, opts):
    a = []
    if opts.get("bios"):
        a += ["-bios", opts["bios"]]
    for slot in ("fda", "fdb", "hda", "hdb"):
        if opts.get(slot):
            a += [f"-{slot}", opts[slot]]
    if p.name == "q800":
        # q800 disks hang off its SCSI bus: ids 0.. in the order given, the CD at 3
        for i, path in enumerate(opts.get("scsi") or ()):
            fmt = "qcow2" if path.endswith(".qcow2") else "raw"
            a += ["-drive", f"id=hd{i},file={path},format={fmt},if=none",
                  "-device", f"scsi-hd,scsi-id={i},drive=hd{i}"]
        if opts.get("cdrom"):
            a += ["-drive", f"id=cd0,file={opts['cdrom']},media=cdrom,if=none",
                  "-device", "scsi-cd,scsi-id=3,drive=cd0"]
    elif opts.get("cdrom"):
        a += ["-cdrom", opts["cdrom"]]
    if opts.get("boot"):
        a += ["-boot", opts["boot"]]
    return a
