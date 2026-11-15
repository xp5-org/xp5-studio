# SPDX-License-Identifier: MIT
"""The QEMU backend: argv, keys, the method table and event translation without QEMU; then a
short live run per machine when its qemu-system-* is installed (q800 also needs a Quadra ROM
in $RMM_Q800_ROM)."""

import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rmm_86box import RpcError  # noqa: E402
from rmm_86box.backends import qemu  # noqa: E402
from rmm_86box.backends.qemu import keys, profiles  # noqa: E402
from rmm_86box.backends.qemu.machine import QemuMachine, _read_ppm  # noqa: E402


class Argv(unittest.TestCase):
    def test_headless_local(self):
        for name in profiles.PROFILES:
            a = profiles.argv(name, "/run/vm1")
            self.assertEqual(a[0], profiles.PROFILES[name].binary)
            self.assertIn("-S", a)
            for flag, val in (("-display", "none"), ("-monitor", "none"), ("-audio", "none"),
                              ("-qmp", "unix:/run/vm1/qmp.sock,server=on,wait=off")):
                self.assertEqual(a[a.index(flag) + 1], val)
            self.assertNotIn("-vnc", a)
            self.assertFalse(any("tcp:" in x for x in a))

    def test_options(self):
        a = profiles.argv("isapc", "/r", fda="d.img", hda="c.qcow2", vnc=True, gdb_port=1234, paused=False)
        self.assertEqual(a[a.index("-fda") + 1], "d.img")
        self.assertEqual(a[a.index("-vnc") + 1], "unix:/r/vnc.sock")
        self.assertEqual(a[a.index("-gdb") + 1], "tcp:127.0.0.1:1234")
        self.assertNotIn("-S", a)
        q = profiles.argv("m68k", "/r", bios="Q.ROM", scsi=["pram.img", "hd.qcow2"], cdrom="s.iso")
        self.assertIn("id=hd1,file=hd.qcow2,format=qcow2,if=none", q)
        self.assertIn("scsi-cd,scsi-id=3,drive=cd0", q)
        with self.assertRaises(ValueError):
            profiles.argv("ss5", "/r", fda="x.img")
        with self.assertRaises(KeyError):
            profiles.get("amiga")


class Keys(unittest.TestCase):
    def test_qnum(self):
        self.assertEqual(keys.qnum("a"), 0x1E)
        self.assertEqual(keys.qnum("rctrl"), 0x9D)
        self.assertEqual(keys.qnum(0x148), 0xC8)
        for bad in ("nosuchkey", 0x200, 0x90):
            with self.assertRaises(KeyError):
                keys.qnum(bad)

    def test_text(self):
        self.assertEqual(keys.text_keys('aB!\n'), [("a", False), ("b", True), ("1", True), ("enter", False)])
        self.assertEqual(keys.combo("Ctrl+Alt+Delete"), ["ctrl", "alt", "del"])
        with self.assertRaises(KeyError):
            keys.text_keys("é")


class Table(unittest.TestCase):
    def setUp(self):
        self.m = QemuMachine("/nonexistent/qmp.sock", "isapc")

    def test_only_what_it_handles(self):
        names = set(self.m.methods.by_name)
        self.assertEqual(names, set(QemuMachine.HANDLERS))
        self.assertEqual(self.m.methods.get("input.events").params, {"events": "array"})
        self.assertIn("machine", dir(self.m))
        self.assertNotIn("cpu", dir(self.m))
        with self.assertRaises(RpcError) as e:
            self.m.cpu.regs.get()
        self.assertEqual(e.exception.code, -32601)

    def test_events_translate_and_chain_buttons(self):
        r = self.m.input.send({"type": "key", "key": "lctrl", "down": True},
                              {"type": "btn", "buttons": 1}, {"type": "rel", "dx": 3, "dy": -2, "dz": -1},
                              {"type": "btn", "buttons": 4})
        self.assertEqual(r, {"queued": 4, "pending": 4})
        q = list(self.m._q)
        self.assertEqual(q[0][1][0]["data"]["key"], {"type": "number", "data": 0x1D})
        self.assertEqual(q[1][1], [{"type": "btn", "data": {"down": True, "button": "left"}}])
        self.assertEqual([e["data"].get("button") for e in q[2][1][2:]], ["wheel-down", "wheel-down"])
        self.assertEqual([(e["data"]["button"], e["data"]["down"]) for e in q[3][1]],
                         [("left", False), ("middle", True)])
        self.m.input.buttons(0)                  # chains from the queued mask, not the sent one
        self.assertEqual([e["data"]["button"] for e in self.m._q[-1][1]], ["middle"])

    def test_events_all_or_nothing(self):
        with self.assertRaises(RpcError) as e:
            self.m.input.send({"type": "key", "key": "a", "down": True}, {"type": "abs", "x": 2, "y": 0})
        self.assertEqual(e.exception.code, -32602)
        self.assertIn("events[1]", str(e.exception))
        self.assertEqual(len(self.m._q), 0)

    def test_ppm(self):
        self.assertEqual(_read_ppm(b"P6\n# c\n2 1\n255\n" + bytes(range(6))), (2, 1, bytes(range(6))))


LIVE = {"isapc": {}, "ss5": {}, "q800": {"bios": os.environ.get("RMM_Q800_ROM")}}


class Live(unittest.TestCase):
    def run_machine(self, name):
        p = profiles.get(name)
        opts = {k: v for k, v in LIVE[name].items() if v}
        if not shutil.which(p.binary):
            self.skipTest(f"{p.binary} not installed")
        if len(opts) != len(LIVE[name]):
            self.skipTest(f"{name}: set {', '.join(LIVE[name])} (env RMM_Q800_ROM)")
        events = []
        m = qemu.start(name, **opts)
        m.on_event(lambda n, d: events.append(n))
        try:
            self.assertEqual(m.state(), "running")
            f = m.screenshot(step=2)
            self.assertGreater(f.w, 0)
            self.assertEqual(len(f.rgb), f.w * f.h * 3)
            self.assertEqual(m.input.devices()["pointer"], "relative")
            m.input.tap("lshift", "a")
            m.input.click()
            with m.paused():
                self.assertEqual(m.state(), "paused")
            deadline = time.time() + 2
            while m.call("input.devices")["pending"] and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(m.input.release_all()["keys"], [])
            self.assertIn("machine.stopped", events)
        finally:
            m.close()
        self.assertEqual(m.proc.ended(), 0)

    def test_isapc(self):
        self.run_machine("isapc")

    def test_ss5(self):
        self.run_machine("ss5")

    def test_q800(self):
        self.run_machine("q800")


if __name__ == "__main__":
    unittest.main()
