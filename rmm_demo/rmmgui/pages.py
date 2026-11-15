# SPDX-License-Identifier: MIT
"""The central pages (Qt): Summary, Screen, Monitor / Debug, Snapshots and VM Settings, each for
the selected node. Each has refresh(tick), called from the GUI timer.

RMM calls never run on the GUI thread: run_on() queues them on the node's own worker (behind
any console line) and returns a Job the page reads on a later tick. Panels for data 86Box cannot
report yet (memory/IO/disk maps, disassembly, devices) carry a "needs RMM method" badge and say
which plan step adds it (PLAN_qt_console.md, phase 3).
"""

import os
import re
import shlex
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from rmm_86box import RpcError, kvm

from . import hooks
from . import theme as T
from .hooks import fmt_args
from .symbols import SymbolError
from .snapview import SnapsPanel
from .rows import DetailTable, RowPanels
from .view import Elided, FrameView, RegsGrid, ScreenPanel, button, mono_font


# -- helpers ------------------------------------------------------------------------------------

class Job:
    """One RMM task on a node's worker: done / result / error, read by the GUI thread."""

    def __init__(self):
        self.done, self.result, self.error = False, None, None


def run_on(node, fn):
    job = Job()

    def go():
        try:
            job.result = fn()
        except Exception as e:  # noqa: BLE001 - reported by the page
            job.error = e
        finally:
            job.done = True
    if node.connected:
        node.put_call(go)
    else:
        job.error, job.done = RuntimeError(f"{node.name} is not connected ({node.state})"), True
    return job


def group(title, widget=None, layout=None):
    g = QGroupBox(title)
    lay = layout or QVBoxLayout()
    lay.setContentsMargins(6, 6, 6, 6)
    if widget is not None:
        lay.addWidget(widget)
    g.setLayout(lay)
    return g


def badge(live, text=None):
    b = QLabel(text or ("live" if live else "needs RMM method"))
    b.setObjectName("badgeLive" if live else "badgeNeed")
    return b


def needs(title, step, what, header=True):
    """A panel 86Box cannot fill yet: what it will show and which plan step adds the method.
    header=False leaves the badge and title to the row it sits in."""
    w = QWidget()
    v = QVBoxLayout(w)
    if header:
        row = QHBoxLayout()
        row.addWidget(badge(False))
        row.addWidget(QLabel(f"<b>{title}</b>"), 1)
        v.addLayout(row)
    t = QLabel(f"{what}<br><br>86Box does not report this over RMM yet: plan step {step} "
               f"(rmm_demo/PLAN_qt_console.md) adds the method, spec-tested on real 86Box first.")
    t.setWordWrap(True)
    t.setObjectName("muted")
    t.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    v.addWidget(t, 1)
    return w


def mono_text(lines=0):
    t = QPlainTextEdit()
    t.setReadOnly(True)
    t.setFont(mono_font())
    t.setLineWrapMode(QPlainTextEdit.NoWrap)
    if lines:
        t.setFixedHeight(t.fontMetrics().height() * lines + 12)
    return t


def set_text(widget, text):
    if widget.toPlainText() != text:
        widget.setPlainText(text)


class KV(QWidget):
    """Label / value rows with fixed geometry: values elide, rows never come and go."""

    def __init__(self, keys, value_width=None):
        super().__init__()
        g = QGridLayout(self)
        g.setContentsMargins(4, 2, 4, 2)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(2)
        self.v = {}
        for r, k in enumerate(keys):
            kl = QLabel(k)
            kl.setObjectName("muted")
            g.addWidget(kl, r, 0)
            self.v[k] = Elided("", value_width, mono=True)
            g.addWidget(self.v[k], r, 1)
        g.setColumnStretch(1, 1)
        g.setRowStretch(len(keys), 1)

    def set(self, key, text, colour=None):
        self.v[key].set(text, colour)


def table(headers, widths=(), stretch=-1):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(t.fontMetrics().height() + 6)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setShowGrid(False)
    t.setWordWrap(False)
    h = t.horizontalHeader()
    for c, w in enumerate(widths):
        t.setColumnWidth(c, w)
    h.setSectionResizeMode(stretch if stretch >= 0 else len(headers) - 1, QHeaderView.Stretch)
    return t


def fill(t, rows):
    """Replace a table's rows only when they changed (keeps the selection otherwise)."""
    rows = [tuple(str(v) for v in r) for r in rows]
    if getattr(t, "_rows", None) == rows:
        return
    t._rows = rows
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            it = QTableWidgetItem(v)
            it.setToolTip(v if len(v) > 24 else "")
            t.setItem(r, c, it)


def hexdump(data, base):
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02X}" for b in chunk)
        hx = hx[:23] + " " + hx[23:] if len(chunk) > 8 else hx
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{base + off:08X}  {hx:<48}  {asc}")
    return "\n".join(out)


def parse_int(s):
    s = s.strip().replace("_", "")
    if ":" in s:                                       # seg:off, real mode
        seg, off = s.split(":", 1)
        return int(seg, 16) * 16 + int(off, 16)
    return int(s, 0) if s.lower().startswith(("0x", "0o", "0b")) else int(s, 16)



# -- breakpoint panel text: what 86Box holds plus what this GUI knows (node.hit_rules) ------------

BP_ON_HIT = {
    "stop": "86Box pauses the guest (the default)",
    "rewrite": "86Box stores a value into the watched location and keeps running (watch w, len 1/2/4)",
    "restore": "this GUI restores a snapshot on the stop event and leaves the guest paused",
    "restore + resume": "this GUI restores a snapshot on the stop event and resumes the guest",
    "python script": "this GUI runs the file's on_hit(hit) on the stop event (import rmm_86box; return "
                     "'resume' to run on). Console: hook w3 FILE.py",
}


def bp_condition(kind, d):
    """The filter 86Box applies before a hit counts, in words."""
    if kind == "watch":
        if "from" in d or "to" in d:
            f = f"0x{d['from']:X}" if "from" in d else "any"
            t = f"0x{d['to']:X}" if "to" in d else "any"
            return f"{f} -> {t}"
        return ""
    if "calls" in d:
        return "contract" + ("" if d.get("stop", True) else " (log only)")
    return ""


def bp_on_hit(kind, d, rule):
    """What a hit leads to: 86Box's own action, else the rule this GUI (or a demo) keeps."""
    act = d.get("action")
    if act == "rewrite":
        return f"86Box: rewrite 0x{d.get('value', 0):X}, keeps running"
    if act == "arm":
        return "86Box: arm a child watch through the pointer written"
    if act == "disarm":
        return "86Box: disarm the child watches"
    if not rule or rule["on_hit"] == "stop":
        return "stop (pause guest)"
    if rule["on_hit"] == "script" and rule.get("hook"):
        h = rule["hook"]
        return h.describe() + (f" -> {h.last}" if h.last else "") + (f", {h.errors} err" if h.errors else "")
    if rule["by"] != "panel":
        return f"{rule['note']}  [{rule['by']}]"
    return f"restore {rule['tag']}" + (", resume" if rule["resume"] else ", stay paused")


def bp_hits(node, key, d, rule):
    """Stops this GUI saw for it (86Box's own count for rewrite), plus restores done."""
    n = d.get("hits", node.hits.get(key, 0)) if d.get("action") == "rewrite" else node.hits.get(key, 0)
    if rule and rule["on_hit"] == "restore":
        return f"{n} / {rule['restores']} rst"
    if rule and rule.get("hook"):
        return f"{n} / {rule['hook'].runs} py"
    return str(n)

class Page(QWidget):
    """A page follows the selected node; changed() is true on the tick the node changes."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._node = None

    def node(self):
        return self.app.group.node()

    def changed(self):
        n = self.node()
        if n is not self._node:
            self._node = n
            return True
        return False


# -- Summary ------------------------------------------------------------------------------------

class SummaryPage(Page):
    def __init__(self, app, images):
        super().__init__(app)
        self.images = images
        g = QGridLayout(self)
        self.preview = FrameView()
        g.addWidget(group("Console preview", self.preview), 0, 0, 2, 1)
        self.state = KV(["State", "Stopped by", "TSC", "TSC rate", "CS:EIP", "Turbo", "Halt-all"])
        g.addWidget(group("State", self.state), 0, 1)
        self.conn = KV(["Monitor", "Mode", "Connection", "Process", "VM dir", "Config file"])
        g.addWidget(group("Connection", self.conn), 0, 2)
        self.hw = mono_text()
        g.addWidget(group("Hardware (config.dump, KVM terms)", self.hw), 1, 1)
        mb = QWidget()
        mv = QVBoxLayout(mb)
        mv.setContentsMargins(0, 0, 0, 0)
        self.media = table(["Slot", "Image", "WP"], (70, 200, 30), stretch=1)
        mv.addWidget(self.media, 1)
        row = QHBoxLayout()
        row.addWidget(button("Insert…", self._insert))
        row.addWidget(button("Eject", self._eject))
        row.addWidget(button("Refresh", lambda: self._list_media(True)))
        row.addStretch(1)
        mv.addLayout(row)
        g.addWidget(group("Media (media.list)", mb), 1, 2)
        self.bundle = KV(["Operation", "Phase", "Working on", "Written", "Elapsed"])
        self.bundle_log = mono_text(5)
        bw = QWidget()
        bl = QHBoxLayout(bw)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.bundle, 1)
        bl.addWidget(self.bundle_log, 2)
        self.bundle_box = group("Project bundle (export / import)", bw)
        self.bundle_box.setVisible(False)
        g.addWidget(self.bundle_box, 2, 0, 1, 3)
        self.events = mono_text(4)
        g.addWidget(group("Events", self.events), 3, 0, 1, 3)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        g.setColumnStretch(2, 2)
        g.setRowStretch(1, 1)
        self.job = None
        self.cfg_path = {}
        self._media_t = 0

    def _bundle_refresh(self):
        b = self.app.bundle
        self.bundle_box.setVisible(b is not None)
        if b is None:
            return
        colour = {"running": T.WARN, "done": T.OK, "failed": T.ERR}[b.state]
        what = f"{b.op} {b.mode}".strip()
        self.bundle.set("Operation", f"{what}: {b.path}")
        self.bundle.set("Phase", b.phase if b.state == "running" else
                        (f"finished ({b.detail})" if b.state == "done" else f"FAILED: {b.detail}"), colour)
        self.bundle.set("Working on", b.detail if b.state == "running" and b.detail else "—")
        w = b.written()
        self.bundle.set("Written", f"{w / 1048576:,.0f} MB" if b.out_file else "—")
        self.bundle.set("Elapsed", f"{b.elapsed():.0f} s" + ("" if b.state != "running" else
                                                           f"  ({w / 1048576 / max(b.elapsed(), 1):.0f} MB/s)" if w else ""))
        set_text(self.bundle_log, "\n".join(f"{t}  {x}" for t, x, _ in list(b.log)[-5:]))

    def _list_media(self, force=False):
        n = self.node()
        if self.job and not self.job.done and not force:
            return
        self._media_t = time.time()

        def go():
            out = {"media": n.rmm.call("media.list")["slots"]}
            if n.name not in self.cfg_path:
                out["path"] = n.rmm.call("config.dump", section="General").get("path", "")
            return out
        self.job = run_on(n, go)
        self.job.node = n

    def _slot(self):
        r = self.media.currentRow()
        rows = getattr(self.media, "_rows", [])
        return rows[r][0] if 0 <= r < len(rows) else None

    def _insert(self):
        slot = self._slot()
        if not slot:
            self.app.hint = "pick a media slot first"
            return
        path, ok = QInputDialog.getText(self, f"Insert into {slot}",
                                        "Image path on the 86Box host (the file must exist there):")
        if ok and path.strip():
            self.app.submit(f"@{self.node().name} {slot}.insert path={path.strip()}")
            self._media_t = 0

    def _eject(self):
        slot = self._slot()
        if slot:
            self.app.submit(f"@{self.node().name} {slot}.eject")
            self._media_t = 0

    def refresh(self, tick):
        n = self.node()
        if self.changed():
            self.job = None
            self._media_t = 0
        f = n.poller.latest if n.poller else None
        self.preview.show_image(self.images.get(n, f) if f is not None else None, "" if f else n.conn_msg)
        st = n.status
        self.state.set("State", n.state, T.state_colour(n.state))
        self.state.set("Stopped by", n.stop_note or "—", T.WARN if n.stop_note else T.MUTED)
        self.state.set("TSC", f"{st.get('tsc', 0):,}" if st else "—")
        self.state.set("TSC rate", f"{n.tsc_rate / 1e6:.1f} M/s" if st else "—")
        self.state.set("CS:EIP", f"{st['cs']:04X}:{st['eip']:08X}" if "cs" in st and "eip" in st else "—")
        self.state.set("Turbo", "—" if n.turbo is None else ("ON (unthrottled)" if n.turbo else "off"),
                       T.WARN if n.turbo else T.MUTED)
        pol = self.app.group.policy
        self.state.set("Halt-all", ("on: " + ",".join(pol.halt_on)) if pol.halt_all else "off")
        s = n.spec
        self.conn.set("Monitor", n.address)
        self.conn.set("Mode", {"attach": "remote (attached)", "launch": "local (launched here)",
                               "qemu": "QEMU (headless; started here or attached over QMP)"}.get(s.mode, s.mode))
        self.conn.set("Connection", n.conn_msg)
        self.conn.set("Process", f"pid {n.proc.pid}" if n.proc else "not launched by this console")
        self.conn.set("VM dir", n.proc.vm_dir if n.proc else os.path.dirname(self.cfg_path.get(n.name, "")) or "—")
        self.conn.set("Config file", self.cfg_path.get(n.name) or "—")
        set_text(self.hw, "\n".join(n.dom_lines) or "(not read yet)")
        set_text(self.events, "\n".join(list(n.events)[:20]))
        self._bundle_refresh()
        if n.connected and time.time() - self._media_t > 5:
            self._list_media()
        j = self.job
        if j and j.done and getattr(j, "node", None) is n:
            if j.error:
                fill(self.media, [("—", f"media.list: {j.error}", "")])
            else:
                if j.result.get("path"):
                    self.cfg_path[n.name] = j.result["path"]
                fill(self.media, [(f"{m['dev']}.{m['slot']}", m.get("path") or "(empty)", "wp" if m.get("wp") else "")
                                  for m in j.result["media"]])
            self.job = None


# -- Screen -------------------------------------------------------------------------------------

class ScreenPage(Page):
    """The guest screen with the registers beside it; the console is its own dock."""

    def __init__(self, app, images):
        super().__init__(app)
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 4, 4, 4)
        self.screen = ScreenPanel(app, images)
        h.addWidget(self.screen, 1)
        side = QVBoxLayout()
        self.regs = RegsGrid(app)
        side.addWidget(group("Registers", self.regs))
        self.snaps = Elided("", self.regs.width(), name="muted")
        side.addWidget(self.snaps)
        side.addStretch(1)
        h.addLayout(side)

    def refresh(self, tick):
        self.screen.refresh()
        self.regs.refresh()
        n = self.node()
        self.snaps.set("snapshots: " + (" ".join(n.snaps[-6:]) or "none (F7 saves one)"))


# -- Monitor / Debug ----------------------------------------------------------------------------

class MonitorPage(Page):
    """Rows (rows.RowPanels): drag a title to move or tile a row, the arrow collapses it, the
    Panels menu brings hidden rows back. The rows 86Box cannot fill yet start collapsed."""

    def __init__(self, app):
        super().__init__(app)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(2)
        self.rows = RowPanels("monitor")
        self.regs = RegsGrid(app, across=4)
        regs = QWidget()
        rh = QHBoxLayout(regs)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.regs, 0, Qt.AlignTop)
        rh.addStretch(1)
        R = self.rows.add
        R("regs", "Registers", regs, badge(True))
        R("bps", "Breakpoints and watchpoints", self._bp_panel(), badge(True))
        R("hex", "Memory (hex)", self._hex_panel(), badge(True))
        R("io", "I/O ports", self._io_panel(), badge(True), collapsed=True)
        for key, title, step, what in (
                ("disasm", "Disassembly @ CS:EIP", "3.6", "Instructions around CS:EIP with the breakpoints "
                 "marked; step over and run-to-return need it too."),
                ("memmap", "Memory map", "3.6 (mem.map)", "86Box's mem_mapping list: base, size, owner, "
                 "read/write/exec, shadow state; a bar over the first MB and the RAM above, double-click "
                 "opens the Hex row there."),
                ("iomap", "I/O map", "3.6 (io.map)", "Which device owns each port range (io_sethandler), "
                 "with its read and write handlers."),
                ("diskmap", "Disk map", "3.4 / 3.6", "Per drive: geometry, the partition table and a sector "
                 "viewer."),
                ("devices", "Devices", "3.6 (devices.list)", "Every device 86Box instantiated, with its I/O, "
                 "IRQ and DMA.")):
            R(key, title, needs(title, step, what, header=False), badge(False), collapsed=True)
        self.rows.done()
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        hint = Elided("drag a title to move or tile a row; the arrow (or a double-click) folds it", name="muted")
        top.addWidget(hint, 1)
        pb = QToolButton()
        pb.setText("Panels")
        pb.setPopupMode(QToolButton.InstantPopup)
        pb.setMenu(self.rows.menu(pb))
        top.addWidget(pb)
        v.addLayout(top)
        v.addWidget(self.rows, 1)
        self.bp_job = self.mem_job = self.io_job = None
        self._stop_seen = None
        self._bp_tick = 0

    # breakpoints
    def _bp_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.bps = table(["Kind", "Id", "Address", "Len", "Label", "Condition", "On hit", "Hits"],
                         (70, 40, 90, 40, 140, 80, 200, 70), stretch=6)
        self.bp_raw = []
        self.bp_detail = DetailTable(self.bps, self._bp_text)
        v.addWidget(self.bp_detail, 1)
        row = QHBoxLayout()
        self.bp_addr = QLineEdit()
        self.bp_addr.setPlaceholderText("linear addr: hex, seg:off, or a symbol (sym load)")
        self.bp_addr.setFont(mono_font())
        self.bp_kind = QComboBox()
        self.bp_kind.addItems(["exec", "watch w", "watch r", "watch rw"])
        self.bp_len = QSpinBox()
        self.bp_len.setRange(1, 4096)
        self.bp_len.setToolTip("watchpoint length in bytes")
        self.bp_label = QLineEdit()
        self.bp_label.setPlaceholderText("label (what the address is)")
        self.bp_label.setToolTip("Kept by this GUI, not by 86Box: shown in the Label column")
        row.addWidget(self.bp_addr, 1)
        row.addWidget(self.bp_kind)
        row.addWidget(self.bp_len)
        row.addWidget(self.bp_label, 1)
        row.addWidget(button("Add", self._bp_add))
        row.addWidget(button("Clear", self._bp_clear))
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.bp_from = QLineEdit()
        self.bp_from.setPlaceholderText("from")
        self.bp_to = QLineEdit()
        self.bp_to.setPlaceholderText("to")
        for e in (self.bp_from, self.bp_to):
            e.setFont(mono_font())
            e.setMaximumWidth(80)
            e.setToolTip("watch w, len 1/2/4 only: fire only when the value goes from -> to (hex; either may be "
                         "left empty = any). 86Box checks it, so a write that does not match never stops the guest.")
        self.bp_hit = QComboBox()
        self.bp_hit.addItems(list(BP_ON_HIT))
        self.bp_hit.setToolTip("\n".join(f"{k}: {d}" for k, d in BP_ON_HIT.items()))
        self.bp_arg = QLineEdit()
        self.bp_arg.setFont(mono_font())
        self.bp_hit.currentTextChanged.connect(self._bp_hit_changed)
        row2.addWidget(QLabel("when"))
        row2.addWidget(self.bp_from)
        row2.addWidget(QLabel("->"))
        row2.addWidget(self.bp_to)
        row2.addWidget(QLabel("on hit"))
        row2.addWidget(self.bp_hit)
        row2.addWidget(self.bp_arg, 1)
        self.bp_pick = button("File...", self._bp_pick_script)
        self.bp_pick.setToolTip("pick the hook script (a .py with on_hit(hit))")
        row2.addWidget(self.bp_pick)
        v.addLayout(row2)
        self._bp_hit_changed(self.bp_hit.currentText())
        self.bp_msg = Elided("", name="muted")
        v.addWidget(self.bp_msg)
        return w

    def _bp_fill(self, f):
        """Type a demo's breakpoint into the Add form and press Add, so the viewer sees where every
        parameter goes (address, kind, length, from -> to, on hit, snapshot tag, label)."""
        self.bp_addr.setText(f"{f['addr']:X}")
        self.bp_kind.setCurrentText(f.get("kind", "watch w"))
        self.bp_len.setValue(f.get("len", 1))
        self.bp_from.setText(f"{f['from']:X}" if "from" in f else "")
        self.bp_to.setText(f"{f['to']:X}" if "to" in f else "")
        self.bp_hit.setCurrentText(f.get("on_hit", "stop"))
        self.bp_arg.setText(f.get("arg", ""))
        self.bp_label.setText(f.get("label", ""))
        self._bp_add()

    def _bp_hit_changed(self, hit):
        arg = {"rewrite": "value to store (hex)", "restore": "snapshot tag (saved now if missing)",
               "restore + resume": "snapshot tag (saved now if missing)",
               "python script": "script.py [key=value ...]   (hook new FILE.py in the console writes one)"}.get(hit)
        self.bp_arg.setEnabled(arg is not None)
        self.bp_arg.setPlaceholderText(arg or "")
        if hasattr(self, "bp_pick"):
            self.bp_pick.setEnabled(hit == "python script")

    def _bp_pick_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "Hook script", os.getcwd(), "Python (*.py)")
        if path:
            rest = self.bp_arg.text().split()[1:]
            self.bp_arg.setText(" ".join([path] + rest))

    def _bp_list(self):
        n = self.node()
        if self.bp_job and not self.bp_job.done:
            return
        self.bp_job = run_on(n, lambda: (n.rmm.call("debug.break.list")["breakpoints"],
                                         n.rmm.call("debug.watch.list")["watchpoints"]))
        self.bp_job.node = n

    def _bp_text(self, r):
        """The notepad box for row r: every field 86Box reported, one per line, then what this GUI
        knows about it (label, what a hit leads to and who runs that, hits, the last stop)."""
        if not 0 <= r < len(self.bp_raw):
            return ""
        kind, d = self.bp_raw[r]
        lines = [f"{'breakpoint (exec)' if kind == 'break' else 'watchpoint ' + str(d.get('kind', ''))}  #{d.get('id')}"]
        for k, val in d.items():
            if k in ("id", "kind"):
                continue
            if k == "addr":
                val = f"0x{val:08X}  (linear {val})"
            lines.append(f"  {k:<12} {val}")
        n = self.node()
        if isinstance(d.get("addr"), int) and n.symbols.tables:
            nm = n.symbols.name_of(d["addr"], with_table=True)
            lines.append(f"  {'symbol':<12} {nm}  (file address {n.symbols.file_addr(d['addr'])})" if nm
                         else f"  {'symbol':<12} none covers it")
        key = (kind, int(d.get("id", -1)))
        rule = n.hit_rules.get(key)
        lines.append("")
        lines.append(f"  {'condition':<12} {bp_condition(kind, d) or 'every ' + ('execution' if kind == 'break' else 'access')}")
        lines.append(f"  {'on hit':<12} {bp_on_hit(kind, d, rule)}")
        if rule:
            if rule["label"]:
                lines.append(f"  {'label':<12} {rule['label']}")
            if rule.get("hook"):
                h = rule["hook"]
                lines.append(f"  {'run by':<12} this GUI: {h.path}")
                lines.append(f"  {'callbacks':<12} {', '.join(h.funcs)}" + (f"   args {fmt_args(h.args)}" if h.args else ""))
                lines.append(f"  {'runs':<12} {h.runs}   errors {h.errors}" + (f"   restores {h.restores}" if h.restores else ""))
                if h.last:
                    lines.append(f"  {'last run':<12} {h.last}")
                if h.last_error:
                    lines.append(f"  {'last error':<12} {h.last_error}")
                if h.state:
                    lines.append(f"  {'state':<12} " + ", ".join(f"{k}={v!r}"[:40] for k, v in list(h.state.items())[:8]))
            else:
                lines.append(f"  {'run by':<12} " + ("this GUI (on the stop event)" if rule["by"] == "panel"
                                                     else f"{rule['by']} (its own loop; 86Box only stops)"))
        lines.append(f"  {'hits':<12} {bp_hits(n, key, d, rule)}")
        last = n.last_hit.get(key)
        if last:
            lines.append("  last stop    " + " ".join(f"{k}={hex(v) if k in ('addr', 'eip') and isinstance(v, int) else v}"
                                                    for k, v in last.items()))
        return "\n".join(lines)

    def _bp_add(self):
        n = self.node()
        try:
            addr = n.symbols.parse(self.bp_addr.text())
        except SymbolError as e:
            self.bp_msg.set(f"address: hex (0x810F, 810F), seg:off (0800:010F) or a symbol: {e}", T.ERR)
            return
        kind = self.bp_kind.currentText()
        hit = self.bp_hit.currentText()
        label = self.bp_label.text().strip()
        if not label and n.symbols.is_symbol(self.bp_addr.text()):
            label = self.bp_addr.text().strip()          # the name typed is what the address is
        arg = self.bp_arg.text().strip()
        extra = {}
        try:
            for key, e in (("from", self.bp_from), ("to", self.bp_to)):
                if e.text().strip():
                    extra[key] = parse_int(e.text())
            if hit == "rewrite":
                extra.update(action="rewrite", value=parse_int(arg))
        except ValueError:
            self.bp_msg.set("from / to / value: hex numbers", T.ERR)
            return
        if extra and kind == "exec":
            self.bp_msg.set("from / to / rewrite are for watch w only", T.ERR)
            return
        tag = arg if hit.startswith("restore") else ""
        if hit.startswith("restore") and not tag:
            self.bp_msg.set("restore: give the snapshot tag to go back to", T.ERR)
            return
        script = None
        if hit == "python script":
            words = arg.split()
            try:
                if not words:
                    raise hooks.HookError("python script: give the .py file (and key=value arguments)")
                script = (words[0], hooks.parse_args(words[1:]))
            except hooks.HookError as e:
                self.bp_msg.set(str(e), T.ERR)
                return
        if kind == "exec":
            set_fn = lambda: n.rmm.call("debug.break.set", addr=addr)  # noqa: E731
        else:
            length = self.bp_len.value()                # read now: the worker runs fn after the widget may have changed
            set_fn = lambda: n.rmm.call("debug.watch.set", addr=addr, len=length, kind=kind.split()[1], **extra)  # noqa: E731

        def fn():
            if tag and tag not in [s["tag"] for s in n.rmm.call("snapshot.list")["snapshots"]]:
                n.rmm.call("snapshot.save", tag=tag)     # the state a hit goes back to: now
                n.say(f"saved snapshot {tag}: a hit restores this state", "muted")
            ident = set_fn()["id"]
            if script:
                key = ("break" if kind == "exec" else "watch", ident)
                try:
                    hooks.attach(n, key, script[0], script[1], by="panel", label=label)
                except hooks.HookError as e:
                    n.say(f"{hooks.ref_of(key)} is set but has no hook: {e}", "err")
            elif label or tag:
                n.set_hit_rule("break" if kind == "exec" else "watch", ident, label=label,
                               on_hit="restore" if tag else "stop", tag=tag, resume=(hit == "restore + resume"))
        self._bp_do(n, fn, f"{kind} @0x{addr:X}" + (f" -> {hit} {arg}" if hit != "stop" else ""))

    def _bp_clear(self):
        r = self.bps.currentRow()
        rows = getattr(self.bps, "_rows", [])
        if not 0 <= r < len(rows):
            self.bp_msg.set("pick a row to clear", T.MUTED)
            return
        kind, ident = rows[r][0], int(rows[r][1])
        n = self.node()
        meth = "debug.break.clear" if kind == "exec" else "debug.watch.clear"
        self._bp_do(n, lambda: n.rmm.call(meth, id=ident), f"clear {kind} #{ident}")

    def _bp_do(self, n, fn, what):
        def go():
            fn()
            return (n.rmm.call("debug.break.list")["breakpoints"], n.rmm.call("debug.watch.list")["watchpoints"])
        self.bp_job = run_on(n, go)
        self.bp_job.node, self.bp_job.what = n, what

    # hex
    def _hex_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.mem_space = QComboBox()
        self.mem_space.addItems(["physical", "linear (paused)"])
        self.mem_addr = QLineEdit("0")
        self.mem_addr.setFont(mono_font())
        self.mem_addr.setPlaceholderText("address: hex, seg:off or a symbol")
        self.mem_addr.returnPressed.connect(self._mem_read)
        self.mem_len = QSpinBox()
        self.mem_len.setRange(16, 65536)
        self.mem_len.setSingleStep(256)
        self.mem_len.setValue(256)
        row.addWidget(self.mem_space)
        row.addWidget(self.mem_addr, 1)
        row.addWidget(QLabel("bytes"))
        row.addWidget(self.mem_len)
        row.addWidget(button("Read", self._mem_read))
        row.addWidget(button("At CS:EIP", self._mem_at_ip))
        v.addLayout(row)
        self.hex = mono_text()
        v.addWidget(self.hex, 1)
        wr = QHBoxLayout()
        self.mem_data = QLineEdit()
        self.mem_data.setFont(mono_font())
        self.mem_data.setPlaceholderText("hex bytes to write at the address (physical RAM, paused only), e.g. 90 90 CC")
        wr.addWidget(self.mem_data, 1)
        wr.addWidget(button("Write", self._mem_write))
        v.addLayout(wr)
        self.mem_msg = Elided("", name="muted")
        v.addWidget(self.mem_msg)
        return w

    def _mem_at_ip(self):
        r = self.node().regs or {}
        if "cs" in r and "eip" in r:
            base = r.get("cs_base", r["cs"] * 16)
            self.mem_space.setCurrentIndex(1)
            self.mem_addr.setText(f"{base + r['eip']:X}")
            self._mem_read()
        else:
            self.mem_msg.set("no registers yet: pause the node first", T.WARN)

    def _mem_read(self):
        n = self.node()
        try:
            addr = n.symbols.parse(self.mem_addr.text())
        except SymbolError as e:
            self.mem_msg.set(f"address: hex, seg:off or a symbol: {e}", T.ERR)
            return
        length, lin = self.mem_len.value(), self.mem_space.currentIndex() == 1
        meth = "mem.lin.read" if lin else "mem.phys.read"

        def go():
            out = bytearray()
            while len(out) < length:
                k = min(4096, length - len(out))
                out += bytes.fromhex(n.rmm.call(meth, addr=addr + len(out), len=k)["data"])
            return addr, bytes(out)
        self.mem_job = run_on(n, go)
        self.mem_job.node, self.mem_job.what = n, f"{meth} 0x{addr:X} +{length}"

    def _mem_write(self):
        n = self.node()
        try:
            addr = n.symbols.parse(self.mem_addr.text())
            data = bytes.fromhex(self.mem_data.text().replace(" ", ""))
        except (ValueError, SymbolError):
            self.mem_msg.set("write: an address and hex bytes", T.ERR)
            return
        if not data:
            return
        length = max(16, self.mem_len.value())

        def go():
            n.rmm.call("mem.phys.write", addr=addr, data=data.hex())
            return addr, bytes.fromhex(n.rmm.call("mem.phys.read", addr=addr, len=min(4096, length))["data"])
        self.mem_space.setCurrentIndex(0)
        self.mem_job = run_on(n, go)
        self.mem_job.node, self.mem_job.what = n, f"wrote {len(data)} byte(s) at 0x{addr:X}"

    # io
    def _io_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        warn = QLabel("A read has the device's side effects (a UART read pops its FIFO). Paused only.")
        warn.setObjectName("muted")
        row.addWidget(warn, 1)
        v.addLayout(row)
        form = QHBoxLayout()
        self.io_port = QLineEdit("3F8")
        self.io_port.setFont(mono_font())
        self.io_size = QComboBox()
        self.io_size.addItems(["1", "2", "4"])
        self.io_val = QLineEdit()
        self.io_val.setFont(mono_font())
        self.io_val.setPlaceholderText("value to write (hex)")
        form.addWidget(QLabel("port"))
        form.addWidget(self.io_port)
        form.addWidget(QLabel("size"))
        form.addWidget(self.io_size)
        form.addWidget(button("Read", self._io_read))
        form.addWidget(self.io_val, 1)
        form.addWidget(button("Write", self._io_write))
        v.addLayout(form)
        self.io_log = mono_text()
        v.addWidget(self.io_log, 1)
        return w

    def _io(self, write):
        n = self.node()
        try:
            port = int(self.io_port.text(), 16)
            val = int(self.io_val.text(), 16) if write else None
        except ValueError:
            self.io_log.appendPlainText("port and value are hex")
            return
        size = int(self.io_size.currentText())
        if write:
            fn = lambda: n.rmm.call("io.write", port=port, value=val, size=size)  # noqa: E731
        else:
            fn = lambda: n.rmm.call("io.read", port=port, size=size)  # noqa: E731
        self.io_job = run_on(n, fn)
        self.io_job.node = n

    def _io_read(self):
        self._io(False)

    def _io_write(self):
        self._io(True)

    def refresh(self, tick):
        n = self.node()
        if self.changed():
            set_text(self.hex, "")
            self._bp_list()
        self.regs.refresh()
        if n.stop_note != self._stop_seen:              # a stop may come from a breakpoint: recount hits
            self._stop_seen = n.stop_note
            self._bp_list()
        if n.want_bp_form:                              # a demo (gamesnap) arms through this form, on screen
            self._bp_fill(n.want_bp_form)
            n.want_bp_form = None
        if tick - self._bp_tick >= 30:                  # a demo or script arms things behind the panel: poll
            self._bp_tick = tick
            self._bp_list()
        j = self.bp_job
        if j and j.done and j.node is n:
            self.bp_job = None
            if j.error:
                self.bp_msg.set(f"{getattr(j, 'what', 'list')}: {j.error}", T.ERR)
            else:
                bps, wps = j.result
                self.bp_raw = [("break", b) for b in bps] + [("watch", w_) for w_ in wps]
                j.node.prune_hit_rules({(k, int(d["id"])) for k, d in self.bp_raw})
                rows = []
                for k, d in self.bp_raw:
                    key = (k, int(d["id"]))
                    rule = j.node.hit_rules.get(key)
                    label = ((rule or {}).get("label", "") or (f"child of rule #{d['rule']}" if "rule" in d else "")
                             or j.node.symbols.name_of(d["addr"]) or "")
                    rows.append(("exec" if k == "break" else f"watch {d['kind']}", d["id"], f"{d['addr']:08X}",
                                 d.get("len", "") if k == "watch" else "", label, bp_condition(k, d),
                                 bp_on_hit(k, d, rule), bp_hits(j.node, key, d, rule)))
                fill(self.bps, rows)
                self.bp_detail.update_detail()
                if hasattr(j, "what"):
                    self.bp_msg.set(f"{j.what}: ok", T.OK)
        j = self.mem_job
        if j and j.done and j.node is n:
            self.mem_job = None
            if j.error:
                self.mem_msg.set(f"{j.what}: {j.error}", T.ERR)
            else:
                addr, data = j.result
                set_text(self.hex, hexdump(data, addr))
                self.mem_msg.set(f"{j.what}: ok", T.OK)
        j = self.io_job
        if j and j.done and j.node is n:
            self.io_job = None
            if j.error:
                self.io_log.appendPlainText(f"error: {j.error}")
            else:
                r = j.result
                self.io_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  port {r['port']:04X}  size {r['size']}  "
                                            f"value {r['value']:0{r['size'] * 2}X}")


# -- Snapshots ----------------------------------------------------------------------------------

PROJECT_SCHEMA = "rmm-project/1"


class SnapshotsPage(Page):
    """The family tree (snapview) and, beside it, the picked snapshot's files: each member
    node's 86Box writes its part with snapshot.export to a file on its own host (next to its
    cfg), and snapshot.load reads it back. A project file keeps the tree and where the files are."""

    def __init__(self, app):
        super().__init__(app)
        h = QSplitter(Qt.Horizontal)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(h)
        self.tree = SnapsPanel(app)
        h.addWidget(self.tree)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(4, 0, 0, 0)
        self.files = table(["Snapshot", "Node", "File on the node's host"], (90, 50), stretch=2)
        sv.addWidget(group("Snapshot files (snapshot.export / snapshot.load)", self.files), 1)
        row = QHBoxLayout()
        row.addWidget(button("Store picked to files", self._export))
        row.addWidget(button("Load picked from files", self._load))
        sv.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("Save project…", self._save_project))
        row.addWidget(button("Open project…", self._open_project))
        sv.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("Export bundle…", self._export_bundle))
        row.addWidget(button("Import bundle…", self._import_bundle))
        sv.addLayout(row)
        self.msg = Elided("", name="muted")
        sv.addWidget(self.msg)
        note = QLabel("Snapshots live inside each 86Box (at most 32, lost when it exits). Store writes the picked "
                      "one to a file on every member's own host; Load reads it back into that 86Box. "
                      "A project file (rmm-project/1) keeps the group, the tree and these file paths.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        sv.addWidget(note)
        h.addWidget(side)
        h.setSizes([800, 420])
        self.job = None
        self.vm_dirs = {}

    def _picked(self):
        tree = self.app.group.tree
        rec = tree.recs.get(tree.selected)
        if rec is None:
            self.msg.set("pick a snapshot in the tree first", T.WARN)
        return rec

    def _vm_dir(self, n):
        if n.proc:
            return n.proc.vm_dir
        if n.name not in self.vm_dirs:
            self.vm_dirs[n.name] = os.path.dirname(n.rmm.call("config.dump", section="General").get("path", ""))
        return self.vm_dirs[n.name]

    def _export(self):
        rec = self._picked()
        if rec is None or self.job:
            return
        grp = self.app.group
        nodes = [grp.nodes[x] for x in rec.nodes if rec.live(x) and grp.nodes[x].connected]
        if not nodes:
            self.msg.set(f"{rec.ref} {rec.tag}: no connected node still holds it", T.ERR)
            return
        self.job = self._fan(nodes, lambda n: self._export_one(n, rec), f"store {rec.ref} {rec.tag}")

    def _export_one(self, n, rec):
        vm_dir = self._vm_dir(n)
        if not vm_dir:
            raise RuntimeError("config.dump gave no cfg path, so there is no VM dir to write into")
        path = f"{vm_dir}/rmm_snaps/{self.app.group.spec.name}_{rec.id}_{rec.tag}.rmmsnap"
        was = n.rmm.call("machine.status").get("state") == "running"
        if was:
            n.induced_until = time.time() + 3.0
            n.rmm.call("machine.pause")
        try:
            n.rmm.call("snapshot.export", tag=rec.tag, path=path, _timeout=120)
        except RpcError as e:
            if "rmm_snaps" in path and e.code not in (7, 1):   # no such directory: write beside the cfg
                path = f"{vm_dir}/{self.app.group.spec.name}_{rec.id}_{rec.tag}.rmmsnap"
                n.rmm.call("snapshot.export", tag=rec.tag, path=path, _timeout=120)
            else:
                raise
        finally:
            if was:
                n.rmm.call("machine.resume")
        self.app.snap_files.setdefault(rec.id, {})[n.name] = path
        return path

    def _load(self):
        rec = self._picked()
        if rec is None or self.job:
            return
        files = self.app.snap_files.get(rec.id, {})
        grp = self.app.group
        nodes = [grp.nodes[x] for x in files if x in grp.nodes and grp.nodes[x].connected]
        if not nodes:
            self.msg.set(f"{rec.ref} {rec.tag}: no stored files for a connected node (Store first)", T.WARN)
            return

        def one(n):
            n.induced_until = time.time() + 3.0
            r = n.rmm.call("snapshot.load", path=files[n.name], tag=rec.tag, _timeout=120)
            return r.get("tag")

        def after(done):
            with grp.tree.lock:
                for x in done:
                    rec.status[x] = "ok"
                grp.tree.restored(rec, done)
            for x in done:
                grp.nodes[x].regs_queued = True
                grp.nodes[x].work.put(("regs", None))
        self.job = self._fan(nodes, one, f"load {rec.ref} {rec.tag}", after)

    def _fan(self, nodes, fn, what, after=None):
        """fn(node) on every node's worker; one Job that is done when all are."""
        jobs = {n.name: run_on(n, lambda n=n: fn(n)) for n in nodes}
        agg = Job()
        agg.parts, agg.what, agg.after = jobs, what, after
        return agg

    def _save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save project", self.app.project_path or
                                              f"{self.app.group.spec.name}.rmmproj.json", "RMM project (*.json)")
        if path:
            try:
                self.app.save_project(path)
                self.msg.set(f"saved {path}", T.OK)
            except OSError as e:
                self.msg.set(f"save: {e}", T.ERR)

    def _export_bundle(self):
        """One zip with the project and its snapshots' bytes (bundle.py)."""
        box = QMessageBox(self)
        box.setWindowTitle("Export bundle")
        box.setText("Which snapshots go into the bundle?")
        box.setInformativeText("Full tree: every snapshot still held, on every node.\n"
                               "Latest only: the newest one; the others are left out of the copies.")
        full = box.addButton("Full tree", QMessageBox.AcceptRole)
        latest = box.addButton("Latest only", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() not in (full, latest):
            return
        mode = "full" if box.clickedButton() is full else "latest"
        path, _ = QFileDialog.getSaveFileName(self, "Export bundle", f"{self.app.group.spec.name}.rmmbundle.zip",
                                              "RMM bundle (*.zip)")
        if path:
            self.app.submit(f"group bundle export {shlex.quote(path)} {mode}")

    def _import_bundle(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import bundle", "", "RMM bundle (*.zip)")
        if not path:
            return
        root = QFileDialog.getExistingDirectory(self, "Unpack into (a new folder is made here)")
        if root:
            dest = os.path.join(root, os.path.basename(path).rsplit(".zip", 1)[0])
            self.app.submit(f"group bundle import {shlex.quote(path)} {shlex.quote(dest)}")

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", "RMM project (*.json)")
        if path:
            self.app.open_project(path)

    def refresh(self, tick):
        self.tree.refresh()
        j = self.job
        if j and all(p.done for p in j.parts.values()):
            self.job = None
            bad = {k: p.error for k, p in j.parts.items() if p.error}
            ok = [k for k, p in j.parts.items() if not p.error]
            if j.after and ok:
                j.after(ok)
            if bad:
                self.msg.set(f"{j.what}: ok on {','.join(ok) or 'none'}; failed " +
                             "; ".join(f"{k}: {v}" for k, v in bad.items()), T.ERR)
            else:
                self.msg.set(f"{j.what}: ok on {','.join(ok)}", T.OK)
            self.app.say(f"{j.what}: " + ("ok" if not bad else f"failed on {','.join(bad)}"),
                         "err" if bad else "ok")
        elif j:
            self.msg.set(f"{j.what}: {sum(p.done for p in j.parts.values())}/{len(j.parts)} …", T.MUTED)
        tree = self.app.group.tree
        rows = []
        for rid, files in sorted(self.app.snap_files.items()):
            rec = tree.recs.get(rid)
            for node, path in files.items():
                rows.append((f"{rec.ref} {rec.tag}" if rec else f"#{rid}", node, path))
        fill(self.files, rows)


# -- VM Settings --------------------------------------------------------------------------------

# an 86Box without config.enums still describes these one at a time
OLD_ENUM_KEYS = {("Machine", "machine"), ("Machine", "cpu_family"), ("Video", "gfxcard"), ("Sound", "sndcard")}
NET_CARD = re.compile(r"^net_\d\d_card$")


def old_enum(section, key):
    return (section, key) in OLD_ENUM_KEYS or (section == "Network" and NET_CARD.match(key))


def refused(section, key):
    return section == "General" and (key.startswith("rmm_") or key == "uuid")


class SettingsPage(Page):
    """The running VM's whole cfg (config.dump) as editable sections. Edits are staged with
    config.load (Validate checks them without staging) and take effect at a hard reset, as in
    86Box's own Settings dialog. Every hardware choice (machine, CPU, video, sound, MIDI,
    network, storage controllers, ISA cards, ports, input devices) is a combo of 86Box's own
    list for the running machine (config.enums: display names, internal names stored), shown
    even when the cfg leaves the key at its default; other keys are free text. [General]
    rmm_* and uuid are read-only: 86Box refuses them."""

    def __init__(self, app):
        super().__init__(app)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.title = Elided("", 260)
        bar.addWidget(self.title)
        bar.addWidget(button("Reload", self._reload))
        bar.addWidget(button("Validate", lambda: self._load(validate=True)))
        bar.addWidget(button("Apply (hard reset)…", lambda: self._load(validate=False), name="danger"))
        bar.addWidget(button("Discard edits", self._discard))
        bar.addWidget(button("Add key…", self._add_key))
        self.msg = Elided("", name="muted")
        bar.addWidget(self.msg, 1)
        v.addLayout(bar)
        split = QSplitter(Qt.Horizontal)
        self.sections = QListWidget()
        self.sections.setFixedWidth(200)
        self.sections.currentTextChanged.connect(self._show_section)
        split.addWidget(self.sections)
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        self.keys = QTableWidget(0, 3)
        self.keys.setHorizontalHeaderLabels(["Key", "Value", ""])
        self.keys.verticalHeader().hide()
        self.keys.verticalHeader().setDefaultSectionSize(self.keys.fontMetrics().height() + 10)
        self.keys.setColumnWidth(0, 220)
        self.keys.setColumnWidth(2, 110)
        self.keys.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.keys.itemChanged.connect(self._edited)
        mv.addWidget(self.keys, 1)
        split.addWidget(mid)
        self.preview = mono_text()
        split.addWidget(group("Pending changes (config.load)", self.preview))
        split.setSizes([200, 620, 360])
        v.addWidget(split, 1)
        self.cfg = {}
        self.edits = {}
        self.enums = {}
        self.errors = {}
        self.job = None
        self._filling = False
        self._cur = None

    # data
    def _reload(self):
        n = self.node()
        self.job = run_on(n, lambda: ("dump", n.rmm.call("config.dump")))
        self.job.node = n
        self.msg.set("reading config.dump …", T.MUTED)

    def _enums(self):
        """Every list in one call; an 86Box without config.enums: the old keys one by one."""
        n = self.node()
        cfg = self.cfg

        def go():
            try:
                return ("enums", {(e["section"], e["key"]): e for e in n.rmm.call("config.enums")["keys"]})
            except RpcError as e:
                if e.code != -32601:
                    raise
            out = {}
            for s, kv in cfg.items():
                for k in kv:
                    if old_enum(s, k):
                        try:
                            d = n.rmm.call("config.describe", section=s, key=k)
                        except RpcError:
                            continue
                        vals = d.get("values") or []
                        out[(s, k)] = dict(d, labels=d.get("labels") or vals)
            return ("enums", out)
        self.job = run_on(n, go)
        self.job.node = n

    def merged(self):
        cfg = {s: dict(kv) for s, kv in self.cfg.items()}
        for (s, k), v in self.edits.items():
            cfg.setdefault(s, {})[k] = v
        return cfg

    def _load(self, validate):
        n = self.node()
        if not self.edits:
            self.msg.set("nothing edited", T.MUTED)
            return
        if not validate and QMessageBox.question(
                self, "Apply settings", f"Stage {len(self.edits)} change(s) on {n.name} and hard-reset it now?\n\n"
                "The guest loses whatever it was doing.") != QMessageBox.Yes:
            return
        full = self.merged()
        body = {s: full[s] for s in {s for s, _ in self.edits}}

        def go():
            r = n.rmm.call("config.load", cfg=body, validate_only=validate)
            if validate:
                return ("validated", r)
            rst = n.rmm.call("machine.reset", hard=True, _timeout=30)
            return ("applied", r, rst, n.rmm.call("config.dump"))
        self.job = run_on(n, go)
        self.job.node = n
        self.msg.set("validating …" if validate else "staging and resetting …", T.MUTED)

    def _discard(self):
        self.edits, self.errors = {}, {}
        self._show_section(self._cur)
        self._update_preview()

    def _add_key(self):
        if not self._cur:
            return
        key, ok = QInputDialog.getText(self, f"Add a key to [{self._cur}]", "key:")
        if ok and key.strip():
            self.edits[(self._cur, key.strip())] = ""
            self._show_section(self._cur)
            self._update_preview()

    # view
    def _fill_sections(self):
        cur = self._cur
        names = list(self.cfg) + [s for s, _ in self.enums if s not in self.cfg]
        names = list(dict.fromkeys(names))
        self.sections.blockSignals(True)
        self.sections.clear()
        self.sections.addItems(names)
        self.sections.blockSignals(False)
        if names:
            self.sections.setCurrentRow(names.index(cur) if cur in names else 0)

    def _show_section(self, section):
        self._cur = section
        if not section:
            return
        kv = {k: e["value"] for (s, k), e in self.enums.items() if s == section}   # keys at their default too
        kv.update(self.cfg.get(section, {}))
        for (s, k), v in self.edits.items():
            if s == section:
                kv[k] = v
        self._filling = True
        self.keys.setRowCount(len(kv))
        for r, (k, v) in enumerate(sorted(kv.items())):
            ki = QTableWidgetItem(k)
            ki.setFlags(Qt.ItemIsEnabled)
            self.keys.setItem(r, 0, ki)
            self.keys.removeCellWidget(r, 1)
            vi = QTableWidgetItem(str(v))
            if refused(section, k):
                vi.setFlags(Qt.ItemIsEnabled)
                vi.setForeground(QColor(*T.MUTED))
                vi.setToolTip("86Box refuses rmm_* and uuid over RMM: set them in 86Box's own Settings")
            if (section, k) in self.edits:
                vi.setBackground(QColor(*T.INPUT_BG))
            if (section, k) in self.errors:
                vi.setForeground(QColor(*T.ERR))
                vi.setToolTip(self.errors[(section, k)])
            self.keys.setItem(r, 1, vi)
            e = self.enums.get((section, k))
            tag = "read-only" if refused(section, k) else ("list" if e else "at reset")
            if e and k not in self.cfg.get(section, {}) and (section, k) not in self.edits:
                tag = "list, default"
            ti = QTableWidgetItem(tag)
            ti.setFlags(Qt.ItemIsEnabled)
            ti.setForeground(QColor(*(T.OK if e else T.MUTED)))
            self.keys.setItem(r, 2, ti)
            if e:
                self._combo(r, section, k, str(v), e)
        self._filling = False

    def _combo(self, row, section, key, value, e):
        """86Box's display names, its internal names stored; cpu_speed also sets cpu_multi."""
        cb = QComboBox()
        cb.setToolTip(f"{section}/{key}: {len(e['values'])} choice(s) for the running machine")
        pairs = list(zip(e.get("labels") or e["values"], e["values"], e.get("multi") or [None] * len(e["values"])))
        if value not in e["values"]:
            pairs.insert(0, (f"{value} (not offered for this machine)", value, None))
        multi = self.merged().get(section, {}).get("cpu_multi")
        cur = next((i for i, (_, v, m) in enumerate(pairs) if v == value and (m is None or multi is None
                                                                               or float(multi) == m)), 0)
        for i, (label, v, m) in enumerate(pairs):
            cb.addItem(label if label == v else f"{label}  [{v}]", (v, m))
        cb.setCurrentIndex(cur)
        cb.currentIndexChanged.connect(lambda i, s=section, k=key, c=cb: self._pick(s, k, c.itemData(i)))
        self.keys.setCellWidget(row, 1, cb)

    def _pick(self, section, key, data):
        value, multi = data
        self._set(section, key, value)
        if key == "cpu_speed" and multi is not None:
            self._set(section, "cpu_multi", f"{multi:g}")

    def _edited(self, item):
        if self._filling or item.column() != 1:
            return
        k = self.keys.item(item.row(), 0).text()
        self._set(self._cur, k, item.text())

    def _set(self, section, key, value):
        e = self.enums.get((section, key))
        if self.cfg.get(section, {}).get(key, e["value"] if e else None) == value:
            self.edits.pop((section, key), None)
        else:
            self.edits[(section, key)] = value
        self.errors.pop((section, key), None)
        self._update_preview()

    def _update_preview(self):
        lines = []
        for (s, k), v in sorted(self.edits.items()):
            old = self.cfg.get(s, {}).get(k)
            lines.append(f"[{s}] {k}: {old if old is not None else '(new)'} -> {v}")
            if (s, k) in self.errors:
                lines.append(f"    refused: {self.errors[(s, k)]}")
        try:
            dom_lines = kvm.describe(kvm.from_cfg(self.merged())) if self.cfg else []
        except Exception as e:  # noqa: BLE001 - an odd edit must not break the page
            dom_lines = [f"(describe: {e})"]
        set_text(self.preview, ("\n".join(lines) or "(no edits)") + "\n\nAs a domain after the reset:\n" +
                 "\n".join(dom_lines))

    def refresh(self, tick):
        n = self.node()
        if self.changed():
            self.cfg, self.edits, self.enums, self.errors = {}, {}, {}, {}
            self.sections.clear()
            self.keys.setRowCount(0)
            self.job = None
        self.title.set(f"{n.name}: {len(self.cfg)} sections, {len(self.edits)} edit(s)")
        if not self.cfg and not self.job and n.connected:
            self._reload()
        j = self.job
        if not (j and j.done and j.node is n):
            return
        self.job = None
        if j.error:
            e = j.error
            if isinstance(e, RpcError) and e.code == 10 and isinstance(e.data, dict):
                for er in e.data.get("errors", []):
                    self.errors[(er["section"], er["key"])] = er["why"]
                self.msg.set(f"refused: {len(self.errors)} key(s), see the red values", T.ERR)
                self._show_section(self._cur)
                self._update_preview()
            else:
                self.msg.set(f"error: {e}", T.ERR)
            return
        kind = j.result[0]
        if kind == "dump":
            self.cfg = j.result[1]["cfg"]
            self._fill_sections()
            self._update_preview()
            self.msg.set(f"read {j.result[1].get('path', '')}", T.MUTED)
            self._enums()
        elif kind == "enums":
            self.enums = j.result[1]
            self._fill_sections()
            self._show_section(self._cur)
        elif kind == "validated":
            r = j.result[1]
            unchecked = r.get("unchecked", [])
            self.msg.set(f"valid: {r.get('keys', '?')} key(s); {len(unchecked)} not checked by 86Box", T.OK)
        elif kind == "applied":
            self.cfg = j.result[3]["cfg"]
            self.edits, self.errors = {}, {}
            self._fill_sections()
            self._update_preview()
            self.msg.set("applied: staged and hard-reset", T.OK)
            self.app.say(f"[{n.name}] settings applied (config.load + hard reset)", "ok", None)
            n.work.put(("vm", None))
            self._enums()                  # the lists follow the machine now running


__all__ = ["SummaryPage", "ScreenPage", "MonitorPage", "SnapshotsPage", "SettingsPage", "PROJECT_SCHEMA",
           "run_on", "Job"]
