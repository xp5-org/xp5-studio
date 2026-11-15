# SPDX-License-Identifier: MIT
"""A QEMU guest: QmpRmm answers the RMM calls a manager uses over QEMU's QMP socket, so snap /
restore / delete / pause / resume treat it like an 86Box. Stdlib only.

Snapshots are the monitor's savevm / loadvm / delvm (internal qcow2 snapshots), sent through QMP's
human-monitor-command. Every writable disk of the guest must be qcow2 and one of them holds the VM
state.

Node keys: port (the QMP port, `-qmp tcp:127.0.0.1:<port>`), optionally qemu_argv (+ qemu_cwd) to
have the caller start the QEMU. With no argv the node attaches to a QEMU somebody else started (the
testlist's test_startqemu opens QMP at monitor_port + 1000); QMP, unlike the HMP monitor, takes
several clients. Calls with no QEMU meaning (registers, fast-forward, memory write) answer like an
86Box that lacks the method.
"""

import contextlib
import json
import os
import queue
import socket
import tempfile
import threading
import time

from ...rpc import RpcError
from .. import Machine
from ..proc import Proc

SNAP_WAIT = 180.0                 # savevm / loadvm write or read the whole RAM image
QMP_WAIT = 10.0


def hmp_failed(reply):
    """The monitor's error text in an HMP reply, else None. savevm / loadvm answer in plain
    text (no QMP error), after echoing nothing since human-monitor-command has no echo."""
    text = (reply or "").strip()
    low = text.lower()
    if low.startswith(("error", "device ")) or "does not have" in low or "cannot " in low \
            or "failed" in low or "unknown command" in low or "no such" in low \
            or "does not exist" in low or "not found" in low:
        return text
    return None


class _Vm:
    """Stands in for rmm.vm: a QEMU has no 86Box domain to describe."""

    def show(self):
        raise RpcError("no 86Box domain on a QEMU node", code=None)


class QmpRmm:
    def __init__(self, port, host="127.0.0.1", timeout=QMP_WAIT):
        self.port = int(port)
        self.host = host
        self.timeout = timeout
        self.vm = _Vm()
        self._sock = None
        self._file = None
        self._replies = queue.Queue()
        self._lock = threading.Lock()            # one command in flight: the reply queue has no ids
        self._listeners = []
        self._reader = None
        self._closing = False
        self.banner = ""

    # -- connection --------------------------------------------------------------------------

    @property
    def connected(self):
        return self._sock is not None

    def on_event(self, fn):
        self._listeners.append(fn)

    def connect(self, wait=0):
        deadline = time.time() + wait
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
                break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.5)
        f = s.makefile("rwb", buffering=0)
        try:
            greeting = json.loads(f.readline())
            ver = greeting["QMP"]["version"]["qemu"]
            self.banner = f"QEMU {ver['major']}.{ver['minor']}.{ver['micro']} (QMP {self.port})"
            f.write(b'{"execute": "qmp_capabilities"}\n')
            reply = json.loads(f.readline())
            if "return" not in reply:
                raise RpcError(f"qmp_capabilities: {reply}")
        except (ValueError, KeyError, OSError) as e:
            s.close()
            raise OSError(f"{self.host}:{self.port} is not a QMP socket ({e})") from None
        s.settimeout(None)
        self._sock, self._file = s, f
        self._closing = False
        self._replies = queue.Queue()
        self._reader = threading.Thread(target=self._read, name=f"qmp-{self.port}", daemon=True)
        self._reader.start()
        return self.banner

    def close(self):
        self._closing = True
        self._drop()

    def _drop(self):
        s, self._sock = self._sock, None
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)     # wakes the reader, so the connection really ends
            with contextlib.suppress(OSError):
                s.close()
            self._replies.put(None)

    def _read(self):
        try:
            for line in self._file:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "event" in msg:
                    self._event(msg["event"], msg.get("data") or {})
                else:
                    self._replies.put(msg)
        except (OSError, ValueError):
            pass
        was_closing = self._closing
        self._drop()
        if not was_closing:
            self._emit("rpc.disconnected", {})

    def _event(self, name, data):
        if name == "STOP":
            self._emit("machine.stopped", {"reason": "pause"})
        elif name == "RESUME":
            self._emit("machine.resumed", {})
        elif name == "SHUTDOWN":
            self._emit("machine.exit", {})
        elif name == "RESET":
            self._emit("machine.reset", {})

    def _emit(self, name, params):
        for fn in list(self._listeners):
            fn(name, params)

    # -- QMP ---------------------------------------------------------------------------------

    def qmp(self, command, timeout=None, **arguments):
        if self._sock is None:
            raise OSError("QMP not connected")
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        with self._lock:
            try:
                self._file.write(json.dumps(msg).encode() + b"\n")
            except OSError:
                self._drop()
                raise
            try:
                reply = self._replies.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise OSError(f"QMP {command}: no reply in {timeout or self.timeout:.0f}s") from None
        if reply is None:
            raise OSError("QMP connection lost")
        if "error" in reply:
            raise RpcError(reply["error"].get("desc", str(reply["error"])), code=8)
        return reply.get("return")

    def hmp(self, line, timeout=None):
        return self.qmp("human-monitor-command", timeout=timeout, **{"command-line": line}) or ""

    # -- the RMM subset a manager uses ---------------------------------------------------------

    def state(self):
        return "running" if self.qmp("query-status").get("running") else "paused"

    @contextlib.contextmanager
    def paused(self):
        was = self.state()
        if was == "running":
            self.call("machine.pause")
        try:
            yield
        finally:
            if was == "running":
                self.call("machine.resume")

    def call(self, method, _timeout=None, **p):
        if method == "machine.status":
            return {"state": self.state(), "tsc": None}
        if method == "machine.pause":
            self.qmp("stop")
            return {}
        if method == "machine.resume":
            self.qmp("cont")
            return {}
        if method == "machine.quit":
            with contextlib.suppress(OSError):
                self.qmp("quit", timeout=3)
            return {}
        if method == "machine.fast_forward.get":
            return {"value": False}
        if method == "snapshot.list":
            return {"snapshots": [{"tag": t} for t in self.snapshot_tags()]}
        if method in ("snapshot.save", "snapshot.restore", "snapshot.delete"):
            cmd = {"snapshot.save": "savevm", "snapshot.restore": "loadvm", "snapshot.delete": "delvm"}[method]
            if method == "snapshot.save" and self.state() == "running":
                raise RpcError("not paused", code=1)
            reply = self.hmp(f"{cmd} {p['tag']}", timeout=SNAP_WAIT)
            bad = hmp_failed(reply)
            if bad:
                raise RpcError(f"{cmd} {p['tag']}: {bad}", code=7 if method != "snapshot.save" else 8)
            return {}
        if method == "mem.phys.read":
            return {"data": self.phys_read(p["addr"], p["len"])}
        raise RpcError(f"{method}: not available on QEMU", code=-32601)

    # -- QEMU-only helpers ----------------------------------------------------------------------

    def snapshot_tags(self):
        """Tags from `info snapshots`: rows are ID TAG VM-SIZE DATE VM-CLOCK."""
        tags = []
        for ln in self.hmp("info snapshots").splitlines():
            cols = ln.split()
            if len(cols) >= 3 and (cols[0].isdigit() or cols[0] == "--"):
                if cols[1] not in tags:
                    tags.append(cols[1])
        return tags

    def phys_read(self, addr, n):
        """n bytes of guest physical memory as hex (x86 and anything xp understands)."""
        out = self.hmp(f"xp /{int(n)}xb 0x{int(addr):x}")
        vals = [t[2:] for ln in out.splitlines() for t in ln.split(":", 1)[-1].split() if t.startswith("0x")]
        return "".join(v.zfill(2) for v in vals)

    def screen_bytes(self):
        """The guest display as PPM bytes (QMP screendump)."""
        fd, path = tempfile.mkstemp(suffix=".ppm", prefix="qmp_screen_")
        os.close(fd)
        try:
            self.qmp("screendump", filename=path)
            with open(path, "rb") as f:
                return f.read()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)


# -- the backend ----------------------------------------------------------------------------------

def validate(spec):
    if spec.launch is not None or not isinstance(spec.port, int):
        return ("a qemu node attaches to a running QEMU's QMP port "
                "(port: a number, no launch; the testlist starts the QEMU)")
    return None


def attaches(spec):
    return not spec.raw.get("qemu_argv")


class QemuMachine(Machine):
    def title_lines(self):
        return [f"QEMU {self.rmm.banner}"]


def start(spec, say=None, **_):
    say = say or (lambda text, tone="text": None)
    port, proc = int(spec.port), None
    argv = spec.raw.get("qemu_argv")                 # set by a bundle import: the caller starts the QEMU
    if argv:
        proc = Proc.spawn(argv, "QEMU", cwd=spec.raw.get("qemu_cwd") or None)
        say(f"started QEMU pid {proc.pid} on QMP port {port}", "muted")
    return QemuMachine("qemu", QmpRmm(port, spec.host, timeout=spec.rpc_timeout), proc, spec.host, port)


def adapter(host, port, timeout=10.0, **_):
    return QmpRmm(port, host, timeout=timeout)
