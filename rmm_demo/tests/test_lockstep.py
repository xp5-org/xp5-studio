# SPDX-License-Identifier: MIT
"""Lock / unlock (hostgroup.py) over in-process reference mocks, no window: an unlocked node is
left out of group snap / restore / halt / resume and halt-all, keeps its own side branch when
locked again, and rollback puts every node back and deletes what the unlocked ones saved.

    python3 -m unittest discover -s tests      (from rmm_demo/)

The same flow on two real 86Box instances is tests/lockstep_real.py (testlist
__testlist__rmm_demo_lockstep.py).
"""

import json
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, "..")):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui.commands import Commands  # noqa: E402
from rmmgui.group import GroupError, GroupSpec  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402
from rmmgui.snaps import GONE, SnapError, SnapTree  # noqa: E402

LOOP_BP = 0x0800 * 16 + 0x10F           # the mock's MEMLOOP `inc` instruction, linear
CELL = 0x9000                           # a byte each test writes to tell the states apart


def wait_for(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Lockstep(unittest.TestCase):
    def make(self, names="abc", policy=None):
        self.lines = []
        spec = GroupSpec({"schema": "rmm-group/1", "name": "t", "policy": policy or {},
                          "nodes": {n: {"mock": True} for n in names}})
        g = HostGroup(spec, tempfile.mkdtemp(), lambda t, tone, n: self.lines.append((n.name if n else None, tone, t)))
        self.addCleanup(g.stop)
        g.start()
        self.assertTrue(wait_for(lambda: all(n.connected and n.status.get("state") == "running"
                                             for n in g.nodes.values())), self.lines)
        self.g = g
        self.cmds = Commands(types.SimpleNamespace(group=g))
        return g

    def poke(self, name, val):
        n = self.g.nodes[name]
        was = n.rmm.state() == "running"
        n.pause(induced=True)
        n.rmm.call("mem.phys.write", addr=CELL, data=f"{val:02x}")
        if was:
            n.resume()

    def peek(self, name):
        n = self.g.nodes[name]
        was = n.rmm.state() == "running"
        n.pause(induced=True)
        v = int(n.rmm.call("mem.phys.read", addr=CELL, len=1)["data"], 16)
        if was:
            n.resume()
        return v

    def run_cmd(self, word, args="", targets=None):
        return self.cmds.run_group(word, args.split(), [self.g.nodes[t] for t in targets] if targets else None)

    # -- the plan's sequence ---------------------------------------------------------------

    def test_unlock_rewind_snap_relock(self):
        g = self.make()
        for i, x in enumerate("abc"):
            self.poke(x, 0x10 + i)
        s1, _ = g.snap(None, "S1")
        self.assertEqual(s1.nodes, ["a", "b", "c"])
        for x in "abc":
            self.poke(x, 0x20)
        g.snap(None, "S2")
        out = self.run_cmd("unlock", "b")
        self.assertIn("b unlocked", out[0])
        self.assertEqual(g.unlocked(), ["b"])
        self.assertEqual(g.forks["b"]["from"], 2)
        # the group goes on without b
        s3, _ = g.snap(None, "S3")
        self.assertEqual((s3.nodes, s3.scope), (["a", "c"], "group"))
        # b alone: back to S1, change, snapshot on its own track
        g.restore("S1", [g.nodes["b"]])
        self.assertEqual(self.peek("b"), 0x11)
        self.assertEqual(self.peek("a"), 0x20)               # a was not touched
        self.assertEqual(g.branch_point("b"), s1.id)
        self.poke("b", 0x33)
        try1, _ = g.snap([g.nodes["b"]], "b-try1")
        self.assertEqual((try1.scope, try1.parents), ("nodes", {"b": s1.id}))
        self.assertEqual([r.tag for r in g.own_records("b")], ["b-try1"])
        self.assertEqual(g.branch_point("b"), s1.id)         # through its own record to S1
        # a group restore skips b
        _, done, _ = g.restore("S2")
        self.assertEqual(sorted(done), ["a", "c"])
        self.assertEqual(self.peek("b"), 0x33)
        lines = "\n".join(self.run_cmd("lock"))
        self.assertIn("unlocked: b  from #1 S1; own snapshots: b-try1", lines)
        # relock: b keeps its state, the next group snapshot includes it, parented on its own record
        self.run_cmd("lock", "", ["b"])
        self.assertEqual((g.unlocked(), g.forks), ([], {}))
        self.assertEqual(self.peek("b"), 0x33)
        s4, _ = g.snap(None, "S4")
        self.assertEqual(s4.nodes, ["a", "b", "c"])
        self.assertEqual(s4.parents, {"a": 2, "b": try1.id, "c": 2})
        self.assertTrue(try1.live("b"))                      # the side branch stays

    def test_rollback_discards_and_relocks(self):
        g = self.make("ab")
        self.poke("a", 0x01)
        self.poke("b", 0x02)
        s1, _ = g.snap(None, "S1")
        g.unlock("b")
        g.restore("S1", [g.nodes["b"]])
        self.poke("b", 0x44)
        g.snap([g.nodes["b"]], "b-try1")
        g.snap([g.nodes["b"]], "b-try2")
        self.poke("a", 0x55)
        g.snap(None, "S2")                                    # a only
        rec, discard = g.rollback_plan("S1")
        self.assertEqual({u: [r.tag for r in rs] for u, rs in discard.items()}, {"b": ["b-try1", "b-try2"]})
        out = self.run_cmd("rollback", "S1")
        self.assertIn("rolled back to #1 S1 on", out[0])
        self.assertIn("b: locked again, deleted b-try1, b-try2", out)
        self.assertEqual((self.peek("a"), self.peek("b")), (0x01, 0x02))
        self.assertEqual(g.unlocked(), [])
        tags = [s["tag"] for s in g.nodes["b"].rmm.call("snapshot.list")["snapshots"]]
        self.assertEqual(sorted(tags), ["S1"])
        self.assertEqual(g.tree.find("b-try1").status["b"], GONE)
        self.assertEqual(g.tree.heads, {"a": s1.id, "b": s1.id})
        self.assertTrue(wait_for(lambda: all(n.rmm.state() == "running" for n in g.nodes.values())))

    def test_refused_rollback_changes_nothing(self):
        g = self.make("ab")
        self.poke("a", 0x01)
        g.snap(None, "S1")
        g.unlock("b")
        g.snap([g.nodes["b"]], "b-own")
        self.poke("a", 0x09)
        self.poke("b", 0x0A)
        g.snap(None, "aonly")                                 # a only: b is unlocked
        with self.assertRaisesRegex(SnapError, "not on b"):
            g.rollback_group("aonly")
        g.nodes["a"].rmm.call("snapshot.delete", tag="S1")    # behind the group's back
        with self.assertRaisesRegex(SnapError, "nothing was changed"):
            g.rollback_group("S1")
        self.assertEqual((self.peek("a"), self.peek("b")), (0x09, 0x0A))
        self.assertEqual(g.unlocked(), ["b"])
        self.assertTrue(g.tree.find("b-own").live("b"))

    def test_halt_and_resume_default_to_locked(self):
        g = self.make("ab")
        g.unlock("b")
        self.run_cmd("halt")
        self.assertTrue(wait_for(lambda: g.nodes["a"].rmm.state() == "paused"))
        self.assertEqual(g.nodes["b"].rmm.state(), "running")
        self.run_cmd("resume")
        self.assertTrue(wait_for(lambda: g.nodes["a"].rmm.state() == "running"))
        self.run_cmd("halt", "", ["a", "b"])                  # explicit targets still reach b
        self.assertTrue(wait_for(lambda: g.nodes["b"].rmm.state() == "paused"))
        g.relock("b")
        g.unlock("a")
        g.unlock("b")
        with self.assertRaisesRegex(SnapError, "every node is unlocked"):
            g.snap()
        with self.assertRaisesRegex(GroupError, "already unlocked"):
            g.unlock("a")

    def test_halt_all_scoping(self):
        g = self.make("abc", {"halt_all": True, "halt_on": ["breakpoint"]})
        a, b, c = (g.nodes[x] for x in "abc")
        g.unlock("b")
        # a stop on unlocked b pauses nobody else
        bp = b.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(wait_for(lambda: b.stop_note.startswith("breakpoint")))
        time.sleep(0.4)
        self.assertIsNone(g.halted)
        self.assertEqual((a.rmm.state(), c.rmm.state()), ("running", "running"))
        self.assertTrue(any("it is unlocked" in t for _, _, t in self.lines))
        b.rmm.call("debug.break.clear", id=bp)
        b.resume()
        self.assertTrue(wait_for(lambda: b.rmm.state() == "running"))
        # a stop on locked a pauses c, not b
        bp = a.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(wait_for(lambda: g.halted is not None and g.halted["paused"] == ["c"]), g.halted)
        self.assertEqual(c.rmm.state(), "paused")
        self.assertEqual(b.rmm.state(), "running")
        a.rmm.call("debug.break.clear", id=bp)
        g.resume()                                           # the locked ones: clears the banner
        self.assertIsNone(g.halted)
        # halt_unlocked: b is paused as well
        g.policy.halt_unlocked = True
        bp = a.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(wait_for(lambda: g.halted is not None and b.rmm.state() == "paused"))
        a.rmm.call("debug.break.clear", id=bp)

    def test_lock_state_round_trip(self):
        g = self.make("ab")
        g.snap(None, "S1")
        g.unlock("b")
        g.snap([g.nodes["b"]], "b-own")
        state = json.loads(json.dumps(g.lock_state()))
        tree = json.loads(json.dumps(g.tree.to_json()))
        spec = GroupSpec({"schema": "rmm-group/1", "name": "t", "nodes": {"a": {"mock": True}, "b": {"mock": True}}})
        h = HostGroup(spec, tempfile.mkdtemp(), lambda *a: None)
        h.tree = SnapTree.from_json(tree, h.nodes)
        h.set_lock_state(state)
        self.assertEqual((h.unlocked(), sorted(h.locked)), (["b"], ["a"]))
        self.assertEqual([r.tag for r in h.own_records("b")], ["b-own"])
        self.assertEqual(h.branch_point("b"), 1)
        self.assertEqual(GroupSpec({"schema": "rmm-group/1", "name": "t", "policy": {"halt_unlocked": True},
                                    "nodes": {"a": {"mock": True}}}).policy.to_spec()["halt_unlocked"], True)


if __name__ == "__main__":
    unittest.main()
