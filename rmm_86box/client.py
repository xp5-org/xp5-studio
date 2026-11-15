# SPDX-License-Identifier: MIT
"""High-level client: the monitor's dotted methods as Python attributes, plus helpers.

    from rmm_86box import Rmm
    with Rmm(55860) as rmm:                      # connects; resumes the guest (pause-on-connect)
        rmm.machine.status()                     # -> {"state": "running", "tsc": ...}
        rmm.snapshot.save(tag="before")          # any method: rmm.<dotted.name>(**params)
        rmm.fdd[0].insert(path="/img/a.img")     # slot methods: rmm.<dev>[n].<verb>()
        f = rmm.screenshot()                     # Frame(w, h, rgb bytes, seq, fresh)
        with rmm.paused():                       # pause, and put the run state back after
            print(rmm.text_screen())             # 80x25 text page from B8000
        rmm.vm.save("mybox"); rmm.vm.load("mybox")   # whole-VM config in KVM terms
        rmm.input.tap("enter"); rmm.input.move(5, -3)    # keys, mouse: through input.events

Every attribute chain is discoverable: dir(rmm), dir(rmm.machine), ... list what the server
offers (from rpc.discover), which is what interactive completion uses.
"""

import base64
import contextlib
import os
import struct
import zlib

from . import kvm
from .registry import REGS, Methods
from .rpc import RpcClient, RpcError

# RMM wire protocol (rpc.hello). PROTOCOL is the newest this client speaks; MIN_SERVER_PROTOCOL
# is the oldest server it still works with. Keep PROTOCOL in step with RMM_PROTOCOL in 86Box src/rmm.c.
PROTOCOL = 1
MIN_SERVER_PROTOCOL = 1


class Frame:
    """One guest screen: `rgb` is w*h*3 bytes, row-major."""

    __slots__ = ("w", "h", "rgb", "seq", "fresh", "step", "src_w", "src_h")

    def __init__(self, r):
        self.w, self.h = r["w"], r["h"]
        self.rgb = base64.b64decode(r["data"])
        self.seq, self.fresh = r.get("seq", 0), r.get("fresh", True)
        self.step, self.src_w, self.src_h = r.get("step", 1), r.get("src_w", r["w"]), r.get("src_h", r["h"])

    def __repr__(self):
        return f"Frame({self.w}x{self.h}, seq={self.seq}, fresh={self.fresh})"

    def png(self):
        """The frame as PNG bytes (stdlib only: zlib plus the chunk framing)."""
        def chunk(tag, data):
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        stride = self.w * 3
        raw = b"".join(b"\x00" + self.rgb[y * stride:(y + 1) * stride] for y in range(self.h))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    def save_png(self, path):
        with open(path, "wb") as f:
            f.write(self.png())
        return path


class Namespace:
    """rmm.machine, rmm.cpu.regs, rmm.fdd[0], ...: attribute access builds the dotted name,
    calling it calls the method."""

    def __init__(self, rmm, prefix):
        self._rmm = rmm
        self._prefix = prefix

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return Namespace(self._rmm, f"{self._prefix}.{name}")

    def __getitem__(self, index):
        return Namespace(self._rmm, f"{self._prefix}.{int(index)}")

    def __call__(self, **params):
        return self._rmm.call(self._prefix, **params)

    def __dir__(self):
        return self._rmm._children(self._prefix)

    def __repr__(self):
        m = self._rmm.methods.get(self._prefix)
        return f"<rmm {m.signature() if m else self._prefix + '.*'}>"


def key_event(key, down):
    """An input.events key event. key: a key name as input.key.send takes ("a", "enter",
    "lctrl", ...) or a set 1 scancode (0x1xx = E0 prefix)."""
    ev = {"type": "key", "down": bool(down)}
    ev["key" if isinstance(key, str) else "scan"] = key
    return ev


def rel_event(dx, dy, dz=0):
    """Relative motion (dy > 0 is down) and wheel notches (dz > 0 is away from the user)."""
    ev = {"type": "rel", "dx": int(dx), "dy": int(dy)}
    if dz:
        ev["dz"] = int(dz)
    return ev


def abs_event(x, y):
    """An absolute position, 0..1 across the screen; needs a tablet in absolute mode."""
    return {"type": "abs", "x": float(x), "y": float(y)}


def btn_event(buttons):
    """The button mask held from now on: bit 0 left, 1 right, 2 middle, 3-4 extra."""
    return {"type": "btn", "buttons": int(buttons)}


class Input(Namespace):
    """rmm.input: the input.* methods, plus helpers that send input.events.

    The events of one call are delivered in order; 86Box spaces keys and button changes in
    guest time, so a whole sequence can go in one call:
        rmm.input.send(key_event("lctrl", True), btn_event(1), btn_event(0), key_event("lctrl", False))
    A viewer that grabs the host pointer sends rel_event()s, one that does not sends
    abs_event()s; rmm.input.devices()["pointer"] says which the guest takes ("absolute" or
    "relative"). release_all() lets go of everything the client holds, as disconnecting does.
    """

    HELPERS = ("send", "press", "release", "tap", "move", "point", "buttons", "click")

    def __init__(self, rmm):
        super().__init__(rmm, "input")

    def send(self, *events):
        """-> {queued, pending}: pending is what still waits for guest time."""
        return self._rmm.call("input.events", events=list(events))

    def press(self, key):
        return self.send(key_event(key, True))

    def release(self, key):
        return self.send(key_event(key, False))

    def tap(self, *keys):
        """Press keys in order and release them in reverse: tap("lctrl", "lalt", "del")."""
        return self.send(*[key_event(k, True) for k in keys], *[key_event(k, False) for k in reversed(keys)])

    def move(self, dx, dy, dz=0):
        return self.send(rel_event(dx, dy, dz))

    def point(self, x, y):
        return self.send(abs_event(x, y))

    def buttons(self, mask):
        return self.send(btn_event(mask))

    def click(self, button=1):
        """Press and release one button (1 left, 2 right, 4 middle); other buttons end released."""
        return self.send(btn_event(button), btn_event(0))

    def __dir__(self):
        return sorted(set(self.HELPERS) | set(super().__dir__()))


class VmConfig:
    """rmm.vm: the running VM's config as a KVM-style domain (rmm_86box.kvm), stored as JSON
    in `store_dir`. Loading stages the cfg; it takes effect on a hard reset."""

    def __init__(self, rmm, store_dir):
        self._rmm = rmm
        self.store = kvm.DomainStore(store_dir)

    def cfg(self):
        return self._rmm.call("config.dump")["cfg"]

    def show(self):
        """The running VM as a domain."""
        return kvm.from_cfg(self.cfg())

    def save(self, name):
        """Store the running VM as <name>; returns the file written."""
        return self.store.save(name, self.show())

    def list(self):
        return self.store.list()

    def get(self, name):
        return self.store.load(name)

    def diff(self, name):
        """[(section, key, running, stored)] that loading <name> would change."""
        return kvm.diff(self.show(), self.store.load(name))

    def load(self, name_or_domain, reset=True, validate_only=False):
        """Stage a stored (or given) domain; with reset, hard-reset so it takes effect.
        Returns {"load": config.load result, "reset": machine.reset result or None}."""
        dom = self.store.load(name_or_domain) if isinstance(name_or_domain, str) else name_or_domain
        res = self._rmm.call("config.load", cfg=kvm.to_cfg(dom), validate_only=validate_only)
        rst = None
        if reset and not validate_only:
            rst = self._rmm.call("machine.reset", hard=True, _timeout=30)
        return {"load": res, "reset": rst}


class Rmm:
    def __init__(self, port, host="127.0.0.1", timeout=10.0, resume_on_connect=True, vm_store=None):
        self.rpc = RpcClient(port, host, timeout)
        self.resume_on_connect = resume_on_connect
        self.methods = Methods()
        self.vm = VmConfig(self, vm_store or os.path.join(os.path.expanduser("~"), ".rmm_86box", "domains"))
        self.last_result = None
        self.server_info = None              # rpc.hello answer; None before connect or on an old 86Box

    # -- connection -----------------------------------------------------------------------

    def connect(self, wait=0):
        """Connect, learn the method table (rpc.discover) and, by default, resume the guest:
        86Box pauses it while a monitor client attaches. Returns the banner."""
        banner = self.rpc.connect(wait)
        try:
            self.server_info = self._hello()
        except RpcError:
            self.close()
            raise
        try:
            self.methods.merge_discover(self.rpc.call("rpc.discover"))
        except RpcError:
            pass
        if self.resume_on_connect and banner.rstrip().endswith("paused"):
            self.rpc.call("machine.resume")
        return banner

    def _hello(self):
        """Version handshake. Returns the server's rpc.hello answer, or None for an 86Box that
        predates it (method not found: protocol 0, which is accepted). Raises RpcError (code 11)
        when either side is too old for the other."""
        try:
            info = self.rpc.call("rpc.hello", dict(protocol=PROTOCOL, client="rmm_86box"))
        except RpcError as e:
            if e.code == -32601:
                return None
            raise
        if info.get("protocol", 0) < MIN_SERVER_PROTOCOL:
            raise RpcError(f"86Box speaks RMM protocol {info.get('protocol')}, this client needs "
                           f">= {MIN_SERVER_PROTOCOL}; update 86Box", code=11, data=info)
        return info

    def close(self):
        self.rpc.close()

    @property
    def connected(self):
        return self.rpc.connected

    def __enter__(self):
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- calls ----------------------------------------------------------------------------

    def call(self, method, _timeout=None, **params):
        r = self.rpc.call(method, params, timeout=_timeout)
        self.last_result = r
        return r

    def on_event(self, callback):
        """callback(name, params) for machine.stopped / machine.resumed / machine.reset /
        media.changed / machine.exit / rpc.disconnected; runs on the reader thread."""
        return self.rpc.on_event(callback)

    def __getattr__(self, name):
        if name.startswith("_") or name in ("rpc", "methods", "vm"):
            raise AttributeError(name)
        return Input(self) if name == "input" else Namespace(self, name)

    def _children(self, prefix):
        """Next name segments under a dotted prefix ("" = top level)."""
        out = set()
        pre = prefix + "." if prefix else ""
        for n in self.methods.names():
            if n.startswith(pre) and n != prefix:
                out.add(n[len(pre):].split(".")[0])
        return sorted(out)

    def __dir__(self):
        own = ["call", "close", "connect", "connected", "on_event", "paused", "screenshot", "regs",
               "read_mem", "write_mem", "text_screen", "state", "turbo", "sticky_pause", "vm", "methods", "last_result"]
        return sorted(set(own) | set(self._children("")))

    # -- helpers --------------------------------------------------------------------------

    def state(self):
        return self.call("machine.status")["state"]

    @property
    def turbo(self):
        """Turbo mode: run the guest unthrottled (86Box's fast forward, `machine.fast_forward`).
        `rmm.turbo = True` switches it on; the same flag is the Qt console's Turbo button."""
        return bool(self.call("machine.fast_forward.get")["value"])

    @turbo.setter
    def turbo(self, on):
        self.call("machine.fast_forward.set", value=bool(on))

    @property
    def sticky_pause(self):
        """True when a pause you ask for outlives this connection (86Box --monitor-sticky-pause or
        `[General] rmm_sticky_pause = 1`); otherwise 86Box resumes the guest when you disconnect."""
        return bool(self.call("rmm.sticky_pause.get")["value"])

    @sticky_pause.setter
    def sticky_pause(self, on):
        self.call("rmm.sticky_pause.set", value=bool(on))

    @contextlib.contextmanager
    def paused(self):
        """Pause for the block (registers, memory and ports need it); resume after if it
        was running."""
        was = self.state()
        if was == "running":
            self.call("machine.pause")
        try:
            yield self
        finally:
            if was == "running" and self.connected:
                self.call("machine.resume")

    def screenshot(self, step=1, wait_ms=200):
        """The guest screen as a Frame. Never pauses the guest."""
        return Frame(self.call("capture.screenshot", step=step, wait_ms=wait_ms))

    def regs(self):
        """General registers + segments; pauses briefly if running."""
        with self.paused():
            return self.call("cpu.regs.get")

    def read_mem(self, addr, length, linear=False):
        """bytes; any length (read 4 KiB at a time); pauses briefly if running."""
        out = bytearray()
        method = "mem.lin.read" if linear else "mem.phys.read"
        with self.paused():
            while len(out) < length:
                n = min(4096, length - len(out))
                out += bytes.fromhex(self.call(method, addr=addr + len(out), len=n)["data"])
        return bytes(out)

    def write_mem(self, addr, data):
        with self.paused():
            for off in range(0, len(data), 4096):
                self.call("mem.phys.write", addr=addr + off, data=bytes(data[off:off + 4096]).hex())

    def text_screen(self, rows=25, cols=80):
        """The colour text page at B8000 as lines (text modes only)."""
        raw = self.read_mem(0xB8000, rows * cols * 2)[::2]
        return [raw[i:i + cols].decode("cp437", "replace") for i in range(0, rows * cols, cols)]


__all__ = ["Rmm", "Frame", "Namespace", "Input", "VmConfig", "RpcError", "REGS",
           "key_event", "rel_event", "abs_event", "btn_event"]
