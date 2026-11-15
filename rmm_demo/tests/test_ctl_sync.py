# SPDX-License-Identifier: MIT
"""rmmctl, the Qt manager and any other tool run the same backend code (rmm_86box.backends) and share
session files; they must not drift apart. A registry in a temp home, group files, a fake VICE monitor,
and (when installed) a real dgnova started by rmmctl and then driven by a GUI Node.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

DEMO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (DEMO, os.path.join(DEMO, ".."), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import rmm_86box.backends as libbackends     # noqa: E402
from rmm_86box import ctl                    # noqa: E402
from rmmgui import backends                  # noqa: E402
from rmmgui.group import GroupSpec           # noqa: E402
from rmmgui.node import Node                 # noqa: E402
from test_vice_backend import FakeMonitor    # noqa: E402

SIMH = {"backend": "simh", "simh": {"disk_image": "/tmp/nova.dsk"}}
HAVE_DGNOVA = sys.platform.startswith("linux") and bool(      # the shared build is a Linux ELF
    shutil.which("dgnova") or os.path.exists("/testsrc/disk_artifacts/bin/dgnova"))


def run_ctl(home, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ctl.main(["--home", home] + [str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def group_node(node, tmp):
    spec = GroupSpec({"schema": "rmm-group/1", "name": "t", "nodes": {"n": node}}, os.path.join(tmp, "t.json"))
    return Node(spec.nodes[0], tmp, lambda *a: None)


def wait_connected(node, secs=8):
    end = time.time() + secs
    while time.time() < end and not (node.connected and node.linked):
        time.sleep(0.1)
    return node.connected


class OneRegistry(unittest.TestCase):
    def test_gui_and_cli_see_the_library_registry(self):
        self.assertIs(backends.get, libbackends.get)
        self.assertEqual(backends.names(), libbackends.names())
        self.assertIn(ctl.NATIVE_BACKEND, backends.names())

    def test_every_backend_is_startable_from_a_bare_node_dict(self):
        from rmm_86box.spec import MachineSpec
        for name in libbackends.names():
            spec = MachineSpec.of("x", {"backend": name})
            b = libbackends.get(name)
            self.assertTrue(callable(b.validate) and callable(b.attaches), name)
            b.validate(spec)                       # an error text or None, never a crash


class SessionFiles(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ctlsync_")
        self.reg = ctl.Registry(self.home)
        self.reg.put(dict(name="dos1", kind="attached", host="127.0.0.1", port=55860, pid=None, started=0))
        self.path = os.path.join(self.home, "s.json")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def session(self, sub, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ctl.cmd_session(self.reg, argparse.Namespace(sub=sub, name=self.path, wait=1, no_check=True, **kw))
        return rc, out.getvalue()

    def write(self, nodes):
        with open(self.path, "w") as f:
            json.dump({"schema": ctl.SESSION_SCHEMA, "name": "s", "nodes": nodes}, f)

    def test_save_keeps_the_managers_other_backend_nodes(self):
        self.write({"nova": SIMH})
        rc, out = self.session("save")
        self.assertEqual(rc, 0)
        self.assertIn("kept 1 simh", out)
        nodes = read_json(self.path)["nodes"]
        self.assertEqual(sorted(nodes), ["dos1", "nova"])
        self.assertEqual(nodes["nova"], SIMH)

    def test_saved_file_is_a_valid_manager_group(self):
        self.write({"nova": SIMH})
        self.session("save")
        spec = GroupSpec(read_json(self.path), self.path)
        self.assertEqual({n.name: n.backend for n in spec.nodes}, {"dos1": "86box", "nova": "simh"})

    def test_show_names_the_backend(self):
        self.write({"nova": SIMH})
        self.assertIn("simh", self.session("show")[1])

    def test_load_skips_a_backend_nobody_registered(self):
        self.write({"odd": {"backend": "hatari", "port": 1}})
        rc, out = self.session("load")
        self.assertIn("odd: skipped (backend 'hatari'", out)
        self.assertFalse(self.reg.has("odd"))


class ViceBothWays(unittest.TestCase):
    """A VICE attached through rmmctl, then the same machine driven by a GUI Node from the session file."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ctlvice_")
        self.mon = FakeMonitor()
        self.snaps = os.path.join(self.home, "snaps")
        os.makedirs(self.snaps)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_attach_snap_session_and_gui(self):
        rc, out, err = run_ctl(self.home, "attach", "c64", f"127.0.0.1:{self.mon.port}", "--backend", "vice",
                               "--snapdir", self.snaps)
        self.assertEqual(rc, 0, err)
        rc, out, _ = run_ctl(self.home, "ps")
        self.assertIn("vice", out)
        self.assertIn("BACKEND", out)
        rc, out, err = run_ctl(self.home, "snap", "c64", "save", "A")
        self.assertEqual((rc, out.strip()), (0, "c64: saved A"), err)
        self.assertEqual(run_ctl(self.home, "snap", "c64", "list")[1].split(), ["A"])
        self.assertEqual(run_ctl(self.home, "break", "c64", "0x1000")[0], 1)       # 86Box-only: refused, not misrouted
        self.assertIn("needs an 86Box", run_ctl(self.home, "break", "c64", "0x1000")[2])

        path = os.path.join(self.home, "s.json")
        self.assertEqual(run_ctl(self.home, "session", "save", path)[0], 0)
        saved = read_json(path)["nodes"]["c64"]
        self.assertEqual(saved["vice"]["port"], self.mon.port)
        node = group_node(saved, self.home)                      # the manager, from rmmctl's file
        node.start()
        try:
            self.assertTrue(wait_connected(node))
            self.assertEqual([s["tag"] for s in node.rmm.call("snapshot.list")["snapshots"]], ["A"])
        finally:
            node.stop()


@unittest.skipUnless(HAVE_DGNOVA, "dgnova not installed")
class SimhBothWays(unittest.TestCase):
    """rmmctl launches a dgnova under backends.serve; the CLI and a GUI Node then drive the same one."""

    LOOP = "deposit 0 jmp 0\nrun 0\n"

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ctlsimh_")
        self.ini = os.path.join(self.home, "loop.ini")
        with open(self.ini, "w") as f:
            f.write(self.LOOP)
        self.node = {"backend": "simh", "simh": {"script_path": self.ini}}

    def tearDown(self):
        run_ctl(self.home, "stop", "--all", "--timeout", 10)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_cli_launch_cli_snap_gui_attach(self):
        rc, out, err = run_ctl(self.home, "start", "nova", "--backend", "simh", "--node", json.dumps(self.node),
                               "--wait", 30)
        self.assertEqual(rc, 0, err)
        rec = ctl.Registry(self.home).get("nova")
        self.assertEqual((rec["backend"], rec["kind"]), ("simh", "launched"))
        self.assertIn("running", run_ctl(self.home, "ps")[1])

        self.assertEqual(run_ctl(self.home, "pause", "nova")[0], 0)
        self.assertIn("paused", run_ctl(self.home, "ps")[1])
        rc, out, err = run_ctl(self.home, "snap", "nova", "save", "s1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(run_ctl(self.home, "snap", "nova", "list")[1].split(), ["s1"])
        self.assertEqual(run_ctl(self.home, "snap", "nova", "restore", "s1")[0], 0)
        self.assertEqual(run_ctl(self.home, "snap", "nova", "delete", "s1")[0], 0)

        attach = {"backend": "simh", "simh": {"attach": True, "port": rec["port"]}}
        gui = group_node(attach, self.home)                      # the manager, on the machine rmmctl launched
        gui.start()
        try:
            self.assertTrue(wait_connected(gui))
            self.assertEqual(gui.rmm.call("machine.status")["state"], "paused")
            self.assertIsInstance(gui.tty(), str)
        finally:
            gui.stop()                                            # an attached node never kills what it did not start

        self.assertEqual(run_ctl(self.home, "resume", "nova")[0], 0)
        rc, out, err = run_ctl(self.home, "stop", "nova", "--timeout", 15)
        self.assertEqual(rc, 0, err)
        self.assertIn("stopped", out)


if __name__ == "__main__":
    unittest.main()
