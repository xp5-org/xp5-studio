# SPDX-License-Identifier: MIT
"""The application window (Qt): menus, toolbars, docks and the central pages.

    menus    File  Edit  View  VM  Debug  Snapshots  Project  Settings  Help
    toolbars Machine (resume pause step … reset), Snapshots, Group: small icon buttons; drag a
             toolbar by its grip to any edge or float it
    banner   the group line: halted by whom, else every node's state (always one row high)
    pages    Summary | Screen | Monitor / Debug | Snapshots | VM Settings  (the selected node)
    docks    Inventory, Console, Snapshot timeline (lock / unlock), Recent tasks, Settings: snap
             to any edge, tab together, or float
             as their own windows (View → Break out); the layout is kept between runs

Menu items that need an RMM method 86Box does not have yet are shown disabled, with the plan
step that adds it in the tooltip (PLAN_qt_console.md), so the menus already have the shape of
86Box's own.
"""

from rmm_86box.backends.qemu import profiles as qemu_profiles
from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFormLayout,
                               QFrame, QGroupBox, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QScrollArea, QSpinBox, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import theme as T
from .icons import icon
from .inventory import Inventory
from .pages import MonitorPage, ScreenPage, SettingsPage, SnapshotsPage, SummaryPage
from .tasks import TasksView
from .timeline import TimelinePanel
from .view import ConsolePanel, Elided, HaltBanner, ImageCache

SCREEN, SNAPSHOTS, SETTINGS = 1, 3, 4
LATER = "needs an RMM method 86Box does not have yet: plan step {}"


class MainWindow(QMainWindow):
    def __init__(self, app, keep_layout=True):
        super().__init__()
        self.app = app
        self.setWindowTitle(app.a.title)
        w, h = app.a.size
        scr = self.screen().availableGeometry() if self.screen() else None
        self.too_big = bool(scr is not None and not scr.isEmpty() and (w > scr.width() or h > scr.height() - 60))
        if self.too_big:                                   # a small desktop: open maximized instead
            w, h = min(w, scr.width()), min(h, scr.height() - 60)
        self.resize(w, h)
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AllowTabbedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AnimatedDocks)
        self.images = ImageCache()
        self.tick = 0
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(6, 4, 6, 4)
        cv.setSpacing(4)
        self.halt = HaltBanner(app)
        cv.addWidget(self.halt)
        self.tabs = QTabWidget()
        self.pages = [SummaryPage(app, self.images), ScreenPage(app, self.images), MonitorPage(app),
                      SnapshotsPage(app), SettingsPage(app)]
        for p, name in zip(self.pages, ("Summary", "Screen", "Monitor / Debug", "Snapshots", "VM Settings")):
            sc = QScrollArea()                             # a small desktop scrolls a page; it never
            sc.setWidget(p)                                # grows the window past the screen
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            self.tabs.addTab(sc, name)
        self.tabs.setCurrentIndex(SNAPSHOTS if app.show_snaps else SCREEN)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._shown_snaps = app.show_snaps
        cv.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.inventory = Inventory(app, self.images)
        self.console = ConsolePanel(app)
        self.tasks = TasksView(app.tasks)
        self.timeline = TimelinePanel(app)
        self.prefs = Preferences(app)
        self.acts = {}
        self.toolbars = self._toolbars()
        self.docks = self._docks()
        self._menus()
        self.status = Elided("", name="muted")
        self.statusBar().addWidget(self.status, 1)
        self._keys()
        self._default_state = self.saveState()
        self.keep_layout = keep_layout
        if keep_layout:
            st = QSettings("rmm_86box", "rmm_console")
            if st.value("geometry"):
                self.too_big = False
                self.restoreGeometry(st.value("geometry"))
            if st.value("state"):
                self.restoreState(st.value("state"))
            for p in self.pages:
                if hasattr(p, "rows"):
                    p.rows.restore()
        self.console.input.setFocus()

    def closeEvent(self, ev):
        if self.keep_layout:
            st = QSettings("rmm_86box", "rmm_console")
            st.setValue("geometry", self.saveGeometry())
            st.setValue("state", self.saveState())
            for p in self.pages:
                if hasattr(p, "rows"):
                    p.rows.save()
        self.app.running = False
        super().closeEvent(ev)

    # -- toolbars ----------------------------------------------------------------------------

    def _bar(self, name):
        tb = QToolBar(name)
        tb.setObjectName(name)                    # saveState() keys on object names
        tb.setMovable(True)
        tb.setFloatable(True)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(Qt.TopToolBarArea, tb)
        return tb

    def _act(self, key, kind, text, slot=None, tip=None, checkable=False, later=None):
        a = QAction(icon(kind), text, self)
        a.setToolTip(tip or text)
        a.setCheckable(checkable)
        if later:
            a.setEnabled(False)
            a.setToolTip(f"{text}: {LATER.format(later)}")
            a._later = True
        elif slot:
            a.triggered.connect(slot)
        self.acts[key] = a
        return a

    def _toolbars(self):
        a = self.app
        m = self._bar("Machine")
        m.addAction(self._act("resume", "play", "Resume", a.resume_node, "Resume the selected node (F5)"))
        m.addAction(self._act("pause", "pause", "Pause", a.pause_node, "Pause the selected node (F5)"))
        m.addSeparator()
        m.addAction(self._act("step", "step", "Step instruction", a.step_one, "Step one instruction (F6)"))
        m.addAction(self._act("stepover", "stepover", "Step over", later="3.6 (disassembly)"))
        m.addAction(self._act("stepout", "stepout", "Run to return", later="3.6 (disassembly)"))
        m.addSeparator()
        m.addAction(self._act("reset", "reset", "Hard reset…", a.confirm_reset, "Hard reset the selected node"))
        m.addSeparator()
        m.addAction(self._act("turbo", "turbo", "Turbo mode", lambda: a.node_toggle("machine.fast_forward"),
                              "Turbo mode: run the selected node unthrottled (86Box fast forward; also mutes its "
                              "sound). Works for launched and attached nodes", checkable=True))
        s = self._bar("Snapshots")
        s.addAction(self._act("snap", "snapshot", "Snapshot this node", a.snap_node, "Snapshot the selected node (F7)"))
        s.addAction(self._act("snapall", "snapall", "Snapshot the group", lambda: a.snap_button("snap"),
                              "Snapshot every node together (Shift+F7)"))
        rb = QToolButton()
        rb.setIcon(icon("restore"))
        rb.setToolTip("Restore: the picked snapshot (else the newest group one) on all its nodes (Shift+F8); "
                      "the arrow lists them")
        rb.setPopupMode(QToolButton.MenuButtonPopup)
        rb.clicked.connect(a.restore_picked)
        self.restore_menu = QMenu(rb)
        self.restore_menu.aboutToShow.connect(self._fill_restore)
        rb.setMenu(self.restore_menu)
        s.addWidget(rb)
        s.addAction(self._act("back", "prev", "Back to this node's last snapshot", a.restore_head,
                              "Back to the selected node's last snapshot or restore (F8)"))
        g = self._bar("Group")
        g.addAction(self._act("resumeall", "playall", "Resume all", lambda: a.submit("resume"), "Resume every node (F9)"))
        g.addAction(self._act("pauseall", "pauseall", "Pause all", lambda: a.submit("halt"), "Pause every node (F10)"))
        g.addAction(self._act("haltall", "halt", "Halt-all", a.toggle_halt_all,
                              "Halt-all: when one node stops, pause the others (F11)", checkable=True))
        g.addAction(self._act("lock", "lock", "Lock / unlock the selected node", lambda: a.toggle_lock(),
                              "Unlock the selected node from the group's snapshots, restores and pause / resume, "
                              "or lock it again (Snapshot timeline)"))
        g.addSeparator()
        g.addAction(self._act("attach", "attach", "Attach to a remote 86Box…", self.attach_dialog))
        g.addAction(self._act("launch", "launch", "Launch 86Box on this host…", self.launch_dialog))
        self.node_pick = QComboBox()
        self.node_pick.setMinimumContentsLength(10)
        self.node_pick.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.node_pick.setToolTip("The selected node (Ctrl+Tab, Alt+1..9)")
        self.node_pick.activated.connect(lambda i: a.select_node(self.node_pick.itemText(i)))
        g.addWidget(self.node_pick)
        return [m, s, g]

    def _fill_restore(self):
        m = self.restore_menu
        m.clear()
        grp = self.app.group
        recs = [r for r in grp.tree.recs.values() if r.live()]
        if not recs:
            m.addAction("(no snapshots yet)").setEnabled(False)
            return
        for r in reversed(recs[-20:]):
            label = f"{r.ref} {r.tag}  [{r.members_text(list(grp.nodes))}]"
            m.addAction(label, lambda r=r: self.app.submit(f"restore {r.ref}"))

    # -- docks -------------------------------------------------------------------------------

    def _dock(self, title, widget, area):
        d = QDockWidget(title, self)
        d.setObjectName(title)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                      QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _docks(self):
        inv = self._dock("Inventory", self.inventory, Qt.LeftDockWidgetArea)
        con = self._dock("Console", self.console, Qt.BottomDockWidgetArea)
        tasks = self._dock("Recent tasks", self.tasks, Qt.BottomDockWidgetArea)
        tl = self._dock("Snapshot timeline", self.timeline, Qt.BottomDockWidgetArea)
        sett = self._dock("Settings", self.prefs, Qt.RightDockWidgetArea)
        self.splitDockWidget(con, tasks, Qt.Horizontal)
        self.tabifyDockWidget(tasks, tl)
        tl.raise_()
        sett.hide()
        self.resizeDocks([inv], [300], Qt.Horizontal)
        self.resizeDocks([con], [280], Qt.Vertical)
        self.resizeDocks([con, tasks], [500, 500], Qt.Horizontal)
        return {"inventory": inv, "console": con, "timeline": tl, "tasks": tasks, "settings": sett}

    def break_out(self, *names):
        """Float docks as their own windows beside the main one (View → Break out)."""
        geo = self.geometry()
        for i, n in enumerate(names):
            d = self.docks[n]
            d.show()
            d.setFloating(True)
            d.resize(720, 360)
            d.move(geo.right() - 760 + i * 60, geo.top() + 120 + i * 300)

    def dock_all(self):
        for d in self.docks.values():
            d.setFloating(False)

    # -- menus -------------------------------------------------------------------------------

    def _menus(self):
        a = self.app
        mb = self.menuBar()

        def menu(title, items):
            m = mb.addMenu(title)
            for it in items:
                add(m, it)
            return m

        def add(m, it):
            if it == "-":
                m.addSeparator()
            elif isinstance(it, QAction):
                m.addAction(it)
            elif isinstance(it[1], list):                # (title, [items]): a submenu
                sub = m.addMenu(it[0])
                for x in it[1]:
                    add(sub, x)
            else:
                text, slot = it[0], it[1]
                act = m.addAction(text)
                if isinstance(slot, str):                 # a plan step: not yet
                    act.setEnabled(False)
                    act.setToolTip(LATER.format(slot))
                    act.setStatusTip(LATER.format(slot))
                else:
                    act.triggered.connect(slot)
                if len(it) > 2:
                    act.setShortcut(QKeySequence(it[2]))
                    act.setShortcutContext(Qt.WidgetShortcut)   # shown only; the window's shortcuts fire
            m.setToolTipsVisible(True)

        menu("&File", [
            ("New project…", self.new_project), ("Open project…", self.pages[SNAPSHOTS]._open_project),
            ("Save project…", self.pages[SNAPSHOTS]._save_project),
            ("Export bundle (project + snapshots)…", self.pages[SNAPSHOTS]._export_bundle),
            ("Import bundle…", self.pages[SNAPSHOTS]._import_bundle), "-",
            ("Open group file…", self.open_group), ("Save group file…", self.save_group),
            ("Export group (live configs)…", self.export_group), "-",
            ("Attach to a remote 86Box…", self.attach_dialog), ("Launch 86Box on this host…", self.launch_dialog), "-",
            ("Quit", self.close, "Ctrl+Q")])
        menu("&Edit", [
            ("Copy screen", lambda: a.submit("copy screen")), ("Copy registers", lambda: a.submit("copy regs")),
            ("Copy console output", lambda: a.submit("copy out")), "-",
            ("Paste into the guest", a.paste_to_guest, "Ctrl+Shift+V"), "-",
            ("Clear the console", lambda: a.submit("clear"), "Ctrl+L")])
        view = mb.addMenu("&View")
        view.addSection("Panels")
        for d in self.docks.values():
            view.addAction(d.toggleViewAction())
        view.addSection("Toolbars")
        for tb in self.toolbars:
            view.addAction(tb.toggleViewAction())
        view.addSeparator()
        view.addAction("Break out console + tasks + settings").triggered.connect(
            lambda: self.break_out("console", "tasks", "settings"))
        view.addAction("Dock everything back").triggered.connect(self.dock_all)
        view.addAction("Reset layout").triggered.connect(lambda: self.restoreState(self._default_state))
        view.addSeparator()
        for i, name in enumerate(("Summary", "Screen", "Monitor / Debug", "Snapshots", "VM Settings")):
            act = view.addAction(name)
            act.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            act.triggered.connect(lambda _=False, i=i: self.tabs.setCurrentIndex(i))
        menu("S&creen", [
            ("Grab a frame now", a.grab, "F1"), ("Polling on/off", a.toggle_poll, "F2"),
            ("Slower polling", lambda: a.step_rate(-1), "F3"), ("Faster polling", lambda: a.step_rate(+1), "F4"), "-",
            ("Scale / filter / aspect", "the console's viewer options (next pass)"),
            ("Show non-primary monitors", "3.8 (capture.screenshot monitor)"),
            ("CGA composite settings…", "3.3"), ("Overscan", "3.3"), ("Grayscale", "3.3")])
        media = []
        media += [("Insert / eject (Summary → Media)", lambda: self.tabs.setCurrentIndex(0))]
        media += [(f"{d}", "3.4") for d in ("MO", "Removable disk (ZIP)", "Tape", "Cassette", "Cartridge")]
        media += [("NIC connected", "3.4")]
        menu("&Media", media)
        menu("V&M", [
            ("Hard reset…", a.confirm_reset), ("Ctrl+Alt+Del", lambda: a.node_line("machine.cad")),
            ("Ctrl+Alt+Esc", lambda: a.node_line("machine.cae")),
            ("Non-maskable interrupt", lambda: a.node_line("machine.nmi")),
            ("ACPI shutdown", lambda: a.node_line("machine.acpi_shutdown")),
            ("Quit its 86Box…", a.confirm_quit), "-",
            ("Turbo mode (fast forward) on/off", lambda: a.node_toggle("machine.fast_forward")),
            ("Force interpretation on/off", lambda: a.node_toggle("cpu.force_interpreter")),
            ("Mute on/off", lambda: a.node_toggle("sound.mute")),
            ("Update mouse every CPU frame", "3.3"), ("Keyboard requires capture", "3.3"),
            ("Right Ctrl is left Alt", "3.3"), "-",
            ("Settings… (VM Settings page)", lambda: self.tabs.setCurrentIndex(SETTINGS)),
            ("MCA devices…", "3.8"), ("Open printer tray…", "3.8")])
        menu("&Debug", [
            ("Resume / pause", a.pause_resume, "F5"), ("Step instruction", a.step_one, "F6"),
            ("Step over", "3.6 (disassembly)"), ("Run to return", "3.6 (disassembly)"), "-",
            ("Breakpoints and memory (Monitor page)", lambda: self.tabs.setCurrentIndex(2)), "-",
            ("Halt-all on/off", a.toggle_halt_all, "F11"),
            ("Halt-all reasons (Settings dock)", lambda: self.docks["settings"].show()), "-",
            ("Begin trace", "3.8"), ("End trace", "3.8")])
        menu("&Snapshots", [
            ("Snapshot this node", a.snap_node, "F7"), ("Snapshot the group", lambda: a.snap_button("snap"), "Shift+F7"),
            ("Back to this node's last snapshot", a.restore_head, "F8"),
            ("Restore the picked / newest group snapshot", a.restore_picked, "Shift+F8"), "-",
            ("Unlock / re-lock this node", lambda: a.toggle_lock()),
            ("Roll the whole group back…", self.timeline._roll),
            ("Snapshot timeline", lambda: (self.docks["timeline"].show(), self.docks["timeline"].raise_())), "-",
            ("Snapshot manager", a.toggle_snaps, "F12")])
        menu("&Project", [
            ("Add node: attach…", self.attach_dialog), ("Add node: launch…", self.launch_dialog),
            ("Add node: QEMU…", self.qemu_dialog), "-",
            ("Show the group (console)", lambda: a.submit("group show"))])
        menu("Se&ttings", [
            ("Console settings", lambda: (self.docks["settings"].show(), self.docks["settings"].raise_())),
            ("86Box remote monitor (listen / allow)", "3.5")])
        menu("&Help", [
            ("Console commands", lambda: a.submit("help")), ("Keys", self.show_keys),
            ("About", lambda: QMessageBox.about(self, "xp5-studio", "xp5-studio: remote machine monitor console\n"
                                                "rmm_86box + PySide6"))])

    # -- dialogs -----------------------------------------------------------------------------

    def _form(self, title, rows, note=""):
        d = QDialog(self)
        d.setWindowTitle(title)
        v = QVBoxLayout(d)
        f = QFormLayout()
        for label, w in rows:
            f.addRow(label, w)
        v.addLayout(f)
        if note:
            n = QLabel(note)
            n.setWordWrap(True)
            n.setObjectName("muted")
            v.addWidget(n)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        self._form_dialog = d  # the caller reads its QLineEdits after exec(); d's children die with d
        return d.exec() == QDialog.Accepted

    def attach_dialog(self):
        name, host, port = QLineEdit(self.app.free_name()), QLineEdit("127.0.0.1"), QSpinBox()
        port.setRange(1, 65535)
        port.setValue(55860)
        if self._form("Attach to a remote 86Box", [("Node name", name), ("Host", host), ("Monitor port", port)],
                      "86Box listens on [General] rmm_port or --monitor-port. Today it binds 127.0.0.1 only "
                      "(plan step 3.5 adds listen/allow), so a remote host needs an SSH tunnel."):
            self.app.add_node(name.text().strip(), {"host": host.text().strip(), "port": port.value()})

    def launch_dialog(self):
        a = self.app
        if not self.prefs.allow_launch.isChecked():
            QMessageBox.information(self, "Launch on this host", "Launching 86Box here is off. Turn on "
                                    "\"Allow launching 86Box on this host\" in the Settings dock.")
            self.docks["settings"].show()
            return
        name = QLineEdit(a.free_name())
        cfg = QComboBox()
        cfg.setEditable(True)
        cfg.addItems(a.vm_configs())
        vm_dir = QLineEdit()
        name.textChanged.connect(lambda t: vm_dir.setText(f"/tmp/rmm_groups/{a.group.spec.name}/{t}"))
        vm_dir.setText(f"/tmp/rmm_groups/{a.group.spec.name}/{name.text()}")
        roms = QLineEdit(self.prefs.roms.text())
        binary = QLineEdit(self.prefs.binary.text())
        binary.setPlaceholderText("blank: the VM config's box_binary")
        if self._form("Launch 86Box on this host", [("Node name", name), ("VM config / domain", cfg),
                                                    ("VM dir", vm_dir), ("ROMs", roms), ("86Box binary", binary)],
                      "A VM config is a __testlist__vm_<id>.py (vm_config_dir); a domain is a name in the domain "
                      "store. The monitor port is picked free; closing the console quits it."):
            src = cfg.currentText().strip()
            launch = {"vm_dir": vm_dir.text().strip(), "wait": 20}
            if roms.text().strip():
                launch["roms"] = roms.text().strip()
            if binary.text().strip():
                launch["binary"] = binary.text().strip()
            body = {"port": "auto", "launch": launch}
            body["domain" if src.startswith("domain:") else "vm_config"] = src.split(":", 1)[-1]
            a.add_node(name.text().strip(), body)

    def qemu_dialog(self):
        a = self.app
        name, prof, media, run_dir, qmp = QLineEdit(a.free_name()), QComboBox(), QLineEdit(), QLineEdit(), QLineEdit()
        for p in qemu_profiles.PROFILES.values():
            prof.addItem(f"{p.name}  ({p.binary} -M {p.machine})", p.name)
            prof.setItemData(prof.count() - 1, p.doc, Qt.ToolTipRole)

        def hint():
            p = qemu_profiles.get(prof.currentData())
            media.setPlaceholderText(" ".join(f"{o}=…" for o in p.options))
        prof.currentIndexChanged.connect(hint)
        hint()
        name.textChanged.connect(lambda t: run_dir.setText(f"/tmp/rmm_groups/{a.group.spec.name}/{t}"))
        run_dir.setText(f"/tmp/rmm_groups/{a.group.spec.name}/{name.text()}")
        qmp.setPlaceholderText("blank: start QEMU here")
        if not self._form("QEMU machine", [("Node name", name), ("Profile", prof), ("Media / firmware", media),
                                           ("Run dir", run_dir), ("Attach to QMP socket", qmp)],
                          "Started headless: no window, QMP on a unix socket in the run dir. Media are "
                          "key=value pairs (scsi takes a comma list). Registers, memory, breakpoints and "
                          "media slots are not offered for QEMU yet."):
            return
        body = {"profile": prof.currentData()}
        if qmp.text().strip():
            body["qmp"] = qmp.text().strip()
        else:
            if not self.prefs.allow_launch.isChecked():
                QMessageBox.information(self, "Launch on this host", "Launching on this host is off. Turn on "
                                        "\"Allow launching 86Box on this host\" in the Settings dock.")
                return
            for word in media.text().split():
                k, _, v = word.partition("=")
                body[k] = v.split(",") if k == "scsi" else v
            body.update(run_dir=run_dir.text().strip(), wait=10)
        a.add_node(name.text().strip(), {"qemu": body})

    def new_project(self):
        name = QLineEdit("project")
        if self._form("New project", [("Name", name)],
                      "A new, empty group: the current one is stopped (86Box instances it launched are "
                      "quit). Then add nodes with Attach or Launch."):
            self.app.new_project(name.text().strip())

    @staticmethod
    def session_dir():
        """Where `rmmctl session save` puts group files, so the two tools share sessions."""
        import os
        home = os.environ.get("RMM_86BOX_HOME") or os.path.join(os.path.expanduser("~"), ".rmm_86box")
        d = os.path.join(home, "sessions")
        os.makedirs(d, exist_ok=True)
        return d

    def open_group(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Open group / rmmctl session file", self.session_dir(), "Group (*.json)")
        if path:
            self.app.submit(f"group load {path}")

    def save_group(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "Save group / rmmctl session file",
                                              f"{self.session_dir()}/{self.app.group.spec.name}.json", "Group (*.json)")
        if path:
            self.app.submit(f"group save {path}")

    def export_group(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "Export group", f"{self.app.group.spec.name}_export.json",
                                              "Group (*.json)")
        if path:
            self.app.submit(f"group export {path}")

    def show_keys(self):
        from . import app as appmod
        doc = appmod.__doc__
        QMessageBox.information(self, "Keys", doc[doc.index("Keys"):])

    # -- keys --------------------------------------------------------------------------------

    def _keys(self):
        a = self.app

        def bind(seq, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ApplicationShortcut)          # floating docks too
            s.activated.connect(fn)

        bind("Ctrl+Tab", lambda: a.cycle_node(1))
        bind("Ctrl+Shift+Tab", lambda: a.cycle_node(-1))
        bind("Ctrl+Shift+Backtab", lambda: a.cycle_node(-1))
        for i in range(1, 10):
            bind(f"Alt+{i}", lambda i=i: self._nth(i - 1))
        bind("Ctrl+Shift+V", a.paste_to_guest)
        bind("F1", a.grab)
        bind("F2", a.toggle_poll)
        bind("F3", lambda: a.step_rate(-1))
        bind("F4", lambda: a.step_rate(+1))
        bind("F5", a.pause_resume)
        bind("F6", a.step_one)
        bind("F7", a.snap_node)
        bind("Shift+F7", lambda: a.snap_button("snap"))
        bind("F8", a.restore_head)
        bind("Shift+F8", a.restore_picked)
        bind("F9", lambda: a.submit("resume"))
        bind("F10", lambda: a.submit("halt"))
        bind("F11", a.toggle_halt_all)
        bind("F12", a.toggle_snaps)
        bind("Ctrl+L", lambda: a.out.clear())
        bind("Ctrl+Q", self.close)

    def _nth(self, i):
        names = list(self.app.group.nodes)
        if i < len(names):
            self.app.select_node(names[i])

    # -- refresh -----------------------------------------------------------------------------

    def _tab_changed(self, i):
        self.app.show_snaps = i == SNAPSHOTS
        self._shown_snaps = self.app.show_snaps

    def refresh(self):
        a = self.app
        self.tick += 1
        if a.show_snaps != self._shown_snaps:              # F12 / `snaps view` from the console
            self._shown_snaps = a.show_snaps
            self.tabs.setCurrentIndex(SNAPSHOTS if a.show_snaps else SCREEN)
        self.halt.refresh()
        self.pages[self.tabs.currentIndex()].refresh(self.tick)
        self.inventory.refresh(self.tick)
        self.console.refresh()
        self.tasks.refresh()
        if not self.docks["timeline"].visibleRegion().isEmpty():
            self.timeline.refresh()
        if self.docks["settings"].isVisible():
            self.prefs.refresh()
        n = a.group.node()
        if n.want_tab is not None:                         # a demo (gamesnap) puts its panel on screen
            self.tabs.setCurrentIndex(n.want_tab)
            n.want_tab = None
        paused, up = n.state == "paused", n.connected
        self.acts["resume"].setEnabled(up and paused)
        self.acts["pause"].setEnabled(up and n.state == "running")
        self.acts["step"].setEnabled(up and paused)
        self.acts["reset"].setEnabled(up)
        self.acts["snap"].setEnabled(up)
        self.acts["turbo"].setEnabled(up and n.turbo is not None)
        self.acts["turbo"].setChecked(bool(n.turbo))
        self.acts["haltall"].setChecked(a.group.policy.halt_all)
        locked = n.name in a.group.locked
        if getattr(self, "_lock_icon", None) != (n.name, locked):
            self._lock_icon = (n.name, locked)
            self.acts["lock"].setIcon(icon("lock" if locked else "unlock"))
            self.acts["lock"].setToolTip(f"{n.name} is {'locked' if locked else 'UNLOCKED'}: click to "
                                         + ("unlock it from the group" if locked else "lock it again as it is"))
        self.acts["launch"].setEnabled(self.prefs.allow_launch.isChecked())
        names = list(a.group.nodes)
        if [self.node_pick.itemText(i) for i in range(self.node_pick.count())] != names:
            self.node_pick.clear()
            self.node_pick.addItems(names)
        if self.node_pick.currentText() != a.group.current:
            self.node_pick.setCurrentText(a.group.current)
        st = (f"{a.group.spec.name} · " + " · ".join(f"{x.name} {x.state}" + (" [local]" if x.proc else "")
                                                     for x in a.group.nodes.values())
              + f"   ·   selected {n.name} ({n.address})")
        self.status.set(st)
        title = f"{a.a.title} — {a.group.spec.name} / {n.name}"
        if self.windowTitle() != title:
            self.setWindowTitle(title)


class Preferences(QWidget):
    """The Settings dock: console preferences (QSettings), never sent to 86Box. The halt-all
    policy is the group's and applies at once."""

    REASONS = ("breakpoint", "watchpoint", "step", "pause", "crash", "disconnect", "exit")

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.st = QSettings("rmm_86box", "rmm_console")
        v = QVBoxLayout(self)
        g = QGroupBox("Halt-all (this group)")
        gv = QVBoxLayout(g)
        self.halt_all = QCheckBox("When one node stops, pause the others")
        self.halt_all.toggled.connect(lambda on: app.group.set_policy(halt_all=on))
        gv.addWidget(self.halt_all)
        self.reasons = {}
        for r in self.REASONS:
            cb = QCheckBox(f"on {r}")
            cb.toggled.connect(self._reasons)
            self.reasons[r] = cb
            gv.addWidget(cb)
        v.addWidget(g)
        g = QGroupBox("Screen polling")
        f = QFormLayout(g)
        self.bg_rate = QComboBox()
        self.bg_rate.addItems([f"{r:g}" for r in T.RATES])
        self.bg_rate.setCurrentText(str(self.st.value("bg_poll_rate", f"{T.BG_POLL_RATE:g}")))
        self.bg_rate.currentTextChanged.connect(self._bg_rate)
        f.addRow("other nodes (fps)", self.bg_rate)
        v.addWidget(g)
        g = QGroupBox("Launching 86Box on this host")
        f = QFormLayout(g)
        self.allow_launch = QCheckBox("Allow launching 86Box on this host")
        self.allow_launch.setChecked(self.st.value("allow_launch", "false") == "true")
        self.allow_launch.toggled.connect(lambda on: self.st.setValue("allow_launch", "true" if on else "false"))
        f.addRow(self.allow_launch)
        self.binary = QLineEdit(str(self.st.value("binary", "")))
        self.binary.setPlaceholderText("blank: the VM config's box_binary")
        self.binary.editingFinished.connect(lambda: self.st.setValue("binary", self.binary.text()))
        f.addRow("86Box binary", self.binary)
        self.roms = QLineEdit(str(self.st.value("roms", "/opt/86box/roms")))
        self.roms.editingFinished.connect(lambda: self.st.setValue("roms", self.roms.text()))
        f.addRow("ROMs", self.roms)
        v.addWidget(g)
        g = QGroupBox("Store")
        f = QFormLayout(g)
        store = Elided(app.a.vm_store, mono=True)
        f.addRow("VM configs (domains)", store)
        v.addWidget(g)
        v.addStretch(1)
        self._bg_rate(self.bg_rate.currentText())
        self._sig = None

    def _reasons(self):
        if self._sig is None:
            return
        on = [r for r, cb in self.reasons.items() if cb.isChecked()]
        self.app.group.set_policy(halt_on=on)

    def _bg_rate(self, text):
        try:
            T.BG_POLL_RATE = float(text)
        except ValueError:
            return
        self.st.setValue("bg_poll_rate", text)

    def refresh(self):
        pol = self.app.group.policy
        sig = (pol.halt_all, tuple(pol.halt_on))
        if sig == self._sig:
            return
        self._sig = None                                  # programmatic: no feedback into the policy
        self.halt_all.setChecked(pol.halt_all)
        for r, cb in self.reasons.items():
            cb.setChecked(r in pol.halt_on)
        self._sig = sig


__all__ = ["MainWindow", "Preferences"]
