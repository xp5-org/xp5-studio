# SPDX-License-Identifier: MIT
"""The window keeps its geometry when guests break and resume (the "every box jumps" bug).

Headless (Qt offscreen) over three in-process reference mocks, which is fine here: nothing is
shown to anyone, and the point is the window's own layout. It records the place and size of
every visible widget (inside lists, tables, text and graphics views only the view itself counts:
rows and scroll bars come and go there by design) on each page, drives the group through running -> a breakpoint
with halt-all -> a step -> resume all -> one node paused by hand -> b unlocked, rewound and
snapped alone -> b locked again, and asserts nothing moved.

    python3 -m unittest discover -s tests      (from rmm_demo/)
    RMM_LAYOUT_PNG=DIR ...                      also save every page in every state to DIR
"""

import argparse
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, "..")):
    sys.path.insert(0, os.path.abspath(p))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;*.warning=false")

try:
    from PySide6.QtWidgets import (QAbstractItemView, QApplication, QGraphicsView, QPlainTextEdit, QTextEdit,
                                   QWidget)
    SCROLLING = (QAbstractItemView, QGraphicsView, QPlainTextEdit, QTextEdit)
except ImportError:                                   # hosts without PySide6 skip the GUI tests
    QApplication = None

LOOP_BP = 0x0800 * 16 + 0x10F                         # the mock's MEMLOOP `inc` instruction


def args(store):
    return argparse.Namespace(no_system_clipboard=True, halt_all=None, vm_store=store, snaps_view=False,
                              title="layout test", size=(1280, 820), run=[], exit_after=0, save_png=None)


@unittest.skipIf(QApplication is None, "no PySide6")
class LayoutStable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from rmmgui.app import App
        from rmmgui.group import GroupSpec
        from rmmgui.shell import MainWindow
        from rmmgui import theme
        cls.qapp = QApplication.instance() or QApplication([])
        cls.qapp.setStyleSheet(theme.stylesheet())
        spec = GroupSpec.load(os.path.join(DEMO, "groups", "mock_trio.json"))
        spec.policy.halt_all = True
        spec.policy.halt_on = ["breakpoint"]
        cls.app = App(args(tempfile.mkdtemp()), spec)
        cls.win = MainWindow(cls.app, keep_layout=False)
        cls.app.win = cls.win
        cls.win.show()
        threading.Thread(target=cls.app._bg_loop, daemon=True).start()
        threading.Thread(target=cls.app.group.start, daemon=True).start()
        cls.png = os.environ.get("RMM_LAYOUT_PNG")

    @classmethod
    def tearDownClass(cls):
        cls.app.running = False
        cls.app.group.stop()
        cls.win.close()

    def pump(self, secs=0.6, until=None):
        deadline = time.time() + max(secs, 8 if until else 0)
        t_min = time.time() + secs
        while time.time() < deadline:
            self.app.balance_polling()
            self.win.refresh()
            self.qapp.processEvents()
            time.sleep(0.03)
            if time.time() >= t_min and (until is None or until()):
                return True
        return until is None

    def geometry(self):
        out = {}
        win = self.win
        for w in win.findChildren(QWidget):
            if not w.isVisible() or w.isWindow():
                continue
            p = w.parentWidget()
            inside = False
            while p is not None and p is not win:
                if isinstance(p, SCROLLING):
                    inside = True
                    break
                p = p.parentWidget()
            if inside:
                continue
            tl = w.mapTo(win, w.rect().topLeft())
            par = w.parentWidget()
            out[f"{type(w).__name__}@{id(w)}<{type(par).__name__}"] = \
                (tl.x(), tl.y(), w.width(), w.height())
        return out

    def capture(self, state):
        shots = {}
        for i in range(self.win.tabs.count()):
            self.win.tabs.setCurrentIndex(i)
            self.pump(0.4)
            shots[i] = self.geometry()
            if self.png:
                os.makedirs(self.png, exist_ok=True)
                self.win.grab().save(os.path.join(self.png, f"{state}_{i}.png"))
        self.win.tabs.setCurrentIndex(1)
        return shots

    def test_break_resume_moves_nothing(self):
        g = self.app.group
        self.assertTrue(self.pump(0.5, lambda: all(n.connected and n.status.get("state") == "running"
                                                   for n in g.nodes.values())), "mocks did not come up")
        self.app.select_node("b")
        states = {"running": self.capture("running")}
        b = g.nodes["b"]
        bp = b.rmm.call("debug.break.set", addr=LOOP_BP)["id"]
        self.assertTrue(self.pump(0.5, lambda: g.halted is not None and b.regs is not None), "no halt")
        states["halted"] = self.capture("halted")
        b.put_line("debug.step count=1")
        self.pump(0.8)
        states["stepped"] = self.capture("stepped")
        b.rmm.call("debug.break.clear", id=bp)
        self.app.submit("resume")
        self.assertTrue(self.pump(0.5, lambda: g.halted is None and b.state == "running"), "no resume")
        states["resumed"] = self.capture("resumed")
        self.app.select_node("a")
        self.app.select_node("b")
        b.put_line("machine.pause")
        self.assertTrue(self.pump(0.5, lambda: b.state == "paused"))
        states["paused"] = self.capture("paused")
        # lock / unlock (the Snapshot timeline dock is part of the window from the start)
        n0 = len(g.tree.recs)
        self.app.submit("snap")
        self.assertTrue(self.pump(0.3, lambda: len(g.tree.recs) > n0 and self.app.idle()), "no group snap")
        s1 = max(g.tree.recs)
        self.app.submit("unlock b")
        self.assertTrue(self.pump(0.3, lambda: "b" not in g.locked))
        self.app.submit(f"@b restore #{s1}")
        self.app.submit("@b snap b-own")
        self.assertTrue(self.pump(0.3, lambda: [r.tag for r in g.own_records("b")] == ["b-own"] and self.app.idle()),
                        self.app.last_block())
        states["unlocked"] = self.capture("unlocked")
        tips = [t for _, t, fn in self.win.timeline.player.hits if fn]
        self.assertTrue(any("restore b alone" in t for t in tips), tips)
        self.assertTrue(any("b is unlocked: click to lock" in t for t in tips), tips)
        self.app.submit("lock b")
        self.assertTrue(self.pump(0.3, lambda: "b" in g.locked))
        states["relocked"] = self.capture("relocked")
        base = states["running"]
        for name, shots in states.items():
            for page, geo in shots.items():
                with self.subTest(state=name, page=page):
                    moved = {k: (base[page].get(k), v) for k, v in geo.items() if base[page].get(k) != v}
                    self.assertEqual(moved, {}, f"{name}, page {page}: widgets moved or resized:\n" +
                                     "\n".join(f"  {k}: {a} -> {b}" for k, (a, b) in sorted(moved.items(), key=lambda kv: "<MainWindow" not in kv[0])[:14]))

    def test_every_page_and_dock_renders(self):
        self.pump(0.5)
        for i in range(self.win.tabs.count()):
            self.win.tabs.setCurrentIndex(i)
            self.pump(0.3)
            self.assertFalse(self.win.grab().isNull())
        tl = self.win.timeline
        tl.view_pick.setCurrentIndex(1)                       # the family tree view of the same dock
        self.pump(0.3)
        self.assertFalse(tl.grab().isNull())
        tl.view_pick.setCurrentIndex(0)
        self.win.break_out("console", "tasks", "settings", "timeline")
        self.pump(0.3)
        for d in self.win.docks.values():
            self.assertFalse(d.grab().isNull())
        self.win.dock_all()
        self.win.tabs.setCurrentIndex(1)
        self.pump(0.2)


    def test_settings_page_lists(self):
        """VM Settings fills every hardware choice from config.enums: a combo of display names
        storing internal names, keys at their default listed too, cpu_speed also setting cpu_multi."""
        from PySide6.QtWidgets import QComboBox
        from rmmgui.shell import SETTINGS
        page = self.win.pages[SETTINGS]
        self.win.tabs.setCurrentIndex(SETTINGS)
        self.assertTrue(self.pump(0.3, lambda: page.enums and page.cfg), "config.enums never arrived")
        names = [page.sections.item(i).text() for i in range(page.sections.count())]
        self.assertIn("Ports (COM & LPT)", names)              # no such section in the cfg: all defaults

        def row(section, key):
            page.sections.setCurrentRow(names.index(section))
            self.pump(0.1)
            for r in range(page.keys.rowCount()):
                if page.keys.item(r, 0).text() == key:
                    return r
            self.fail(f"{section}/{key} not shown")

        r = row("Machine", "cpu_speed")
        cb = page.keys.cellWidget(r, 1)
        self.assertIsInstance(cb, QComboBox)
        e = page.enums[("Machine", "cpu_speed")]
        self.assertEqual((cb.count(), cb.currentData()[0]), (len(e["values"]), e["value"]))
        cb.setCurrentIndex((cb.currentIndex() + 1) % cb.count())
        v, m = cb.currentData()
        self.assertEqual((page.edits[("Machine", "cpu_speed")], page.edits[("Machine", "cpu_multi")]), (v, f"{m:g}"))
        r = row("Sound", "sndcard2")
        self.assertEqual(page.keys.item(r, 2).text(), "list, default")
        cb = page.keys.cellWidget(r, 1)
        self.assertEqual(cb.currentData()[0], "none")
        pick = {cb.itemData(i)[0]: i for i in range(cb.count())}
        cb.setCurrentIndex(pick["sb16"])
        self.assertEqual(page.edits[("Sound", "sndcard2")], "sb16")
        cb.setCurrentIndex(pick["none"])                     # back to the default: no edit left
        self.assertNotIn(("Sound", "sndcard2"), page.edits)
        page._discard()
        self.win.tabs.setCurrentIndex(1)
        self.pump(0.2)

    def test_zz_project_round_trip(self):
        """Save project, open it again: same group, the same snapshot tree and file index
        (runs last: opening replaces the group)."""
        g = self.app.group
        self.assertTrue(self.pump(0.2, lambda: all(n.connected for n in g.nodes.values())))
        rec, _ = g.snap(None, "proj")
        self.app.snap_files[rec.id] = {"a": "/vm/a/x.rmmsnap"}
        g.unlock("c")
        path = os.path.join(tempfile.mkdtemp(), "p.rmmproj.json")
        self.app.save_project(path)
        before = g.tree.to_json()
        self.app.open_project(path)
        self.assertTrue(self.pump(0.2, lambda: self.app.group is not g and
                                  all(n.connected for n in self.app.group.nodes.values())))
        self.assertEqual(list(self.app.group.nodes), ["a", "b", "c"])
        self.assertEqual(self.app.group.tree.to_json()["snapshots"][-1]["tag"], "proj")
        self.assertEqual(self.app.group.tree.heads, before["heads"])
        self.assertEqual(self.app.snap_files, {rec.id: {"a": "/vm/a/x.rmmsnap"}})
        self.assertEqual(self.app.group.unlocked(), ["c"])
        # the new mocks never held "proj": the next snapshot.list marks it gone, not deleted
        self.assertTrue(self.pump(0.2, lambda: not self.app.group.tree.recs[rec.id].live()))


if __name__ == "__main__":
    unittest.main()
