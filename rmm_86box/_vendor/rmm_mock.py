#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reference implementation of the RMM spec, in Python: what 86Box's monitor is meant to
do, with a simulated MEMLOOP.COM guest. rmm_spec.py --target mock must pass every test
here; that is what shows the spec is coherent before any C is written. Not an emulator:
only what the spec observes is modelled.

Simulated guest: MEMLOOP at CS=0800h: `inc word [04F0]` at 0x10F (4 bytes), `jmp short`
at 0x113, CS published at 0000:04F2, 6 MiB RAM, BIOS ROM at F0000-FFFFF, video RAM at A0000.

    rmm_mock.py [PORT]      run standalone (default 55899); Ctrl-C to stop
"""

import base64
import copy
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rmm_registry
import rmm_schema

CS = 0x0800
LOOP_EIP, LOOP_NEXT = 0x10F, 0x113
RAM_KB = 6144
ROM = (0xF0000, 0x100000)
SLOTS = {"fdd": [0, 1]}            # drives the mock machine has; no CD-ROM
# hw="removable": the same machine with one of every removable drive the registry names, as
# its cfg would list them (the spec's `removable` group runs against this)
HW_REMOVABLE = {"fdd": [0, 1], "cdrom": [0], "rdisk": [0], "mo": [0], "tape": [0], "cassette": [0],
                "cartridge": [0]}
# hw="joy_ms" / "ch_lt": a 2-axis joystick + Microsoft serial mouse on COM1 / a CH Flightstick Pro +
# Logitech serial mouse on COM2; hw="mfm" / "esdi" / "xta": that hard disk controller card with one
# disk (the spec's `input` and `hdc` groups run against these, then against a machine with none)
HW_INPUT = {"joy_ms": dict(joystick="2axis_2button", mouse="msserial", port=0),
            "ch_lt": dict(joystick="ch_flightstick_pro", mouse="ltserial", port=1)}
JOY_AXES_BUTTONS = {"2axis_2button": (2, 2), "ch_flightstick_pro": (4, 4)}
COM_BASE = (0x3F8, 0x2F8, 0x3E8, 0x2E8)
HW_HDC = {"mfm": ("st506_at", "mfm", [0x1F0, 0x3F6], {0x1F7: 0x50}),
          "esdi": ("esdi_at", "esdi", [0x1F0, 0x3F6], {0x1F7: 0x50}),
          "xta": ("xta_wdxt150", "xta", [0x320], {0x321: 0x00})}
# hw="rtc_ev170" / "rtc_pii147" / "fdc_b215": an ISA RTC card (MM58167 at 2C0h / 240h) or a floppy controller
# card (main status register 3F4h = RQM, idle); the spec's `isa` group runs against these, then against a machine with none
HW_ISA = {"rtc_ev170": ("Other peripherals", "isartc_type", "ev170", 0x2C0),
          "rtc_pii147": ("Other peripherals", "isartc_type", "pii147", 0x240),
          "fdc_b215": ("Storage controllers", "fdc", "b215", None)}
HW_CFG = {"Floppy and CD-ROM drives": {"cdrom_01_parameters": "1, scsi", "cdrom_01_scsi_location": "0:03",
                                        "cassette_enabled": 1, "cdrom_01_image_path": ""},
          "Other removable devices": {"tape_01_parameters": "0, scsi", "tape_01_scsi_location": "0:04",
                                      "mo_01_parameters": "0, scsi", "mo_01_scsi_location": "0:05",
                                      "rdisk_01_parameters": "1, atapi", "rdisk_01_ide_channel": "1:0"}}
KEYS = {"enter", "shift", "ctrl", "alt", "esc", "f12", "a", "b", "space", "del", "tab"}
CONFIG_DESC = {
    ("Machine", "mem_size"): dict(type="int", applies="reset", min=1024, max=16384, step=1024, default=RAM_KB),
    ("General", "sound_gain"): dict(type="int", applies="now", min=0, max=18, step=1, default=0),
}
# hardware-name keys (config.enums): config.describe gives them as enums, config.load checks them.
# (section, key or pattern) -> (names, default when the cfg leaves the key out)
ENUMS = {
    ("Machine", "machine"): (["ms4144", "acerp3", "486pi", "ibmpc", "ibmat", "ibmpcjr"], None),
    ("Machine", "cpu_family"): (["i486dx", "i486sx", "am486dx", "8088", "286"], None),
    ("Machine", "cpu_speed"): (None, None),          # per CPU family: CPU_SPEEDS
    ("Machine", "fpu_type"): (None, None),           # per CPU family: CPU_FPUS, default the first
    ("Machine", "cpu_waitstates"): (None, "0"),      # 0..8 on a 286, else 0
    ("Video", "gfxcard"): (["none", "internal", "cl_gd5420_isa", "vga", "et4000ax"], None),
    ("Video", "gfxcard_2"): (["none", "mda", "hercules"], "none"),
    ("Sound", "sndcard"): (["none", "sb16", "sbprov2", "adlib"], "none"),
    ("Sound", "sndcardN"): (["none", "sb16", "sbprov2", "adlib"], "none"),
    ("Sound", "midi_device"): (["none", "rtmidi"], "none"),
    ("Sound", "midi_in_device"): (["none", "rtmidi"], "none"),
    ("Sound", "fm_driver"): (["nuked", "ymfm"], "nuked"),
    ("Network", "net_NN_card"): (["none", "ne2k", "rtl8029as", "3c503"], "none"),
    ("Storage controllers", "fdc"): (["none", "internal", "b215", "dtk_pii158b"], "internal"),
    ("Storage controllers", "cdrom_interface"): (["none", "mcd"], "none"),
    ("Storage controllers", "hdc_N"): (["none", "ide_isa", "st506_at", "esdi_at", "xta_wdxt150"], "none"),
    ("Storage controllers", "scsicard_N"): (["none", "aha154xc", "bt545s", "t130b"], "none"),
    ("Other peripherals", "isartc_type"): (["none", "ev170", "pii147", "mplus2"], "none"),
    ("Other peripherals", "isaromN_type"): (["none", "isarom", "isarom_dual"], "none"),
    ("Other peripherals", "isamemN_type"): (["none", "ibmat", "ev159"], "none"),
    ("Ports (COM & LPT)", "lptN_device"): (["none", "dss", "lpt_dac", "text_prt"], "none"),
    ("Ports (COM & LPT)", "serialN_device"): (["none", "nullmodem", "serial_passthrough"], "none"),
    ("Input devices", "keyboard_type"): (["keyboard_at", "keyboard_ps2"], "keyboard_at"),
    ("Input devices", "mouse_type"): (["none", "msserial", "ltserial", "ps2"], "none"),
    ("Input devices", "tablet_type"): (["none"], "none"),
    ("Input devices", "joystick_type"): (["none", "2axis_2button", "ch_flightstick_pro"], "none"),
}
# the concrete keys of each pattern
ENUM_SLOTS = {"sndcardN": [f"sndcard{i}" for i in range(2, 5)], "net_NN_card": [f"net_0{i}_card" for i in range(1, 5)],
              "hdc_N": [f"hdc_{i}" for i in range(1, 5)], "scsicard_N": [f"scsicard_{i}" for i in range(1, 5)],
              "isaromN_type": [f"isarom{i}_type" for i in range(4)],
              "isamemN_type": [f"isamem{i}_type" for i in range(4)],
              "lptN_device": [f"lpt{i}_device" for i in range(1, 5)],
              "serialN_device": [f"serial{i}_device" for i in range(1, 8)]}
# CPU families each mock machine takes (the real check is cpu_family_is_eligible)
MACHINE_CPUS = {"ms4144": ["i486dx", "i486sx", "am486dx"], "acerp3": ["i486dx", "i486sx", "am486dx"],
                "486pi": ["i486dx", "i486sx", "am486dx"], "ibmpc": ["8088"], "ibmat": ["286"], "ibmpcjr": ["8088"]}
# family -> [(rspeed, multi, name)]; two entries may share a speed (the multiplier tells them apart)
CPU_SPEEDS = {"i486dx": [(25000000, 1.0, "i486DX/25"), (33333333, 1.0, "i486DX/33"), (50000000, 1.0, "i486DX/50")],
              "i486sx": [(16000000, 1.0, "i486SX/16"), (25000000, 1.0, "i486SX/25")],
              "am486dx": [(33333333, 1.0, "Am486DX/33"), (40000000, 1.0, "Am486DX/40")],
              "8088": [(4772728, 1.0, "8088/4.77"), (8000000, 1.0, "8088/8")],
              "286": [(6000000, 1.0, "286/6"), (12500000, 1.0, "286/12.5")]}
CPU_FPUS = {"i486dx": ["internal"], "am486dx": ["internal"], "i486sx": ["none", "i487sx"], "8088": ["none", "8087"],
            "286": ["none", "287"]}
IMAGE_SECTIONS = ("Hard disks", "Floppy and CD-ROM drives", "Other removable devices")
CGA = [(0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170), (170, 0, 0), (170, 0, 170), (170, 85, 0),
       (170, 170, 170), (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255), (255, 85, 85),
       (255, 85, 255), (255, 255, 85), (255, 255, 255)]


def enum_key(sec, key):
    """The ENUMS row for a concrete key, None if it is not a hardware-name key."""
    if (sec, key) in ENUMS and key not in ENUM_SLOTS:
        return (sec, key)
    for pat, keys in ENUM_SLOTS.items():
        if key in keys and (sec, pat) in ENUMS:
            return (sec, pat)
    return None


def enum_keys():
    """Every concrete hardware-name key, in table order."""
    for sec, k in ENUMS:
        for key in ENUM_SLOTS.get(k, [k]):
            yield sec, key


def enum_values(ek, machine, family):
    """(names, labels, extra) a key offers for this machine / CPU family."""
    names = ENUMS[ek][0]
    if ek == ("Machine", "cpu_family"):
        names = MACHINE_CPUS.get(machine, names)
    elif ek == ("Machine", "cpu_speed"):
        sp = CPU_SPEEDS.get(family, [])
        return [str(s) for s, _, _ in sp], [n for _, _, n in sp], dict(multi=[m for _, m, _ in sp])
    elif ek == ("Machine", "fpu_type"):
        names = CPU_FPUS.get(family, ["none"])
    elif ek == ("Machine", "cpu_waitstates"):
        names = [str(i) for i in range(9)] if family == "286" else ["0"]
    return list(names), ["None" if n == "none" else n.replace("_", " ").title() for n in names], {}


PROTOCOL = 1          # keep in step with RMM_PROTOCOL in 86Box src/rmm.c
PROTOCOL_MIN = 1


class E(Exception):
    def __init__(self, code, msg, data=None):
        super().__init__(msg)
        self.code, self.msg, self.data = code, msg, data


def bad(msg):
    return E(-32602, msg)


class Machine:
    """The simulated guest. All state under `lock`; `emit` delivers notifications."""

    def __init__(self, emit):
        self.lock = threading.RLock()
        self.emit = emit
        self.ram = bytearray(RAM_KB * 1024)
        self.running = False
        self.dead = False
        self.tsc = 123456
        self.icount = 0
        self.skip_bp = False
        self.step_left = 0
        self.regs = {}
        self.bps, self.watches, self.snaps = {}, {}, {}
        self.ports, self.indexed = {}, {}   # io: plain latches, and index/data register files
        self.resets = 0   # a snapshot restores only within the reset generation it was taken in
        self.next_id = 1
        self.props = {}
        self.auto = False                           # paused by pause-on-connect, not by the client
        self.boot_ram()
        self.reset_regs()
        threading.Thread(target=self._loop, daemon=True).start()

    def boot_ram(self):
        r = self.ram
        r[0x413:0x415] = (640).to_bytes(2, "little")
        r[0x4F2:0x4F4] = CS.to_bytes(2, "little")
        base = CS * 16
        r[base + LOOP_EIP:base + LOOP_EIP + 4] = bytes.fromhex("FF06F004")
        r[base + LOOP_NEXT:base + LOOP_NEXT + 2] = bytes.fromhex("EBFA")
        r[0xF0000:0xF0004] = b"ROM!"
        r[0xB8000:0xB8000 + 8] = b"R\x07M\x07M\x07L\x07"       # a screen with something on it
        for i, ch in enumerate(b"A:\\>"):                      # a prompt, cursor after it (BDA 0040:0050)
            r[0xB8000 + 160 + i * 2:0xB8000 + 160 + i * 2 + 2] = bytes((ch, 0x07))
        r[0x450:0x452] = bytes((4, 1))

    def echo(self, text):
        """What DOS at the prompt would show for typed text: characters at the BIOS cursor
        (0040:0050, so snapshots roll it back with the screen), scrolling at the bottom."""
        r, vb = self.ram, 0xB8000
        col, row = r[0x450], r[0x451]
        for ch in text:
            if ch == "\n":
                col, row = 0, row + 1
            elif ch == "\b":
                col = max(col - 1, 0)
                r[vb + (row * 80 + col) * 2] = 32
            else:
                r[vb + (row * 80 + col) * 2:vb + (row * 80 + col) * 2 + 2] = bytes((ord(ch) & 0xFF, 0x07))
                col += 1
                if col == 80:
                    col, row = 0, row + 1
            if row == 25:
                r[vb:vb + 3840] = r[vb + 160:vb + 4000]
                r[vb + 3840:vb + 4000] = b" \x07" * 80
                row = 24
        r[0x450:0x452] = bytes((col, row))

    def reset_regs(self):
        self.regs = dict(eax=0, ecx=0, edx=0, ebx=0x1234, esp=0xFFFE, ebp=0, esi=0, edi=0,
                         eip=LOOP_EIP, eflags=0x202, cs=CS, ss=CS, ds=0, es=CS, fs=0, gs=0,
                         cs_base=CS * 16, cr0=0, cr2=0, cr3=0, cr4=0)
        self.icount = 0

    # -- the guest ----------------------------------------------------------------------

    @property
    def armed(self):
        return bool(self.bps or self.watches)

    def _cur_eip(self):
        return LOOP_EIP if self.icount % 2 == 0 else LOOP_NEXT

    def _bump(self, n_evens):
        w = (int.from_bytes(self.ram[0x4F0:0x4F2], "little") + n_evens) & 0xFFFF
        self.ram[0x4F0:0x4F2] = w.to_bytes(2, "little")

    def _loop(self):
        while not self.dead:
            time.sleep(0.002)
            with self.lock:
                if not self.running:
                    continue
                if not self.armed and not self.step_left:
                    n = 20000                       # fast path: the stock executor
                    evens = (self.icount + n + 1) // 2 - (self.icount + 1) // 2
                    self._bump(evens)
                    self.icount += n
                    self.tsc += n
                    self.regs["eip"] = self._cur_eip()
                else:
                    for _ in range(1000):           # debug executor: a tenth of the speed
                        if not self.running or self._step():
                            break

    def _step(self):
        """Execute one instruction. True when the machine stopped."""
        eip = self._cur_eip()
        lin = CS * 16 + eip
        if not self.skip_bp:
            for bid, addr in self.bps.items():
                if addr == lin:
                    self._stop("breakpoint", addr=addr, id=bid)
                    return True
        self.skip_bp = False
        hit = None
        if eip == LOOP_EIP:                     # `inc word [04F0h]`: reads, then writes, 0x4F0-0x4F1
            self._bump(1)
            for wid, w in self.watches.items():
                if w["addr"] <= 0x4F1 and 0x4F0 < w["addr"] + w["len"]:
                    hit = (wid, w, "r" if "r" in w["kind"] else "w")
                    break
        self.icount += 1
        self.tsc += 1
        self.regs["eip"] = self._cur_eip()
        if hit:
            self._stop("watchpoint", addr=hit[1]["addr"], kind=hit[2], id=hit[0])
            return True
        if self.step_left:
            self.step_left -= 1
            if self.step_left == 0:
                self._stop("step", addr=CS * 16 + self.regs["eip"])
                return True
        return False

    def _stop(self, reason, **extra):
        self.running = False
        self.emit("machine.stopped", dict(reason=reason, **extra))

    # -- control -------------------------------------------------------------------------

    def pause(self, silent=False):
        with self.lock:
            if self.running:
                self.running = False
                self.step_left = 0
                if not silent:
                    self.emit("machine.stopped", {"reason": "pause"})

    def resume(self, silent=False):
        with self.lock:
            if not self.running:
                self.running = True
                self.skip_bp = True
                if not silent:
                    self.emit("machine.resumed", {})

    def state(self):
        return "running" if self.running else "paused"


def hexint(v, name):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise bad(f"{name} must be a non-negative integer")
    return v


class Server:
    pass


class MockServer:
    def __init__(self, port=0, hw=None):
        self.slots = dict(HW_REMOVABLE if hw == "removable" else SLOTS)
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        self.tmp = tempfile.mkdtemp(prefix="rmm_mock_")
        self.cfg_path = os.path.join(self.tmp, "86box.cfg")
        self.fake_image = os.path.join(self.tmp, "c_floppy.img")
        with open(self.fake_image, "wb") as f:
            f.write(b"\0" * 1024)
        self.cfg = {"Machine": {"machine": "ms4144", "cpu_family": "i486dx", "cpu_speed": 25000000,
                                "mem_size": RAM_KB},
                    "General": {"sound_gain": 0, "rmm_port": self.port},
                    "Video": {"gfxcard": "cl_gd5420_isa"},
                    "Floppy and CD-ROM drives": {"fdd_01_fn": "/testsrc/images/dos622_simple.img",
                                                 "fdd_01_type": "35_2hd", "fdd_02_type": "35_2hd"}}
        if hw == "removable":
            for sec, kv in HW_CFG.items():
                self.cfg.setdefault(sec, {}).update(kv)
            self.cfg["Machine"]["machine"] = "ibmpcjr"                       # the only machine with a cartridge slot
            self.cfg["Floppy and CD-ROM drives"]["cartridge_01_fn"] = "wp:///testsrc/images/BASIC.JRC"
        self.history = {}                  # (dev, slot) -> paths that were in it, newest last
        self.cass = {}                     # cassette slot -> {"mode": idle|play|record|rewind|ffwd}
        self.pending_reset_apply = []      # [(section, key, value)], applied by the next reset
        self.pending_load = None           # config.load: (cfg, replace), applied first by the next reset
        self.load_done = None
        self.frame_seq = 0
        self.media = {("fdd", 0): "/testsrc/images/dos622_simple.img",
                      ("fdd", 1): "/testsrc/sourcedir/86box_remote_monitor/memwrite_build/b_floppy.img"}
        self.wp = {}
        if hw == "removable":
            self.media[("cartridge", 0)], self.wp[("cartridge", 0)] = "/testsrc/images/BASIC.JRC", True
        self.in_dev = HW_INPUT.get(hw)                 # input devices the machine has, or None
        self.uart = []                                 # bytes the serial mouse has sent, not yet read
        self.mouse_buttons = 0
        self.keys_held = set()                         # scancodes this client holds down (input.events)
        self.joy = None
        self.cfg["Input devices"] = {"keyboard_type": "keyboard_at", "mouse_type": "none", "tablet_type": "none",
                                     "joystick_type": "none"}
        if self.in_dev:
            ax, bt = JOY_AXES_BUTTONS[self.in_dev["joystick"]]
            self.joy = dict(axes=[0] * ax, buttons=[False] * bt, timers=0)
            self.cfg["Input devices"].update(mouse_type=self.in_dev["mouse"], joystick_type=self.in_dev["joystick"])
        self.hdc = HW_HDC.get(hw)
        if self.hdc:
            name, bus, _ports, regs = self.hdc
            self.cfg["Storage controllers"] = {"hdc_1": name}
            self.cfg["Hard disks"] = {"hdd_01_parameters": f"17, 4, 615, 0, {bus}", f"hdd_01_{bus}_channel": "0"}
        self.isa = HW_ISA.get(hw)
        if self.isa:
            self.cfg.setdefault(self.isa[0], {})[self.isa[1]] = self.isa[2]
        self.sock = None
        self.wlock = threading.Lock()
        self.m = Machine(self._emit)
        if self.isa and self.isa[2] == "b215":
            self.m.ports[0x3F4] = 0x80
        if self.hdc:
            for port, v in self.hdc[3].items():
                self.m.ports[port] = v
        self.m.running = True
        self.stopping = False
        self.quit_after_reply = False
        self.json_seen = False

    # -- lifecycle ----------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        self.stopping = True
        self.m.dead = True
        for s in (self.sock, self.listener):
            try:
                if s:
                    s.shutdown(socket.SHUT_RDWR)    # close() alone leaves a thread blocked in accept/recv
            except OSError:
                pass
            try:
                s and s.close()
            except OSError:
                pass

    def _accept(self):
        while not self.stopping:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self._session(conn)

    def _emit(self, method, params):
        if not self.json_seen:                      # legacy sessions never see notifications
            return
        self._send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def _send(self, text):
        with self.wlock:
            try:
                if self.sock:
                    self.sock.sendall((text + "\n").encode())
            except OSError:
                pass

    def _session(self, conn):
        self.sock = conn
        self.json_seen = False
        m = self.m
        was = m.running
        m.pause(silent=True)                        # pause-on-connect
        m.auto = was and not m.running
        self._send(f"OK 86Box RMM mock {m.state()}")
        fp = conn.makefile("rb")
        try:
            for raw in fp:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("{"):
                    self._json(line)
                else:
                    self._legacy(line)
                if m.dead:
                    break
        except OSError:
            pass
        finally:
            with m.lock:                            # detach: drop debug state, resume
                m.bps.clear()
                m.watches.clear()
                m.step_left = 0
            self.keys_held.clear()                  # and release what the client held
            if self.mouse_buttons:
                self._mouse(0, 0, 0)
            sticky = bool(m.props.get("rmm.sticky_pause")) and not m.auto and not m.running
            if not m.dead and not sticky:
                m.resume(silent=True)
            self.sock = None
            try:
                conn.close()
            except OSError:
                pass

    # -- legacy dialect ------------------------------------------------------------------

    def _legacy(self, line):
        m = self.m
        cmd, _, args = line.partition(" ")
        cmd = cmd.lower()
        try:
            if cmd in ("status", "info"):
                self._send(f"OK {m.state()} tsc={m.tsc} cs:eip={m.regs['cs']:04X}:{m.regs['eip']:08X}")
            elif cmd in ("pause", "stop"):
                m.pause()
                self._send(f"OK paused tsc={m.tsc}")
            elif cmd in ("continue", "cont", "c"):
                m.resume()
                self._send(f"OK running tsc={m.tsc}")
            elif cmd == "regs":
                self._need_paused()
                self._send("OK " + " ".join(f"{k}={v:08X}" for k, v in m.regs.items()))
            elif cmd in ("mem", "x"):
                a, n = args.split()
                d = self.phys_read(int(a, 16), int(n, 0))
                self._send(f"OK {int(a, 16):08X} {d.hex().upper()}")
            elif cmd == "memw":
                parts = args.split()
                self.phys_write(int(parts[0], 16), bytes.fromhex(parts[1]), raw=len(parts) > 2)
                self._send(f"OK {int(parts[0], 16):08X} {len(parts[1]) // 2}")
            elif cmd == "sendkey":
                if not all(k in KEYS for k in args.split("+")):
                    raise E(0, "unknown key")
                self._send("OK")
            elif cmd == "type":
                with m.lock:
                    m.echo(args)
                self._send("OK")
            elif cmd in ("savevm", "snapshot", "loadvm", "restore", "delvm"):
                tag = args.strip()
                if not tag or len(tag) > 63 or " " in tag:
                    raise E(0, "tag must be 1..63 characters, no spaces")
                if cmd == "delvm":
                    self.m_snapshot_delete(dict(tag=tag))
                    self._send("OK")
                else:
                    # as QEMU's HMP: the run state is left as it was
                    was_running = m.running
                    m.pause()
                    try:
                        if cmd in ("savevm", "snapshot"):
                            self.m_snapshot_save(dict(tag=tag))
                        else:
                            self.m_snapshot_restore(dict(tag=tag))
                    finally:
                        if was_running:
                            m.resume()
                    self._send(f"OK {m.state()}")
            elif cmd == "snapshots":
                self._send(" ".join(["OK"] + list(m.snaps)))
            elif cmd in ("exportvm", "loadfile"):
                parts = args.strip().split(None, 1) if cmd == "exportvm" else [args.strip()]
                if not all(parts) or len(parts) != (2 if cmd == "exportvm" else 1):
                    self._send("ERR usage: exportvm TAG PATH" if cmd == "exportvm" else "ERR usage: loadfile PATH")
                else:
                    was_running = m.running
                    m.pause()
                    try:
                        if cmd == "exportvm":
                            self.m_snapshot_export(dict(tag=parts[0], path=parts[1]))
                            got = ""
                        else:
                            got = self.m_snapshot_load(dict(path=parts[0]))["tag"] + " "
                    finally:
                        if was_running:
                            m.resume()
                    self._send(f"OK {got}{m.state()}")
            else:
                self._send(f"ERR unknown command '{cmd}'")
        except E as e:
            self._send(f"ERR {e.code} {e.msg}" if e.code in (1, 5, 7, 8) else f"ERR {e.msg}")
        except (ValueError, IndexError):
            self._send("ERR usage")

    # -- JSON-RPC ------------------------------------------------------------------------

    def _json(self, line):
        self.json_seen = True
        try:
            req = json.loads(line)
        except ValueError:
            return self._send(json.dumps({"jsonrpc": "2.0", "id": None,
                                          "error": {"code": -32700, "message": "parse error"}}))
        rid = req.get("id") if isinstance(req, dict) else None
        notif = isinstance(req, dict) and "id" not in req
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return self._send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                          "error": {"code": -32600, "message": "invalid request"}}))
        params = req.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise bad("params must be an object")
            result = self.dispatch(req["method"], params)
            resp = {"jsonrpc": "2.0", "id": rid, "result": result}
        except E as e:
            err = {"code": e.code, "message": e.msg}
            if e.data is not None:
                err["data"] = e.data
            resp = {"jsonrpc": "2.0", "id": rid, "error": err}
        if not notif:
            self._send(json.dumps(resp))
        if self.quit_after_reply:
            self._quit()

    def _need_paused(self):
        if self.m.running:
            raise E(1, "not paused")

    def phys_read(self, addr, n):
        self._need_paused()
        if not 0 < n <= 4096:
            raise bad("len must be 1..4096")
        return bytes(self.m.ram[a] if a < len(self.m.ram) else 0xFF for a in range(addr, addr + n))

    def phys_write(self, addr, data, raw=False):
        self._need_paused()
        if not 0 < len(data) <= 4096:
            raise bad("data must be 1..4096 bytes")
        if addr + len(data) > 1 << 32:
            raise bad("range runs past the end of the 32-bit address space")
        took, first_bad = 0, None
        for i, b in enumerate(data):
            a = addr + i
            if ROM[0] <= a < ROM[1] or a >= len(self.m.ram):
                first_bad = a if first_bad is None else first_bad
            else:
                self.m.ram[a] = b
                took += 1
        if first_bad is not None:
            raise E(5, f"write dropped: {took} of {len(data)} bytes took", dict(took=took, len=len(data), first_bad=first_bad))

    def dispatch(self, method, p):
        m = self.m
        if method == "rpc.discover":
            return self.discover()
        mt = re.match(r"^([a-z]+)\.(\d+)\.(\w+)$", method)
        if mt and mt.group(1) in self.slots or (mt and f"{mt.group(1)}.N.{mt.group(3)}" in self.registry_names()):
            return self.media_call(mt.group(1), int(mt.group(2)), mt.group(3), p)
        base, _, verb = method.rpartition(".")
        if verb in ("get", "set") and base in self.prop_names():
            if verb == "get":
                return {"value": m.props.get(base, False)}
            m.props[base] = p.get("value")
            return {"value": m.props[base]}
        h = getattr(self, "m_" + method.replace(".", "_"), None)
        if h:
            return h(p)
        if method in self.registry_names():
            return {"ok": True}            # declared, behaviour not modelled by the mock
        raise E(-32601, f"method not found: {method}")

    # registry views ---------------------------------------------------------------------

    def registry_names(self):
        return {r["method"] for r in rmm_registry.R if r["status"] != "gui"}

    def prop_names(self):
        names = {r["method"] for r in rmm_registry.R if "get/set" in r["note"] and r["status"] != "gui"}
        reg = self.registry_names()
        # a property registered as separate (and with no handler of its own, unlike cpu.regs) "<base>.get" / "<base>.set" entries (rmm.sticky_pause)
        names |= {n[:-4] for n in reg if n.endswith(".get") and n[:-4] + ".set" in reg
                  and not hasattr(self, "m_" + n.replace(".", "_"))}
        return names

    def m_rpc_hello(self, p):
        cproto = p.get("protocol", PROTOCOL)
        if not isinstance(cproto, int) or isinstance(cproto, bool) or cproto < 0:
            raise bad("'protocol' must be an integer >= 0")
        if cproto < PROTOCOL_MIN:
            raise E(11, f"client protocol {cproto} is older than the minimum {PROTOCOL_MIN}",
                    dict(protocol=PROTOCOL, min_protocol=PROTOCOL_MIN))
        caps = sorted({r["method"].split(".")[0] for r in rmm_registry.R
                       if r["status"] != "gui" and "." in r["method"]})
        return dict(server="86Box", rmm_version="mock", protocol=PROTOCOL, min_protocol=PROTOCOL_MIN,
                    negotiated=min(cproto, PROTOCOL), capabilities=caps,
                    limits=dict(mem_max=4096, line_max=16384))

    def discover(self):
        """rmm.c's answer: rmm_schema's entry for every method it has, a bare one for what only
        the registry knows (planned methods the mock models ahead of the C)."""
        out = []
        for r in rmm_registry.R:
            name = r["method"]
            if r["status"] == "gui":
                continue
            props = name in self.prop_names()
            row = rmm_schema.METHODS.get(name + ".set" if props else name)
            e = rmm_schema.discover_entry(name, row) if row else {"name": name, "params": {}}
            if ".N." in name:
                e["indices"] = self.slots.get(name.split(".")[0], [])
            if props:
                e["verbs"] = ["get", "set"]
            out.append(e)
        return {"methods": out, "events": rmm_schema.discover_events(), "errors": rmm_schema.discover_errors()}

    # machine ---------------------------------------------------------------------------

    def m_machine_status(self, p):
        m = self.m
        return dict(state=m.state(), sticky_pause=bool(m.props.get("rmm.sticky_pause")), tsc=m.tsc, cs=m.regs["cs"], eip=m.regs["eip"])

    def m_machine_pause(self, p):
        self.m.auto = False
        self.m.pause()
        return dict(state="paused")

    def m_machine_resume(self, p):
        self.m.resume()
        return dict(state="running")

    def m_machine_reset(self, p):
        m = self.m
        with m.lock:
            applied = self.pending_reset_apply_run()
            m.reset_regs()
            m.regs.update(cs=0xF000, eip=0xFFF0)
            m.tsc += 1
            m.icount = 0
            m.resets += 1
        self._emit("machine.reset", {"hard": bool(p.get("hard", True))})
        loaded, self.load_done = self.load_done, None
        return dict(state=m.state(), config_applied=applied, config_loaded=loaded)

    def m_machine_cad(self, p):
        return {"ok": True}

    def m_machine_cae(self, p):
        return {"ok": True}

    def m_machine_nmi(self, p):
        return {"ok": True}

    def m_machine_acpi_shutdown(self, p):
        raise E(4, "this machine has no ACPI power button")

    def m_machine_quit(self, p):
        self.quit_after_reply = True
        return {"ok": True}

    def _quit(self):
        self._emit("machine.exit", {})
        self.m.dead = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # cpu / mem ----------------------------------------------------------------------------

    def m_cpu_regs_get(self, p):
        self._need_paused()
        return dict(self.m.regs)

    SETTABLE = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip", "eflags")

    def m_cpu_regs_set(self, p):
        if not p:
            raise bad("give at least one register (eax..edi, eip, eflags)")
        for k, v in p.items():
            if k not in self.SETTABLE:
                raise bad(f"'{k}' is not a settable register (eax..edi, eip, eflags)")
            if hexint(v, k) > 0xFFFFFFFF:
                raise bad(f"'{k}' must be an integer 0..4294967295")
        self._need_paused()
        for k, v in p.items():
            self.m.regs[k] = v | 2 if k == "eflags" else v
        return dict(self.m.regs)

    def _span(self, p):
        a = hexint(p.get("addr"), "addr")
        n = p.get("len")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 4096:
            raise bad("len must be 1..4096")
        if a + n > 1 << 32:
            raise bad("range runs past the end of the 32-bit address space")
        return a, n

    def m_mem_phys_read(self, p):
        a, n = self._span(p)
        return dict(addr=a, len=n, data=self.phys_read(a, n).hex())

    # the simulated guest is in real mode with paging off: linear == physical. A linear
    # address with no present page would be error 9 {first_bad}.
    m_mem_lin_read = m_mem_phys_read

    def m_mem_phys_write(self, p):
        a = hexint(p.get("addr"), "addr")
        try:
            data = bytes.fromhex(p.get("data", ""))
        except (ValueError, TypeError):
            raise bad("data must be an even number of hex digits")
        self.phys_write(a, data, raw=bool(p.get("raw")))
        return dict(addr=a, len=len(data))

    # io: every port is a latch (reads give back the last write, 0xFF if none), except the
    # index/data pairs (CMOS, VGA CRTC/sequencer/graphics), which keep one register per index.
    # A word or dword is that many byte accesses at port, port+1, ... as on an 8-bit device.
    INDEX_DATA = {0x71: 0x70, 0x3D5: 0x3D4, 0x3C5: 0x3C4, 0x3CF: 0x3CE}

    def _io_args(self, p, write):
        self._need_paused()
        port, size = p.get("port"), p.get("size", 1)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 0xFFFF:
            raise bad("port must be 0..0xFFFF")
        if size not in (1, 2, 4) or isinstance(size, bool):
            raise bad("size must be 1, 2 or 4")
        if port + size - 1 > 0xFFFF:
            raise bad("port + size runs past 0xFFFF")
        value = None
        if write:
            value = p.get("value")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << (8 * size):
                raise bad(f"value must fit in {size} byte(s)")
        return port, size, value

    def _uart_base(self):
        return COM_BASE[self.in_dev["port"]] if self.in_dev and self.in_dev["mouse"] == "msserial" else None

    @staticmethod
    def _rtc_reg(reg):
        """MM58167 counters from the host clock, in BCD (registers 2-7); the rest read 0."""
        now = time.localtime()
        bcd = lambda n: (n // 10) << 4 | n % 10
        vals = {2: now.tm_sec, 3: now.tm_min, 4: now.tm_hour, 5: now.tm_wday + 1, 6: now.tm_mday, 7: now.tm_mon}
        return bcd(vals[reg]) if reg in vals else 0

    def _inb(self, port):
        m = self.m
        if port == 0x201 and self.joy:                        # game port: buttons low = pressed, axis timers
            btn = 0xF0
            for i, down in enumerate(self.joy["buttons"]):
                if down:
                    btn &= ~(0x10 << i)
            return self.joy["timers"] | (0xF0 if self.joy["timers"] else btn)
        if self.isa and self.isa[3] is not None and self.isa[3] <= port < self.isa[3] + 32:
            if 8 <= port - self.isa[3] <= 15:                 # the alarm registers latch what was written
                return m.ports.get(port, 0)
            return self._rtc_reg(port - self.isa[3])
        base = self._uart_base()
        if base is not None and base <= port <= base + 7:
            if port == base + 5:
                return 0x60 | (1 if self.uart else 0)
            if port == base:
                return self.uart.pop(0) if self.uart else 0
        if port in self.INDEX_DATA:
            idx = self.INDEX_DATA[port]
            return m.indexed.get((idx, m.ports.get(idx, 0)), 0xFF)
        return m.ports.get(port, 0xFF)

    def _outb(self, port, v):
        m = self.m
        if port == 0x201 and self.joy:                        # the trigger: a timer per present axis
            self.joy["timers"] = (1 << len(self.joy["axes"])) - 1
            return
        base = self._uart_base()
        if base is not None and port == base + 4:
            if v & 2 and not m.ports.get(port, 0) & 2:        # RTS rising: the mouse identifies itself
                self.uart.append(0x4D)
        if port in self.INDEX_DATA:
            idx = self.INDEX_DATA[port]
            m.indexed[(idx, m.ports.get(idx, 0))] = v
        else:
            m.ports[port] = v

    def m_io_read(self, p):
        port, size, _ = self._io_args(p, False)
        v = sum(self._inb(port + i) << (8 * i) for i in range(size))
        return dict(port=port, size=size, value=v)

    def m_io_write(self, p):
        port, size, v = self._io_args(p, True)
        for i in range(size):
            self._outb(port + i, (v >> (8 * i)) & 0xFF)
        return dict(port=port, size=size, value=v)

    def m_mem_info(self, p):
        return dict(ram_kb=self.cfg["Machine"]["mem_size"])

    def m_input_key_send(self, p):
        combo = str(p.get("combo", ""))
        if not all(k in KEYS for k in combo.split("+")):
            raise bad("unknown key")
        with self.m.lock:
            if combo == "enter":
                self.m.echo("\nA:\\>")
            elif len(combo) == 1:
                self.m.echo(combo)
            elif combo == "space":
                self.m.echo(" ")
        return {"ok": True}

    def m_input_text_type(self, p):
        t = p.get("text")
        if not isinstance(t, str) or not t.isascii():
            raise bad("untypeable character")
        with self.m.lock:
            self.m.echo(t)
        return {"ok": True}

    def m_input_devices(self, p):
        d = self.in_dev
        mouse = None
        if d:
            mouse = dict(type=d["mouse"], buttons=2)
            if d["mouse"] in ("msserial", "ltserial"):
                mouse["port"] = d["port"]
        joy = None
        if self.joy:
            joy = dict(type=d["joystick"], axes=len(self.joy["axes"]), buttons=len(self.joy["buttons"]), gameport=0x201)
        return dict(keyboard=dict(type="keyboard_at"), mouse=mouse, joystick=joy, tablet=None, pointer="relative",
                    pending=0)

    def m_input_joystick_set(self, p):
        if not self.joy:
            raise E(3, "no joystick")
        new = {}
        for key, kind in (("axes", int), ("buttons", bool)):
            if key not in p:
                continue
            v = p[key]
            if not isinstance(v, list) or len(v) > len(self.joy[key]):
                raise bad(f"{key}: a list of at most {len(self.joy[key])}")
            for x in v:
                if kind is int and (isinstance(x, bool) or not isinstance(x, int) or not -32768 <= x <= 32767):
                    raise bad("axis must be an integer -32768..32767")
                if kind is bool and not isinstance(x, bool):
                    raise bad("button must be true or false")
            new[key] = v
        for key, v in new.items():
            self.joy[key][:len(v)] = v
        self.joy["timers"] = 0
        return {"ok": True}

    def _mouse(self, dx, dy, b):
        """What the attached mouse reports for motion dx, dy with buttons b held."""
        if self.in_dev and self.in_dev["mouse"] == "msserial" and (dx or dy or b != self.mouse_buttons):
            x, y = dx & 0xFF, dy & 0xFF
            self.uart += [0x40 | ((y >> 6) & 3) << 2 | ((x >> 6) & 3) | (0x20 if b & 1 else 0) | (0x10 if b & 2 else 0),
                          x & 0x3F, y & 0x3F]
        self.mouse_buttons = b

    @staticmethod
    def _int(v, lo, hi, what):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise bad(f"{what} must be an integer {lo}..{hi}")
        return v

    def m_input_mouse_move(self, p):
        if "dx" not in p or "dy" not in p:
            raise bad("missing parameter 'dx' or 'dy'")
        dx = self._int(p["dx"], -32768, 32767, "dx")
        dy = self._int(p["dy"], -32768, 32767, "dy")
        b = self._int(p["buttons"], 0, 31, "buttons") if "buttons" in p else self.mouse_buttons
        if not self.in_dev:
            raise E(3, "no mouse")
        self._mouse(dx, dy, b)
        return dict(queued=1 + ("buttons" in p), pending=0)

    def _input_event(self, i, ev):
        """One input.events element, checked: -> (kind, args)."""
        def fail(code, msg):
            return E(code, f"events[{i}]: {msg}")
        if not isinstance(ev, dict):
            raise fail(-32602, "an event is an object")
        t = ev.get("type")
        try:
            if t == "key":
                if not isinstance(ev.get("down"), bool):
                    raise bad("'down' must be true or false")
                if isinstance(ev.get("key"), str):
                    scan = rmm_schema.KEYS.get(ev["key"].lower())
                    if scan is None:
                        raise bad(f"unknown key '{ev['key']}'")
                elif "scan" in ev:
                    scan = self._int(ev["scan"], 1, 0x1FF, "'scan'")
                else:
                    raise bad("a key needs 'scan' or 'key'")
                return "key", (scan, ev["down"])
            if t == "rel":
                if "dx" not in ev or "dy" not in ev:
                    raise bad("rel needs dx and dy")
                args = (self._int(ev["dx"], -32768, 32767, "'dx'"), self._int(ev["dy"], -32768, 32767, "'dy'"),
                        self._int(ev.get("dz", 0), -127, 127, "'dz'"))
                if not self.in_dev:
                    raise E(3, "no mouse (mouse_type and tablet_type are none)")
                return "rel", args
            if t == "btn":
                if "buttons" not in ev:
                    raise bad("missing parameter 'buttons'")
                b = self._int(ev["buttons"], 0, 31, "'buttons'")
                if not self.in_dev:
                    raise E(3, "no mouse (mouse_type and tablet_type are none)")
                return "btn", (b,)
            if t == "abs":
                x, y = ev.get("x"), ev.get("y")
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in (x, y)):
                    raise bad("'x' and 'y' must be numbers 0..1")
                raise E(3, "no tablet (tablet_type is none)")         # the mock machine has none
            raise bad("'type' must be key, rel, abs or btn")
        except E as e:
            raise fail(e.code, e.msg) from None

    def m_input_events(self, p):
        evs = p.get("events")
        if not isinstance(evs, list):
            raise bad("'events' must be a list")
        if len(evs) > 256:
            raise bad("at most 256 events per call")
        batch = [self._input_event(i, ev) for i, ev in enumerate(evs)]     # all or nothing
        for kind, args in batch:                     # no guest clock to pace by: all delivered now
            if kind == "key":
                (self.keys_held.add if args[1] else self.keys_held.discard)(args[0])
            elif kind == "rel":
                self._mouse(args[0], args[1], self.mouse_buttons)
            else:
                self._mouse(0, 0, args[0])
        return dict(queued=len(batch), pending=0)

    def m_input_release_all(self, p):
        keys = sorted(self.keys_held)
        buttons = self.mouse_buttons
        self.keys_held.clear()
        if buttons:
            self._mouse(0, 0, 0)
        return dict(keys=keys, buttons=buttons, pending=0)

    def m_storage_list(self, p):
        if not self.hdc:
            return dict(controllers=[], disks=[])
        name, bus, ports, _ = self.hdc
        return dict(controllers=[dict(kind="hdc", name=name, bus=bus, ports=list(ports))],
                    disks=[dict(n=0, bus=bus, channel=0, cylinders=615, heads=4, spt=17, image="hdd_1.img")])

    # media ------------------------------------------------------------------------------

    def m_media_list(self, p):
        slots = [dict(dev=d, slot=i, empty=not self.media.get((d, i)), path=self.media.get((d, i), ""),
                      wp=self.wp.get((d, i), False))
                 for d, ids in self.slots.items() for i in ids]
        for e in slots:
            if e["dev"] == "cassette":
                e["mode"] = self.cass.get(e["slot"], {}).get("mode", "idle")
        return dict(slots=slots)

    def media_call(self, dev, slot, verb, p):
        if slot not in self.slots.get(dev, []):
            raise E(3, f"{dev} slot {slot} out of range")
        key = (dev, slot)
        hist = self.history.setdefault(key, [])
        if verb == "eject":
            if not self.media.get(key):
                raise E(2, "slot empty")
            hist.append(self.media[key])
            self.media[key] = ""
            self._emit("media.changed", dict(dev=dev, slot=slot, path=""))
        elif verb == "insert":
            path = p.get("path")
            if not isinstance(path, str) or not os.path.exists(path):
                raise bad("path not found")
            if self.media.get(key):
                hist.append(self.media[key])
            self.media[key], self.wp[key] = path, bool(p.get("wp", False))
            self._emit("media.changed", dict(dev=dev, slot=slot, path=path))
        elif verb == "new":
            path = p.get("path")
            if not isinstance(path, str) or not path:
                raise bad("path required")
            if os.path.exists(path):
                raise bad("path exists")
            with open(path, "wb") as f:
                f.truncate(int(p.get("size", 1 << 20)))
            if self.media.get(key):
                hist.append(self.media[key])
            self.media[key], self.wp[key] = path, False
            self._emit("media.changed", dict(dev=dev, slot=slot, path=path))
        elif verb == "reload":
            if self.media.get(key) or not hist:
                raise E(2, "nothing to reload" if not self.media.get(key) else "slot not empty")
            path = hist.pop()
            self.media[key] = path
            self._emit("media.changed", dict(dev=dev, slot=slot, path=path))
        elif verb in ("record", "play", "rewind", "ffwd"):
            if dev != "cassette":
                raise E(3, f"{dev} has no {verb}")
            if not self.media.get(key):
                raise E(2, "slot empty")
            self.cass[slot] = dict(mode=verb)
        return {"ok": True}

    # config -------------------------------------------------------------------------------

    def _cfg_key(self, p, need_key=True):
        sec, key = p.get("section"), p.get("key")
        if sec not in self.cfg:
            raise bad(f"no such section {sec!r}")
        if key is not None and key not in self.cfg[sec]:
            raise bad(f"no such key {sec}/{key}")
        if need_key and key is None:
            raise bad("key required")
        return sec, key

    def m_config_get(self, p):
        sec, key = self._cfg_key(p, need_key=False)
        if key is None:
            return dict(values=dict(self.cfg[sec]))
        r = dict(value=self.cfg[sec][key])
        for ps, pk, pv in self.pending_reset_apply:
            if (ps, pk) == (sec, key):
                r["pending"] = pv
        return r

    def _enum_entry(self, sec, key):
        ek = enum_key(sec, key)
        mc = self.cfg["Machine"]
        names, labels, extra = enum_values(ek, mc["machine"], mc["cpu_family"])
        default = ENUMS[ek][1] if ENUMS[ek][1] is not None else names[0]
        e = dict(section=sec, key=key, type="enum", applies="reset",
                 value=str(self.cfg.get(sec, {}).get(key, default)), values=names, labels=labels)
        e.update(extra)
        return e

    def m_config_enums(self, p):
        sec = p.get("section")
        if sec is not None and not (isinstance(sec, str) and (sec in self.cfg or any(s == sec for s, _ in ENUMS))):
            raise bad(f"no section [{sec}]")
        return dict(keys=[self._enum_entry(s, k) for s, k in enum_keys() if sec is None or s == sec])

    def m_config_describe(self, p):
        if enum_key(p.get("section"), p.get("key")):
            e = self._enum_entry(p["section"], p["key"])
            del e["section"], e["key"]
            return e
        sec, key = self._cfg_key(p)
        return dict(CONFIG_DESC[(sec, key)])

    def _save_cfg(self):
        with open(self.cfg_path, "w") as f:
            for s_, kv in self.cfg.items():
                f.write(f"[{s_}]\n" + "".join(f"{k} = {v}\n" for k, v in kv.items()) + "\n")

    def m_config_set(self, p):
        sec, key = self._cfg_key(p)
        if (sec, key) not in CONFIG_DESC:
            raise bad(f"{sec}/{key} has no schema yet")
        d = CONFIG_DESC[(sec, key)]
        v = p.get("value")
        if isinstance(v, bool) or not isinstance(v, int) or not d["min"] <= v <= d["max"] or v % d["step"]:
            raise bad(f"value must be an int in {d['min']}..{d['max']} step {d['step']}")
        if d["applies"] == "now":
            self.cfg[sec][key] = v
            self._save_cfg()
            return dict(applies="now", pending=False)
        self.pending_reset_apply = [e for e in self.pending_reset_apply if e[:2] != (sec, key)]
        if v != self.cfg[sec][key]:
            self.pending_reset_apply.append((sec, key, v))
        return dict(applies="reset", pending=v != self.cfg[sec][key])

    def m_config_pending(self, p):
        return dict(pending=[dict(section=a, key=b, value=c) for a, b, c in self.pending_reset_apply],
                    load=copy.deepcopy(self.pending_load[0]) if self.pending_load else None)

    def m_config_discard(self, p):
        n = len(self.pending_reset_apply) + (1 if self.pending_load else 0)
        self.pending_reset_apply = []
        self.pending_load = None
        return dict(discarded=n)

    def m_config_dump(self, p):
        sec = p.get("section")
        if sec is not None and not isinstance(sec, str):
            raise bad("'section' must be a string")
        if sec is not None and sec not in self.cfg:
            raise bad(f"no section [{sec}]")
        cfg = {s_: {k: str(v) for k, v in kv.items()} for s_, kv in self.cfg.items()
               if kv and (sec is None or s_ == sec)}
        return dict(cfg=cfg, path=self.cfg_path)

    def m_config_load(self, p):
        cfg, rep_, val = p.get("cfg"), p.get("replace", True), p.get("validate_only", False)
        if not isinstance(cfg, dict) or not isinstance(rep_, bool) or not isinstance(val, bool):
            raise bad("want {cfg: {section: {key: value}}, replace?: bool, validate_only?: bool}")
        errors, unchecked, keys = [], [], 0
        lm = cfg.get("Machine") if isinstance(cfg.get("Machine"), dict) else {}
        m = lm.get("machine", self.cfg["Machine"]["machine"])
        fam = lm.get("cpu_family", self.cfg["Machine"]["cpu_family"])

        def err(s_, k, v, why):
            errors.append(dict(section=s_, key=k, value=v, why=why))

        for s_, kv in cfg.items():
            if not isinstance(kv, dict):
                err(s_, "", kv, "a section must be an object of key: value")
                continue
            for k, v in kv.items():
                keys += 1
                ek = enum_key(s_, k)
                if not (v is None or isinstance(v, (str, int, float, bool))):
                    err(s_, k, v, "a value must be a string, number, bool or null")
                elif s_ == "General" and (k.startswith("rmm_") or k == "uuid"):
                    err(s_, k, v, "the monitor's own settings and the VM uuid cannot be loaded")
                elif v is None:
                    pass
                elif ek in (("Machine", "cpu_speed"), ("Machine", "cpu_waitstates")):
                    if str(v).split(".")[0] not in enum_values(ek, m, fam)[0]:
                        err(s_, k, v, "not offered for this CPU family (see config.enums)")
                elif ek == ("Machine", "fpu_type"):
                    if v not in enum_values(ek, m, fam)[0]:
                        err(s_, k, v, "this CPU has no such FPU (see config.enums)")
                elif ek:
                    if not isinstance(v, str):
                        err(s_, k, v, "must be a string (an internal name)")
                    elif v not in ENUMS[ek][0]:
                        err(s_, k, v, "no such name in 86Box's table (see config.describe)")
                    elif ek == ("Machine", "cpu_family") and m in MACHINE_CPUS and v not in MACHINE_CPUS[m]:
                        err(s_, k, v, "this CPU family does not fit the machine")
                elif (s_, k) == ("Machine", "mem_size"):
                    d = CONFIG_DESC[("Machine", "mem_size")]
                    try:
                        iv = int(v)
                    except (TypeError, ValueError):
                        iv = -1
                    if not d["min"] <= iv <= d["max"] or (iv - d["min"]) % d["step"]:
                        err(s_, k, v, "outside this machine's RAM range or step (config.describe Machine mem_size)")
                elif s_ in IMAGE_SECTIONS and (k.endswith("_fn") or k.endswith("_image_path")) and v:
                    if not os.path.exists(str(v)):
                        err(s_, k, v, "image file not found")
                else:
                    unchecked.append(dict(section=s_, key=k))
        if m in MACHINE_CPUS and m != self.cfg["Machine"]["machine"] and "cpu_family" not in cfg.get("Machine", {}) \
                and self.cfg["Machine"]["cpu_family"] not in MACHINE_CPUS[m]:
            err("Machine", "cpu_family", m, "the running CPU family does not fit the new machine; give cpu_family")
        if errors:
            raise E(10, f"config refused: {len(errors)} bad key(s)", dict(errors=errors))
        if not val:
            self.pending_load = (copy.deepcopy(cfg), rep_)
        return dict(staged=not val, applies="reset", keys=keys, unchecked=unchecked)

    def _apply_load(self):
        cfg, replace = self.pending_load
        self.pending_load = None
        n = 0
        for s_, kv in cfg.items():
            if replace and s_ != "General":
                self.cfg[s_] = {}
            sec = self.cfg.setdefault(s_, {})
            for k, v in kv.items():
                n += 1
                if v is None:
                    sec.pop(k, None)
                elif (s_, k) == ("Machine", "mem_size"):
                    sec[k] = int(v)
                else:
                    sec[k] = v if isinstance(v, str) else str(int(v) if isinstance(v, bool) else v)
        self.cfg["Machine"].setdefault("mem_size", RAM_KB)
        self.cfg["Machine"].setdefault("machine", "ms4144")
        self.cfg["Machine"].setdefault("cpu_family", "i486dx")
        self.load_done = n

    def pending_reset_apply_run(self):
        if self.pending_load:
            self._apply_load()
        applied = [dict(section=a, key=b, value=c) for a, b, c in self.pending_reset_apply]
        for a, b, c in self.pending_reset_apply:
            self.cfg[a][b] = c
        self.pending_reset_apply = []
        if applied or self.load_done is not None:
            self._save_cfg()
        return applied

    # capture --------------------------------------------------------------------------------

    def m_capture_screenshot(self, p):
        """The text page at B8000 drawn as 8x16 cells: a block in the attribute's colours for
        every non-blank character. Enough to watch typing and rollbacks with no VM."""
        step = p.get("step", 1)
        wait = p.get("wait_ms", 200)
        if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 8:
            raise bad("'step' must be 1..8")
        if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 2000:
            raise bad("'wait_ms' must be an integer 0..2000")
        W, H = 640, 400
        with self.m.lock:
            page = bytes(self.m.ram[0xB8000:0xB8000 + 4000])
            fresh = self.m.running
        img = bytearray(W * H * 3)
        for row in range(25):
            for col in range(80):
                ch, at = page[(row * 80 + col) * 2], page[(row * 80 + col) * 2 + 1]
                bg = bytes(CGA[(at >> 4) & 7]) * 8
                fg = bytes(CGA[at & 15]) * 6
                for y in range(16):
                    o = ((row * 16 + y) * W + col * 8) * 3
                    img[o:o + 24] = bg
                    if ch not in (0, 32, 255) and 2 <= y <= 13:
                        img[o + 3:o + 21] = fg
        if step > 1:
            img = bytearray(b"".join(bytes(img[(y * W + x) * 3:(y * W + x) * 3 + 3])
                                     for y in range(0, H, step) for x in range(0, W, step)))
        if fresh:
            self.frame_seq += 1
        return dict(w=(W + step - 1) // step, h=(H + step - 1) // step, src_w=W, src_h=H, step=step,
                    format="rgb24", seq=self.frame_seq, fresh=fresh, data=base64.b64encode(bytes(img)).decode())

    # debug ----------------------------------------------------------------------------------

    def m_debug_break_set(self, p):
        a = hexint(p.get("addr"), "addr")
        with self.m.lock:
            bid = self.m.next_id
            self.m.next_id += 1
            self.m.bps[bid] = a
        return dict(id=bid)

    def m_debug_break_clear(self, p):
        with self.m.lock:
            if p.get("id") not in self.m.bps:
                raise bad("no such breakpoint")
            del self.m.bps[p["id"]]
        return {"ok": True}

    def m_debug_break_list(self, p):
        return dict(breakpoints=[dict(id=i, addr=a) for i, a in self.m.bps.items()])

    def m_debug_step(self, p):
        self._need_paused()
        n = p.get("count", 1)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise bad("count must be >= 1")
        with self.m.lock:
            self.m.step_left = n
            self.m.skip_bp = True
            self.m.running = True
        return {"ok": True}

    def m_debug_watch_set(self, p):
        a, n = hexint(p.get("addr"), "addr"), hexint(p.get("len"), "len")
        if p.get("kind") not in ("r", "w", "rw") or not 1 <= n <= 4096 or a + n > 1 << 32:
            raise bad("kind must be r|w|rw, len 1..4096, inside 4 GiB")
        with self.m.lock:
            wid = self.m.next_id
            self.m.next_id += 1
            self.m.watches[wid] = dict(addr=a, len=n, kind=p["kind"])
        return dict(id=wid)

    def m_debug_watch_clear(self, p):
        with self.m.lock:
            if p.get("id") not in self.m.watches:
                raise bad("no such watchpoint")
            del self.m.watches[p["id"]]
        return {"ok": True}

    def m_debug_watch_list(self, p):
        return dict(watchpoints=[dict(id=i, **w) for i, w in self.m.watches.items()])

    # snapshot -------------------------------------------------------------------------------

    def m_snapshot_save(self, p):
        self._need_paused()
        tag = p.get("tag")
        if not isinstance(tag, str) or not tag:
            raise bad("tag required")
        m = self.m
        m.snaps[tag] = dict(ram=bytes(m.ram), regs=dict(m.regs), tsc=m.tsc, icount=m.icount, gen=m.resets,
                            ports=dict(m.ports), indexed=dict(m.indexed))
        return {"ok": True}

    def m_snapshot_restore(self, p):
        m = self.m
        s = m.snaps.get(p.get("tag"))
        if s is None:
            raise E(7, f"no snapshot {p.get('tag')!r}")
        if s["gen"] != m.resets:
            raise E(8, "the machine was reset after this snapshot was taken")
        with m.lock:
            m.running = False
            m.ram[:] = s["ram"]
            m.regs, m.tsc, m.icount = dict(s["regs"]), s["tsc"], s["icount"]
            m.ports, m.indexed = dict(s["ports"]), dict(s["indexed"])
        return {"ok": True}

    def m_snapshot_list(self, p):
        return dict(snapshots=[dict(tag=t, tsc=s["tsc"], state="disk" if s.get("disk") else "memory", bytes=0)
                               for t, s in self.m.snaps.items()])

    MOCK_SNAP_MAGIC = "RMMMOCK1"

    def m_snapshot_export(self, p):
        self._need_paused()
        s = self.m.snaps.get(p.get("tag"))
        if s is None:
            raise E(7, f"no snapshot {p.get('tag')!r}")
        path = p.get("path")
        if not isinstance(path, str) or not path:
            raise bad("path required")
        if not os.path.isabs(path):
            raise E(8, f"'{path}' is not an absolute path")
        doc = dict(magic=self.MOCK_SNAP_MAGIC, tag=p["tag"], ram=s["ram"].hex(), regs=s["regs"], tsc=s["tsc"],
                   icount=s["icount"], ports=[[k, v] for k, v in s["ports"].items()],
                   indexed=[[list(k) if isinstance(k, tuple) else k, v] for k, v in s["indexed"].items()])
        try:
            with open(path + ".part", "w") as f:
                json.dump(doc, f)
            os.replace(path + ".part", path)
        except OSError as e:
            raise E(8, f"{path}: {e}")
        return {}

    def m_snapshot_load(self, p):
        path = p.get("path")
        if not isinstance(path, str) or not path:
            raise bad("path required")
        if not os.path.isabs(path):
            raise E(8, f"'{path}' is not an absolute path")
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise E(8, f"{path} is not a snapshot file: {e}")
        if not isinstance(doc, dict) or doc.get("magic") != self.MOCK_SNAP_MAGIC:
            raise E(8, f"{path} is not a snapshot file")
        with self.m.lock:
            self.m.running = False
        tag = p.get("tag") or doc["tag"]
        self.m.snaps[tag] = dict(ram=bytes.fromhex(doc["ram"]), regs=doc["regs"], tsc=doc["tsc"], icount=doc["icount"],
                                 gen=self.m.resets, ports={k: v for k, v in doc["ports"]},
                                 indexed={(tuple(k) if isinstance(k, list) else k): v for k, v in doc["indexed"]},
                                 disk=True)
        if p.get("restore", True):
            self.m_snapshot_restore(dict(tag=tag))
        return dict(tag=tag)

    def m_snapshot_writer(self, p):
        return {}       # snapshots stay in memory; the writer tests are real-target only

    def m_snapshot_reloc(self, p):
        return {}

    def m_snapshot_ptrscan(self, p):
        return dict(hits=[], total=0)

    def m_snapshot_flush(self, p):
        return dict(pending=0, errors=0)

    def m_snapshot_delete(self, p):
        if p.get("tag") not in self.m.snaps:
            raise E(7, f"no snapshot {p.get('tag')!r}")
        del self.m.snaps[p["tag"]]
        return {"ok": True}


if __name__ == "__main__":
    srv = MockServer(int(sys.argv[1]) if len(sys.argv) > 1 else 55899)
    srv.start()
    print(f"rmm mock listening on 127.0.0.1:{srv.port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
