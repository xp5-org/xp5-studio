# SPDX-License-Identifier: MIT
"""A VICE (x64sc, xvic, x128, ...) machine: ViceRmm answers the RMM calls a manager and
FramePoller use over VICE's binary monitor (TCP, -binarymonitor), so group snap / restore /
delete / pause / resume and the screen panel treat it like an 86Box. Stdlib only.

Binary monitor (API v2). Request:  STX 02 | api 02 | body len u32 | request id u32 | cmd u8 | body.
Response: STX 02 | api | body len u32 | type u8 | error u8 | request id u32 | body. Responses with
id 0xffffffff are events (0x62 stopped, 0x63 resumed).

VICE runs freely until a command arrives; most commands stop it (a stopped event) and 0xaa
(`exit`) resumes. ViceRmm keeps a logical running / paused flag: a read the poller makes while the
node is logically running (frame, memory) re-issues `exit` if the command stopped the emulator.

A snapshot is the monitor's dump / undump to <snapdir>/<tag>.vsf. Frames are `display get`.
Attach to a VICE somebody else started with "vice": {"attach": true, "port": N}, or let start()
launch it from "vice": {"binary", "image", "args", "snapdir", "cwd"}.

Run against VICE 3.9 (x64sc): `display get` accepts only the indexed 8-bit format (RGB gives error
0x81), so frames are palette-mapped here. Unit tests use rmm_demo/tests/test_vice_backend.py's fake monitor.
"""

import base64
import contextlib
import glob
import os
import queue
import shutil
import socket
import struct
import tempfile
import threading
import time

from ..rpc import RpcError
from ..spec import free_port
from . import Machine
from .proc import Proc

STX, API = 0x02, 0x02
EVENT_ID = 0xFFFFFFFF
CMD_MEM_GET, CMD_REGS_GET, CMD_DUMP, CMD_UNDUMP = 0x01, 0x31, 0x41, 0x42
CMD_PING, CMD_DISPLAY, CMD_PALETTE, CMD_EXIT, CMD_QUIT = 0x81, 0x84, 0x91, 0xAA, 0xBB
EV_STOPPED, EV_RESUMED = 0x62, 0x63
MEM_MAIN = 0                                     # memspace: main CPU address space
SNAP_WAIT = 60.0
CMD_WAIT = 10.0


class ViceRmm:
    def __init__(self, port, host="127.0.0.1", snapdir=None, timeout=CMD_WAIT):
        self.port, self.host, self.timeout = int(port), host, timeout
        self.snapdir = snapdir
        self.vm = _Vm()
        self.banner = f"VICE (binary monitor {self.port})"
        self._sock = None
        self._replies = {}                       # request id -> Queue of (type, error, body)
        self._lock = threading.Lock()
        self._ids = 0
        self._listeners = []
        self._closing = False
        self._stopped = False                    # the emulator is stopped in the monitor
        self._paused = False                     # logical state the group asked for
        self._exit_sent = False

    # -- connection --------------------------------------------------------------------------

    @property
    def connected(self):
        return self._sock is not None

    def on_event(self, fn):
        self._listeners.append(fn)

    def _emit(self, name, params):
        for fn in list(self._listeners):
            fn(name, params)

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
        s.settimeout(None)
        self._sock = s
        self._closing = False
        threading.Thread(target=self._read, args=(s,), name=f"vice-{self.port}", daemon=True).start()
        try:
            self._cmd(CMD_PING)
        except (OSError, RpcError) as e:
            self.close()
            raise OSError(f"{self.host}:{self.port} is not a VICE binary monitor ({e})") from None
        return self.banner

    def close(self):
        self._closing = True
        self._drop()

    def _drop(self):
        s, self._sock = self._sock, None
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)     # wakes the reader; a plain close() leaves the monitor's one client slot held
            with contextlib.suppress(OSError):
                s.close()
        for q in list(self._replies.values()):
            q.put(None)

    def _recv(self, s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise OSError("monitor closed")
            buf += chunk
        return buf

    def _read(self, s):
        try:
            while True:
                head = self._recv(s, 12)
                if head[0] != STX:
                    raise OSError("bad monitor framing")
                length, rtype, err, rid = struct.unpack("<IBBI", head[2:])
                body = self._recv(s, length) if length else b""
                if rid == EVENT_ID:
                    self._event(rtype, body)
                else:
                    q = self._replies.get(rid)
                    if q is not None:
                        q.put((rtype, err, body))
        except OSError:
            pass
        was_closing = self._closing
        if self._sock is s:
            self._drop()
        if not was_closing:
            self._emit("rpc.disconnected", {})

    def _event(self, rtype, body):
        if rtype == EV_STOPPED:
            self._stopped = True
            if self._paused:
                self._emit("machine.stopped", {"reason": "pause"})
        elif rtype == EV_RESUMED:
            self._stopped = False
            if not self._paused:
                self._emit("machine.resumed", {})

    # -- monitor commands -----------------------------------------------------------------

    def _cmd(self, cmd, body=b"", timeout=None):
        """One command, its response body. Raises RpcError on a monitor error code."""
        s = self._sock
        if s is None:
            raise OSError("monitor not connected")
        with self._lock:
            self._ids = (self._ids + 1) & 0x7FFFFFFF
            rid = self._ids
            q = self._replies[rid] = queue.Queue()
            try:
                s.sendall(struct.pack("<BBIIB", STX, API, len(body), rid, cmd) + body)
                got = q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise OSError(f"monitor command {cmd:#x}: no reply in {timeout or self.timeout:.0f}s") from None
            except OSError:
                self._drop()
                raise
            finally:
                self._replies.pop(rid, None)
        if got is None:
            raise OSError("monitor connection lost")
        rtype, err, rbody = got
        if err:
            raise RpcError(f"monitor command {cmd:#x} failed (error {err:#x})", code=8)
        return rbody

    def _quiet(self, cmd, body=b"", timeout=None):
        """A read the poller makes: keep the emulator running if the node is logically running."""
        try:
            return self._cmd(cmd, body, timeout)
        finally:                                 # even a rejected read must not leave VICE halted in the monitor
            if not self._paused and self._stopped:
                with contextlib.suppress(OSError, RpcError):
                    self._cmd(CMD_EXIT)

    # -- the RMM subset HostGroup, Node and FramePoller use ----------------------------------

    def state(self):
        if self._sock is None:
            if not self._exit_sent:
                self._exit_sent = True
                self._emit("machine.exit", {})
            raise OSError("VICE exited")
        self._cmd(CMD_PING)
        return "paused" if self._paused else "running"

    def pause(self):
        if self._paused:
            return
        self._paused = True
        self._cmd(CMD_REGS_GET, struct.pack("<B", MEM_MAIN))     # any command stops it
        self._stopped = True
        self._emit("machine.stopped", {"reason": "pause"})

    def resume(self):
        if not self._paused:
            return
        self._cmd(CMD_EXIT)
        self._paused = False
        self._stopped = False
        self._emit("machine.resumed", {})

    def call(self, method, _timeout=None, **p):
        if method == "machine.status":
            return {"state": self.state(), "tsc": None}
        if method == "machine.pause":
            self.pause()
            return {}
        if method == "machine.resume":
            self.resume()
            return {}
        if method == "machine.quit":
            with contextlib.suppress(OSError, RpcError):
                self._cmd(CMD_QUIT, timeout=3)
            return {}
        if method == "machine.fast_forward.get":
            return {"value": False}
        if method == "snapshot.list":
            return {"snapshots": [{"tag": t} for t in self.snapshot_tags()]}
        if method in ("snapshot.save", "snapshot.restore", "snapshot.delete"):
            return self._snapshot(method, p["tag"])
        if method == "mem.phys.read":
            return {"data": self.mem_read(p["addr"], p["len"]).hex()}
        if method == "capture.screenshot":
            return self.screenshot()
        raise RpcError(f"{method}: not available on VICE", code=-32601)

    # -- snapshots ------------------------------------------------------------------------

    def _vsf(self, tag):
        if not self.snapdir:
            raise RpcError("no snapshot directory known for this VICE (attach with its snapdir)", code=8)
        if not tag or os.sep in tag or tag.startswith("."):
            raise RpcError(f"bad snapshot tag {tag!r}", code=7)
        return os.path.join(self.snapdir, tag + ".vsf")

    def snapshot_tags(self):
        if not self.snapdir:
            return []
        return sorted(os.path.basename(f)[:-4] for f in glob.glob(os.path.join(self.snapdir, "*.vsf")))

    def _snapshot(self, method, tag):
        path = self._vsf(tag)
        if method == "snapshot.save":
            if not self._paused:
                raise RpcError("not paused", code=1)
            os.makedirs(self.snapdir, exist_ok=True)
            name = path.encode()
            self._cmd(CMD_DUMP, struct.pack("<BBB", 0, 1, len(name)) + name, SNAP_WAIT)
            if not os.path.exists(path):
                raise RpcError(f"snapshot.save {tag}: VICE wrote no {path}", code=8)
        elif method == "snapshot.restore":
            if not os.path.exists(path):
                raise RpcError(f"snapshot.restore {tag}: no such snapshot", code=7)
            name = path.encode()
            self._cmd(CMD_UNDUMP, struct.pack("<B", len(name)) + name, SNAP_WAIT)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                raise RpcError(f"snapshot.delete {tag}: no such snapshot", code=7) from None
        return {}

    # -- memory and screen ----------------------------------------------------------------

    def mem_read(self, addr, n):
        """n bytes of the main CPU's address space (VICE's is 64 KiB)."""
        addr, n = int(addr), int(n)
        if n <= 0 or addr < 0 or addr + n > 0x10000:
            raise RpcError(f"mem.phys.read {addr:#x}+{n}: outside 64 KiB", code=2)
        body = self._quiet(CMD_MEM_GET, struct.pack("<BHHBH", 0, addr, addr + n - 1, MEM_MAIN, 0))
        (got,) = struct.unpack_from("<H", body)
        return body[2:2 + got]

    def _palette(self):
        body = self._quiet(CMD_PALETTE, struct.pack("<B", 1))
        (count,) = struct.unpack_from("<H", body)
        pal, off = [], 2
        for _ in range(count):
            size = body[off]
            pal.append(tuple(body[off + 1:off + 4]))
            off += 1 + size
        return pal

    def screenshot(self):
        """display get as the dict Frame() takes: the visible (inner) area as 24-bit RGB."""
        body = self._quiet(CMD_DISPLAY, struct.pack("<BB", 1, 0))        # VIC-II, indexed 8-bit (3.9 rejects RGB)
        (info,) = struct.unpack_from("<I", body)
        dw, dh, xo, yo, iw, ih, bpp = struct.unpack_from("<HHHHHHB", body, 4)
        (ilen,) = struct.unpack_from("<I", body, 4 + 13)
        img = body[4 + 13 + 4:4 + 13 + 4 + ilen]
        if bpp == 8:
            pal = self._palette()
            px = [pal[b] if b < len(pal) else (0, 0, 0) for b in img]
        elif bpp in (24, 32):
            step = bpp // 8
            px = [tuple(img[i:i + 3]) for i in range(0, ilen, step)]
        else:
            raise RpcError(f"display get: {bpp} bits per pixel not handled", code=1)
        rows = []
        for y in range(yo, yo + ih):
            rows.append(b"".join(bytes(px[y * dw + x]) for x in range(xo, xo + iw)))
        return {"w": iw, "h": ih, "src_w": dw, "src_h": dh, "seq": 0, "fresh": True,
                "data": base64.b64encode(b"".join(rows)).decode()}


class _Vm:
    def show(self):
        raise RpcError("no 86Box domain on a VICE node", code=None)


# -- the backend ----------------------------------------------------------------------------------

def validate(spec):
    if spec.launch is not None:
        return "a vice node is described by its \"vice\" object, not \"launch\""
    cfg = spec.raw.get("vice")
    if not isinstance(cfg, dict):
        return "a vice node needs \"vice\": {\"image\": ...} or {\"attach\": true, \"port\": N}"
    if cfg.get("attach"):
        if not isinstance(cfg.get("port"), int):
            return "an attached vice node needs \"vice\": {\"attach\": true, \"port\": <monitor port>}"
    elif not (cfg.get("image") or cfg.get("args")):
        return "a vice node needs \"vice\": {\"image\": ...} (or \"args\"), or \"attach\": true with a port"
    return None


def attaches(spec):
    return bool(spec.raw["vice"].get("attach"))


def attach_port(spec):
    return int(spec.raw["vice"]["port"])


class ViceMachine(Machine):
    frames = True

    def title_lines(self):
        return [self.rmm.banner]


def start(spec, say=None, **_):
    say = say or (lambda text, tone="text": None)
    cfg = spec.raw["vice"]
    snapdir, own = cfg.get("snapdir"), False
    if not snapdir:
        snapdir, own = tempfile.mkdtemp(prefix=f"vice_{spec.name}_"), True
    proc = None
    if cfg.get("attach"):
        port = int(cfg["port"])
    else:
        port = free_port(spec.host)
        argv = [cfg.get("binary") or shutil.which("x64sc") or "x64sc",
                "-binarymonitor", "-binarymonitoraddress", f"ip4://{spec.host}:{port}"]
        argv += list(cfg.get("args") or [])
        if cfg.get("image"):
            argv += ["-autostart", cfg["image"]]
        proc = Proc.spawn(argv, "VICE", cwd=cfg.get("cwd") or None)
        say(f"started VICE pid {proc.pid} on monitor port {port}", "muted")
    rmm = ViceRmm(port, spec.host, snapdir=snapdir, timeout=spec.rpc_timeout)
    return ViceMachine("vice", rmm, proc, spec.host, port,
                       cleanup=(lambda: shutil.rmtree(snapdir, ignore_errors=True)) if own else None)


def adapter(host, port, timeout=10.0, snapdir=None, **_):
    return ViceRmm(port, host, snapdir=snapdir, timeout=timeout)
