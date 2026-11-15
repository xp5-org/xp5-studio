# SPDX-License-Identifier: MIT
"""rmmctl (rmm_86box.ctl) against the reference mock: attached instances in-process, launched ones
through a stand-in 86Box that serves the mock on --monitor-port.

    python -m unittest discover -s tests      (from the repo root)
"""

import contextlib
import io
import struct
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..")
sys.path.insert(0, LIB)

from rmm_86box import ctl, kvm  # noqa: E402
from rmm_86box.mock import start_mock  # noqa: E402

def monitor_port_mode(srv):
    """86Box started with --monitor-port neither pauses on connect nor resumes on detach; the mock models
    the cfg rmm_port flow, so silence its silent pause/resume."""
    pause, resume = srv.m.pause, srv.m.resume
    srv.m.pause = lambda silent=False: None if silent else pause(silent)
    srv.m.resume = lambda silent=False: None if silent else resume(silent)


FAKE = f"""#!{sys.executable}
import sys, time
sys.path.insert(0, {LIB!r})
from rmm_86box.mock import start_mock
sys.path.insert(0, {HERE!r})
from test_rmmctl import monitor_port_mode
args = sys.argv
srv = start_mock(int(args[args.index("--monitor-port") + 1]))
monitor_port_mode(srv)
while True:
    time.sleep(0.2)
"""


def run(home, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ctl.main(["--home", home] + [str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


class CtlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.srv = start_mock()
        monitor_port_mode(self.srv)
        self.addr = f"127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def attach(self, name="a"):
        rc, out, _ = run(self.home, "attach", name, self.addr)
        self.assertEqual(rc, 0, out)

    def test_ps_lists_attached(self):
        self.attach()
        rc, out, _ = run(self.home, "ps")
        self.assertEqual(rc, 0)
        head, row = out.splitlines()[:2]
        self.assertEqual(head.split()[:3], ["NAME", "STATE", "TURBO"])
        self.assertEqual(row.split()[:3], ["a", "running", "off"])
        self.assertIn(self.addr, row)

    def test_ps_json_and_down_hidden_without_a(self):
        self.attach()
        run(self.home, "attach", "gone", "127.0.0.1:1", "--no-check")
        self.assertEqual([r["name"] for r in json.loads(run(self.home, "ps", "--json")[1])], ["a"])
        names = lambda *x: [ln.split()[0] for ln in run(self.home, "ps", *x)[1].splitlines()[1:]]  # noqa: E731
        self.assertEqual(names(), ["a"])
        self.assertEqual(names("-a"), ["a", "gone"])
        self.assertIn("down", run(self.home, "ps", "-a")[1])

    def test_attach_refuses_dead_address_and_duplicates(self):
        rc, _, err = run(self.home, "attach", "x", "127.0.0.1:1")
        self.assertEqual(rc, 1)
        self.assertIn("does not answer", err)
        self.attach()
        self.assertEqual(run(self.home, "attach", "a", self.addr)[0], 1)
        self.assertEqual(run(self.home, "attach", "a", self.addr, "--force")[0], 0)

    def test_pause_resume(self):
        self.attach()
        rc, out, _ = run(self.home, "pause", "a")
        self.assertEqual((rc, out.split(":")[0]), (0, "a"))
        self.assertIn("paused", run(self.home, "ps")[1])
        run(self.home, "resume", "a")
        self.assertIn("running", run(self.home, "ps")[1])

    def test_pause_that_does_not_survive_the_disconnect_is_an_error(self):
        """Real 86Box resumes a guest its client paused when the client disconnects; say so."""
        self.srv.m.resume = type(self.srv.m).resume.__get__(self.srv.m)      # the mock's own resume-on-detach
        self.attach()
        rc, out, err = run(self.home, "pause", "a")
        self.assertEqual(rc, 1)
        self.assertIn("resumed it when rmmctl disconnected", err)
        self.assertIn("running", run(self.home, "ps")[1])

    def test_pause_inside_one_console_session_holds(self):
        self.attach()
        rc, out, _ = run(self.home, "console", "a", "machine.pause", "machine.status")
        self.assertIn("paused", out)

    def test_attach_does_not_resume_a_paused_guest(self):
        self.attach()
        run(self.home, "pause", "a")
        run(self.home, "status", "a")
        self.assertIn("paused", run(self.home, "ps")[1])

    def test_turbo(self):
        self.attach()
        self.assertIn("turbo off", run(self.home, "turbo", "a")[1])
        self.assertIn("turbo on", run(self.home, "turbo", "a", "on")[1])
        self.assertEqual(json.loads(run(self.home, "ps", "--json")[1])[0]["turbo"], True)
        self.assertIn("turbo off", run(self.home, "turbo", "a", "toggle")[1])
        self.assertIn("turbo off", run(self.home, "turbo", "a", "off")[1])

    def test_all_flag_and_unknown_name(self):
        self.attach("a")
        self.attach("b")
        rc, out, _ = run(self.home, "turbo", "--all", "on")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(ln.split(":")[0] for ln in out.splitlines()), ["a", "b"])
        rc, _, err = run(self.home, "pause", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no instance", err)

    def test_status(self):
        self.attach()
        rc, out, _ = run(self.home, "status", "a")
        self.assertEqual(rc, 0)
        self.assertIn("state    running", out)
        self.assertIn("turbo    off", out)

    def test_console_one_shot(self):
        self.attach()
        rc, out, _ = run(self.home, "console", "a", "machine.status")
        self.assertEqual(rc, 0)
        self.assertIn("running", out)

    def test_rm_forgets(self):
        self.attach()
        self.assertEqual(run(self.home, "rm", "a")[0], 1)          # up: refused without -f
        run(self.home, "pause", "a")
        self.srv.stop()
        self.assertEqual(run(self.home, "rm", "a")[0], 0)
        self.assertEqual(run(self.home, "ps", "-a")[1].splitlines()[1:], [])

    def test_vm_list_and_show(self):
        os.makedirs(self.home)
        ctl.Registry(self.home).domains.save("one", kvm.from_cfg({"Machine": {"machine": "ms4144", "mem_size": "6144"}}))
        self.assertEqual(run(self.home, "vm", "list")[1].split(), ["one"])
        self.assertIn("ms4144", run(self.home, "vm", "show", "one")[1])

    def test_bad_name(self):
        self.assertEqual(run(self.home, "attach", "a/b", self.addr)[0], 1)

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_launch_start_ps_turbo_stop(self):
        fake = os.path.join(self.tmp.name, "fake86box")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        vm = os.path.join(self.tmp.name, "vm")
        os.makedirs(vm)
        open(os.path.join(vm, "86box.cfg"), "w").write("[Machine]\nmachine = ms4144\n")
        rc, out, err = run(self.home, "start", "v1", "--vm-dir", vm, "--binary", fake, "--turbo", "--paused")
        try:
            self.assertEqual(rc, 0, err)
            rows = json.loads(run(self.home, "ps", "--json")[1])
            self.assertEqual([(r["name"], r["state"], r["turbo"], r["kind"]) for r in rows],
                             [("v1", "paused", True, "launched")])
            self.assertEqual(run(self.home, "start", "v1", "--vm-dir", vm, "--binary", fake)[0], 1)  # already up
            self.assertIn("alive", run(self.home, "status", "v1")[1])
        finally:
            rc, out, _ = run(self.home, "stop", "v1", "--timeout", "0.5")
        self.assertEqual((rc, out.strip()), (0, "v1: stopped"))
        self.assertEqual(json.loads(run(self.home, "ps", "--json")[1]), [])
        self.assertEqual(json.loads(run(self.home, "ps", "-a", "--json")[1])[0]["state"], "exited")

    def test_start_needs_cfg_or_domain(self):
        rc, _, err = run(self.home, "start", "v", "--vm-dir", os.path.join(self.tmp.name, "empty"), "--binary", shutil.which("true"))
        self.assertEqual(rc, 1)
        self.assertIn("86box.cfg", err)

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_start_reports_early_exit(self):
        vm = os.path.join(self.tmp.name, "vm")
        os.makedirs(vm)
        open(os.path.join(vm, "86box.cfg"), "w").write("[Machine]\n")
        rc, _, err = run(self.home, "start", "v", "--vm-dir", vm, "--binary", shutil.which("false"), "--wait", "5")
        self.assertEqual(rc, 1)
        self.assertIn("exited with", err)

    def _script(self, body):
        path = os.path.join(self.tmp.name, "bad86box")
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys, time\n" + body)
        os.chmod(path, 0o755)
        vm = os.path.join(self.tmp.name, "vm")
        os.makedirs(vm, exist_ok=True)
        with open(os.path.join(vm, "86box.cfg"), "w") as f:
            f.write("[Machine]\n")
        return path, vm

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_start_hang_reports_the_86box_error_and_cleans_up(self):
        """86Box with no ROMs sits in an error dialog: alive, no monitor. The reason must reach the CLI."""
        path, vm = self._script('sys.stderr.write("fatal: no ROMs found in /nowhere\\n"); sys.stderr.flush()\n'
                                'time.sleep(60)\n')
        rc, _, err = run(self.home, "start", "v", "--vm-dir", vm, "--binary", path, "--wait", "2")
        self.assertEqual(rc, 1)
        self.assertIn("never answered", err)
        self.assertIn("86Box: fatal: no ROMs found in /nowhere", err)
        self.assertIn("no --roms", err)
        self.assertEqual(json.loads(run(self.home, "ps", "-a", "--json")[1]), [])      # nothing left registered
        self.assertFalse([p for p in ctl._children.values() if p.poll() is None])     # nor running

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_start_exit_reports_stderr(self):
        path, vm = self._script('sys.stderr.write("ROM set missing: ms4144\\n"); sys.exit(3)\n')
        rc, _, err = run(self.home, "start", "v", "--vm-dir", vm, "--binary", path, "--wait", "5")
        self.assertEqual(rc, 1)
        self.assertIn("exited with 3", err)
        self.assertIn("86Box: ROM set missing: ms4144", err)

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_start_roms_dir_must_exist_and_is_passed(self):
        path, vm = self._script('open(sys.argv[0] + ".args", "w").write(" ".join(sys.argv[1:]))\nsys.exit(9)\n')
        rc, _, err = run(self.home, "start", "v", "--vm-dir", vm, "--binary", path, "--roms", "/no/such")
        self.assertIn("not a directory", err)
        roms = os.path.join(self.tmp.name, "roms")
        os.makedirs(roms)
        run(self.home, "start", "v", "--vm-dir", vm, "--binary", path, "--roms", roms, "--wait", "5")
        self.assertIn(f"-R {roms}", open(path + ".args").read())

    def test_status_of_a_stuck_instance_shows_the_log(self):
        path, vm = self._script('sys.stderr.write("fatal: no ROMs\\n"); sys.stderr.flush()\ntime.sleep(60)\n')
        run(self.home, "attach", "x", "127.0.0.1:1", "--no-check")
        rec = ctl.Registry(self.home).get("x")
        rec.update(kind="launched", pid=None, stderr=os.path.join(vm, "e.txt"))
        open(rec["stderr"], "w").write("fatal: no ROMs\n")
        ctl.Registry(self.home).put(rec)
        rc, out, _ = run(self.home, "status", "x")
        self.assertEqual(rc, 1)
        self.assertIn("86box    fatal: no ROMs", out)

    # -- sticky pause ----------------------------------------------------------------------

    def real_detach(self):
        self.srv.m.resume = type(self.srv.m).resume.__get__(self.srv.m)      # the mock's own resume-on-detach

    def test_sticky_off_a_pause_is_undone_on_disconnect(self):
        self.real_detach()
        self.attach()
        self.assertIn("sticky pause off", run(self.home, "sticky", "a")[1])
        self.assertEqual(run(self.home, "pause", "a")[0], 1)

    def test_turbo_and_sticky_all_take_a_mode(self):
        self.attach()
        self.assertIn("turbo on", run(self.home, "turbo", "--all", "on")[1])
        self.assertIn("sticky pause on", run(self.home, "sticky", "--all", "on")[1])

    def test_break_int_vector(self):
        self.attach()
        self.srv.m.ram[0x20:0x24] = bytes([0x34, 0x12, 0x00, 0xF0])           # INT 8 -> F000:1234
        rc, out, _ = run(self.home, "break", "a", "int8")
        self.assertEqual(rc, 0, out)
        self.assertIn("0x000f1234", out)

    def test_sticky_on_keeps_a_pause(self):
        self.real_detach()
        self.attach()
        rc, out, _ = run(self.home, "sticky", "a", "on")
        self.assertEqual((rc, out.strip()), (0, "a: sticky pause on"))
        rc, out, err = run(self.home, "pause", "a")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("resumed it", err)
        self.assertIn("paused", run(self.home, "ps")[1])
        self.assertIn("sticky   on", run(self.home, "status", "a")[1])
        run(self.home, "resume", "a")
        self.assertIn("running", run(self.home, "ps")[1])

    def test_sticky_does_not_keep_the_pause_on_connect_pause(self):
        """Only a pause the client asked for is kept: the one made on connect is always undone."""
        self.real_detach()
        self.attach()
        run(self.home, "sticky", "a", "on")
        self.srv.m.resume(silent=True)
        r = ctl.connect(ctl.Registry(self.home).get("a"))        # the mock pauses on connect
        r.close()
        self.assertIn("running", run(self.home, "ps")[1])

    # -- screenshot, media, break, config --------------------------------------------------

    def test_screenshot_auto_increments_and_overwrites(self):
        self.attach()
        d = os.path.join(self.tmp.name, "shots")
        for want in ("a-001.png", "a-002.png"):
            rc, out, _ = run(self.home, "screenshot", "a", "-d", d)
            self.assertEqual(rc, 0)
            self.assertIn(want, out)
        png = open(os.path.join(d, "a-001.png"), "rb").read()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (640, 400))
        for _ in range(2):                                  # a fixed name is rewritten
            run(self.home, "screenshot", "a", "-d", d, "-o", "boot.png")
        run(self.home, "screenshot", "a", "-d", d, "-o", "step{n}.png")
        run(self.home, "screenshot", "a", "-d", d, "-o", "step{n}.png")
        self.assertEqual(sorted(os.listdir(d)), ["a-001.png", "a-002.png", "boot.png", "step1.png", "step2.png"])

    def test_media_list_insert_eject(self):
        self.attach()
        img = os.path.join(self.tmp.name, "disk.img")
        open(img, "wb").write(b"\0" * 512)
        out = run(self.home, "media", "a")[1]
        self.assertIn("fdd.0", out)
        rc, out, _ = run(self.home, "media", "a", "insert", "fdd.0", img, "--wp")
        self.assertEqual(rc, 0, out)
        listing = run(self.home, "media", "a", "list")[1]
        self.assertRegex(listing, r"fdd\.0\s+\S*disk\.img\s+wp")
        self.assertEqual(run(self.home, "media", "a", "eject", "fdd.0")[0], 0)
        self.assertEqual(run(self.home, "media", "a", "eject", "fdd.0")[0], 1)       # already empty
        self.assertEqual(run(self.home, "media", "a", "insert", "nodrive", img)[0], 1)

    def test_break_sets_and_reports(self):
        self.attach()
        rc, out, _ = run(self.home, "break", "a", "0xF0000", "f000:0100")
        self.assertEqual(rc, 0)
        self.assertIn("break #1 at 0x000f0000", out)
        self.assertIn("break #2 at 0x000f0100", out)
        self.assertEqual(run(self.home, "break", "a", "zzz")[0], 1)

    def test_config_get_set_pending(self):
        self.attach()
        rc, out, _ = run(self.home, "config", "a", "get", "General")
        self.assertEqual(rc, 0, out)
        self.assertEqual(run(self.home, "config", "a", "pending")[0], 0)
        self.assertEqual(run(self.home, "config", "a")[0], 0)
        self.assertEqual(run(self.home, "config", "a", "set", "General")[0], 1)

    # -- sessions --------------------------------------------------------------------------

    def test_session_roundtrip_is_a_qt_group_file(self):
        self.attach("one")
        run(self.home, "attach", "two", str(self.srv.port))                  # a bare port is this machine
        rc, out, _ = run(self.home, "session", "save", "pair")
        self.assertEqual(rc, 0, out)
        path = os.path.join(self.home, "sessions", "pair.json")
        raw = json.load(open(path))
        self.assertEqual((raw["schema"], raw["name"], sorted(raw["nodes"])), ("rmm-group/1", "pair", ["one", "two"]))
        self.assertEqual(raw["nodes"]["two"], {"host": "127.0.0.1", "port": self.srv.port})
        self.assertIn("pair", run(self.home, "session", "ls")[1])
        self.assertIn("one", run(self.home, "session", "show", "pair")[1])
        other = os.path.join(self.tmp.name, "other")                         # a fresh machine
        rc, out, _ = run(other, "session", "load", os.path.join(self.home, "sessions", "pair.json"))
        self.assertEqual(rc, 0, out)
        ps = run(other, "ps")[1]
        self.assertIn("one", ps)
        self.assertIn("two", ps)
        self.assertEqual(run(self.home, "session", "rm", "pair")[0], 0)
        self.assertEqual(run(self.home, "session", "load", "pair")[0], 1)

    def test_session_save_keeps_what_the_qt_manager_wrote(self):
        self.attach()
        os.makedirs(os.path.join(self.home, "sessions"))
        path = os.path.join(self.home, "sessions", "g.json")
        json.dump({"schema": "rmm-group/1", "policy": {"halt_all": True}, "nodes": {"x": {"port": 1}}}, open(path, "w"))
        run(self.home, "session", "save", "g")
        raw = json.load(open(path))
        self.assertEqual(raw["policy"], {"halt_all": True})
        self.assertEqual(list(raw["nodes"]), ["a"])

    @unittest.skipIf(os.name == "nt", "the fake 86Box is a shell script")
    def test_session_load_launches_with_a_fake_86box(self):
        vm = os.path.join(self.tmp.name, "vm")
        os.makedirs(vm)
        open(os.path.join(vm, "86box.cfg"), "w").write("[General]\n")
        fake = os.path.join(self.tmp.name, "86Box")
        open(fake, "w").write(FAKE)
        os.chmod(fake, 0o755)
        path = os.path.join(self.tmp.name, "s.json")
        json.dump({"schema": "rmm-group/1", "nodes": {"d": {"port": "auto", "launch": {"binary": fake, "vm_dir": vm}}}},
                  open(path, "w"))
        try:
            rc, out, err = run(self.home, "session", "load", path)
            self.assertEqual(rc, 0, err)
            self.assertIn("running", run(self.home, "ps")[1])
        finally:
            run(self.home, "stop", "d")


if __name__ == "__main__":
    unittest.main()
