# SPDX-License-Identifier: MIT
"""How to start and drive an emulator, in one place: 86Box, QEMU, VICE and SIMH. The Qt manager
(rmmgui), rmmctl and any other tool call the same code.

    from rmm_86box import backends
    from rmm_86box.spec import MachineSpec

    spec = MachineSpec.of("c64", {"backend": "vice", "vice": {"image": "game.d64"}})
    m = backends.get(spec.backend).start(spec)
    m.rmm.connect(wait=20)
    m.rmm.call("machine.pause"); m.rmm.call("snapshot.save", tag="a")
    m.stop()

A backend has a name, a `validate(spec)` that returns an error text or None, and a `start(spec)`
that launches the emulator (or attaches to a running one) and returns a Machine. Every Machine's
`rmm` answers the same method names as the 86Box monitor, for the subset the emulator can do
(machine.status|pause|resume|quit, snapshot.list|save|restore|delete, mem.phys.read,
capture.screenshot); what it cannot do raises RpcError(code=-32601), as an 86Box without that
method would. `Machine.full_rmm` says the adapter is the whole 86Box `Rmm` (console, registers,
breakpoints, media); `Machine.frames` says it answers capture.screenshot.

An emulator's backend module also holds its control code (qemu/adapter.py + qemu/legacy.py QmpRmm, vice.py ViceRmm,
simh.py SimhRmm), so adding one is: write backends/<name>.py, register() it below.
"""

import importlib

from ..spec import MachineSpec  # noqa: F401 - re-exported for callers


class Backend:
    def __init__(self, name, module, uses_port=True, mode=None, serve=False):
        self.name = name
        self._module = module                    # imported on first use: simh pulls in pty, vice struct code, ...
        self.uses_port = uses_port               # False: no TCP port to check for clashes
        self.mode = mode                         # fixed NodeSpec.mode, else launch / attach
        self.serve = serve                       # control dies with the launcher: a long-lived caller must
                                                 # run it under serve.py and attach (SIMH's pty)

    @property
    def impl(self):
        return importlib.import_module(self._module, __name__)

    def validate(self, spec):
        """An error text for a spec this backend cannot run, else None."""
        fn = getattr(self.impl, "validate", None)
        return fn(spec) if fn else None

    def attaches(self, spec):
        """True when start(spec) only connects to a running machine."""
        fn = getattr(self.impl, "attaches", None)
        return fn(spec) if fn else spec.launch is None

    def attach_port(self, spec):
        """The TCP port an attaching spec names."""
        fn = getattr(self.impl, "attach_port", None)
        return fn(spec) if fn else spec.port

    def start(self, spec, **ctx):
        """Launch or attach -> Machine. ctx: vm_store, vm_config_dir, say(text, tone)."""
        return self.impl.start(spec, **ctx)

    def adapter(self, host, port, timeout=10.0, **opts):
        """A not yet connected adapter for a machine that is already running (rmmctl's records)."""
        return self.impl.adapter(host, port, timeout=timeout, **opts)


class Machine:
    """A started or attached machine. rmm: the adapter. proc: the Proc this call launched, None for
    an attached one. cleanup() runs after stop()."""

    full_rmm = False
    frames = False
    frame_rate = 5.0                             # the poller's default rate when the spec gives none

    def __init__(self, backend, rmm, proc=None, host="127.0.0.1", port=None, cleanup=None):
        self.backend = backend
        self.rmm = rmm
        self.proc = proc
        self.host = host
        self.port = port
        self._cleanup = cleanup

    def title_lines(self):
        return [getattr(self.rmm, "banner", "") or self.backend]

    def tty(self, tail=None):
        """Console text for a backend whose console is a terminal (SIMH), else None."""
        return None

    def type_line(self, text):
        raise OSError(f"{self.backend} has no terminal console")

    def stop(self):
        if self.proc is not None and self.proc.on_exit != "keep":
            self.proc.stop(self.rmm)
        self.rmm.close()
        if self._cleanup:
            self._cleanup()


_REGISTRY = {}


def register(backend):
    _REGISTRY[backend.name] = backend
    return backend


def find(name):
    return _REGISTRY.get(name)


def get(name):
    b = _REGISTRY.get(name)
    if b is None:
        raise KeyError(f"backend {name!r} is not one of {', '.join(names())}")
    return b


def names():
    return sorted(_REGISTRY)


register(Backend("86box", ".box86"))
register(Backend("qemu", ".qemu.adapter", mode="qemu"))
register(Backend("simh", ".simh", uses_port=False, mode="simh", serve=True))
register(Backend("vice", ".vice", uses_port=False, mode="vice"))
