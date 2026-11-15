# SPDX-License-Identifier: MIT
"""The backend registry and the SIMH node: group validation, then a real dgnova paused, snapped,
changed and restored through SimhRmm. The dgnova half skips when the binary is missing.

    python3 -m unittest discover -s tests      (from rmm_demo/)
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, ".."), "/testsrc/pyhelpers"):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui import backends  # noqa: E402
from rmmgui.group import GroupError, GroupSpec  # noqa: E402

LOOP_INI = """\
deposit 0 jmp 0
run 0
"""


def spec(nodes):
    return GroupSpec({"schema": "rmm-group/1", "name": "t", "nodes": nodes}, "t.json")


class Registry(unittest.TestCase):
    def test_builtins(self):
        self.assertEqual(backends.names(), ["86box", "qemu", "simh", "vice"])
        with self.assertRaises(KeyError):
            backends.get("hatari")

    def test_unknown_backend(self):
        with self.assertRaises(GroupError) as e:
            spec({"a": {"backend": "hatari", "port": 1234}})
        self.assertIn("86box, qemu, simh, vice", str(e.exception))

    def test_simh_needs_config_not_port(self):
        with self.assertRaises(GroupError):
            spec({"a": {"backend": "simh"}})
        g = spec({"a": {"backend": "simh", "simh": {"script_path": "/x.ini"}},
                  "b": {"backend": "simh", "simh": {"script_path": "/y.ini"}}})
        self.assertEqual([n.mode for n in g.nodes], ["simh", "simh"])

    def test_qemu_still_needs_port(self):
        with self.assertRaises(GroupError):
            spec({"a": {"backend": "qemu"}})


@unittest.skipUnless(sys.platform.startswith("linux") and (shutil.which("dgnova")
                                                          or os.path.exists("/testsrc/disk_artifacts/bin/dgnova")),
                     "no Linux dgnova here")
class RealSimh(unittest.TestCase):
    def test_pause_snap_restore(self):
        from rmmgui.node import Node
        tmp = tempfile.mkdtemp()
        ini = os.path.join(tmp, "loop.ini")
        with open(ini, "w") as f:
            f.write(LOOP_INI)
        g = spec({"a": {"backend": "simh", "simh": {"script_path": ini}}})
        node = Node(g.nodes[0], tmp, lambda *a: None)
        node.start()
        try:
            rmm = node.rmm
            self.assertEqual(rmm.connect(), rmm.banner)
            self.assertEqual(rmm.call("machine.status")["state"], "running")
            rmm.call("machine.pause")
            self.assertEqual(rmm.call("machine.status")["state"], "paused")
            rmm.call("snapshot.save", tag="s1")
            self.assertEqual([s["tag"] for s in rmm.call("snapshot.list")["snapshots"]], ["s1"])
            rmm.call("snapshot.restore", tag="s1")
            rmm.call("snapshot.delete", tag="s1")
            self.assertEqual(rmm.call("snapshot.list")["snapshots"], [])
            rmm.call("machine.resume")
            self.assertEqual(rmm.call("machine.status")["state"], "running")
        finally:
            node.stop()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
