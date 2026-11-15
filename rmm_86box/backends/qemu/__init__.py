# SPDX-License-Identifier: MIT
"""QEMU behind the Rmm surface: local, headless.

    from rmm_86box.backends import qemu
    m = qemu.start("isapc", fda="/img/dos.img")         # QemuMachine, connected and running
    m = qemu.start("q800", bios="Quadra-650.ROM", scsi=["pram.img", "hd.qcow2"])
    m = qemu.start("ss5", hda="sol.qcow2", cdrom="sol.iso")
    m = qemu.attach("/path/qmp.sock", "isapc")          # a QEMU someone else started with -qmp unix:
    m.close()                                           # stops a QEMU that start() launched

Profiles: profiles.PROFILES (isapc, q800, ss5; aliases i386-isapc, m68k, sparc).
"""

from .machine import QemuMachine
from .process import QemuProcess
from .profiles import PROFILES, Profile


def start(profile, wait=10.0, resume=True, **opts):
    """Launch `profile` headless and connect. opts: run_dir, binary, memory_mib, vnc, gdb_port,
    args, env, and the profile's media options (fda, hda, cdrom, scsi, bios, boot, ...)."""
    proc = QemuProcess(profile, **opts).start()
    m = QemuMachine(proc.qmp_path, proc.profile, proc=proc, resume_on_connect=resume)
    try:
        m.connect(wait=wait)
    except Exception as e:
        log = proc.tail()                        # before stop(): it removes a run dir it made
        proc.stop()
        raise RuntimeError(f"QEMU {proc.profile.name} did not come up ({e}):\n" + "\n".join(log)) from None
    return m


def attach(qmp_address, profile, wait=0, resume=False):
    m = QemuMachine(qmp_address, profile, resume_on_connect=resume)
    m.connect(wait=wait)
    return m


__all__ = ["QemuMachine", "QemuProcess", "PROFILES", "Profile", "start", "attach"]
