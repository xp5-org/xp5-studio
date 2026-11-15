# SPDX-License-Identifier: MIT
"""The controller (Qt / PySide6): the App's actions and the main loop. State lives in the group
(hostgroup.py); the window is shell.py, its pages pages.py, the shared widgets view.py.

Threads: nodes and the group-line worker run on their own threads and only append to `out` or
change plain attributes; a 30 Hz timer on the GUI thread redraws from that state, so no widget
is touched from another thread. The few GUI calls a worker needs (the clipboard) go through
GuiCall.

Keys
  Enter run   Tab complete (again: next)   Up/Down history   Esc drop selection / clear line
  Ctrl+C copy (selected output lines, else the input selection, else the input line)
  Ctrl+X cut   Ctrl+V or Shift+Insert paste (a multi-line paste runs each full line)
  Ctrl+Shift+V type the clipboard into the selected guest   Ctrl+A select the line
  Shift+arrows/Home/End select   Ctrl+Left/Right word   Ctrl+Backspace/Delete word
  Ctrl+K / Ctrl+U cut to end / start   Ctrl+L clear the console
  PageUp/PageDown or the wheel scroll the console   Ctrl+End back to the bottom
  Ctrl+Tab / Ctrl+Shift+Tab next/previous node   Alt+1..9 node by position
  F1 grab  F2 poll on/off  F3/F4 rate  F5 pause/resume  F6 step
  F7 snap the node   Shift+F7 snap every node   F8 restore the node's last snap/restore
  Shift+F8 restore the picked snapshot (else the newest group one) on all its nodes
  F9 resume all   F10 pause all   F11 halt-all on/off   F12 Snapshots tab
Mouse
  click a node tab to select it; drag over console output to select lines. Snapshots tab: click a
  box to pick it (its buttons restore or delete it), wheel or drag pans.
"""

import collections
import glob
import json
import os
import queue
import re
import shlex
import threading
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from . import theme as T
from .clipboard import Clipboard
from .commands import GROUP_WORDS, Commands
from .group import GroupError, GroupSpec
from .hostgroup import HostGroup, TargetError
from .snaps import SnapError, SnapTree
from .tasks import TaskLog

from rmm_86box import RpcError, kvm
from rmm_86box.complete import Completion

PROJECT_SCHEMA = "rmm-project/1"


class Output(collections.deque):
    """The console lines (text, tone, node name or None). `total` counts every line ever added
    and `gen` every clear, so the view can append just what is new."""

    def __init__(self):
        super().__init__(maxlen=4000)
        self.total, self.gen = 0, 0

    def append(self, item):
        super().append(item)
        self.total += 1

    def clear(self):
        super().clear()
        self.gen += 1
        self.total = 0


class GuiCall(QObject):
    """Run a function on the GUI thread and wait for its result, from any thread."""
    _call = Signal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(self._run, Qt.QueuedConnection)

    def _run(self, job):
        fn, box, done = job
        try:
            box.append(fn())
        except Exception as e:  # noqa: BLE001
            box.append(e)
        done.set()

    def __call__(self, fn, timeout=2.0):
        if threading.current_thread() is threading.main_thread():
            return fn()
        box, done = [], threading.Event()
        self._call.emit((fn, box, done))
        if not done.wait(timeout) or isinstance(box[0], Exception):
            raise RuntimeError("GUI thread did not answer")
        return box[0]


class App:
    """The demo's state and actions; the window is MainWindow."""

    def __init__(self, a, spec):
        self.a = a
        self.out = Output()
        self.clip = Clipboard(use_system=not a.no_system_clipboard)
        self.hint = "Tab completes.  help  lists methods.  @name line  targets a node.  demo timetravel  rewinds the screen."
        self.cmds = Commands(self)
        self.bg = queue.Queue()                       # group-level lines, one at a time
        self.bg_busy = False
        self.bundle = None                            # bundle.Progress of the last export/import
        self.running = True
        self.show_snaps = bool(getattr(a, "snaps_view", False))
        self.snap_follow = True                      # keep the newest snapshot in view until panned
        self.tasks = TaskLog()
        self.snap_files = {}                         # snapshot record id -> {node: file on its host}
        self.project_path = None
        self.win = None
        self.group = self._make_group(spec)
        proj = getattr(a, "project_data", None)
        if proj:                                      # --project: the saved tree and lock state come back too
            self.project_path = os.path.abspath(a.project)
            if proj.get("tree"):
                self.group.tree = SnapTree.from_json(proj["tree"], self.group.nodes)
            self.group.set_lock_state(proj.get("lock"))
            self.snap_files = {int(k): dict(v) for k, v in (proj.get("files") or {}).items()}
        self.started = True

    def _make_group(self, spec):
        if self.a.halt_all is not None:
            spec.policy.halt_all = self.a.halt_all
        return HostGroup(spec, self.a.vm_store, self.say, self.cmds.for_node)

    # -- output ------------------------------------------------------------------------------

    def say(self, text, tone="text", node=None):
        grp = getattr(self, "group", None)
        if node is not None and grp is not None and len(grp.nodes) > 1:
            text = f"[{node.name}] {text}"
        self.out.append((text, tone, node.name if node is not None else None))
        if tone in ("ok", "warn", "err"):            # the Recent tasks dock
            target = node.name if node is not None else (grp.spec.name if grp is not None else None)
            self.tasks.add(target, text, tone)

    def last_block(self):
        """Output since the newest echoed command ("> ...")."""
        out = list(self.out)
        for i in range(len(out) - 1, -1, -1):
            if out[i][0].startswith("> "):
                return [t for t, _, _ in out[i + 1:]]
        return [t for t, _, _ in out]

    # -- running lines ---------------------------------------------------------------------

    @staticmethod
    def _words(args):
        try:
            return shlex.split(args)                 # quoted paths
        except ValueError:
            return args.split()

    def submit(self, line, echo=True):
        if echo:
            self.say("> " + line, "accent")
        try:
            targets, rest = self.group.split_target(line)
        except TargetError as e:
            self.say(f"error: {e}", "err")
            return
        explicit = line.lstrip().startswith("@")
        word, _, args = rest.partition(" ")
        if word in GROUP_WORDS:
            self.bg.put((word, self._words(args), targets if explicit else None))
        else:
            self.group.run_line(line)

    def _bg_loop(self):
        while self.running:
            try:
                word, args, targets = self.bg.get(timeout=0.2)
            except queue.Empty:
                continue
            self.bg_busy = True
            try:
                if word not in ("clear", "quit", "group", "use", "lock", "unlock"):
                    self._wait_nodes(targets)
                for t in self.cmds.run_group(word, args, targets):
                    self.say(t, "err" if t.startswith("error") else "text")
            except (GroupError, TargetError, SnapError, ValueError, KeyError, OSError) as e:
                self.say(f"error: {e}", "err")
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                self.say(f"error: {type(e).__name__}: {e}", "err")
            finally:
                self.bg_busy = False

    def _wait_nodes(self, targets, secs=15.0):
        """Group lines (--run, a group's "run") may come before the nodes are up: wait, as a
        node's own worker does, until each target is connected and has a status (or is gone)."""
        deadline = time.time() + secs
        nodes = targets or list(self.group.nodes.values())
        while self.running and time.time() < deadline:
            if all(n.ended or (n.connected and n.status) for n in nodes):
                return
            time.sleep(0.1)

    def idle(self):
        return (self.started and self.bg.empty() and not self.bg_busy and
                all(n.work.empty() and not n.busy for n in self.group.nodes.values()))

    def load_group_path(self, path):
        self.replace_group(GroupSpec.load(path))     # raises GroupError before touching anything

    def replace_group(self, spec, tree=None, files=None, lock=None):
        """Stop the running group and start `spec` (with a saved snapshot tree, file index and
        lock state)."""
        old = self.group
        self.say(f"stopping group {old.spec.name}", "muted")
        old.stop()
        self.group = self._make_group(spec)
        if tree:
            self.group.tree = SnapTree.from_json(tree, self.group.nodes)
        self.group.set_lock_state(lock)
        self.snap_files = {int(k): dict(v) for k, v in (files or {}).items()}
        self.snap_follow = True
        self.say(f"group {spec.name}: {', '.join(n.name for n in spec.nodes)}  ({spec.policy})", "ok")
        self.group.start()

    # -- projects ----------------------------------------------------------------------------

    def project_data(self):
        g = self.group
        return {"schema": PROJECT_SCHEMA, "group_path": g.spec.path,
                "group": dict(g.spec.raw, name=g.spec.name, policy=g.policy.to_spec()),
                "tree": g.tree.to_json(), "files": {str(k): v for k, v in self.snap_files.items()},
                "lock": g.lock_state()}

    def save_project(self, path):
        """The group, its snapshot tree and the snapshot files' paths on each node's host."""
        data = self.project_data()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        self.project_path = path
        self.say(f"project saved: {path}", "ok")
        return path

    def open_project(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema") != PROJECT_SCHEMA:
                raise GroupError(f"schema {data.get('schema')!r}: want {PROJECT_SCHEMA}")
            spec = GroupSpec(data["group"], data.get("group_path") or path)
        except (OSError, ValueError, KeyError) as e:
            self.say(f"error: open project {path}: {e}", "err")
            return
        self.project_path = path

        def go():
            self.replace_group(spec, data.get("tree"), data.get("files"), data.get("lock"))
        threading.Thread(target=go, name="open-project", daemon=True).start()

    def new_project(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            self.say("error: a project name is letters, digits, '.', '_' or '-'", "err")
            return
        # a group has at least one node: the reference mock stands in until a real one is added
        spec = GroupSpec({"schema": "rmm-group/1", "name": name, "nodes": {"m": {"mock": True}}})
        self.project_path = None
        self.say(f"new project {name}: add nodes with Attach or Launch (a mock stands in until then)", "muted")
        threading.Thread(target=lambda: self.replace_group(spec), name="new-project", daemon=True).start()

    # -- nodes -------------------------------------------------------------------------------

    def free_name(self):
        taken = set(self.group.nodes)
        i = len(taken) + 1
        while f"n{i}" in taken:
            i += 1
        return f"n{i}"

    def vm_configs(self):
        """What a launch can start from: VM config testlists, then 'domain:NAME' from the store."""
        out = []
        d = self.group.spec.vm_config_dir
        if d and os.path.isdir(d):
            out += sorted(os.path.basename(p)[len("__testlist__vm_"):-3] for p in glob.glob(os.path.join(d, "__testlist__vm_*.py")))
        out += ["domain:" + n for n in kvm.DomainStore(self.a.vm_store).list()]
        return out

    def add_node(self, name, body):
        def go():
            try:
                n = self.group.add_node(name, body)
                self.say(f"added node {name} ({n.spec.mode}, {n.address})", "ok")
                self.select_node(name)
            except (GroupError, OSError, RuntimeError, ValueError) as e:
                self.say(f"error: add node {name}: {e}", "err")
        threading.Thread(target=go, name=f"add-{name}", daemon=True).start()

    def node_line(self, line):
        self.group.node().put_line(line)

    def node_toggle(self, prop):
        node = self.group.node()

        def go():
            try:
                v = node.rmm.call(f"{prop}.get")["value"]
                r = node.rmm.call(f"{prop}.set", value=not v)
                node.say(f"{prop}: {'on' if r.get('value') else 'off'}", "ok")
            except (RpcError, OSError) as e:
                node.say(f"error: {prop}: {e}", "err")
        node.put_call(go)

    def _confirm(self, title, text):
        return QMessageBox.question(self.win, title, text) == QMessageBox.Yes

    def confirm_reset(self):
        node = self.group.node()
        if self._confirm("Hard reset", f"Hard-reset {node.name}? The guest loses whatever it was doing."):
            node.put_line("machine.reset hard=true")

    def confirm_quit(self):
        node = self.group.node()
        if self._confirm("Quit 86Box", f"Quit {node.name}'s 86Box process (machine.quit, no save prompt)?"):
            node.put_line("machine.quit")

    def load_config(self, name):
        node = self.group.node()
        if self._confirm("Load VM config", f"Stage VM config {name} on {node.name} and hard-reset it?"):
            node.put_line(f"vm load {name}")

    def complete(self, text):
        """Completion with @targets: '@' + Tab completes node names; after '@x ' it completes
        on node x's console."""
        grp = self.group
        if text.startswith("@"):
            if " " not in text:
                head = text[1:]
                pre, _, cur = head.rpartition(",")
                names = [n for n in list(grp.nodes) + ["*"] if n.startswith(cur)]
                base = "@" + (pre + "," if pre else "")
                return Completion(0, [base + n for n in names], "@node | @a,b | @*  then a line for those nodes")
            cut = text.index(" ") + 1
            try:
                targets, _ = grp.split_target(text)
            except TargetError as e:
                return Completion(cut, [], str(e))
            c = targets[0].console.complete(text[cut:]) if targets[0].console else Completion(cut, [])
            return Completion(c.start + cut, c.items, c.hint)
        node = grp.node()
        if node.console is None:
            return Completion(0, [], node.conn_msg)
        return node.console.complete(text)

    def update_hint(self, text=""):
        try:
            c = self.complete(text)
            if c.hint:
                self.hint = c.hint
        except Exception:  # noqa: BLE001
            pass

    # -- node helpers ----------------------------------------------------------------------

    def select_node(self, name):
        self.group.use(name)

    def cycle_node(self, d):
        names = list(self.group.nodes)
        i = names.index(self.group.current)
        self.select_node(names[(i + d) % len(names)])

    def step_rate(self, d):
        n = self.group.node()
        if not n.poller:
            return
        cur = n.poll_rate
        i = min(range(len(T.RATES)), key=lambda j: abs(T.RATES[j] - cur))
        n.poll_rate = T.RATES[max(0, min(len(T.RATES) - 1, i + d))]

    def balance_polling(self):
        """The selected node polls at its own rate; the others slow to thumbnails."""
        for n in self.group.nodes.values():
            if not n.poller:
                continue
            want = n.poll_rate if n.name == self.group.current else min(n.poll_rate, T.BG_POLL_RATE)
            if abs(n.poller.rate - want) > 1e-6:
                n.poller.rate = want

    def pick_group_snap(self):
        tree = self.group.tree
        rec = tree.recs.get(tree.selected)
        if rec and rec.live():
            return rec
        live = [r for r in tree.recs.values() if r.scope == "group" and r.live()]
        if not live:
            self.hint = "no group snapshot yet (Shift+F7 takes one)"
            return None
        return live[-1]

    # -- actions (keys and buttons) ----------------------------------------------------------

    def toggle_snaps(self):
        self.show_snaps = not self.show_snaps

    def toggle_poll(self):
        p = self.group.node().poller
        if p:
            p.enabled = not p.enabled

    def grab(self):
        node = self.group.node()
        if node.poller:
            node.put_call(node.poller.grab)

    def pause_resume(self):
        node = self.group.node()
        node.put_line("machine.resume" if node.status.get("state") == "paused" else "machine.pause")

    def resume_node(self):
        self.group.node().put_line("machine.resume")

    def pause_node(self):
        self.group.node().put_line("machine.pause")

    def step_one(self):
        self.group.node().put_line("debug.step count=1")

    def snap_button(self, line):
        self.snap_follow = True
        self.submit(line)

    def snap_node(self):
        self.submit(f"@{self.group.node().name} snap")

    def restore_picked(self):
        rec = self.pick_group_snap()
        if rec:
            self.submit(f"restore {rec.ref}")

    def restore_head(self):
        node = self.group.node()
        head = self.group.tree.heads.get(node.name)
        if head is not None:
            self.submit(f"@{node.name} restore #{head}")
        else:
            self.hint = f"{node.name} has no snapshot to go back to (F7 takes one)"

    def toggle_lock(self, name=None):
        name = name or self.group.current
        self.submit(f"{'unlock' if name in self.group.locked else 'lock'} {name}")

    def rollback(self, ref):
        """Roll the whole group back to `ref`, after saying what it throws away."""
        g = self.group
        try:
            rec, discard = g.rollback_plan(ref)
        except SnapError as e:
            self.say(f"error: {e}", "err")
            return
        lost = [f"{u}'s {', '.join(r.tag for r in recs)}" for u, recs in discard.items() if recs]
        text = (f"Restore every node ({', '.join(g.nodes)}) to {rec.ref} {rec.tag}?"
                + (f"\n\nDeletes {'; '.join(lost)} (saved since unlocking)." if lost else "")
                + ("\n\nLocks " + ", ".join(discard) + " again." if discard else ""))
        if self.win is None or self._confirm("Roll the group back", text):
            self.submit(f"rollback {rec.ref}")

    def toggle_halt_all(self):
        self.submit(f"group halt-all {'off' if self.group.policy.halt_all else 'on'}")

    def paste_to_guest(self):
        self.submit("paste")

    # -- main loop -------------------------------------------------------------------------

    def run(self):
        qapp = QApplication.instance() or QApplication([])
        qapp.setStyleSheet(T.stylesheet())
        gui = GuiCall()
        if QGuiApplication.platformName() != "offscreen":
            cb = qapp.clipboard()
            self.clip.use_qt(lambda t: gui(lambda: cb.setText(t)), lambda: gui(cb.text))
        from .shell import MainWindow
        offscreen = QGuiApplication.platformName() == "offscreen"
        win = MainWindow(self, keep_layout=not offscreen and not self.a.exit_after)
        self.win = win
        if win.too_big and not offscreen:
            win.showMaximized()
        else:
            win.show()
        threading.Thread(target=self._bg_loop, name="group-lines", daemon=True).start()
        g = self.group
        self.say(f"xp5-studio, group {g.spec.name}: " + ", ".join(f"{n.name} ({n.mode})" for n in g.spec.nodes)
                 + f";  {g.policy}", "muted")
        self.say("Try: Tab on an empty line, `help`, `nodes`, `@* machine.status`, `demo timetravel`, `vm show`", "muted")
        self.started = False

        def start():
            g.start()                                  # a launch "wait" may take a while
            for line in self.a.run:
                self.submit(line)
            self.started = True
        threading.Thread(target=start, name="group-start", daemon=True).start()
        t0 = time.time()

        def tick():
            if not self.running:
                qapp.quit()
                return
            if self.a.exit_after and time.time() - t0 > self.a.exit_after and self.idle():
                win.refresh()
                if self.a.save_png:
                    win.grab().save(self.a.save_png)
                qapp.quit()
                return
            self.balance_polling()
            win.refresh()
        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(33)
        qapp.exec()
        timer.stop()
        self.running = False
        self.group.stop()
