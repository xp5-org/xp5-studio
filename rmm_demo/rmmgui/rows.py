# SPDX-License-Identifier: MIT
"""Row panels (Qt): a page made of full-width rows the user rearranges.

    ▾ Registers                                   live   [float] [hide]
      ...
    ▸ Disassembly @ CS:EIP                  needs RMM method   [float] [hide]   (collapsed: one line)
    ▾ Breakpoints and watchpoints                 live   [float] [hide]
      ...

Each row is a QDockWidget in a small QMainWindow of its own, so a row can be dragged by its
title to another place, dropped beside another row (tiled), onto it (tabbed) or out of the
window (floated). The arrow collapses a row to its title line. The Panels menu shows hidden
rows again, stacks every row full width (the default), tiles them two across, or resets.

DetailTable: a table whose cells elide; a click on a row opens its full text in a notepad-style
box under the table (selectable, copyable); a second click on that row, or its close button, closes it.
"""

from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QMainWindow, QMenu, QPlainTextEdit,
                               QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from .view import mono_font

MAX_H = 16777215                                     # QWIDGETSIZE_MAX


class RowTitle(QWidget):
    """A row's title line: collapse arrow, title, badge, float and close."""

    def __init__(self, dock, title, badge=None):
        super().__init__()
        self.dock = dock
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(6)
        self.arrow = QToolButton()
        self.arrow.setArrowType(Qt.DownArrow)
        self.arrow.setAutoRaise(True)
        self.arrow.setToolTip("Collapse / expand this row")
        self.arrow.clicked.connect(lambda: dock.set_collapsed(not dock.collapsed))
        h.addWidget(self.arrow)
        t = QLabel(f"<b>{title}</b>")
        h.addWidget(t, 1)
        if badge is not None:
            h.addWidget(badge)
        st = self.style()
        for sp, tip, slot in ((QStyle.SP_TitleBarNormalButton, "Float as its own window / dock it again",
                               lambda: dock.setFloating(not dock.isFloating())),
                              (QStyle.SP_TitleBarCloseButton, "Hide (the Panels menu shows it again)", dock.close)):
            b = QToolButton()
            b.setIcon(st.standardIcon(sp))
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            h.addWidget(b)
        self.setToolTip("Drag to move: beside another row tiles it, onto one tabs it, outside floats it")

    def mouseDoubleClickEvent(self, ev):
        self.dock.set_collapsed(not self.dock.collapsed)


class Row(QDockWidget):
    def __init__(self, key, title, widget, badge=None, parent=None):
        super().__init__(title, parent)
        self.setObjectName(key)                      # saveState() keys on object names
        self.setWidget(widget)
        self.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                         QDockWidget.DockWidgetClosable)
        self.bar = RowTitle(self, title, badge)
        self.setTitleBarWidget(self.bar)
        self.collapsed = False

    def set_collapsed(self, on):
        self.collapsed = on
        self.widget().setVisible(not on)
        self.bar.arrow.setArrowType(Qt.RightArrow if on else Qt.DownArrow)
        self.setMaximumHeight(self.bar.sizeHint().height() + 4 if on else MAX_H)


class RowPanels(QMainWindow):
    """The rows of one page. `name` keys the saved layout (QSettings rmm_86box/rmm_console)."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.setWindowFlags(Qt.Widget)
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AllowTabbedDocks | QMainWindow.AllowNestedDocks)
        filler = QWidget()                           # the rows take the whole page: no centre
        filler.setMaximumSize(0, 0)
        self.setCentralWidget(filler)
        self.rows = []
        self.collapse_default = {}
        self._default = None

    def add(self, key, title, widget, badge=None, collapsed=False):
        r = Row(key, title, widget, badge, self)
        self.rows.append(r)
        self.collapse_default[key] = collapsed
        self.addDockWidget(Qt.TopDockWidgetArea, r, Qt.Vertical)
        r.set_collapsed(collapsed)
        return r

    def done(self):
        """After the last add(): remember the default layout (Reset layout goes back to it)."""
        self._default = self.saveState()

    def restore(self):
        """Put the layout saved by save() back (the window does, when it keeps its layout)."""
        st = QSettings("rmm_86box", "rmm_console")
        state, folded = st.value(f"{self.name}/state"), st.value(f"{self.name}/collapsed")
        if isinstance(state, QByteArray) and not state.isEmpty():
            self.restoreState(state)
        if isinstance(folded, str):
            keys = set(folded.split(",")) - {""}
            for r in self.rows:
                r.set_collapsed(r.objectName() in keys)

    def save(self):
        st = QSettings("rmm_86box", "rmm_console")
        st.setValue(f"{self.name}/state", self.saveState())
        st.setValue(f"{self.name}/collapsed", ",".join(r.objectName() for r in self.rows if r.collapsed))

    def menu(self, parent=None):
        """Panels ▾: show / hide each row, then the arrangements."""
        m = QMenu("Panels", parent)
        for r in self.rows:
            m.addAction(r.toggleViewAction())
        m.addSeparator()
        m.addAction("Stack as rows", self.stack)
        m.addAction("Tile two across", self.tile)
        m.addAction("Collapse all", lambda: [r.set_collapsed(True) for r in self.rows])
        m.addAction("Expand all", lambda: [r.set_collapsed(False) for r in self.rows])
        m.addAction("Reset layout", self.reset)
        return m

    def _shown(self):
        out = []
        for r in self.rows:
            r.setFloating(False)
            if not r.isHidden():
                out.append(r)
        return out

    def stack(self):
        rows = self._shown()
        for r in rows:
            self.removeDockWidget(r)
        for r in rows:
            self.addDockWidget(Qt.TopDockWidgetArea, r, Qt.Vertical)
            r.show()

    def tile(self):
        rows = self._shown()
        for r in rows:
            self.removeDockWidget(r)
        left = rows[0::2]
        for r in left:
            self.addDockWidget(Qt.TopDockWidgetArea, r, Qt.Vertical)
            r.show()
        for a, b in zip(left, rows[1::2]):
            self.splitDockWidget(a, b, Qt.Horizontal)
            b.show()

    def reset(self):
        for r in self.rows:
            r.setFloating(False)
            r.show()
        if self._default is not None:
            self.restoreState(self._default)
        for r in self.rows:
            r.set_collapsed(self.collapse_default[r.objectName()])


class DetailTable(QWidget):
    """A table plus a notepad-style box: click a row to read all of it there, click it again
    (or the close button) to close. detail(row index) -> text; by default "Header: value" per column."""

    def __init__(self, t, detail=None):
        super().__init__()
        self.t = t
        self.detail = detail
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(t, 1)
        self.box = QWidget()
        bv = QVBoxLayout(self.box)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(0)
        head = QHBoxLayout()
        self.head = QLabel("")
        self.head.setObjectName("muted")
        head.addWidget(self.head, 1)
        x = QToolButton()
        x.setIcon(self.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        x.setAutoRaise(True)
        x.setToolTip("Close")
        x.clicked.connect(self.close_detail)
        head.addWidget(x)
        bv.addLayout(head)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font())
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text.setMinimumHeight(self.text.fontMetrics().height() * 4 + 12)
        self.text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        bv.addWidget(self.text, 1)
        v.addWidget(self.box, 1)
        self.box.hide()
        self.open_row = None
        t.cellClicked.connect(self._clicked)

    def _clicked(self, r, _c):
        if self.open_row == r and self.box.isVisible():
            self.close_detail()
            return
        self.open_row = r
        self.box.show()
        self.update_detail()

    def close_detail(self):
        self.open_row = None
        self.box.hide()

    def update_detail(self):
        """After the table refills: keep the open row's text current (or close it if gone)."""
        r = self.open_row
        if r is None:
            return
        if not 0 <= r < self.t.rowCount():
            self.close_detail()
            return
        if self.detail:
            text = self.detail(r)
        else:
            heads = [self.t.horizontalHeaderItem(c).text() for c in range(self.t.columnCount())]
            text = "\n".join(f"{h}: {self.t.item(r, c).text() if self.t.item(r, c) else ''}"
                             for c, h in enumerate(heads))
        self.head.setText(f"row {r + 1}: click it again to close")
        if self.text.toPlainText() != text:
            self.text.setPlainText(text)
