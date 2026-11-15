# SPDX-License-Identifier: MIT
"""rmmgui without a window: group files, the line editor, the cfg writer, and a running group of
reference mocks (halt-all, targeting, crash detection with a fake 86Box that segfaults).

    python3 -m unittest discover -s tests      (from rmm_demo/)
"""

import json
import os
import shutil
import signal
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
LIB = os.path.join(DEMO, "..")
CLIENT = os.path.join(DEMO, "..", "rmm_86box", "_vendor")
for p in (DEMO, LIB):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui.editline import LineEditor  # noqa: E402
from rmmgui.group import GroupError, GroupSpec, merge  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402
from rmmgui.snaps import GONE, REPLACED, SnapError, SnapTree, layout  # noqa: E402
from rmmgui.launch import describe_exit, node_cfg, read_cfg, write_cfg  # noqa: E402
from rmm_86box import kvm  # noqa: E402

LOOP_BP = 0x0800 * 16 + 0x10F           # the mock's MEMLOOP `inc` instruction, linear


def spec(nodes, **top):
    return GroupSpec(dict({"schema": "rmm-group/1", "name": "t", "nodes": nodes}, **top))


def wait_for(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class GroupFile(unittest.TestCase):
    def test_merge_replaces_lists_and_none_deletes(self):
        self.assertEqual(merge({"a": {"x": 1, "y": 2}, "l": [1]}, {"a": {"y": None, "z": 3}, "l": [2]}),
                         {"a": {"x": 1, "z": 3}, "l": [2]})

    def test_extends_defaults_and_tokens(self):
        g = spec({"a": {"extends": "base", "port": 1000},
                  "b": {"extends": "a", "port": 1001, "launch": {"vm_dir": "{root}/{node}-{port}"}}},
                 vars={"root": "/r/{group}"},
                 defaults={"host": "127.0.0.1", "poll": {"rate": 3}},
                 templates={"base": {"poll": {"step": 2}, "launch": {"vm_dir": "{root}/{node}", "binary": "/bin/86"}}})
        a, b = g.node("a"), g.node("b")
        self.assertEqual(a.poll, {"rate": 3, "step": 2})
        self.assertEqual(a.launch["vm_dir"], "/r/t/a")
        self.assertEqual(b.launch, {"vm_dir": "/r/t/b-1001", "binary": "/bin/86"})
        self.assertEqual(a.mode, "launch")

    def test_depends_on_orders_start(self):
        g = spec({"a": {"port": 1, "depends_on": ["c"]}, "b": {"port": 2}, "c": {"port": 3}})
        self.assertEqual([n.name for n in g.nodes], ["c", "a", "b"])

    def test_errors(self):
        cases = [
            ({"a": {"port": 5}, "b": {"port": 5}}, "both use"),
            ({"a": {"port": "auto"}}, "needs launch"),
            ({"a": {}}, "port is required"),
            ({"a": {"port": 1, "depends_on": ["b"]}, "b": {"port": 2, "depends_on": ["a"]}}, "loop"),
            ({"a": {"port": 1, "extends": "nope"}}, "no such template"),
            ({"a": {"port": 1, "bogus": 1}}, "unknown keys"),
            ({"a b": {"port": 1}}, "node name"),
            ({"a": {"port": 1, "launch": {}}}, "vm_dir"),
        ]
        for nodes, why in cases:
            with self.subTest(why=why), self.assertRaisesRegex(GroupError, why):
                spec(nodes)
        with self.assertRaisesRegex(GroupError, "unknown reason"):
            spec({"a": {"mock": True}}, policy={"halt_on": ["sneeze"]})

    def test_aliases_and_save_round_trip(self):
        g = spec({"a": {"mock": True}}, policy={"halt_all": True, "halt_on": ["segfault", "bp", "halt"]})
        self.assertEqual(g.policy.halt_on, ["crash", "breakpoint", "pause"])
        with tempfile.TemporaryDirectory() as d:
            p = g.save(os.path.join(d, "g.json"))
            again = GroupSpec.load(p)
            self.assertEqual(again.policy.to_spec(), g.policy.to_spec())
            r = json.load(open(g.save(os.path.join(d, "r.json"), resolved=True)))
            self.assertEqual(r["nodes"]["a"], {"mock": True})

    def test_shipped_examples_parse(self):
        gdir = os.path.join(DEMO, "groups")
        for f in sorted(os.listdir(gdir)):
            with self.subTest(f=f):
                GroupSpec.load(os.path.join(gdir, f))

    def test_vm_config_testlist(self):
        """A testrunnerapp VM config as a node's machine: its box_cfg (tokens filled), then the
        node's box_cfg on top."""
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "__testlist__vm_x1.py"), "w").write(textwrap.dedent('''
                CONFIG = {"vm_config": True, "vm_id": "x1", "projbasedir": "/p/", "projdir": "q",
                          "vm_seed": "{projbasedir}{projdir}/seed/{vm_id}", "box_binary": "/b/86Box",
                          "box_cfg": {"Machine": {"machine": "ms4144", "mem_size": "6144"},
                                      "General": {"vid_renderer": "qt_software"}}}
            '''))
            g = spec({"a": {"vm_config": "x1", "port": "auto", "launch": {"vm_dir": d + "/a"},
                            "box_cfg": {"Machine": {"mem_size": 16384}}}}, vm_config_dir=d)
            cfg, vmc = node_cfg(g.node("a"), kvm.DomainStore(d), g.vm_config_dir)
            self.assertEqual(cfg["Machine"], {"machine": "ms4144", "mem_size": "16384"})
            self.assertEqual(vmc["vm_seed"], "/p/q/seed/x1")
            self.assertEqual(vmc["box_binary"], "/b/86Box")

    def test_real_vm_config_reads(self):
        path = os.path.join(DEMO, "..", "__testlist__vm_ms4144_486.py")
        if not os.path.exists(path):
            self.skipTest("no ms4144_486 VM config here")
        from rmmgui.group import load_vm_config
        v = load_vm_config(path)
        self.assertEqual(v["box_cfg"]["Machine"]["machine"], "ms4144")
        self.assertNotIn("{", v["vm_seed"])


class Cfg(unittest.TestCase):
    def test_domain_owns_its_sections_and_cfg_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            store = kvm.DomainStore(d)
            dom = json.load(open(os.path.join(DEMO, "domains", "dos622_486_sb16.json")))
            store.save("sb", dom)
            g = spec({"a": {"domain": "sb", "port": 9, "launch": {"vm_dir": d},
                            "box_cfg": {"General": {"vid_renderer": "qt_software"}}}})
            existing = {"Machine": {"stale_key": "1"}, "Input devices": {"mouse_type": "none"}}
            cfg, _ = node_cfg(g.node("a"), store, None, existing)
            self.assertNotIn("stale_key", cfg["Machine"])
            self.assertEqual(cfg["Input devices"], {"mouse_type": "none"})
            self.assertEqual(cfg["Sound"]["sndcard"], "sb16")
            p = os.path.join(d, "86box.cfg")
            write_cfg(p, cfg)
            self.assertEqual(read_cfg(p), cfg)

    def test_describe_exit(self):
        self.assertEqual(describe_exit(-signal.SIGSEGV), ("crash", "SIGSEGV"))
        self.assertEqual(describe_exit(-signal.SIGTERM)[0], "exit")
        self.assertEqual(describe_exit(0), ("exit", "exit 0"))
        self.assertEqual(describe_exit(139)[0], "crash")
        self.assertEqual(describe_exit(0xC0000005), ("crash", "access violation"))


class EditLine(unittest.TestCase):
    def test_selection_cut_and_typing_over(self):
        e = LineEditor()
        e.insert("machine.status")
        e.home()
        for _ in range(7):
            e.right(select=True)
        self.assertEqual(e.selected_text(), "machine")
        self.assertEqual(e.cut(), "machine")
        self.assertEqual(e.text, ".status")
        e.select_all()
        e.insert("x")
        self.assertEqual((e.text, e.cursor), ("x", 1))

    def test_multiline_paste_runs_full_lines(self):
        e = LineEditor()
        e.insert("@a ")
        run = e.paste("machine.status\nmachine.pause\n\nsnapshot.li")
        self.assertEqual(run, ["@a machine.status", "machine.pause"])
        self.assertEqual((e.text, e.cursor), ("snapshot.li", 11))
        e.insert("st")
        self.assertEqual(e.text, "snapshot.list")

    def test_word_moves_and_history(self):
        e = LineEditor()
        e.insert("mem.phys.read addr=0x10")
        e.left(word=True)
        self.assertEqual(e.text[e.cursor:], "0x10")
        e.backspace(word=True)
        self.assertEqual(e.text, "mem.phys.read 0x10")
        self.assertEqual(e.submit(), "mem.phys.read 0x10")
        e.up()
        self.assertEqual(e.text, "mem.phys.read 0x10")
        e.down()
        self.assertEqual(e.text, "")

    def test_qemu_nodes(self):
        g = spec({"q": {"qemu": {"profile": "m68k", "bios": "{node}.rom", "scsi": ["a.img"], "run_dir": "/r/{node}"}},
                  "s": {"qemu": {"profile": "ss5", "qmp": "/run/s/qmp.sock"}},
                  "a": {"port": 55860}})
        self.assertEqual([n.mode for n in g.nodes], ["qemu", "qemu-attach", "attach"])
        self.assertEqual(g.node("q").qemu["bios"], "q.rom")
        for bad, msg in (({"profile": "amiga"}, "no QEMU profile"),
                         ({"profile": "ss5", "fda": "x.img"}, r"unknown keys \['fda'\]"),
                         ({"profile": "isapc", "on_exit": "pause"}, "quit or keep"),
                         ("isapc", "qemu is an object")):
            with self.assertRaisesRegex(GroupError, msg):
                spec({"q": {"qemu": bad}})
        with self.assertRaisesRegex(GroupError, "takes no port, launch"):
            spec({"q": {"qemu": {"profile": "isapc"}, "port": 1, "launch": {"vm_dir": "/x"}}})


class MockGroup(unittest.TestCase):
    """Real HostGroups over in-process mocks."""

    def make(self, nodes, policy=None):
        self.lines = []
        g = HostGroup(spec(nodes, policy=policy or {}), tempfile.mkdtemp(),
                      lambda t, tone, n: self.lines.append((n.name if n else None, tone, t)))
        self.addCleanup(g.stop)
        g.start()
        self.assertTrue(wait_for(lambda: all(n.connected for n in g.nodes.values())), self.lines)
        self.assertTrue(wait_for(lambda: all(n.status.get("state") == "running" for n in g.nodes.values())))
        return g


class Running(MockGroup):
    def test_halt_all_on_breakpoint(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}, "c": {"mock": True}},
                      {"halt_all": True, "halt_on": ["breakpoint", "pause"]})
        a, b, c = (g.nodes[x] for x in "abc")
        bp = b.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(wait_for(lambda: g.halted is not None))
        self.assertEqual((g.halted["by"], g.halted["reason"]), ("b", "breakpoint"))
        self.assertTrue(wait_for(lambda: a.rmm.state() == c.rmm.state() == "paused"))
        self.assertEqual(sorted(g.halted["paused"]), ["a", "c"])
        time.sleep(0.5)                          # a's and c's own pause events must not re-trigger
        self.assertEqual(len(g.history), 1, g.history)
        b.rmm.call("debug.break.clear", id=bp)           # else b stops again at once and halts the rest again
        g.resume()
        self.assertIsNone(g.halted)
        self.assertTrue(wait_for(lambda: a.rmm.state() == "running"))

    def test_halt_all_off_leaves_others_running(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}})
        g.nodes["a"].rmm.call("debug.break.set", addr=LOOP_BP)
        self.assertTrue(wait_for(lambda: g.nodes["a"].stop_note.startswith("breakpoint")))
        time.sleep(0.3)
        self.assertIsNone(g.halted)
        self.assertEqual(g.nodes["b"].rmm.state(), "running")

    def test_add_node_while_running(self):
        g = self.make({"a": {"mock": True}})
        with self.assertRaisesRegex(GroupError, "already"):
            g.add_node("a", {"mock": True})
        with self.assertRaisesRegex(GroupError, "port is required"):
            g.add_node("x", {})
        self.assertEqual(list(g.nodes), ["a"])               # a refused body changed nothing
        n = g.add_node("b", {"mock": True})
        self.assertTrue(wait_for(lambda: n.connected and n.status.get("state") == "running"))
        self.assertIn("b", g.spec.raw["nodes"])
        rec, failed = g.snap()
        self.assertEqual((rec.nodes, failed), (["a", "b"], {}))
        self.assertEqual(g.tree.heads, {"a": 1, "b": 1})

    def test_targets(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}})
        self.assertEqual([n.name for n in g.split_target("@a,b machine.status")[0]], ["a", "b"])
        self.assertEqual([n.name for n in g.split_target("@* x")[0]], ["a", "b"])
        self.assertEqual(g.split_target("machine.status")[0][0].name, "a")
        g.run_line("@b machine.pause")
        self.assertTrue(wait_for(lambda: g.nodes["b"].rmm.state() == "paused"))
        self.assertEqual(g.nodes["a"].rmm.state(), "running")
        g.run_line("@b")
        self.assertEqual(g.current, "b")
        g.run_line("@zz machine.status")
        self.assertTrue(any("no such node" in t for _, _, t in self.lines))


class Tree(unittest.TestCase):
    def test_branches_heads_and_replace(self):
        t = SnapTree(["a", "b"])
        r1 = t.add("base", ["a", "b"], "group")
        r2 = t.add("bonly", ["b"], "nodes")
        self.assertEqual((r2.parents, t.heads), ({"b": 1}, {"a": 1, "b": 2}))
        r3 = t.add("two", ["a", "b"], "group")        # a from #1, b from #2
        self.assertEqual((r3.parent(), r3.other_parents()), (2, [1]))
        t.restored(r1, ["a", "b"])
        r4 = t.add("side", ["a"], "nodes")           # a branch off base
        self.assertEqual(r4.parent(), 1)
        self.assertEqual(sorted(r.id for r in t.children()[1]), [2, 4])
        t.add("base", ["a"], "nodes")                # 86Box replaces a tag on save
        self.assertEqual(r1.status, {"a": REPLACED, "b": "ok"})
        self.assertIs(t.find("base", ["b"]), r1)
        self.assertEqual(t.find("base").id, 5)
        self.assertRaises(SnapError, t.find, "nope")
        self.assertRaises(SnapError, t.check_tag, "two words")
        text = "\n".join(t.lines())
        self.assertIn("#3 two  [all]", text)
        self.assertIn("also from #1", text)
        pos, w, h = layout(t, 100, 40, 10, 20)
        self.assertEqual(set(pos), {None, 1, 2, 3, 4, 5})
        self.assertLess(pos[None][1], pos[1][1])
        self.assertTrue(pos[2][0] <= pos[1][0] <= pos[4][0])  # parent centred over its children

    def test_json_round_trip(self):
        t = SnapTree(["a", "b"])
        t.add("base", ["a", "b"], "group", tsc={"a": 5})
        r2 = t.add("bonly", ["b"], "nodes")
        r2.note = "n"
        t.deleted(r2, ["b"])
        u = SnapTree.from_json(json.loads(json.dumps(t.to_json())), ["a", "b"])
        self.assertEqual(u.to_json(), t.to_json())
        self.assertEqual(u.add("next", ["a"], "nodes").parents, {"a": 1})   # heads came back
        self.assertEqual(u.auto_tag(), "s4")

    def test_reconcile(self):
        t = SnapTree(["a"])
        r1 = t.add("x", ["a"], "nodes")
        t.reconcile("a", ["x", "typed"])             # a tag the tree never saw
        self.assertEqual([r.tag for r in t.recs.values()], ["x", "typed"])
        self.assertEqual(t.recs[2].scope, "console")
        t.reconcile("a", ["typed"])                  # x deleted behind our back
        self.assertEqual(r1.status["a"], GONE)
        self.assertEqual(t.auto_tag(), "s3")


class GroupSnaps(MockGroup):
    """snap / restore across mocks: RAM rolls back on every node together, or on one."""

    def poke(self, n, val):
        n.pause(induced=True)
        n.rmm.call("mem.phys.write", addr=0x9000, data=f"{val:02x}")
        n.resume()

    def peek(self, n):
        n.pause(induced=True)
        v = int(n.rmm.call("mem.phys.read", addr=0x9000, len=1)["data"], 16)
        n.resume()
        return v

    def test_group_snap_and_restore(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}, "c": {"mock": True}},
                      {"halt_all": True, "halt_on": ["breakpoint", "pause"]})
        a, b, c = (g.nodes[x] for x in "abc")
        for i, n in enumerate((a, b, c)):
            self.poke(n, 0x10 + i)
        rec, failed = g.snap()
        self.assertEqual((rec.tag, rec.nodes, rec.scope, failed), ("s1", ["a", "b", "c"], "group", {}))
        self.assertTrue(wait_for(lambda: all(n.rmm.state() == "running" for n in (a, b, c))))
        self.assertIsNone(g.halted)                  # the group's own pauses are no cause
        for n in (a, b, c):
            self.poke(n, 0xEE)
        g.snap([b], "bonly")
        self.poke(b, 0x77)
        rec2, done, failed = g.restore("s1")
        self.assertEqual((sorted(done), failed), (["a", "b", "c"], {}))
        self.assertEqual([self.peek(n) for n in (a, b, c)], [0x10, 0x11, 0x12])
        self.assertTrue(wait_for(lambda: all(n.rmm.state() == "running" for n in (a, b, c))))
        self.assertEqual(g.tree.heads, {"a": 1, "b": 1, "c": 1})
        # one node only, by target
        g.restore("bonly", [b], run=False)
        self.assertEqual(b.rmm.state(), "paused")
        self.assertEqual(a.rmm.state(), "running")
        self.assertEqual(self.peek(b), 0xEE)
        self.assertEqual(self.peek(a), 0x10)
        # a node not in the snapshot is refused
        self.assertRaises(SnapError, g.restore, "bonly", [a])
        self.assertIsNone(g.halted)

    def test_restore_refused_when_a_node_lost_it(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}})
        a, b = g.nodes["a"], g.nodes["b"]
        g.snap(None, "keep")
        self.poke(a, 0x55)
        b.rmm.call("snapshot.delete", tag="keep")    # behind the group's back
        with self.assertRaises(SnapError) as cm:
            g.restore("keep")
        self.assertIn("b no longer holds it", str(cm.exception))
        self.assertEqual(self.peek(a), 0x55)         # a was not touched
        self.assertTrue(wait_for(lambda: g.tree.recs[1].status["b"] == GONE, 6))

    def test_console_lines_and_group_name_target(self):
        g = self.make({"a": {"mock": True}, "b": {"mock": True}})
        self.assertEqual(len(g.split_target("@t x")[0]), 2)   # spec() names the group "t"
        g.run_line("@b snapshot.save tag=typed")
        self.assertTrue(wait_for(lambda: any(r.tag == "typed" for r in g.tree.recs.values())))
        rec = g.tree.find("typed")
        self.assertEqual((rec.nodes, rec.scope), (["b"], "console"))
        g.snap(None, "later")
        g.run_line("@b snapshot.restore tag=typed")
        self.assertTrue(wait_for(lambda: g.tree.heads["b"] == rec.id))
        self.assertEqual(g.tree.heads["a"], 2)


FAKE_86BOX = r'''
import os, signal, sys, time
sys.path.insert(0, %r)
from rmm_mock import MockServer
port = int(sys.argv[sys.argv.index("--monitor-port") + 1])
srv = MockServer(port); srv.start()
time.sleep(float(os.environ.get("CRASH_AFTER", "2")))
os.kill(os.getpid(), signal.SIGSEGV)
'''


HOOK_COUNT = """
import rmm_86box

def on_arm(hit):
    hit.state["armed"] = hit.kind + str(hit.id)
    hit.state["seen"] = []

def on_hit(hit):
    assert isinstance(hit.rmm, rmm_86box.Rmm)
    hit.state["seen"].append((hit.count, hit.reason, hit.addr, len(hit.read(hit.addr, 4)), "eax" in hit.regs()))
    if hit.count >= hit.args["stop_after"]:
        hit.clear()
    return "resume"

def on_disarm(hit):
    hit.state["disarmed"] = True
"""


class Hooks(MockGroup):
    """`hook` (rmmgui.hooks): a Python file's on_hit runs on each real stop event of the reference
    mock, on the node's worker, with rmm_86box importable and the guest paused under it."""

    def script(self, text, name="h.py"):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))
        return path

    def console(self, n, *words):
        """Run `hook ...` on the node's worker, like a console line, and return its output."""
        from rmmgui import hooks
        out = []
        n.put_call(lambda: out.append(hooks.console(n, list(words))))
        self.assertTrue(wait_for(lambda: out))
        return out[0]

    def test_on_hit_runs_resumes_and_clears(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        bp = n.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(wait_for(lambda: n.rmm.state() == "paused"))     # first stop, before the hook
        out = self.console(n, f"b{bp}", self.script(HOOK_COUNT), "stop_after=4")
        self.assertIn("armed (on_arm, on_hit, on_disarm)", out[0])
        rule = n.hit_rules[("break", bp)]
        hook = rule["hook"]
        self.assertEqual(hook.state["armed"], f"break{bp}")
        n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: hook.state.get("disarmed")), (hook.state, self.lines))
        self.assertEqual([s[0] for s in hook.state["seen"]], [2, 3, 4])      # hit 1 came before the hook
        self.assertTrue(all(s[1:] == ("breakpoint", LOOP_BP, 4, True) for s in hook.state["seen"]), hook.state)
        self.assertEqual((hook.runs, hook.errors), (3, 0))
        self.assertNotIn(("break", bp), n.hit_rules)
        self.assertEqual(n.rmm.call("debug.break.list")["breakpoints"], [])
        self.assertTrue(wait_for(lambda: n.rmm.state() == "running"))

    def test_error_stays_paused_then_reload_on_save(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        path = self.script("""
            def on_hit(hit):
                return 1 // 0
        """)
        bp = n.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.console(n, f"b{bp}", path)
        hook = n.hit_rules[("break", bp)]["hook"]
        n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: hook.errors == 1), self.lines)
        time.sleep(0.3)
        self.assertEqual(n.rmm.state(), "paused")                 # a broken hook never runs the guest on
        self.assertTrue(any("ZeroDivisionError" in t and "Traceback" in t or "1 // 0" in t
                            for _, tone, t in self.lines if tone == "err"), self.lines)
        with open(path, "w") as f:
            f.write("def on_hit(hit):\n    hit.state['ok'] = hit.state.get('ok', 0) + 1\n"
                    "    return 'stay' if hit.state['ok'] >= 2 else 'resume'\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: hook.state.get("ok") == 2), self.lines)
        self.assertTrue(any("reloaded" in t for _, _, t in self.lines))
        self.assertEqual(n.rmm.state(), "paused")                 # "stay"
        self.assertEqual((hook.runs, hook.errors), (2, 1))
        self.assertIn("stayed paused", hook.last)
        out = self.console(n)
        self.assertIn(f"b{bp}", out[0])
        self.assertIn("runs 2 errors 1", out[1])
        self.assertIn("last error: on_hit: ZeroDivisionError", out[2])

    def test_console_words(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        self.assertIn("no hooks", self.console(n)[0])
        self.assertIn("86Box has no watch #9", self.console(n, "w9", "x.py")[0])
        self.assertIn("w<ID>", self.console(n, "zz", "x.py")[0])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "starter.py")
        bp = n.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertIn("wrote", self.console(n, "new", path, f"b{bp}")[0])
        self.assertIn("exists", self.console(n, "new", path)[0])
        self.assertIn("armed", self.console(n, f"b{bp}", path)[0])     # the starter loads and arms as is
        hook = n.hit_rules[("break", bp)]["hook"]
        self.assertTrue(wait_for(lambda: n.rmm.state() == "paused"))     # the stop before the hook: by hand
        n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: hook.runs >= 3), self.lines)          # it resumes: hits keep coming
        self.assertIn("hook off", self.console(n, "off", f"b{bp}")[0])
        self.assertNotIn(("break", bp), n.hit_rules)
        self.assertTrue(wait_for(lambda: any("disarmed after" in t for _, _, t in self.lines)), self.lines)
        bad = self.script("x = 1\n", "nohit.py")
        self.assertIn("has no on_hit", self.console(n, f"b{bp}", bad)[0])


@unittest.skipIf(sys.platform.startswith("win"), "POSIX signals")
class Crash(unittest.TestCase):
    def test_segfault_halts_the_rest(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "fake86box")
        open(fake, "w").write("#!" + sys.executable + "\n" + FAKE_86BOX % os.path.abspath(CLIENT))
        os.chmod(fake, 0o755)
        lines = []
        g = HostGroup(spec({"m": {"mock": True},
                            "x": {"port": "auto", "launch": {"binary": fake, "vm_dir": d + "/x", "env": {"CRASH_AFTER": "2"},
                                                             "on_exit": "keep"},
                                  "box_cfg": {"General": {"vid_renderer": "qt_software"}}}},
                           policy={"halt_all": True, "halt_on": ["segfault"]}),
                      d, lambda t, tone, n: lines.append(t))
        self.addCleanup(g.stop)
        g.start()
        self.assertTrue(wait_for(lambda: all(n.connected for n in g.nodes.values())), lines)
        self.assertTrue(os.path.exists(os.path.join(d, "x", "86box.cfg")))
        self.assertTrue(wait_for(lambda: g.halted is not None, 10), lines)
        self.assertEqual((g.halted["by"], g.halted["reason"]), ("x", "crash"))
        self.assertIn("SIGSEGV", g.halted["detail"])
        self.assertTrue(wait_for(lambda: g.nodes["m"].rmm.state() == "paused"))
        self.assertEqual(g.nodes["x"].state, "crash")


@unittest.skipUnless(shutil.which("qemu-system-i386"), "qemu-system-i386 not installed")
class QemuNodes(MockGroup):
    """qemu nodes in a real HostGroup next to a mock: started headless, halt-all both ways,
    a crash seen as one, and attach leaving the QEMU running."""

    def test_launched_pause_and_crash_halt_the_mock(self):
        g = self.make({"m": {"mock": True}, "q": {"qemu": {"profile": "isapc", "wait": 10}}},
                      {"halt_all": True, "halt_on": ["pause", "crash"]})
        q, m = g.nodes["q"], g.nodes["m"]
        run_dir = q.proc.vm_dir
        self.assertTrue(os.path.exists(os.path.join(run_dir, "qmp.sock")))
        self.assertTrue(wait_for(lambda: q.dom_lines and "isapc" in q.dom_lines[1]), q.dom_lines)
        self.assertGreater(q.rmm.screenshot(step=4).w, 0)
        q.rmm.call("machine.pause")
        self.assertTrue(wait_for(lambda: g.halted is not None), self.lines)
        self.assertEqual((g.halted["by"], g.halted["reason"]), ("q", "pause"))
        self.assertTrue(wait_for(lambda: m.rmm.state() == "paused"))
        self.assertIsNone(q.regs)                 # no registers over QMP: nothing, not an error
        g.resume()
        self.assertTrue(wait_for(lambda: q.rmm.state() == m.rmm.state() == "running"))
        os.kill(q.proc.pid, signal.SIGSEGV)
        self.assertTrue(wait_for(lambda: g.halted is not None, 10), self.lines)
        self.assertEqual((g.halted["by"], g.halted["reason"]), ("q", "crash"))
        self.assertEqual(q.state, "crash")
        g.stop()
        self.assertFalse(os.path.exists(run_dir))  # a temp run dir goes with the node

    def test_attach_leaves_qemu_running(self):
        from rmm_86box.backends.qemu import QemuProcess
        p = QemuProcess("isapc", paused=False).start()
        self.addCleanup(p.stop)
        g = self.make({"s": {"qemu": {"profile": "isapc", "qmp": p.qmp_path}}})
        s = g.nodes["s"]
        self.assertEqual((s.spec.mode, s.proc), ("qemu-attach", None))
        out = []
        s.put_line("input.devices", out.extend)
        self.assertTrue(wait_for(lambda: out), self.lines)
        self.assertIn("relative", "\n".join(out))
        path = os.path.join(tempfile.mkdtemp(), "g.json")
        g.export(path)
        with open(path) as f:
            self.assertEqual(json.load(f)["nodes"]["s"], {"qemu": {"profile": "isapc", "qmp": p.qmp_path}})
        g.stop()
        self.assertIsNone(p.ended())


if __name__ == "__main__":
    unittest.main()
