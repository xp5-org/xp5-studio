# SPDX-License-Identifier: MIT
"""The Inventory dock (vSphere style): the project (the running group) with its nodes, and the
VM configs in the domain store. A node row carries a live thumbnail, its state and where its
86Box is (host:port, local when this console launched it). Click a node to select it; right-click
for its actions. Column widths are fixed, so changing text never moves the dock.
"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTreeWidget, QTreeWidgetItem

from rmm_86box import kvm

from . import theme as T
from .view import THUMB

DOT = {"running": "●", "paused": "‖"}


def where(node):
    s = node.spec
    if s.mock:
        return "in-process mock"
    if s.qemu is not None:
        return f"qemu {s.qemu['profile']}" + ("  [local]" if node.proc else "")
    return f"{s.host}:{node.port or s.port}" + ("  [local]" if s.launch is not None else "")


class Inventory(QTreeWidget):
    def __init__(self, app, images):
        super().__init__()
        self.app, self.images = app, images
        self.setHeaderLabels(["Name", "State", "Where", "Note"])
        self.setIconSize(QSize(*THUMB))
        self.setIndentation(12)
        self.setRootIsDecorated(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setUniformRowHeights(False)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemClicked.connect(self._clicked)
        h = self.header()
        h.setStretchLastSection(True)
        for c, w in ((0, 110), (1, 70), (2, 150)):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
            self.setColumnWidth(c, w)
        self.items = {}
        self.frames = {}
        self.key = None
        self.cfg_names = None
        self.blank = QPixmap(*THUMB)
        self.blank.fill(QColor(*T.SCREEN_BG))

    def rebuild(self):
        self.clear()
        grp = self.app.group
        self.proj = QTreeWidgetItem(self, [grp.spec.name, "project", f"{len(grp.nodes)} node(s)", ""])
        f = self.proj.font(0)
        f.setBold(True)
        self.proj.setFont(0, f)
        self.items, self.frames = {}, {}
        for n in grp.nodes.values():
            it = QTreeWidgetItem(self.proj, [n.name, "", where(n), ""])
            it.setData(0, Qt.UserRole, ("node", n.name))
            it.setIcon(0, QIcon(self.blank))
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            self.items[n.name] = it
        self.cfgs = QTreeWidgetItem(self, ["VM configs", "", "domain store", ""])
        self.cfg_names = None
        self.expandAll()
        self.key = (grp.spec.name, tuple(grp.nodes))

    def _configs(self):
        try:
            names = kvm.DomainStore(self.app.a.vm_store).list()
        except OSError:
            names = []
        if names == self.cfg_names:
            return
        self.cfg_names = names
        self.cfgs.takeChildren()
        for nm in names:
            it = QTreeWidgetItem(self.cfgs, [nm, "", "", ""])
            it.setFirstColumnSpanned(True)
            it.setData(0, Qt.UserRole, ("cfg", nm))
            it.setToolTip(0, f"right-click: load {nm} into the selected node (vm load, then a hard reset)")

    def refresh(self, tick=0):
        grp = self.app.group
        if (grp.spec.name, tuple(grp.nodes)) != self.key:
            self.rebuild()
        if tick % 30 == 0:
            self._configs()
        for name, it in self.items.items():
            n = grp.nodes[name]
            st = n.state
            text = f"{DOT.get(st, '○')} {st}"
            if it.text(1) != text:
                it.setText(1, text)
                it.setForeground(1, QBrush(QColor(*T.state_colour(st))))
            w = where(n)                           # a launched node's port is known once it starts
            if it.text(2) != w:
                it.setText(2, w)
                it.setToolTip(2, w)
            note = n.stop_note
            if name not in grp.locked:                     # out of the group's snapshots for now
                note = "unlocked" + (f" · {note}" if note else "")
            if it.text(3) != note:
                it.setText(3, note)
                it.setToolTip(0, f"{name}: {st}" + (f" ({note})" if note else ""))
                it.setToolTip(3, note)
                it.setForeground(3, QBrush(QColor(*(T.ERR if st in ("crash", "error") else T.WARN))))
            sel = name == grp.current
            if it.isSelected() != sel:
                it.setSelected(sel)
            f = n.poller.latest if n.poller else None
            if f is not None and f is not self.frames.get(name):
                self.frames[name] = f
                img = self.images.get(n, f).scaled(*THUMB, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                it.setIcon(0, QIcon(QPixmap.fromImage(img)))

    def _clicked(self, it, _col):
        kind = it.data(0, Qt.UserRole)
        if kind and kind[0] == "node":
            self.app.select_node(kind[1])

    def _menu(self, pos):
        it = self.itemAt(pos)
        kind = it.data(0, Qt.UserRole) if it else None
        if not kind:
            return
        a = self.app
        m = QMenu(self)
        if kind[0] == "node":
            name = kind[1]
            a.select_node(name)
            n = a.group.nodes[name]
            m.addAction("Resume" if n.state == "paused" else "Pause", a.pause_resume)
            m.addAction("Step one instruction", a.step_one)
            m.addSeparator()
            m.addAction(f"Snapshot {name}", a.snap_node)
            m.addAction(f"Back to {name}'s last snapshot", a.restore_head)
            m.addSeparator()
            m.addAction(f"Unlock {name} from the group" if name in a.group.locked else f"Re-lock {name} here",
                        lambda: a.toggle_lock(name))
            m.addSeparator()
            m.addAction("Hard reset…", a.confirm_reset)
        else:
            cur = a.group.current
            m.addAction(f"Load into {cur} (hard reset)…", lambda: a.load_config(kind[1]))
            m.addAction(f"Diff against {cur} (console)", lambda: a.submit(f"@{cur} vm diff {kind[1]}"))
            m.addAction("Check it (validate only)", lambda: a.submit(f"@{cur} vm check {kind[1]}"))
        m.exec(self.viewport().mapToGlobal(pos))
