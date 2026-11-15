# SPDX-License-Identifier: MIT
"""QemuMachine: a QEMU VM behind the Rmm surface, over QMP.

    RMM                         QEMU
    machine.status/pause/resume query-status / stop / cont
    machine.reset               system_reset
    capture.screenshot          screendump (PPM in the run dir; VNC later, see vnc.py)
    input.events & helpers      input-send-event (qnum keys, rel/abs axes, btn up/down)
    input.devices               query-mice + the profile
    snapshot.save/restore/list  savevm / loadvm / info snapshots over HMP (needs a qcow2 disk)
    events                      STOP RESUME RESET SHUTDOWN -> machine.stopped/resumed/reset/exit

Not yet: registers, memory and breakpoints (gdbstub, gdb.py), media slots
(blockdev-change-medium), VNC frames. Those names are not offered, so callers see -32601.

QMP delivers input at once with no guest-time hook, so keys and button changes are spaced
in host time on a sender thread (KEY_GAP / BTN_GAP), the way 86Box spaces them in guest time.
"""

import base64
import collections
import os
import threading
import time

from ...rpc import RpcError
from ..base import Backend
from . import keys, profiles
from .qmp import Qmp

KEY_GAP = 0.010                  # seconds between key events (PS/2, ADB and ESCC queues are short)
BTN_GAP = 0.030                  # between button changes
ABS_MAX = 0x7FFF                 # QEMU's absolute axis range
BUTTONS = ("left", "right", "middle", "side", "extra")    # bit 0.. of the RMM button mask
QUEUE_MAX = 256

PAUSED = {"paused", "prelaunch", "debug", "suspended", "inmigrate", "postmigrate", "finish-migrate",
          "save-vm", "restore-vm", "watchdog", "io-error"}


class QemuVm:
    """rmm.vm for QEMU: the profile and options as a KVM-style domain (read only)."""

    def __init__(self, m):
        self._m = m

    def show(self):
        m = self._m
        p, opts = m.profile, (m.proc.opts if m.proc else {})
        disks = []
        for slot, dev in (("fda", "floppy"), ("fdb", "floppy"), ("hda", "disk"), ("hdb", "disk"), ("cdrom", "cdrom")):
            if opts.get(slot):
                disks.append({"device": dev, "target": slot if slot != "cdrom" else "hdc", "source": opts[slot]})
        for i, path in enumerate(opts.get("scsi") or ()):
            disks.append({"device": "disk", "bus": "scsi", "target": f"sd{chr(97 + i)}", "source": path})
        return {"name": p.name, "machine": p.machine, "cpu": {"model": p.cpu or p.arch},
                "memory_kib": (m.proc.memory_mib if m.proc else p.memory_mib) * 1024,
                "video": {"model": "qemu"}, "disks": disks}


class QemuMachine(Backend):
    KIND = "qemu"
    HANDLERS = {
        "rpc.discover": "discover",
        "machine.status": "m_status",
        "machine.pause": "m_pause",
        "machine.resume": "m_resume",
        "machine.reset": "m_reset",
        "machine.fast_forward.get": "m_fast_forward",
        "capture.screenshot": "m_screenshot",
        "input.events": "m_events",
        "input.mouse.move": "m_mouse_move",
        "input.key.send": "m_key_send",
        "input.text.type": "m_text_type",
        "input.release_all": "m_release_all",
        "input.devices": "m_devices",
        "snapshot.save": "m_snap_save",
        "snapshot.restore": "m_snap_restore",
        "snapshot.list": "m_snap_list",
        "snapshot.delete": "m_snap_delete",
        "qmp.execute": "m_qmp",
    }
    EXTRA = {
        "qmp.execute": ("command:string arguments:object?", "return", "",
                        "QEMU passthrough: one QMP command and its return"),
    }

    def __init__(self, qmp_address, profile, proc=None, timeout=10.0, resume_on_connect=True,
                 stop_on_close=True):
        self.profile = profiles.get(profile) if isinstance(profile, str) else profile
        self.proc = proc                         # QemuProcess when we started it, else None
        self.resume_on_connect = resume_on_connect
        self.stop_on_close = stop_on_close       # False: the owner of proc stops it (the GUI's node)
        super().__init__()
        self.vm = QemuVm(self)
        self.qmp = Qmp(qmp_address, timeout)
        self.qmp.on_event = self._qmp_event
        self.qmp.on_close = lambda: self._emit("rpc.disconnected")
        self._seq = 0
        self._held_keys = set()                  # qnums down
        self._buttons = 0                        # mask the guest holds now
        self._btn_target = 0                     # mask once the queue has drained
        self._q = collections.deque()            # (kind, qmp events) waiting for the sender
        self._q_wake = threading.Event()
        self._sender = None

    # -- connection ----------------------------------------------------------------------------

    @property
    def connected(self):
        return self.qmp.connected

    def connect(self, wait=0):
        ver = self.qmp.connect(wait)
        self.server_info = {"server": "qemu", "version": ver, "profile": self.profile.name}
        st = self.qmp.execute("query-status")["status"]
        if self.resume_on_connect and st in ("prelaunch", "paused"):
            self.qmp.execute("cont")
            st = "running"
        if not self._sender:
            self._sender = threading.Thread(target=self._send_loop, name="qemu-input", daemon=True)
            self._sender.start()
        v = f"{ver.get('major')}.{ver.get('minor')}.{ver.get('micro')}"
        return f"QEMU {v} {self.profile.machine} {self._state(st)}"

    def close(self):
        """Drop the connection; a QEMU we started is stopped with it."""
        if self.qmp.connected:
            try:
                self.m_release_all()
            except RpcError:
                pass
        if self.proc and self.stop_on_close:
            self.proc.stop(self.qmp)
        self.qmp.close()

    def _qmp_event(self, name, data):
        ev = {"STOP": ("machine.stopped", {"reason": "pause"}), "RESUME": ("machine.resumed", {}),
              "RESET": ("machine.reset", {"hard": True}), "SHUTDOWN": ("machine.exit", {})}.get(name)
        if ev:
            self._emit(*ev)

    @staticmethod
    def _state(status):
        return "running" if status == "running" else "paused" if status in PAUSED else status

    # -- machine ----------------------------------------------------------------------------------

    def m_status(self):
        st = self.qmp.execute("query-status")
        return {"state": self._state(st["status"]), "status": st["status"], "sticky_pause": True}

    def m_pause(self):
        self.qmp.execute("stop")
        return {"state": "paused"}

    def m_resume(self):
        self.qmp.execute("cont")
        return {"state": "running"}

    def m_reset(self, hard=True):
        self.qmp.execute("system_reset")
        return {"state": self.m_status()["state"], "config_applied": False, "config_loaded": False}

    def m_fast_forward(self):
        return {"value": False}                  # TCG has no throttle to lift

    def m_qmp(self, command, arguments=None):
        return {"return": self.qmp.execute(command, **(arguments or {}))}

    # -- frames -----------------------------------------------------------------------------------

    def m_screenshot(self, step=1, wait_ms=None):
        path = os.path.join(self._run_dir(), "screen.ppm")
        self.qmp.execute("screendump", filename=path, format="ppm")
        with open(path, "rb") as f:
            w, h, rgb = _read_ppm(f.read())
        step = max(1, int(step))
        if step > 1:
            rows = [rgb[y * w * 3:(y + 1) * w * 3] for y in range(0, h, step)]
            rgb = b"".join(b"".join(r[x * 3:x * 3 + 3] for x in range(0, w, step)) for r in rows)
        self._seq += 1
        return {"w": -(-w // step), "h": -(-h // step), "src_w": w, "src_h": h, "step": step,
                "format": "rgb24", "seq": self._seq, "fresh": True, "data": base64.b64encode(rgb).decode()}

    def _run_dir(self):
        if self.proc:
            return self.proc.run_dir
        if isinstance(self.qmp.address, str):
            return os.path.dirname(self.qmp.address)
        raise RpcError("capture.screenshot: attached over TCP, no local run dir to dump into", code=4)

    # -- input --------------------------------------------------------------------------------------

    def m_events(self, events):
        """Same events and rules as 86Box's input.events: all parsed before any is queued."""
        if not isinstance(events, list) or len(events) > QUEUE_MAX:
            raise RpcError(f"input.events: events is a list of at most {QUEUE_MAX}", code=-32602)
        batch, buttons = [], self._btn_target
        for i, ev in enumerate(events):
            try:
                kind, out, buttons = self._parse(ev, buttons)
            except (KeyError, TypeError, ValueError) as e:
                raise RpcError(f"events[{i}]: {e}", code=-32602) from None
            batch.append((kind, out))
        if len(self._q) + len(batch) > QUEUE_MAX:
            raise RpcError("input.events: queue full", code=4)
        self._q.extend(batch)
        self._btn_target = buttons
        self._q_wake.set()
        return {"queued": len(batch), "pending": len(self._q)}

    def _parse(self, ev, buttons):
        t = ev.get("type")
        if t == "key":
            k = ev["key"] if "key" in ev else ev["scan"]
            return "key", [keys.qkey(k, ev["down"])], buttons
        if t == "rel":
            out = [{"type": "rel", "data": {"axis": "x", "value": int(ev["dx"])}},
                   {"type": "rel", "data": {"axis": "y", "value": int(ev["dy"])}}]
            dz = int(ev.get("dz", 0))
            wheel = "wheel-up" if dz > 0 else "wheel-down"
            for _ in range(abs(dz)):
                out += [_btn(wheel, True), _btn(wheel, False)]
            return "rel", out, buttons
        if t == "abs":
            x, y = float(ev["x"]), float(ev["y"])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError("x and y are 0..1")
            return "abs", [{"type": "abs", "data": {"axis": "x", "value": round(x * ABS_MAX)}},
                           {"type": "abs", "data": {"axis": "y", "value": round(y * ABS_MAX)}}], buttons
        if t == "btn":
            b = int(ev["buttons"])
            if not 0 <= b <= 31:
                raise ValueError("buttons is a mask 0..31")
            out = [_btn(name, bool(b >> i & 1)) for i, name in enumerate(BUTTONS) if (b ^ buttons) >> i & 1]
            return "btn", out, b
        raise ValueError(f"unknown type {t!r}")

    def _send_loop(self):
        while True:
            self._q_wake.wait()
            self._q_wake.clear()
            while self._q:
                kind, out = self._q.popleft()
                if not self.qmp.connected or not out:
                    continue
                try:
                    self.qmp.execute("input-send-event", events=out)
                except RpcError:
                    continue
                for e in out:
                    if e["type"] == "key":
                        q = e["data"]["key"]["data"]
                        (self._held_keys.add if e["data"]["down"] else self._held_keys.discard)(q)
                    elif e["type"] == "btn" and e["data"]["button"] in BUTTONS:
                        bit = 1 << BUTTONS.index(e["data"]["button"])
                        self._buttons = (self._buttons | bit) if e["data"]["down"] else (self._buttons & ~bit)
                if kind in ("key", "btn"):
                    time.sleep(KEY_GAP if kind == "key" else BTN_GAP)

    def m_mouse_move(self, dx, dy, buttons=None):
        evs = ([{"type": "btn", "buttons": buttons}] if buttons is not None else []) + [{"type": "rel", "dx": dx, "dy": dy}]
        return self.m_events(evs)

    def m_key_send(self, combo):
        ks = keys.combo(combo)
        return self.m_events([{"type": "key", "key": k, "down": True} for k in ks]
                             + [{"type": "key", "key": k, "down": False} for k in reversed(ks)])

    def m_text_type(self, text):
        try:
            seq = keys.text_keys(text)
        except KeyError as e:
            raise RpcError(f"input.text.type: {e}", code=-32602) from None
        evs = []
        for k, shift in seq:
            pre = [{"type": "key", "key": "shift", "down": True}] if shift else []
            post = [{"type": "key", "key": "shift", "down": False}] if shift else []
            evs += pre + [{"type": "key", "key": k, "down": True}, {"type": "key", "key": k, "down": False}] + post
        for i in range(0, len(evs), QUEUE_MAX):                 # long text: wait for room
            while len(self._q) + len(evs[i:i + QUEUE_MAX]) > QUEUE_MAX:
                time.sleep(0.05)
            self.m_events(evs[i:i + QUEUE_MAX])
        return {}

    def m_release_all(self):
        self._q.clear()
        held = sorted(self._held_keys)
        out = [{"type": "key", "data": {"down": False, "key": {"type": "number", "data": q}}} for q in held]
        out += [_btn(name, False) for i, name in enumerate(BUTTONS) if self._buttons >> i & 1]
        b = self._buttons
        if out:
            self.qmp.execute("input-send-event", events=out)
        self._held_keys.clear()
        self._buttons = self._btn_target = 0
        return {"keys": held, "buttons": b, "pending": 0}

    def m_devices(self):
        mice = self.qmp.execute("query-mice") or []
        cur = next((m for m in mice if m.get("current")), None)
        p = self.profile
        tablet = next(({"type": m["name"]} for m in mice if m.get("absolute")), None)
        return {"keyboard": {"type": p.keyboard},
                "mouse": {"type": p.mouse, "buttons": p.mouse_buttons} if cur else None,
                "joystick": None, "tablet": tablet,
                "pointer": "absolute" if cur and cur.get("absolute") else "relative",
                "pending": len(self._q)}

    # -- snapshots (HMP: the disk must be qcow2) ---------------------------------------------

    def _hmp(self, line, code):
        out = (self.qmp.hmp(line) or "").strip()
        if out:                                  # savevm/loadvm/delvm print only on failure
            raise RpcError(f"{line}: {out}", code=code)
        return out

    def m_snap_save(self, tag):
        self._hmp(f"savevm {tag}", 8)
        return {}

    def m_snap_restore(self, tag):
        self.qmp.execute("stop")
        self._hmp(f"loadvm {tag}", 7)
        return {}

    def m_snap_delete(self, tag):
        self._hmp(f"delvm {tag}", 7)
        return {}

    def m_snap_list(self):
        """Tags from `info snapshots`: the TAG column of each row under the header."""
        out, tags = self.qmp.hmp("info snapshots") or "", []
        rows = out.splitlines()
        hdr = next((i for i, r in enumerate(rows) if r.split()[:2] == ["ID", "TAG"]), None)
        if hdr is not None:
            for r in rows[hdr + 1:]:
                f = r.split()
                if len(f) >= 2 and f[0] not in ("--",):
                    tags.append({"tag": f[1]})
        return {"snapshots": tags}


def _btn(name, down):
    return {"type": "btn", "data": {"down": bool(down), "button": name}}


def _read_ppm(data):
    """P6 PPM, maxval 255 -> (w, h, rgb bytes)."""
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    if fields[0] != b"P6" or int(fields[3]) != 255:
        raise RpcError("screendump: not a P6/255 PPM", code=4)
    w, h = int(fields[1]), int(fields[2])
    return w, h, data[pos + 1:pos + 1 + w * h * 3]
