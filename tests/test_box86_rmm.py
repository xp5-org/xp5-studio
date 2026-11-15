# SPDX-License-Identifier: MIT
"""rmm_86box against the reference mock (rmm_86box/_vendor/rmm_mock.py): no VM needed.

    python -m unittest discover -s tests      (from the repo root)
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from rmm_86box import (Console, FramePoller, Rmm, RpcError, btn_event, key_event, kvm, parse_call,  # noqa: E402
                       rel_event)
from rmm_86box import client as client_mod  # noqa: E402
from rmm_86box.mock import start_mock  # noqa: E402
from rmm_86box.registry import Methods  # noqa: E402

# what config.dump gives for the matrix 'probe' cell's VM: floppy, IDE disk, ATAPI CD, NIC, SB16
PROBE_CFG = {
    "Machine": {"cpu_family": "i486dx", "cpu_multi": "1", "cpu_speed": "25000000", "cpu_use_dynarec": "1",
                "fpu_type": "internal", "machine": "ms4144", "mem_size": "6144"},
    "Video": {"gfxcard": "cl_gd5420_isa"},
    "Sound": {"sndcard": "sb16"},
    "Network": {"net_01_card": "ne2k", "net_01_net_type": "none"},
    "Hard disks": {"hdd_01_fn": "/x/c_hdd.img", "hdd_01_ide_channel": "0:0",
                   "hdd_01_parameters": "17, 4, 615, 0, ide", "hdd_01_speed": "1989_3500rpm"},
    "Floppy and CD-ROM drives": {"fdd_01_fn": "/img/dos622.img", "fdd_01_turbo": "1", "fdd_01_type": "35_2hd",
                                 "fdd_02_turbo": "1", "fdd_02_type": "35_2hd",
                                 "cdrom_01_parameters": "1, atapi", "cdrom_01_ide_channel": "1:0",
                                 "cdrom_01_speed": "8", "cdrom_01_image_path": "/x/disc_a.iso"},
}


class Kvm(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        dom = kvm.from_cfg(PROBE_CFG, "probe")
        self.assertEqual(kvm.to_cfg(dom), PROBE_CFG)
        self.assertEqual(kvm.from_cfg(kvm.to_cfg(dom), "probe"), dom)

    def test_domain_reads_in_kvm_terms(self):
        dom = kvm.from_cfg(PROBE_CFG)
        self.assertEqual(dom["machine"], "ms4144")
        self.assertEqual(dom["memory_kib"], 6144)
        self.assertEqual(dom["cpu"], {"model": "i486dx", "speed_hz": 25000000, "multi": "1",
                                      "fpu": "internal", "dynarec": True})
        self.assertEqual(dom["sound"], {"model": "sb16"})
        self.assertEqual(dom["interfaces"], [{"slot": 1, "model": "ne2k", "type": "none"}])
        targets = {(d["device"], d["target"]) for d in dom["disks"]}
        self.assertEqual(targets, {("floppy", "fda"), ("floppy", "fdb"), ("disk", "hda"), ("cdrom", "hdc")})
        cd = [d for d in dom["disks"] if d["device"] == "cdrom"][0]
        self.assertEqual((cd["bus"], cd["source"], cd["speed"]), ("atapi", "/x/disc_a.iso", "8"))

    def test_edits_map_back(self):
        dom = kvm.from_cfg(PROBE_CFG)
        dom["memory_kib"] = 16384
        dom["sound"] = None
        dom["disks"] = [d for d in dom["disks"] if d["device"] != "cdrom"]
        cfg = kvm.to_cfg(dom)
        self.assertEqual(cfg["Machine"]["mem_size"], "16384")
        self.assertEqual(cfg["Sound"], {})
        self.assertFalse(any(k.startswith("cdrom_") for k in cfg["Floppy and CD-ROM drives"]))
        changed = {(s, k) for s, k, _, _ in kvm.diff(kvm.from_cfg(PROBE_CFG), dom)}
        self.assertIn(("Machine", "mem_size"), changed)
        self.assertIn(("Sound", "sndcard"), changed)

    def test_store(self):
        with tempfile.TemporaryDirectory() as d:
            st = kvm.DomainStore(d)
            st.save("a-1", kvm.from_cfg(PROBE_CFG))
            self.assertEqual(st.list(), ["a-1"])
            self.assertEqual(st.load("a-1")["name"], "a-1")
            with self.assertRaises(ValueError):
                st.save("../evil", {})


class Parsing(unittest.TestCase):
    def test_parse_call(self):
        m = Methods()
        self.assertEqual(parse_call("mem.phys.read addr=0xB8000 len=16", m),
                         ("mem.phys.read", {"addr": 0xB8000, "len": 16}))
        self.assertEqual(parse_call('input.text.type dir a:', m), ("input.text.type", {"text": "dir a:"}))
        self.assertEqual(parse_call('snapshot.save before', m), ("snapshot.save", {"tag": "before"}))
        self.assertEqual(parse_call('fdd.0.insert path="/a b.img" wp=true', m),
                         ("fdd.0.insert", {"path": "/a b.img", "wp": True}))
        self.assertEqual(parse_call('config.load {"cfg": {"Machine": {}}}', m),
                         ("config.load", {"cfg": {"Machine": {}}}))

    def test_slot_methods_resolve(self):
        m = Methods()
        self.assertIs(m.get("fdd.1.insert"), m.by_name["fdd.N.insert"])
        self.assertIn("fdd.1.eject", m.names())

    def test_discover_from_pre_schema_server_keeps_static(self):
        """An 86Box from before the schema sends name and params only."""
        m = Methods()
        m.merge_discover({"methods": [{"name": "machine.status", "params": {}},
                                      {"name": "sound.mute", "params": {"value": "bool"}, "verbs": ["get", "set"]}]})
        st = m.get("machine.status")
        self.assertIn("sticky_pause", st.returns)
        self.assertTrue(st.doc)
        self.assertEqual((m.get("sound.mute.get").params, m.get("sound.mute.get").errors), ({}, []))
        self.assertEqual(m.get("sound.mute.set").errors, [4])
        self.assertIn("machine.stopped", m.events)

    def test_discover_schema_wins(self):
        m = Methods()
        m.merge_discover({"methods": [{"name": "io.read", "params": {"port": "int", "size": "int?"},
                                       "enums": {"size": [1, 2]}, "returns": ["value"], "paused": True,
                                       "errors": [1], "doc": "newer doc"}],
                          "events": [{"name": "x.happened", "fields": {"n": "int"}, "doc": ""}],
                          "errors": [{"code": 1, "name": "not paused"}, {"code": 12, "name": "new"}]})
        io = m.get("io.read")
        self.assertEqual((io.enums, io.returns, io.doc, io.paused), ({"size": [1, 2]}, ["value"], "newer doc", True))
        self.assertEqual(m.events, {"x.happened": {"n": "int"}})
        self.assertEqual(m.errors[12], "new")


class AgainstMock(unittest.TestCase):
    def setUp(self):
        self.srv = start_mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.rmm = Rmm(self.srv.port, vm_store=self.tmp.name)
        self.rmm.connect()

    def tearDown(self):
        self.rmm.close()
        self.srv.stop()
        self.tmp.cleanup()

    def test_discover_carries_schema(self):
        ms = self.rmm.methods
        self.assertTrue(ms.from_server)
        io = ms.get("io.read")
        self.assertEqual((io.enums, io.paused), ({"size": [1, 2, 4]}, True))
        self.assertIn("child_kind", ms.get("debug.watch.set").params)
        self.assertEqual(ms.get("snapshot.restore").errors, [7, 8])
        self.assertEqual(ms.errors[11], "client protocol too old")
        self.assertEqual(ms.events["media.changed"], {"dev": "string", "slot": "int", "path": "string"})
        out = "\n".join(Console(self.rmm).run("help debug.watch.set"))
        self.assertIn("kind: r | w | rw", out)
        self.assertIn("errors: 4 busy, absent or disabled", out)

    def test_autopause_follows_schema(self):
        c = Console(self.rmm)
        self.rmm.call("machine.resume")
        out = c.run("mem.info")
        self.assertNotIn("(paused for the call, resumed)", out)
        out = c.run("io.read port=0x3f8")
        self.assertIn("(paused for the call, resumed)", out)
        self.assertEqual(self.rmm.state(), "running")

    def test_input_without_a_mouse(self):
        d = self.rmm.input.devices()
        self.assertEqual((d["mouse"], d["tablet"], d["pointer"]), (None, None, "relative"))
        for call in (lambda: self.rmm.input.move(1, 1), lambda: self.rmm.input.click(),
                     lambda: self.rmm.input.mouse.move(dx=1, dy=1), lambda: self.rmm.input.point(0.5, 0.5)):
            with self.assertRaises(RpcError) as cm:
                call()
            self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self.rmm.input.tap("lctrl", "a")["queued"], 4)     # keys need no mouse

    def test_input_events_all_or_nothing(self):
        with self.assertRaises(RpcError) as cm:
            self.rmm.input.send(key_event("lctrl", True), {"type": "key", "scan": 0x200, "down": True})
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("events[1]", str(cm.exception))
        for bad in ({"type": "warp"}, {"type": "key", "down": True}, {"type": "key", "key": "nosuchkey", "down": True},
                    {"type": "key", "scan": 30, "down": 1}):
            with self.assertRaises(RpcError):
                self.rmm.input.send(bad)
        self.assertEqual(self.rmm.input.release_all()["keys"], [])        # the good lctrl was not queued
        with self.assertRaises(RpcError):
            self.rmm.call("input.events", events={"type": "key"})
        with self.assertRaises(RpcError):
            self.rmm.input.send(*[key_event("a", True)] * 257)

    def test_input_held_keys_are_released(self):
        self.rmm.input.press("lctrl")
        self.rmm.input.send(key_event(0x2A, True), key_event(0x2A, False))
        r = self.rmm.input.release_all()
        self.assertEqual((r["keys"], r["buttons"]), ([0x1D], 0))
        self.assertEqual(self.rmm.input.release_all()["keys"], [])
        self.rmm.input.press("a")
        self.rmm.close()                                   # disconnecting lets go too
        for _ in range(100):
            if not self.srv.keys_held:
                break
            time.sleep(0.01)
        self.assertEqual(self.srv.keys_held, set())

    def test_input_helpers_are_listed(self):
        names = dir(self.rmm.input)
        self.assertTrue({"tap", "move", "click", "events", "devices", "release_all"} <= set(names), names)
        self.assertEqual(self.rmm.methods.get("input.events").params, {"events": "array"})

    def test_hello_handshake(self):
        info = self.rmm.server_info
        self.assertEqual(info["protocol"], client_mod.PROTOCOL)
        self.assertEqual(info["negotiated"], client_mod.PROTOCOL)
        self.assertIn("mem", info["capabilities"])
        self.assertEqual(info["limits"]["mem_max"], 4096)

    def test_hello_rejects_old_client(self):
        with self.assertRaises(RpcError) as cm:
            self.rmm.call("rpc.hello", protocol=0)
        self.assertEqual(cm.exception.code, 11)
        self.assertEqual(cm.exception.data["min_protocol"], 1)

    def test_hello_newer_client_negotiates_down(self):
        self.assertEqual(self.rmm.call("rpc.hello", protocol=99)["negotiated"], client_mod.PROTOCOL)

    def test_hello_missing_on_old_server_is_accepted(self):
        class Old:
            def call(self, method, params=None, timeout=None):
                raise RpcError("method not found: rpc.hello", code=-32601)
        r = Rmm(self.srv.port, vm_store=self.tmp.name)
        r.rpc = Old()
        self.assertIsNone(r._hello())

    def test_connect_refuses_too_new_requirement(self):
        r = Rmm(self.srv.port, vm_store=self.tmp.name)
        old = client_mod.MIN_SERVER_PROTOCOL
        client_mod.MIN_SERVER_PROTOCOL = 99
        try:
            self.rmm.close()      # the mock serves one client at a time
            with self.assertRaises(RpcError) as cm:
                r.connect(wait=5)
            self.assertEqual(cm.exception.code, 11)
            self.assertFalse(r.connected)
        finally:
            client_mod.MIN_SERVER_PROTOCOL = old

    def test_connect_resumes_and_namespaces_call(self):
        self.assertEqual(self.rmm.machine.status()["state"], "running")
        self.assertIn("snapshot", dir(self.rmm))
        self.assertIn("save", dir(self.rmm.snapshot))
        self.rmm.machine.pause()
        self.assertEqual(self.rmm.state(), "paused")
        self.assertEqual(self.rmm.machine.fast_forward.set(value=True), {"value": True})

    def test_turbo_property(self):
        self.assertFalse(self.rmm.turbo)
        self.rmm.turbo = True
        self.assertTrue(self.rmm.turbo)
        self.assertEqual(self.rmm.machine.fast_forward.get(), {"value": True})
        self.rmm.turbo = False
        self.assertFalse(self.rmm.turbo)

    def test_paused_context_restores_running(self):
        with self.rmm.paused():
            self.assertEqual(self.rmm.state(), "paused")
            self.assertEqual(self.rmm.cpu.regs.get()["cs"], 0x0800)
        self.assertEqual(self.rmm.state(), "running")

    def test_errors_carry_codes(self):
        with self.assertRaises(RpcError) as e:
            self.rmm.mem.phys.read(addr=0, len=4)        # running: not paused
        self.assertEqual(e.exception.code, 1)

    def test_screenshot_and_text(self):
        self.rmm.input.text.type(text="hello")
        f = self.rmm.screenshot(step=2)
        self.assertEqual(len(f.rgb), f.w * f.h * 3)
        self.assertIn("A:\\>hello", self.rmm.text_screen()[1])

    def test_events_reach_callbacks(self):
        got = []
        self.rmm.on_event(lambda n, p: got.append(n))
        self.rmm.machine.pause()
        self.rmm.machine.reset(hard=True)
        deadline = time.time() + 2
        while "machine.reset" not in got and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn("machine.reset", got)

    def test_threads_share_the_connection(self):
        poller = FramePoller(self.rmm, rate=30, step=4).start()
        try:
            for _ in range(20):
                self.assertEqual(self.rmm.machine.status()["state"], "running")
            time.sleep(0.5)
            self.assertGreater(poller.frames, 3)
            poller.enabled = False
            time.sleep(0.2)
            n = poller.frames
            time.sleep(0.4)
            self.assertEqual(poller.frames, n, "a disabled poller must not ask for frames")
            poller.rate = 1000
            self.assertEqual(poller.rate, 30.0)
        finally:
            poller.stop()

    def test_vm_save_change_load(self):
        self.rmm.vm.save("base")
        dom = self.rmm.vm.get("base")
        for d in dom["disks"]:                  # the mock's /testsrc/images/... default only exists on the Linux hosts
            if d.get("source") and not os.path.exists(d["source"]):
                d["source"] = os.path.abspath(__file__)
                self.rmm.vm.store.save("base", dom)
                self.rmm.vm.load("base")
        dom["memory_kib"] = 4096
        self.rmm.vm.store.save("small", dom)
        self.assertEqual(self.rmm.vm.diff("small"), [("Machine", "mem_size", "6144", "4096")])
        r = self.rmm.vm.load("small")
        self.assertEqual(r["reset"]["config_loaded"], r["load"]["keys"])
        self.assertEqual(self.rmm.mem.info()["ram_kb"], 4096)
        self.assertEqual(self.rmm.vm.show()["memory_kib"], 4096)
        self.rmm.vm.load("base")
        self.assertEqual(self.rmm.vm.diff("base"), [])

    def test_vm_load_refused_names_the_key(self):
        dom = self.rmm.vm.show()
        dom["cpu"]["model"] = "8088"                     # does not fit ms4144
        with self.assertRaises(RpcError) as e:
            self.rmm.vm.load(dom)
        self.assertEqual(e.exception.code, 10)
        self.assertEqual(e.exception.data["errors"][0]["key"], "cpu_family")


class InputWithMouse(unittest.TestCase):
    """The mock machine with a Microsoft serial mouse on COM1 (and a joystick)."""

    def setUp(self):
        self.srv = start_mock(hw="joy_ms")
        self.rmm = Rmm(self.srv.port)
        self.rmm.connect()

    def tearDown(self):
        self.rmm.close()
        self.srv.stop()

    def test_devices(self):
        d = self.rmm.input.devices()
        self.assertEqual(d["mouse"], {"type": "msserial", "buttons": 2, "port": 0})
        self.assertEqual(d["pointer"], "relative")
        self.assertEqual(d["joystick"]["gameport"], 0x201)

    def test_mouse_move_is_a_serial_packet(self):
        self.assertEqual(self.rmm.input.mouse.move(dx=10, dy=-5, buttons=1), {"queued": 2, "pending": 0})
        self.assertEqual(self.srv.uart[-3:], [0x6C, 0x0A, 0x3B])
        self.rmm.input.mouse.move(dx=1, dy=0)                    # buttons omitted: still held
        self.assertEqual(self.srv.mouse_buttons, 1)
        for p in (dict(dx="left", dy=0), dict(dx=40000, dy=0), dict(dx=0)):
            with self.assertRaises(RpcError) as cm:
                self.rmm.input.mouse.move(**p)
            self.assertEqual(cm.exception.code, -32602)

    def test_events_drag_and_release(self):
        r = self.rmm.input.send(btn_event(1), rel_event(3, 4), rel_event(0, 0, dz=-1))
        self.assertEqual(r["queued"], 3)
        self.assertEqual(self.srv.mouse_buttons, 1)
        self.assertEqual(self.rmm.input.release_all()["buttons"], 1)
        self.assertEqual(self.srv.mouse_buttons, 0)
        self.rmm.input.buttons(2)
        self.rmm.close()
        for _ in range(100):
            if not self.srv.mouse_buttons:
                break
            time.sleep(0.01)
        self.assertEqual(self.srv.mouse_buttons, 0)


class ConsoleAndCompletion(unittest.TestCase):
    def setUp(self):
        self.srv = start_mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.rmm = Rmm(self.srv.port, vm_store=self.tmp.name)
        self.rmm.connect()
        self.con = Console(self.rmm)

    def tearDown(self):
        self.rmm.close()
        self.srv.stop()
        self.tmp.cleanup()

    def items(self, line):
        return self.con.complete(line).items

    def test_autoresume_after_restore(self):
        self.con.run("snapshot.save tag=t1")
        self.con.run("snapshot.restore tag=t1")
        self.assertEqual(self.rmm.state(), "paused")          # default: restore leaves it paused
        self.rmm.call("machine.resume")
        self.assertEqual(self.con.run("autoresume on"), ["autoresume on"])
        out = self.con.run("snapshot.restore tag=t1")
        self.assertIn("(resumed after restore)", out)
        self.assertEqual(self.rmm.state(), "running")

    def test_method_names_complete_by_segment(self):
        self.assertEqual(self.items("snap"), ["snapshot."])
        self.assertEqual(self.items("snapshot.res"), ["snapshot.restore"])
        self.assertEqual(self.items("snapshot.re"), ["snapshot.reloc", "snapshot.restore"])
        self.assertIn("machine.fast_forward.", self.items("machine.f"))
        self.assertIn("vm", self.items("v"))
        self.assertEqual(self.con.complete("snap").apply("snap"), "snapshot.")

    def test_params_and_values(self):
        self.assertEqual(self.items("debug.watch.set kind="), ["kind=r", "kind=w", "kind=rw"])
        self.assertIn("addr=", self.items("mem.phys.read "))
        self.assertNotIn("addr=", self.items("mem.phys.read addr=1 "))
        self.con.run("snapshot.save before")             # autopause makes this work running
        self.assertEqual(self.items("snapshot.restore tag="), ["tag=before"])
        self.assertIn("key=mem_size", self.items("config.get section=Machine key="))
        self.assertIn("value=4096", self.items("config.set section=Machine key=mem_size value="))
        self.assertIn('section="Floppy and CD-ROM drives"', self.items("config.dump section="))

    def test_hint_shows_what_comes_back(self):
        self.assertIn("-> {w, h", self.con.complete("capture.screenshot ").hint)

    def test_python_form(self):
        self.assertIn("rmm.snapshot.save", self.items("rmm.snapshot."))
        self.assertIn("rmm.fdd[0].insert", self.items("rmm.fdd[0].i"))
        self.assertIn("rmm.screenshot", self.items("rmm.scr"))
        self.con.run("rmm.machine.status()")
        self.assertIn('_["tsc"]', self.items('_["'))
        self.assertEqual(self.con.run('_["state"]'), ["'running'"])

    def test_builtin_vm_and_errors(self):
        self.con.run("vm save base")
        self.assertEqual(self.items("vm load "), ["base"])
        self.assertTrue(self.con.run("vm show")[1].startswith("machine  ms4144"))
        out = self.con.run('config.load {"cfg": {"Machine": {"machine": "nope"}}}')
        self.assertTrue(out[0].startswith("error [10]"), out)
        self.assertIn("nope", out[1])
        self.assertTrue(self.con.run("no.such.method")[0].startswith("error"))
        self.assertTrue(self.con.run("mem.phys.read addr=0xB8000 len=4")[0].startswith("000B8000"))


if __name__ == "__main__":
    unittest.main()
