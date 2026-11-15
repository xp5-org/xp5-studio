# SPDX-License-Identifier: MIT
"""The registry face of the QEMU backend (backends.get("qemu")): the machine specs of the Qt manager,
rmmctl and scripts.

A spec takes one of two shapes:
  "qemu": {"profile": "isapc", "fda": ..., "qmp": ...}   this package's QemuMachine: started headless
                                                         here (no qmp), or attached by QMP (unix socket
                                                         path or host:port); profiles.PROFILES
  "port": N [, "qemu_argv": [...]]                       the older shape: a QEMU someone else started with
                                                         -qmp tcp:..., driven by legacy.QmpRmm
"""

from .. import Machine
from ..proc import Proc
from . import legacy, profiles
from .machine import QemuMachine
from .process import QemuProcess

KEYS = {"profile", "qmp", "run_dir", "binary", "memory_mib", "args", "env", "vnc", "gdb_port",
        "on_exit", "wait"}                      # plus the profile's own options (profiles.Profile.options)
NOT_WITH_QEMU = ("port", "mock", "launch", "vm_config", "domain", "box_cfg")


class QemuProc(Proc):
    """A QEMU this library started, in the shape the GUI reads: the Proc interface over a QemuProcess."""

    def __init__(self, qp, on_exit="quit"):
        super().__init__(qp.proc, "QEMU", log=qp.log, vm_dir=qp.run_dir, on_exit=on_exit)
        self.qp = qp
        self.args = qp.argv

    def stop(self, rmm=None, timeout=5.0):
        """QMP quit when connected, then SIGTERM, then SIGKILL; a temp run dir goes with it."""
        self.qp.stop(getattr(rmm, "qmp", None), timeout)


class Qemu(Machine):
    """A QEMU behind QemuMachine: the whole Rmm surface minus registers and breakpoints (-32601)."""

    full_rmm = True
    frames = True
    frame_rate = 10.0

    def __init__(self, rmm, proc=None, host="127.0.0.1", address=None):
        super().__init__("qemu", rmm, proc, host)
        self.address = address or f"qmp {rmm.qmp.address}"

    def title_lines(self):
        from ... import kvm
        return kvm.describe(self.rmm.vm.show())


def _block(spec):
    return getattr(spec, "qemu", None)


def validate(spec):
    q = _block(spec)
    if q is None:
        return legacy.validate(spec)
    if not isinstance(q, dict):
        return "qemu is an object"
    clash = [k for k in NOT_WITH_QEMU if spec.raw.get(k) not in (None, False)]
    if clash:
        return f"a qemu node takes no {', '.join(clash)} (QEMU options go in the qemu block)"
    try:
        p = profiles.get(q.get("profile") or "")
    except KeyError as e:
        return f"qemu.profile: {e.args[0]}"
    bad = set(q) - KEYS - set(p.options)
    if bad:
        return f"qemu: unknown keys {sorted(bad)}; {p.name} takes {', '.join(sorted(KEYS | set(p.options)))}"
    if q.get("on_exit", "quit") not in ("quit", "keep"):
        return "qemu.on_exit is quit or keep"
    return None


def attaches(spec):
    q = _block(spec)
    return bool(q.get("qmp")) if q is not None else legacy.attaches(spec)


def attach_port(spec):
    return None if _block(spec) is not None else spec.port


def _address(qmp, host, timeout):
    """A unix socket path stays a path; host:port becomes (host, port)."""
    h, _, port = qmp.rpartition(":")
    return (h or host, int(port)) if port.isdigit() and "/" not in qmp else qmp


def start(spec, say=None, **ctx):
    q = _block(spec)
    if q is None:
        return legacy.start(spec, say=say, **ctx)
    say = say or (lambda text, tone="text": None)
    q = dict(q)
    profile, qmp = q.pop("profile"), q.pop("qmp", None)
    on_exit = q.pop("on_exit", "quit")
    q.pop("wait", None)                         # the group's launch-wait, read by hostgroup
    if qmp:
        m = QemuMachine(_address(qmp, spec.host, spec.rpc_timeout), profile, timeout=spec.rpc_timeout,
                        resume_on_connect=False)
        return Qemu(m, None, spec.host, f"qmp {qmp}")
    qp = QemuProcess(profile, **q).start()
    say(f"started QEMU {qp.profile.name} pid {qp.pid}  ({qp.run_dir})", "muted")
    m = QemuMachine(qp.qmp_path, qp.profile, proc=qp, timeout=spec.rpc_timeout, stop_on_close=False)
    return Qemu(m, QemuProc(qp, on_exit), spec.host, f"qmp {qp.qmp_path}")


def adapter(host, port, timeout=10.0, **opts):
    return legacy.adapter(host, port, timeout=timeout, **opts)
