# SPDX-License-Identifier: MIT
"""rmmctl, the xp5-studio command line: list and drive emulator instances from a shell, the way virsh or docker do.
Stdlib only. It drives 86Box, QEMU, VICE and SIMH through rmm_86box.backends, the same code the Qt manager uses.

    rmmctl ps [-a] [--json]                       instances: state, turbo, where, pid, uptime
    rmmctl status NAME                            one instance in detail
    rmmctl start NAME --vm-dir DIR [--binary B] [--roms DIR] [--port N|auto] [--domain D] [--turbo] [--paused]
    rmmctl start NAME --node JSON|@FILE           any backend, from a session-file node: {"backend": "vice", "vice": {...}}
    rmmctl attach NAME HOST:PORT [--backend B]    register a machine that runs elsewhere (qemu: its QMP port)
    rmmctl snap NAME list|save|restore|delete [TAG]   group-style snapshots on any backend
    rmmctl pause|resume NAME... | --all             (a pause outlasts rmmctl only with sticky pause: see sticky)
    rmmctl turbo NAME [on|off|toggle]             run unthrottled (86Box fast forward)
    rmmctl stop NAME...  [--all]                  machine.quit; a launched process is signalled if it hangs
    rmmctl rm NAME... [-f]                        forget an instance (-f: stop it first)
    rmmctl sticky NAME... [on|off]                keep a pause after rmmctl disconnects (86Box sticky pause)
    rmmctl break NAME ADDR... [--wait S]          (ADDR: 0x1234, SEG:OFF or intN) arm breakpoints, run to one, report registers
    rmmctl media NAME [list|insert|eject|reload|new]   change disks: insert fdd.0 PATH, eject cdrom.0
    rmmctl config NAME get|set|describe|pending|discard   86Box settings (applied at the next hard reset)
    rmmctl screenshot NAME [-o FILE] [-d DIR]     PNG of the guest screen; %d or {n} in FILE auto-increments
    rmmctl session save|load|show|rm NAME | ls   the registered instances as a Qt manager group file (all backends)
    rmmctl console NAME [LINE ...]                the RMM console, interactive or one-shot
    rmmctl vm list|show NAME                      the VM configs in the domain store

Instances are records in <home>/instances/NAME.json, home being --home, $RMM_86BOX_HOME or
~/.rmm_86box. A record is a name and an address, so it works the same for an 86Box this tool
launched and one attached over TCP. Exit status is 0 when every named instance answered.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

from . import backends
from .backends import serve
from .client import Frame, Rmm
from .console import Console
from .kvm import DomainStore, describe
from .rpc import RpcError
from .spec import MachineSpec, SpecError

PROBE_TIMEOUT = 1.5
_children = {}                 # pid -> Popen, so a process we started and that ended is reaped, not a zombie


class CtlError(Exception):
    pass


# -- the instance registry ------------------------------------------------------------------------

class Registry:
    def __init__(self, home=None):
        self.home = home or os.environ.get("RMM_86BOX_HOME") or os.path.join(os.path.expanduser("~"), ".rmm_86box")
        self.dir = os.path.join(self.home, "instances")
        self.domains = DomainStore(os.path.join(self.home, "domains"))

    def _file(self, name):
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in name):
            raise CtlError(f"an instance name is letters, digits, '.', '_' or '-' (got {name!r})")
        return os.path.join(self.dir, name + ".json")

    def names(self):
        return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json")) if os.path.isdir(self.dir) else []

    def get(self, name):
        try:
            with open(self._file(name)) as f:
                return json.load(f)
        except FileNotFoundError:
            raise CtlError(f"no instance {name!r} (rmmctl ps -a lists them)") from None

    def has(self, name):
        return os.path.exists(self._file(name))

    def put(self, rec):
        os.makedirs(self.dir, exist_ok=True)
        path = self._file(rec["name"])
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    def drop(self, name):
        try:
            os.remove(self._file(name))
        except FileNotFoundError:
            pass


# -- probing --------------------------------------------------------------------------------------

BAD = ("error", "fatal", "rom", "fail", "cannot", "can't", "not found", "missing", "no such", "abort", "unable")


def diagnose(rec, n=6):
    """Why a launched 86Box is not answering: the lines of its stderr and 86box.log that look like an
    error (a missing ROM set shows up here, as a dialog and a stderr line), else the last few."""
    lines = []
    for path in (rec.get("stderr"), rec.get("log")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines += [ln.rstrip() for ln in f.read().splitlines() if ln.strip() and not ln.startswith("#")]
        except (OSError, TypeError):
            pass
    hits = [ln for ln in lines if any(b in ln.lower() for b in BAD)]
    return (hits or lines)[-n:]


def explain(rec, why):
    """A CtlError text: why, then what 86Box said about it."""
    tail = diagnose(rec)
    return why + ("\n" + "\n".join("  86Box: " + ln for ln in tail) if tail else "\n  (86Box wrote nothing to its log or stderr)")


def pid_alive(pid):
    if not pid:
        return False
    p = _children.get(pid)
    if p is not None:
        return p.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


NATIVE_BACKEND = "86box"                  # the one whose monitor is the whole Rmm: console, breakpoints, media, config


def backend_of(rec):
    return rec.get("backend") or NATIVE_BACKEND


def connect(rec, timeout=PROBE_TIMEOUT, full=None):
    """A connected adapter for the record. Never resumes a guest that 86Box paused on attach.
    full="break": the command needs an 86Box's whole monitor, so any other backend refuses it."""
    be = backend_of(rec)
    if full and be != NATIVE_BACKEND:
        raise CtlError(f"{rec['name']}: {full} needs an 86Box; this is a {be} machine")
    try:
        backend = backends.get(be)
    except KeyError as e:
        raise CtlError(f"{rec['name']}: {e.args[0]}") from None
    opts = {"resume_on_connect": False} if be == NATIVE_BACKEND else {"snapdir": rec.get("snapdir")}
    r = backend.adapter(rec["host"], rec["port"], timeout=timeout, **opts)
    try:
        r.connect()
    except (OSError, RpcError) as e:
        r.close()
        raise CtlError(f"{rec['name']}: {rec['host']}:{rec['port']} does not answer ({e})") from None
    return r


def probe(rec):
    """The live view of a record: state (running | paused | starting | down), turbo, tsc."""
    out = {"name": rec["name"], "kind": rec["kind"], "backend": backend_of(rec), "where": f"{rec['host']}:{rec['port']}",
           "pid": rec.get("pid"), "started": rec.get("started"), "state": "down", "turbo": None, "sticky": None, "tsc": None}
    try:
        r = connect(rec)
    except CtlError:
        if rec["kind"] == "launched":
            out["state"] = "starting" if pid_alive(rec.get("pid")) and time.time() - rec.get("started", 0) < 30 else (
                "unresponsive" if pid_alive(rec.get("pid")) else "exited")
        return out
    try:
        st = r.call("machine.status")
        out["state"], out["tsc"] = st.get("state", "?"), st.get("tsc")
        try:
            out["turbo"] = bool(r.call("machine.fast_forward.get")["value"])
        except RpcError:
            pass
        try:
            out["sticky"] = bool(r.call("rmm.sticky_pause.get")["value"])
        except RpcError:
            pass                                        # an 86Box without the setting
    except (OSError, RpcError):
        pass
    finally:
        r.close()
    return out


# -- output ---------------------------------------------------------------------------------------

def uptime(started):
    if not started:
        return "-"
    s = int(max(0, time.time() - started))
    if s >= 86400:
        return f"{s // 86400}d{s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60}m"
    return f"{s // 60}m{s % 60}s" if s >= 60 else f"{s}s"


def table(rows, cols):
    widths = [max(len(c), *(len(str(r[i])) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()]
    lines += ["  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


# -- commands -------------------------------------------------------------------------------------

def targets(reg, a):
    names = reg.names() if getattr(a, "all", False) else list(a.names)
    if not names:
        raise CtlError("name an instance (or --all)")
    for n in names:
        reg.get(n)
    return names


def cmd_ps(reg, a):
    rows = [probe(reg.get(n)) for n in reg.names()]
    if not a.all:
        rows = [r for r in rows if r["state"] in ("running", "paused", "starting")]
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(table([(r["name"], r["state"], "-" if r["turbo"] is None else ("on" if r["turbo"] else "off"),
                  r["where"], r["kind"], r["backend"], r["pid"] or "-", uptime(r["started"]) if r["kind"] == "launched" else "-")
                 for r in rows], ["NAME", "STATE", "TURBO", "WHERE", "KIND", "BACKEND", "PID", "UP"]))
    return 0


def cmd_status(reg, a):
    rec = reg.get(a.name)
    p = probe(rec)
    print(f"name     {rec['name']}\nstate    {p['state']}\nwhere    {p['where']}  ({rec['kind']}, {p['backend']})")
    if rec.get("pid"):
        print(f"pid      {rec['pid']}  ({'alive' if pid_alive(rec['pid']) else 'gone'})")
    for k in ("vm_dir", "binary", "log"):
        if rec.get(k):
            print(f"{k:<8} {rec[k]}")
    if p["state"] not in ("running", "paused"):
        for ln in diagnose(rec):
            print(f"86box    {ln}")
        return 1
    print(f"turbo    {'on' if p['turbo'] else 'off' if p['turbo'] is not None else '?'}")
    print(f"sticky   {'on' if p['sticky'] else 'off' if p['sticky'] is not None else '? (this 86Box has no sticky pause)'}")
    print(f"tsc      {p['tsc']}")
    r = connect(rec)
    try:
        for s in r.call("media.list").get("slots", []) if p["backend"] == NATIVE_BACKEND else []:
            print(f"media    {s.get('slot', s.get('name', '?'))}: {s.get('path') or s.get('image') or '(empty)'}")
    except (RpcError, OSError):
        pass
    finally:
        r.close()
    return 0


def free_port(host="127.0.0.1"):
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def attach_node(be, host, port, snapdir=None):
    """The session-file node that attaches to a machine of backend `be` on host:port."""
    if be == "qemu":
        return {"backend": "qemu", "host": host, "port": port}
    return {"backend": be, "host": host, be: dict({"attach": True, "port": port}, **({"snapdir": snapdir} if snapdir else {}))}


def start_node(reg, name, raw, wait=20.0, check=True):
    """Start or attach a machine of any backend from its session-file node (the dict a group file holds
    for it) and register it. The launching, validation and control are rmm_86box.backends', the code the
    Qt manager runs. A backend whose control dies with the launcher (SIMH's pty) runs under backends.serve."""
    spec = MachineSpec.of(name, raw)
    try:
        backend = backends.get(spec.backend)
    except KeyError as e:
        raise CtlError(f"{name}: {e.args[0]}") from None
    bad = backend.validate(spec)
    if bad:
        raise CtlError(f"{name}: {bad}")
    if reg.has(name) and probe(reg.get(name))["state"] in ("running", "paused", "starting"):
        raise CtlError(f"{name} is already up")
    rec = dict(name=name, backend=spec.backend, host=spec.host, pid=None, started=time.time(), node=raw)
    if backend.attaches(spec):
        rec.update(kind="attached", port=backend.attach_port(spec), snapdir=(raw.get(spec.backend) or {}).get("snapdir"))
        if check:
            connect(rec).close()
        reg.put(rec)
        print(f"{name}: attached {spec.backend} {rec['host']}:{rec['port']}")
        return 0
    if spec.backend == "vice":                          # snapshots need a directory that outlives this command
        raw = dict(raw, vice=dict(raw["vice"], snapdir=raw["vice"].get("snapdir") or os.path.join(reg.home, "snapshots", name)))
        os.makedirs(raw["vice"]["snapdir"], exist_ok=True)
        spec, rec["node"], rec["snapdir"] = MachineSpec.of(name, raw), raw, raw["vice"]["snapdir"]
    log_dir = os.path.join(reg.home, "logs")
    try:
        if backend.serve:
            popen, port, rec["log"], rec["stderr"] = serve.spawn(spec, log_dir, wait)
        else:
            machine = backend.start(spec)
            popen, port = (machine.proc.popen if machine.proc else None), machine.port
    except (ValueError, SpecError, RuntimeError, OSError) as e:
        raise CtlError(f"{name}: {e}") from None
    if popen is None:
        raise CtlError(f"{name}: the {spec.backend} backend started no process (it attaches: give it a port)")
    _children[popen.pid] = popen
    rec.update(kind="launched", port=port, pid=popen.pid)
    reg.put(rec)
    deadline = time.time() + wait
    while time.time() < deadline:
        if popen.poll() is not None:
            reg.drop(name)
            raise CtlError(explain(rec, f"{name}: {spec.backend} exited with {popen.returncode}"))
        try:
            connect(rec).close()
            print(f"{name}: started {spec.backend} pid {popen.pid} on {rec['host']}:{port}")
            return 0
        except CtlError:
            time.sleep(0.2)
    popen.terminate()
    reg.drop(name)
    raise CtlError(explain(rec, f"{name}: {spec.backend} (pid {popen.pid}) never answered on port {port} in {wait:g}s"))


def node_arg(a):
    """The --node argument: JSON text, or @FILE holding it."""
    text = open(a.node[1:]).read() if a.node.startswith("@") else a.node
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise CtlError(f"--node: not JSON ({e})") from None
    if not isinstance(raw, dict):
        raise CtlError("--node is a node object, like one entry of a session file's nodes")
    if a.backend and raw.get("backend", a.backend) != a.backend:
        raise CtlError(f"--backend {a.backend} contradicts the node's backend {raw['backend']!r}")
    return raw


def cmd_start(reg, a):
    a.node, a.backend = getattr(a, "node", None), getattr(a, "backend", None)
    if a.node or (a.backend and a.backend != NATIVE_BACKEND):
        if not a.node:
            raise CtlError(f"start --backend {a.backend} needs --node JSON|@FILE: the node a session file would hold")
        raw = node_arg(a)
        if raw.get("backend", NATIVE_BACKEND) == NATIVE_BACKEND:
            raise CtlError("an 86Box starts from --vm-dir (or `session load`); --node is for the other backends")
        return start_node(reg, a.name, raw, a.wait)
    if not a.vm_dir:
        raise CtlError("start NAME --vm-dir DIR (an 86Box), or --backend B --node JSON")
    if reg.has(a.name) and probe(reg.get(a.name))["state"] in ("running", "paused", "starting"):
        raise CtlError(f"{a.name} is already up")
    binary = a.binary or shutil.which("86Box") or "86Box"
    vm_dir = os.path.abspath(a.vm_dir)
    os.makedirs(vm_dir, exist_ok=True)
    cfg = os.path.join(vm_dir, "86box.cfg")
    if a.domain:
        from .kvm import to_cfg
        sections = to_cfg(reg.domains.load(a.domain))
        with open(cfg, "w") as f:
            for sec, kv in sections.items():
                f.write(f"[{sec}]\n" + "".join(f"{k} = {v}\n" for k, v in kv.items()) + "\n")
    elif not os.path.exists(cfg):
        raise CtlError(f"{cfg} does not exist; give --domain NAME or a VM dir that has an 86box.cfg")
    port = free_port() if a.port == "auto" else int(a.port)
    log = os.path.join(vm_dir, "86box.log")
    err_path = os.path.join(vm_dir, "86box.stderr")
    open(log, "w").close()
    roms = a.roms or os.environ.get("RMMCTL_ROMS")
    if roms and not os.path.isdir(roms):
        raise CtlError(f"--roms {roms}: not a directory")
    args = [binary, "-P", vm_dir, "-N", "--monitor-port", str(port), "-L", log] + (["-R", roms] if roms else []) \
        + (["--monitor-sticky-pause"] if a.sticky else []) + list(a.extra or [])
    try:
        with open(err_path, "w") as err:
            proc = subprocess.Popen(args, cwd=vm_dir, stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
                                    start_new_session=not sys.platform.startswith("win"))
    except OSError as e:
        raise CtlError(f"cannot run {binary}: {e}") from None
    _children[proc.pid] = proc
    rec = dict(name=a.name, kind="launched", host="127.0.0.1", port=port, pid=proc.pid, vm_dir=vm_dir,
               binary=binary, log=log, stderr=err_path, roms=roms, started=time.time())
    reg.put(rec)
    deadline = time.time() + a.wait
    r = None
    while time.time() < deadline and r is None:
        if proc.poll() is not None:
            reg.drop(a.name)
            raise CtlError(explain(rec, f"{a.name}: 86Box exited with {proc.returncode} (log {log})"))
        try:
            r = connect(rec)
        except CtlError:
            time.sleep(0.2)
    if r is None:
        why = explain(rec, f"{a.name}: 86Box (pid {proc.pid}) is up but its monitor on port {port} never answered "
                           f"in {a.wait:g}s" + ("" if roms else "; no --roms given, so a missing ROM set is the usual cause"))
        proc.terminate()                                # a failed start leaves nothing behind
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reg.drop(a.name)
        raise CtlError(why)
    try:
        if a.turbo:
            r.call("machine.fast_forward.set", value=True)
        if a.paused:
            r.call("machine.pause")
    finally:
        r.close()
    print(f"{a.name}: started pid {proc.pid} on 127.0.0.1:{port}")
    return 0


def cmd_attach(reg, a):
    host, _, port = a.address.rpartition(":")
    if not host and a.address.isdigit():
        port = a.address
    if not port.isdigit():
        raise CtlError("address is HOST:PORT, or just PORT for this machine")
    if reg.has(a.name) and not a.force:
        raise CtlError(f"{a.name} exists (use --force to replace it)")
    if getattr(a, "backend", None) and a.backend != NATIVE_BACKEND:
        node = attach_node(a.backend, host or "127.0.0.1", int(port), getattr(a, "snapdir", None))
        return start_node(reg, a.name, node, check=not a.no_check)
    rec = dict(name=a.name, kind="attached", host=host or "127.0.0.1", port=int(port), pid=None,
               started=time.time())
    if not a.no_check:
        connect(rec).close()
    reg.put(rec)
    print(f"{a.name}: attached {rec['host']}:{rec['port']}")
    return 0


def each(reg, a, fn, verb, after=None):
    """Run fn(rmm) on each named instance. after(rec) runs once its connection is closed and returns an
    error text when what fn did did not survive the disconnect."""
    bad = 0
    for n in targets(reg, a):
        try:
            r = connect(reg.get(n))
        except CtlError as e:
            print(e, file=sys.stderr)
            bad = 1
            continue
        try:
            print(f"{n}: {fn(r)}")
        except (RpcError, OSError) as e:
            print(f"{n}: {verb} failed: {e}", file=sys.stderr)
            bad = 1
        finally:
            r.close()
        problem = after and after(reg.get(n))
        if problem:
            print(f"{n}: {problem}", file=sys.stderr)
            bad = 1
    return bad


def pause_held(rec):
    if probe(rec)["state"] == "paused":
        return None
    return ("86Box resumed it when rmmctl disconnected (sticky pause is off, so a pause lasts only while a client is "
            "connected). `rmmctl sticky NAME on` keeps it; `rmmctl start --sticky` does so from the start")


def cmd_pause(reg, a):
    return each(reg, a, lambda r: r.call("machine.pause").get("state", "paused"), "pause", after=pause_held)


def cmd_resume(reg, a):
    return each(reg, a, lambda r: r.call("machine.resume").get("state", "running"), "resume")


def cmd_turbo(reg, a):
    mode = None
    if a.names and (a.all or len(a.names) > 1) and a.names[-1] in ("on", "off", "toggle"):
        mode = a.names.pop()
    a.mode = mode

    def go(r):
        cur = bool(r.call("machine.fast_forward.get")["value"])
        want = {"on": True, "off": False, "toggle": not cur, None: cur}[a.mode]
        if want != cur:
            cur = bool(r.call("machine.fast_forward.set", value=want)["value"])
        return "turbo " + ("on" if cur else "off")
    return each(reg, a, go, "turbo")


def cmd_stop(reg, a):
    bad = 0
    for n in targets(reg, a):
        rec = reg.get(n)
        try:
            r = connect(rec)
            try:
                r.call("machine.quit", _timeout=3)
            except (RpcError, OSError):
                pass                                   # the socket closes as it exits
            r.close()
        except CtlError:
            pass
        pid = rec.get("pid")
        if pid:
            deadline = time.time() + a.timeout
            while pid_alive(pid) and time.time() < deadline:
                time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.3)
                if pid_alive(pid) and not sys.platform.startswith("win"):
                    os.kill(pid, signal.SIGKILL)
            if pid in _children:
                _children[pid].wait()
        elif probe(rec)["state"] in ("running", "paused"):
            deadline = time.time() + a.timeout
            while probe(rec)["state"] in ("running", "paused") and time.time() < deadline:
                time.sleep(0.2)
        gone = probe(rec)["state"] in ("down", "exited")
        print(f"{n}: {'stopped' if gone else 'still answering'}")
        bad |= not gone
    return bad


def cmd_rm(reg, a):
    for n in targets(reg, a):
        if probe(reg.get(n))["state"] in ("running", "paused", "starting"):
            if not a.force:
                raise CtlError(f"{n} is up (rmm stop it, or rm -f)")
            a.timeout = 5.0
            cmd_stop(reg, argparse.Namespace(names=[n], all=False, timeout=5.0))
        reg.drop(n)
        print(f"{n}: removed")
    return 0


def cmd_terminal(rec, a):
    """console on a machine whose console is a terminal (SIMH): type each LINE, then show the screen text."""
    r = connect(rec, timeout=10.0)
    try:
        for line in a.lines:
            r.call("console.type", text=line)
        if a.lines:
            time.sleep(0.5)
        print(r.call("console.tty", tail=2000)["text"])
        return 0
    except RpcError as e:
        raise CtlError(f"{rec['name']}: this {backend_of(rec)} machine has no console ({e})") from None
    finally:
        r.close()


def cmd_console(reg, a):
    from .__main__ import repl
    rec = reg.get(a.name)
    if backend_of(rec) != NATIVE_BACKEND:
        return cmd_terminal(rec, a)
    r = connect(rec, timeout=10.0)
    con = Console(r)
    try:
        if a.lines:
            for line in a.lines:
                for out in con.run(line):
                    print(out)
            return 0
        return repl(con)
    finally:
        r.close()


def cmd_vm(reg, a):
    if a.sub == "list":
        for n in reg.domains.list():
            print(n)
        return 0
    if not a.name:
        raise CtlError("vm show NAME")
    print("\n".join(describe(reg.domains.load(a.name))))
    return 0



# -- snapshots ------------------------------------------------------------------------------------

def cmd_snap(reg, a):
    """snap NAME list | save TAG | restore TAG | delete TAG, on any backend. save and restore need a paused
    machine: a running one is paused for the call and resumed after, within the one connection."""
    verb = a.verb
    if verb != "list" and not a.tag:
        raise CtlError(f"snap NAME {verb} TAG")
    r = connect(reg.get(a.name), timeout=60.0)
    try:
        if verb == "list":
            tags = [s["tag"] for s in r.call("snapshot.list").get("snapshots", [])]
            print("\n".join(tags) if tags else f"{a.name}: no snapshots")
            return 0
        was = r.call("machine.status").get("state")
        if verb != "delete" and was == "running":
            r.call("machine.pause")
        try:
            r.call(f"snapshot.{verb}", _timeout=180, tag=a.tag)
        finally:
            if verb != "delete" and was == "running":
                r.call("machine.resume")
        print(f"{a.name}: {dict(save='saved', restore='restored', delete='deleted')[verb]} {a.tag}")
        return 0
    except RpcError as e:
        print(f"{a.name}: snap {verb} failed: {e}", file=sys.stderr)
        return 1
    finally:
        r.close()


# -- sticky pause ---------------------------------------------------------------------------------

def cmd_sticky(reg, a):
    mode = None
    if a.names and (a.all or len(a.names) > 1) and a.names[-1] in ("on", "off"):
        mode = a.names.pop()
    for n in targets(reg, a):
        if backend_of(reg.get(n)) != NATIVE_BACKEND:
            raise CtlError(f"{n}: sticky pause is an 86Box setting; this is a {backend_of(reg.get(n))} machine")

    def go(r):
        try:
            cur = bool(r.call("rmm.sticky_pause.get")["value"])
            if mode and (mode == "on") != cur:
                cur = bool(r.call("rmm.sticky_pause.set", value=mode == "on")["value"])
        except RpcError:
            raise CtlError("this 86Box has no sticky pause (rebuild with rmm.c from this tree)") from None
        return "sticky pause " + ("on" if cur else "off")
    return each(reg, a, go, "sticky")


# -- breakpoints ----------------------------------------------------------------------------------

def parse_addr(text):
    """A linear address: 0x1234, 1234h, or a real-mode SEG:OFF."""
    t = text.strip().lower()
    try:
        if ":" in t:
            seg, off = t.split(":", 1)
            return (int(seg.rstrip("h"), 16) << 4) + int(off.rstrip("h"), 16)
        return int(t[:-1], 16) if t.endswith("h") else int(t, 0)
    except ValueError:
        raise CtlError(f"{text!r} is not an address (0x1234, 1234h or SEG:OFF)") from None


def resolve_addr(r, text):
    """parse_addr, plus `intN` / `int:N`: the handler an interrupt vector points at now (real mode IVT)."""
    t = text.strip().lower()
    if t.startswith("int"):
        try:
            n = int(t[3:].lstrip(":"), 0)
        except ValueError:
            raise CtlError(f"{text!r}: intN takes a vector number (int8, int:0x21)") from None
        off, seg = struct.unpack("<HH", r.read_mem(n * 4, 4))
        return (seg << 4) + off
    return parse_addr(text)


def break_table(r):
    rows = []
    for b in r.call("debug.break.list").get("breakpoints", []):
        extra = " ".join(f"{k}={b[k]}" for k in ("pre", "post", "calls", "violations") if k in b)
        rows.append((f"#{b['id']}", f"{b['addr']:#010x}", extra))
    return table(rows, ["ID", "ADDRESS", "CONTRACT / HITS"]) if rows else "(no breakpoints armed)"


def cmd_break(reg, a):
    """Arm breakpoints, let the guest run to one, report where it stopped. Breakpoints belong to the
    connection (86Box clears them on disconnect), so setting and waiting happen in one command."""
    rec = reg.get(a.name)
    r = connect(rec, timeout=10.0, full="break")
    try:
        ids = []
        for text in a.addrs:
            params = {"addr": resolve_addr(r, text)}
            for k in ("pre", "post"):
                if getattr(a, k):
                    params[k] = getattr(a, k)
            ids.append((r.call("debug.break.set", **params)["id"], params["addr"]))
        for bid, addr in ids:
            print(f"{a.name}: break #{bid} at {addr:#010x}")
        print(break_table(r))
        if not a.wait:
            return 0
        if r.state() == "paused":
            r.call("machine.resume")
        print(f"{a.name}: running to a breakpoint (up to {a.wait:g}s)")
        ev = r.rpc.wait_event("machine.stopped", a.wait, where=lambda p: p.get("reason") in ("breakpoint", "watchpoint"))
        if ev is None:
            print(f"{a.name}: no breakpoint hit in {a.wait:g}s", file=sys.stderr)
            return 1
        regs = r.call("cpu.regs.get")
        print(f"{a.name}: stopped: {ev.get('reason')} #{ev.get('id')} at {ev.get('addr', 0):#010x}")
        print("  " + "  ".join(f"{k}={regs[k]:x}" for k in ("eax", "ebx", "ecx", "edx", "esp", "eip", "cs") if k in regs))
        print(break_table(r))
        if ev.get("violation"):
            print(f"  contract {ev['violation']}-condition clause {ev.get('clause')} broken: {ev.get('lhs')} vs {ev.get('rhs')}")
        return 0
    except RpcError as e:
        print(f"{a.name}: break failed: {e}", file=sys.stderr)
        return 1
    finally:
        r.close()


# -- removable media ------------------------------------------------------------------------------

def media_slot(text):
    dev, _, idx = text.partition(".")
    if not dev or not idx.isdigit():
        raise CtlError(f"a media slot is DEVICE.N, like fdd.0 or cdrom.1 (got {text!r})")
    return dev, int(idx)


def cmd_media(reg, a):
    """media NAME [list] | insert DEV.N PATH [--wp] | eject DEV.N | reload DEV.N | new DEV.N PATH [--size B]"""
    verb, rest = (a.args[0], a.args[1:]) if a.args else ("list", [])
    need = {"list": 0, "insert": 2, "eject": 1, "reload": 1, "new": 2}
    if verb not in need:
        raise CtlError(f"media: list, insert, eject, reload or new (got {verb!r})")
    if len(rest) < need[verb]:
        raise CtlError({"insert": "media NAME insert DEV.N PATH", "eject": "media NAME eject DEV.N",
                        "reload": "media NAME reload DEV.N", "new": "media NAME new DEV.N PATH [--size BYTES]"}[verb])
    r = connect(reg.get(a.name), timeout=10.0, full="media")
    try:
        if verb == "list":
            rows = [(f"{s['dev']}.{s['slot']}", s.get("path") or "(empty)", "wp" if s.get("wp") else "",
                     s.get("mode", "")) for s in r.call("media.list").get("slots", [])]
            print(table(rows, ["SLOT", "IMAGE", "WP", "MODE"]))
            return 0
        dev, idx = media_slot(rest[0])
        params = {}
        if verb in ("insert", "new"):
            params["path"] = os.path.abspath(rest[1])
        if verb == "insert" and a.wp:
            params["wp"] = True
        if verb == "new" and a.size:
            params["size"] = a.size
        if verb == "new":                               # 86Box has insert/eject only: make the file here, then insert it
            if os.path.exists(params["path"]):
                raise CtlError(f"{params['path']} exists")
            with open(params["path"], "wb") as f:
                f.truncate(params.pop("size", 1474560))
            verb = "insert"
        r.call(f"{dev}.{idx}.{verb}", **params)
        now = next((s for s in r.call("media.list")["slots"] if s["dev"] == dev and s["slot"] == idx), {})
        print(f"{a.name}: {dev}.{idx} {a.args[0]}: {now.get('path') or '(empty)'}")
        return 0
    except RpcError as e:
        print(f"{a.name}: media {verb} failed: {e}", file=sys.stderr)
        return 1
    finally:
        r.close()


# -- settings -------------------------------------------------------------------------------------

def cmd_config(reg, a):
    """config NAME get SECTION [KEY] | set SECTION KEY VALUE | pending | discard | describe SECTION KEY"""
    verb, rest = (a.args[0], a.args[1:]) if a.args else ("pending", [])
    r = connect(reg.get(a.name), timeout=10.0, full="config")
    try:
        if verb == "get" and rest:
            res = r.call("config.get", section=rest[0], **({"key": rest[1]} if len(rest) > 1 else {}))
        elif verb == "set" and len(rest) == 3:
            try:
                val = int(rest[2], 0)
            except ValueError:
                val = rest[2]
            res = r.call("config.set", section=rest[0], key=rest[1], value=val)
        elif verb == "describe" and len(rest) == 2:
            res = r.call("config.describe", section=rest[0], key=rest[1])
        elif verb in ("pending", "discard") and not rest:
            res = r.call(f"config.{verb}")
        else:
            raise CtlError("config NAME get SECTION [KEY] | set SECTION KEY VALUE | describe SECTION KEY | pending | discard")
        print(json.dumps(res, indent=2, sort_keys=True))
        if verb == "set":
            print(f"{a.name}: staged; it applies at the next hard reset (`rmmctl console {a.name} machine.reset hard=true`)")
        return 0
    except RpcError as e:
        print(f"{a.name}: config {verb} failed: {e}", file=sys.stderr)
        return 1
    finally:
        r.close()


# -- screenshots ----------------------------------------------------------------------------------

def next_name(pattern):
    """pattern with {n} (or %d / %03d): the first number whose file does not exist yet."""
    if "{n" not in pattern and "%" not in pattern:
        return pattern
    n = 1
    while True:
        path = pattern.format(n=n) if "{n" in pattern else pattern % n
        if not os.path.exists(path):
            return path
        n += 1


def cmd_screenshot(reg, a):
    """screenshot NAME [-o FILE] [-d DIR]: a PNG of the guest screen. FILE may contain {n} (or {n:03d}):
    every run takes the next free number, so the same command can be repeated. A FILE without it is
    overwritten, and the default is NAME-{n:03d}.png."""
    r = connect(reg.get(a.name), timeout=10.0)
    try:
        frame = r.screenshot(step=a.step, wait_ms=a.wait_ms) if isinstance(r, Rmm) else Frame(r.call("capture.screenshot"))
    except RpcError as e:
        print(f"{a.name}: screenshot failed: {e}", file=sys.stderr)
        return 1
    finally:
        r.close()
    pattern = a.output or f"{a.name}-{{n:03d}}.png"
    if a.dir:
        os.makedirs(a.dir, exist_ok=True)
        pattern = os.path.join(a.dir, pattern)
    path = next_name(pattern)
    frame.save_png(path)
    print(f"{a.name}: {path}  {frame.src_w}x{frame.src_h}" + ("" if frame.fresh else "  (guest paused: not a new frame)"))
    return 0


# -- sessions -------------------------------------------------------------------------------------
# A session is the Qt manager's group file (rmm-group/1, rmm_demo/rmmgui/group.py), so a set of
# instances saved here opens there ("Open group file") and the other way round.

SESSION_SCHEMA = "rmm-group/1"


def session_path(reg, name):
    if os.sep in name or name.endswith(".json"):
        return os.path.abspath(name)
    return os.path.join(reg.home, "sessions", name + ".json")


# A session file is shared with the manager and drives the same backends (rmm_86box.backends). A node of a
# backend this tool has not registered stays in the file as it is, never rewritten or started.

def session_nodes(reg):
    nodes = {}
    for n in reg.names():
        rec = reg.get(n)
        if backend_of(rec) != NATIVE_BACKEND:
            nodes[n] = dict(rec["node"])                # the node it was started or attached from
            continue
        if rec["kind"] == "launched":
            launch = {k: rec[k] for k in ("binary", "vm_dir", "roms") if rec.get(k)}
            body = {"port": "auto", "launch": launch}
        else:
            body = {"host": rec["host"], "port": rec["port"]}
        body["x-sticky"] = rec.get("sticky")
        nodes[n] = {k: v for k, v in body.items() if v is not None}
    return nodes


def node_backend(b):
    """The backend a session node names; a `qemu` block (the Qt manager's own QEMU node) means qemu."""
    return b.get("backend") or ("qemu" if b.get("qemu") is not None else NATIVE_BACKEND)


def foreign_nodes(raw):
    """The nodes of a session file that belong to a backend other than 86Box. A save keeps those it holds no
    record of (the manager may be running them), so a node the manager added is never lost."""
    return {n: b for n, b in (raw.get("nodes") or {}).items() if node_backend(b) != NATIVE_BACKEND}


def cmd_session(reg, a):
    sub = a.sub
    if sub == "ls":
        d = os.path.join(reg.home, "sessions")
        names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
        for n in names:
            try:
                with open(os.path.join(d, n + ".json")) as f:
                    nodes = json.load(f).get("nodes", {})
                print(f"{n}  ({len(nodes)} instance{'s' if len(nodes) != 1 else ''}: {', '.join(nodes)})")
            except (OSError, ValueError):
                print(f"{n}  (unreadable)")
        return 0
    if not a.name:
        raise CtlError(f"session {sub} NAME")
    path = session_path(reg, a.name)
    if sub == "save":
        nodes = session_nodes(reg)
        if not nodes:
            raise CtlError("nothing registered to save (rmmctl attach / start first)")
        raw = {}
        if os.path.exists(path):                       # keep what the Qt manager added (policy, vars, templates)
            with open(path) as f:
                raw = json.load(f)
        kept = {n: b for n, b in foreign_nodes(raw).items() if n not in nodes}
        nodes = dict(nodes, **kept)                    # simh / vice / qemu nodes the manager put there stay
        raw.update(schema=SESSION_SCHEMA, name=os.path.splitext(os.path.basename(path))[0], nodes=nodes)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
        print(f"session {raw['name']}: saved {len(nodes) - len(kept)} instance{'s' if len(nodes) - len(kept) != 1 else ''} to {path}"
              + (f" (kept {len(kept)} {'/'.join(sorted({node_backend(b) for b in kept.values()}))} node"
                 f"{'s' if len(kept) != 1 else ''} for the Qt manager)" if kept else ""))
        return 0
    if sub == "rm":
        if not os.path.exists(path):
            raise CtlError(f"no session {a.name!r}")
        os.remove(path)
        print(f"session {a.name}: removed")
        return 0
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise CtlError(f"no session {a.name!r} (rmmctl session ls)") from None
    except ValueError as e:
        raise CtlError(f"{path}: {e}") from None
    if raw.get("schema", SESSION_SCHEMA) != SESSION_SCHEMA:
        raise CtlError(f"{path}: schema {raw.get('schema')!r}, expected {SESSION_SCHEMA!r}")
    nodes = raw.get("nodes") or {}
    if sub == "show":
        print(f"session {raw.get('name', a.name)}  ({path})")
        print(table([(n, node_backend(b),
                      "launch" if b.get("launch") is not None else "mock" if b.get("mock") else "attach",
                      (b["qemu"].get("qmp") or b["qemu"].get("profile", "?")) if b.get("qemu") is not None
                      else f"{b.get('host', '127.0.0.1')}:{b.get('port')}", (b.get("launch") or {}).get("vm_dir", ""))
                     for n, b in nodes.items()], ["NAME", "BACKEND", "MODE", "WHERE", "VM DIR"]))
        return 0
    bad = 0                                             # load
    for n, b in nodes.items():
        if reg.has(n) and probe(reg.get(n))["state"] in ("running", "paused", "starting"):
            print(f"{n}: already up")
            continue
        try:
            if b.get("qemu") is not None:
                print(f"{n}: skipped (needs the Qt manager: qemu nodes)")
            elif b.get("backend", NATIVE_BACKEND) != NATIVE_BACKEND:
                if backends.find(b["backend"]) is None:
                    print(f"{n}: skipped (backend {b['backend']!r} is not one of {', '.join(backends.names())})")
                else:
                    start_node(reg, n, b, a.wait, check=not a.no_check)
            elif b.get("mock") or b.get("vm_config") or (b.get("launch") is None and not isinstance(b.get("port"), int)):
                print(f"{n}: skipped (needs the Qt manager: mock / vm_config nodes)")
            elif b.get("launch") is not None:
                lc = b["launch"]
                cmd_start(reg, argparse.Namespace(
                    name=n, vm_dir=lc["vm_dir"], binary=lc.get("binary"), roms=lc.get("roms"),
                    port=str(b["port"]) if b.get("port") not in (None, "auto") else "auto", domain=b.get("domain") if isinstance(b.get("domain"), str) else None,
                    turbo=False, paused=False, sticky=b.get("x-sticky", True), wait=a.wait, extra=lc.get("args") or []))
            else:
                cmd_attach(reg, argparse.Namespace(name=n, address=f"{b.get('host', '127.0.0.1')}:{b['port']}",
                                                   force=True, no_check=a.no_check))
        except CtlError as e:
            print(f"{n}: {e}", file=sys.stderr)
            bad = 1
        except KeyError as e:
            print(f"{n}: launch needs {e}", file=sys.stderr)
            bad = 1
    return bad


# -- entry point ----------------------------------------------------------------------------------

def parser():
    ap = argparse.ArgumentParser(prog="rmmctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="state directory (default $RMM_86BOX_HOME or ~/.rmm_86box)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        p.set_defaults(fn=fn)
        return p

    p = add("ps", cmd_ps, help="list instances")
    p.add_argument("-a", "--all", action="store_true", help="include down / exited ones")
    p.add_argument("--json", action="store_true")
    p = add("status", cmd_status, help="one instance in detail")
    p.add_argument("name")
    p = add("start", cmd_start, help="launch an 86Box, or any backend from a --node")
    p.add_argument("name")
    p.add_argument("--backend", choices=backends.names(), help="default 86box; the others take --node")
    p.add_argument("--node", help="the machine as a session-file node: JSON, or @FILE")
    p.add_argument("--vm-dir")
    p.add_argument("--binary")
    p.add_argument("--roms", help="86Box ROM directory (-R); default $RMMCTL_ROMS")
    p.add_argument("--port", default="auto")
    p.add_argument("--domain", help="write the VM's 86box.cfg from this stored domain first")
    p.add_argument("--turbo", action="store_true")
    p.add_argument("--paused", action="store_true")
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the monitor")
    p.add_argument("--sticky", dest="sticky", action="store_true", default=True,
                   help="a pause survives rmmctl disconnecting (the default; 86Box --monitor-sticky-pause)")
    p.add_argument("--no-sticky", dest="sticky", action="store_false", help="86Box resumes a paused guest on disconnect")
    p.add_argument("extra", nargs="*", help="further 86Box arguments, after --")
    p = add("attach", cmd_attach, help="register a machine that runs elsewhere")
    p.add_argument("name")
    p.add_argument("address", metavar="HOST:PORT")
    p.add_argument("--backend", choices=backends.names(), help="default 86box (qemu: the QMP port; vice: binary monitor; simh: a backends.serve port)")
    p.add_argument("--snapdir", help="vice: where its snapshots (.vsf) live")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-check", action="store_true", help="register without connecting")
    for nm, fn in (("pause", cmd_pause), ("resume", cmd_resume), ("stop", cmd_stop), ("rm", cmd_rm)):
        p = add(nm, fn)
        p.add_argument("names", nargs="*")
        p.add_argument("--all", action="store_true")
        if nm in ("stop", "rm"):
            p.add_argument("--timeout", type=float, default=8.0)
        if nm == "rm":
            p.add_argument("-f", "--force", action="store_true")
    p = add("snap", cmd_snap, help="snap NAME list|save|restore|delete [TAG]")
    p.add_argument("name")
    p.add_argument("verb", choices=["list", "save", "restore", "delete"])
    p.add_argument("tag", nargs="?")
    p = add("turbo", cmd_turbo, help="turbo NAME... [on|off|toggle]: unthrottled run; no mode prints it")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = add("sticky", cmd_sticky, help="sticky NAME... [on|off]: keep a pause after rmmctl disconnects")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = add("break", cmd_break, help="break NAME ADDR... [--wait S]: run to a breakpoint and report")
    p.add_argument("name")
    p.add_argument("addrs", nargs="+", metavar="ADDR")
    p.add_argument("--wait", type=float, default=0, help="resume and wait this many seconds for a hit")
    p.add_argument("--pre", help="contract precondition, e.g. 'arg0 == 5'")
    p.add_argument("--post", help="contract postcondition on return")
    p = add("media", cmd_media, help="media NAME [list|insert DEV.N PATH|eject DEV.N|reload DEV.N|new DEV.N PATH]")
    p.add_argument("name")
    p.add_argument("args", nargs="*")
    p.add_argument("--wp", action="store_true", help="insert write-protected")
    p.add_argument("--size", type=int, help="new: image size in bytes")
    p = add("config", cmd_config, help="config NAME get|set|describe|pending|discard ...")
    p.add_argument("name")
    p.add_argument("args", nargs="*")
    p = add("screenshot", cmd_screenshot, help="screenshot NAME [-o FILE] [-d DIR]: save the guest screen as PNG")
    p.add_argument("name")
    p.add_argument("-o", "--output", help="file name; {n} or {n:03d} auto-increments (default NAME-{n:03d}.png)")
    p.add_argument("-d", "--dir", help="directory for the file")
    p.add_argument("--step", type=int, default=1, help="1..8: downscale")
    p.add_argument("--wait-ms", dest="wait_ms", type=int, default=200)
    p = add("session", cmd_session, help="session save|load|show|rm NAME, session ls")
    p.add_argument("sub", choices=["save", "load", "show", "rm", "ls"])
    p.add_argument("name", nargs="?")
    p.add_argument("--wait", type=float, default=20.0)
    p.add_argument("--no-check", action="store_true", help="load: register attach nodes without connecting")
    p = add("console", cmd_console, help="RMM console on an instance")
    p.add_argument("name")
    p.add_argument("lines", nargs="*")
    p = add("vm", cmd_vm, help="stored VM configs")
    p.add_argument("sub", choices=["list", "show"])
    p.add_argument("name", nargs="?")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = parser()
    a = ap.parse_args(argv)
    if a.cmd == "start" and a.extra and a.extra[0] == "--":
        a.extra = a.extra[1:]
    try:
        return a.fn(Registry(a.home), a) or 0
    except CtlError as e:
        print(f"rmmctl: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
