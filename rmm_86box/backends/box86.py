# SPDX-License-Identifier: MIT
"""86Box: write its 86box.cfg, copy a VM config's NVR seed, run the binary with --monitor-port, and
attach the full `Rmm` client to it. Or attach to an 86Box somebody else started (no `launch`).

--monitor-port is used rather than [General] rmm_port: it wins over the cfg, is never saved
back (so cloned VM dirs do not end up sharing a port), and does not pause on connect.

Node keys (rmm-group/1): port (a number, or "auto" with launch), launch {binary, vm_dir, roms,
args, env, log, on_exit, wait}, vm_config, domain, box_cfg, vm_store.
"""

import os
import shutil
import subprocess
import sys
import time

from .. import kvm
from ..client import Rmm
from ..rpc import RpcError
from ..spec import SpecError, free_port, load_vm_config, merge, port_in_use
from . import Machine
from .proc import IS_WINDOWS, Proc, describe_exit  # noqa: F401 - describe_exit re-exported


# -- 86box.cfg ------------------------------------------------------------------------------------

def read_cfg(path):
    """{section: {key: value}} from an 86box.cfg (missing file -> {})."""
    out, sec = {}, None
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = out.setdefault(line[1:-1], {})
        elif sec is not None and "=" in line:
            k, _, v = line.partition("=")
            sec[k.strip()] = v.strip()
    return out


def write_cfg(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for sec, kv in cfg.items():
            if not kv:
                continue
            f.write(f"[{sec}]\n")
            for k, v in kv.items():
                f.write(f"{k} = {v}\n")
            f.write("\n")
    os.replace(tmp, path)


def node_cfg(spec, store, vm_config_dir=None, existing=None):
    """The cfg a launched node runs with, and the VM config it came from (or None):
    the existing cfg, then the VM config's box_cfg, then the domain's sections, then box_cfg."""
    raw = spec.raw
    cfg = existing or {}
    vmc = None
    if raw.get("vm_config"):
        vmc = load_vm_config(raw["vm_config"], vm_config_dir)
        cfg = merge(cfg, vmc["box_cfg"])
    dom = raw.get("domain")
    if dom:
        if isinstance(dom, str):
            try:
                dom = store.load(dom)
            except (AttributeError, OSError, ValueError) as e:
                raise SpecError(f"{spec.name}: domain {raw['domain']!r}: {e}") from None
        dcfg = kvm.to_cfg(dom)
        for sec in kvm.SECTIONS:                 # the domain owns these sections whole
            cfg.pop(sec, None)
        cfg = merge(cfg, dcfg)
    cfg = merge(cfg, raw.get("box_cfg") or {})
    cfg = {s: {k: str(int(v)) if isinstance(v, bool) else str(v) for k, v in kv.items()}
           for s, kv in cfg.items() if kv}
    return cfg, vmc


# -- the process ----------------------------------------------------------------------------------

def app_dir():
    """The folder a release bundle unpacked into (xp5-studio next to 86Box and roms/), else None:
    $XP5_STUDIO_DIR, or the folder of the running program (on macOS the one holding xp5-studio.app)
    when it has an 86Box and a roms/ beside it."""
    d = os.environ.get("XP5_STUDIO_DIR")
    if d:
        return d if os.path.isdir(d) else None
    for prog in (sys.argv[0], sys.executable, os.environ.get("CFProcessPath"), __file__):
        try:
            d = os.path.dirname(os.path.realpath(prog)) if prog else ""
        except OSError:                     # a path inside a .pyz on Windows (WinError 267)
            d = os.path.dirname(os.path.abspath(prog))
        for _ in range(6):                  # up out of xp5-studio.app/Contents/MacOS (or a package folder)
            if not d or d == os.path.dirname(d):
                break
            if os.path.isdir(os.path.join(d, "roms")) and any(
                    os.path.exists(os.path.join(d, n)) for n in ("86Box", "86Box.exe", "86Box.app")):
                return d
            d = os.path.dirname(d)
    return None


def bundled(name):
    """Path of `name` (86Box, or the roms dir) in the release bundle, else None."""
    d = app_dir()
    if not d:
        return None
    for rel in ((name, name + ".exe", os.path.join(name + ".app", "Contents", "MacOS", name)) if name != "roms"
                else ("roms",)):
        p = os.path.join(d, rel)
        if os.path.exists(p):
            return p
    return None


class Launched(Proc):
    """One 86Box this library started."""

    def __init__(self, spec, port, store, vm_config_dir=None):
        self.spec = spec
        self.port = port
        L = spec.launch
        vm_dir = os.path.abspath(L["vm_dir"])
        os.makedirs(vm_dir, exist_ok=True)
        cfg_path = os.path.join(vm_dir, "86box.cfg")
        cfg, vmc = node_cfg(spec, store, vm_config_dir, read_cfg(cfg_path))
        if vmc and vmc.get("vm_seed") and os.path.isdir(os.path.join(vmc["vm_seed"], "nvr")) \
                and not os.path.isdir(os.path.join(vm_dir, "nvr")):
            shutil.copytree(os.path.join(vmc["vm_seed"], "nvr"), os.path.join(vm_dir, "nvr"))
        if cfg:
            write_cfg(cfg_path, cfg)
        elif not os.path.exists(cfg_path):
            raise SpecError(f"{spec.name}: {cfg_path} does not exist and the node gives no "
                            "vm_config, domain or box_cfg to write one")
        self.binary = (L.get("binary") or (vmc or {}).get("box_binary") or bundled("86Box")
                       or shutil.which("86Box") or "86Box")
        log = L.get("log") or os.path.join(vm_dir, "86box.log")
        args = [self.binary, "-P", vm_dir, "-N", "--monitor-port", str(port), "-L", log]
        roms = L.get("roms") or bundled("roms")
        if roms:
            args += ["-R", roms]
        args += [str(a) for a in L.get("args") or []]
        env = dict(os.environ, **{k: str(v) for k, v in (L.get("env") or {}).items()})
        if app_dir():                      # the frozen studio's own Qt6 must not leak into 86Box's Qt
            for k in [k for k in env if k.startswith(("QT_", "QML", "DYLD_", "LD_LIBRARY_PATH", "PYTHON"))
                      and k not in (L.get("env") or {}) and k != "QT_QPA_PLATFORM"]:
                del env[k]
        open(log, "w").close()                            # 86Box appends across runs
        self.args = args
        self.started = time.time()
        try:
            popen = subprocess.Popen(args, cwd=vm_dir, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=open(log, "a"),   # a crash before 86Box logs is still readable
                                     start_new_session=not IS_WINDOWS)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"{e} (bundle not found from argv0={sys.argv[0]!r} exe={sys.executable!r} "
                                    f"file={__file__!r})") from e
        super().__init__(popen, "86Box", log=log, vm_dir=vm_dir, on_exit=L.get("on_exit", "quit"))

    def bind_failed(self):
        return any("failed to bind" in ln for ln in self.log_tail(50, "RMM:"))


# -- the backend ----------------------------------------------------------------------------------

class Box86Machine(Machine):
    full_rmm = True
    frames = True
    frame_rate = 10.0

    def title_lines(self):
        try:
            return kvm.describe(self.rmm.vm.show())
        except (RpcError, OSError, KeyError) as e:
            return [f"(config.dump: {e})"]


def validate(spec):
    return None


def start(spec, vm_store=None, vm_config_dir=None, say=None, **_):
    say = say or (lambda text, tone="text": None)
    proc, port = None, spec.port if isinstance(spec.port, int) else None
    if spec.launch is not None:
        port = free_port(spec.host) if spec.port == "auto" else int(spec.port)
        if port_in_use(port, spec.host):
            raise RuntimeError(f"{spec.name}: {spec.host}:{port} is already in use (another 86Box "
                               "or a test run); give the node another port or \"auto\"")
        proc = Launched(spec, port, kvm.DomainStore(vm_store), vm_config_dir)
        say(f"started 86Box pid {proc.pid} on {spec.host}:{port}  ({proc.vm_dir})", "muted")
    elif port is None:
        raise SpecError(f"{spec.name}: an 86Box node needs a port")
    rmm = Rmm(port, spec.host, timeout=spec.rpc_timeout, vm_store=vm_store)
    return Box86Machine("86box", rmm, proc, spec.host, port)


def adapter(host, port, timeout=10.0, **opts):
    return Rmm(port, host, timeout=timeout, **opts)
