# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""MOCKUP of the RMM console as an application ("vSphere console with CPU breakpoints").

Static sample data only: nothing here talks to 86Box. It exists so the layout can be reviewed
before it is built. Panels marked "needs RMM method" show data 86Box cannot report yet.

    console_mockup.py                     open the window (DISPLAY must be set)
    console_mockup.py --page 2            open on a page (0 Summary .. 4 Configure)

Layout: toolbars drag by their grip to any edge or float; docks (Inventory, Console, Snapshot
timeline, Recent tasks, Settings) snap to any edge, tab together, or float as separate windows.
The layout is kept between runs (QSettings); View → Reset layout puts it back.
    console_mockup.py --save-png DIR      render every page and dialog to DIR and exit
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt  # noqa: E402
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen,  # noqa: E402
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget,  # noqa: E402
                               QFormLayout, QFrame, QGraphicsScene, QGraphicsView, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMenu,
                               QMainWindow, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                               QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QTabWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from rmmgui import theme  # noqa: E402
from rmmgui.theme import hexc  # noqa: E402

MOCK = "MOCKUP — static data"

EXTRA_QSS = f"""
QMenuBar {{ background: {hexc(theme.PANEL)}; border-bottom: 1px solid {hexc(theme.BORDER)}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {hexc(theme.SELECT)}; }}
QMenu {{ background: {hexc(theme.PANEL)}; border: 1px solid {hexc(theme.BORDER)}; }}
QToolBar {{ background: {hexc(theme.PANEL)}; border-bottom: 1px solid {hexc(theme.BORDER)}; spacing: 4px; }}
QToolButton {{ padding: 3px; border: 1px solid transparent; border-radius: 3px; }}
QToolButton:hover {{ border-color: {hexc(theme.BORDER)}; background: {hexc(theme.BUTTON)}; }}
QTabWidget::pane {{ border: 1px solid {hexc(theme.BORDER)}; background: {hexc(theme.PANEL)}; }}
QTabBar::tab {{ background: {hexc(theme.BUTTON)}; border: 1px solid {hexc(theme.BORDER)};
               padding: 5px 14px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {hexc(theme.PANEL)}; border-bottom-color: {hexc(theme.PANEL)};
                        color: {hexc(theme.ACCENT)}; }}
QTreeWidget, QTableWidget, QListWidget {{ background: {hexc(theme.PANEL)}; border: 1px solid {hexc(theme.BORDER)};
               selection-background-color: {hexc(theme.SELECT)}; selection-color: {hexc(theme.TEXT)}; }}
QHeaderView::section {{ background: {hexc(theme.BUTTON)}; border: none; border-right: 1px solid {hexc(theme.BORDER)};
               padding: 3px 6px; }}
QGroupBox {{ background: {hexc(theme.PANEL)}; border: 1px solid {hexc(theme.BORDER)}; border-radius: 4px;
            margin-top: 14px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {hexc(theme.MUTED)}; }}
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ background: transparent; }}
QLabel#badgeLive {{ background: #d8f0de; color: {hexc(theme.OK)}; border-radius: 3px; padding: 1px 6px; }}
QLabel#badgeNeed {{ background: #fde8cc; color: {hexc(theme.WARN)}; border-radius: 3px; padding: 1px 6px; }}
QLabel#mock {{ background: {hexc(theme.ERR)}; color: white; padding: 1px 8px; border-radius: 3px; }}
QLabel#warnbox {{ background: {hexc(theme.HALT_BG)}; border: 1px solid {hexc(theme.WARN)}; padding: 6px; }}
QLabel#h1 {{ font-size: 15px; font-weight: bold; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {hexc(theme.MUTED)};
               background: white; }}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {hexc(theme.ACCENT)}; }}
QStatusBar {{ background: {hexc(theme.PANEL)}; border-top: 1px solid {hexc(theme.BORDER)}; }}
"""


def mono(size=10):
    f = QFont("DejaVu Sans Mono")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


def badge(live):
    b = QLabel("live today" if live else "needs RMM method")
    b.setObjectName("badgeLive" if live else "badgeNeed")
    return b


def group(title, widget=None, layout=None):
    g = QGroupBox(title)
    lay = layout or QVBoxLayout()
    if widget is not None:
        lay.addWidget(widget)
    g.setLayout(lay)
    return g


def table(headers, rows, mono_cols=(), stretch=-1):
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setShowGrid(False)
    t.setAlternatingRowColors(True)
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            it = QTableWidgetItem(str(v))
            if c in mono_cols:
                it.setFont(mono(9))
            t.setItem(r, c, it)
    t.resizeColumnsToContents()
    h = t.horizontalHeader()
    h.setSectionResizeMode(stretch if stretch >= 0 else len(headers) - 1, QHeaderView.Stretch)
    return t


def kv_grid(pairs, cols=2):
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(4, 4, 4, 4)
    g.setHorizontalSpacing(14)
    for i, (k, v) in enumerate(pairs):
        r, c = divmod(i, cols)
        kl = QLabel(k)
        kl.setObjectName("muted")
        vl = QLabel(v)
        vl.setFont(mono(10))
        vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(kl, r, c * 2)
        g.addWidget(vl, r, c * 2 + 1)
    g.setColumnStretch(cols * 2 - 1, 1)
    return w


# -- a fake guest screen ----------------------------------------------------------------------

DOS_SCREEN = [
    "Starting MS-DOS...",
    "",
    "C:\\>cd GAMES\\NETDOOM",
    "C:\\GAMES\\NETDOOM>ipxsetup -nodes 3",
    "",
    "IPXSETUP v1.2  looking for 3 nodes",
    "  node 1 found  (02:00:00:00:00:01)",
    "  node 3 found  (02:00:00:00:00:03)",
    "  waiting for node 2...",
    "",
]


class GuestScreen(QFrame):
    """Paints a static 80x25-ish text screen, aspect kept, so it reads as a VM console."""

    def __init__(self, lines=DOS_SCREEN, paused=False, caption=""):
        super().__init__()
        self.lines, self.paused, self.caption = lines, paused, caption
        self.setMinimumSize(320, 200)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(*theme.SCREEN_BG))
        w, h = self.width(), self.height()
        sw = min(w, h * 4 / 3)
        sh = sw * 3 / 4
        r = QRectF((w - sw) / 2, (h - sh) / 2, sw, sh)
        p.fillRect(r, QColor(0, 0, 0))
        f = mono()
        f.setPixelSize(max(3, int(sh / 27)))
        p.setFont(f)
        p.setPen(QColor(170, 170, 170))
        lh = sh / 25
        for i, ln in enumerate(self.lines):
            p.drawText(QPointF(r.left() + 6, r.top() + lh * (i + 1)), ln)
        p.drawText(QPointF(r.left() + 6, r.top() + lh * (len(self.lines) + 1)), "_")
        if self.paused:
            p.fillRect(QRectF(r.right() - 90, r.top() + 6, 84, 20), QColor(*theme.WARN))
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRectF(r.right() - 90, r.top() + 6, 84, 20), Qt.AlignCenter, "PAUSED")
        if self.caption:
            p.setPen(QColor(220, 220, 220))
            p.drawText(QRectF(r.left(), r.bottom() - 18, r.width() - 6, 16), Qt.AlignRight, self.caption)


# -- toolbar icons: drawn, so they need no icon theme or emoji font ---------------------------

def icon(kind, size=40):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    k = size / 40.0
    p.scale(k, k)
    col = {"play": theme.OK, "pause": theme.WARN, "stop": theme.ERR, "reset": theme.ERR,
           "lock": theme.ACCENT, "unlock": theme.WARN}.get(kind, (60, 60, 60))
    c = QColor(*col)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    tri = lambda x0, y0, x1, ym, y1: p.drawPolygon(QPolygonF([QPointF(x0, y0), QPointF(x1, ym), QPointF(x0, y1)]))
    if kind == "play":
        tri(11, 7, 33, 20, 33)
    elif kind == "pause":
        p.drawRoundedRect(QRectF(10, 8, 7, 24), 1.5, 1.5)
        p.drawRoundedRect(QRectF(23, 8, 7, 24), 1.5, 1.5)
    elif kind == "stop":
        p.drawRoundedRect(QRectF(9, 9, 22, 22), 2, 2)
    elif kind == "step":                         # play-to-bar: one instruction
        tri(8, 9, 26, 20, 31)
        p.drawRect(QRectF(27, 9, 5, 22))
    elif kind in ("stepover", "stepout"):
        pen = QPen(c, 3.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        if kind == "stepover":                   # arc over a dot
            path.moveTo(8, 24)
            path.cubicTo(10, 6, 30, 6, 31, 20)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPolygon(QPolygonF([QPointF(25, 17), QPointF(37, 17), QPointF(31, 26)]))
            p.drawEllipse(QPointF(20, 31), 3.5, 3.5)
        else:                                    # up out of a frame
            path.moveTo(20, 32)
            path.lineTo(20, 12)
            p.drawPath(path)
            p.drawLine(QPointF(9, 34), QPointF(31, 34))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPolygon(QPolygonF([QPointF(12, 15), QPointF(28, 15), QPointF(20, 5)]))
    elif kind == "reset":
        pen = QPen(c, 3.5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(8, 8, 24, 24), 120 * 16, 290 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(6, 6), QPointF(16, 8), QPointF(8, 17)]))
    elif kind == "snapshot":                     # camera
        p.drawRoundedRect(QRectF(5, 12, 30, 21), 3, 3)
        p.drawRect(QRectF(14, 8, 12, 5))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(20, 22.5), 6.5, 6.5)
        p.setBrush(c)
        p.drawEllipse(QPointF(20, 22.5), 3.5, 3.5)
    elif kind == "restore":                      # rewind to a mark
        p.drawRect(QRectF(6, 9, 4, 22))
        p.drawPolygon(QPolygonF([QPointF(22, 9), QPointF(10, 20), QPointF(22, 31)]))
        p.drawPolygon(QPolygonF([QPointF(34, 9), QPointF(22, 20), QPointF(34, 31)]))
    elif kind in ("prev", "next"):              # jump to previous / next snapshot mark
        if kind == "prev":
            p.drawRect(QRectF(8, 9, 4, 22))
            p.drawPolygon(QPolygonF([QPointF(32, 9), QPointF(13, 20), QPointF(32, 31)]))
        else:
            p.drawRect(QRectF(28, 9, 4, 22))
            p.drawPolygon(QPolygonF([QPointF(8, 9), QPointF(27, 20), QPointF(8, 31)]))
    elif kind in ("lock", "unlock"):
        pen = QPen(c, 3.2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if kind == "lock":
            p.drawArc(QRectF(12, 5, 16, 18), 0, 180 * 16)
            p.drawLine(QPointF(12, 14), QPointF(12, 19))
            p.drawLine(QPointF(28, 14), QPointF(28, 19))
        else:
            p.drawArc(QRectF(20, 3, 16, 18), 0, 180 * 16)
            p.drawLine(QPointF(36, 12), QPointF(36, 17))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(8, 18, 24, 17), 2, 2)
    elif kind == "delete":
        p.drawRect(QRectF(11, 13, 18, 21))
        p.drawRect(QRectF(8, 8, 24, 3))
        p.drawRect(QRectF(16, 5, 8, 3))
    elif kind == "attach":                       # plug
        p.drawRoundedRect(QRectF(10, 14, 20, 13), 3, 3)
        p.drawRect(QRectF(14, 6, 3, 8))
        p.drawRect(QRectF(23, 6, 3, 8))
        p.drawRect(QRectF(18, 27, 4, 8))
    elif kind == "launch":                       # screen with a plus
        p.drawRoundedRect(QRectF(5, 7, 30, 21), 2, 2)
        p.drawRect(QRectF(15, 29, 10, 4))
        p.setBrush(QColor(255, 255, 255))
        p.drawRect(QRectF(18, 11, 4, 13))
        p.drawRect(QRectF(13.5, 15.5, 13, 4))
    p.end()
    return QIcon(pm)


# -- the shell ---------------------------------------------------------------------------------

NODES = [  # (name, host:port, state, note, local)
    ("p1", "10.0.4.21:55860", "running", "", False),
    ("p2", "10.0.4.22:55860", "paused", "bp @0008:810F", False),
    ("p3", "127.0.0.1:36731", "running", "", True),
]


def state_dot(state):
    return {"running": "●", "paused": "‖", "offline": "○"}.get(state, "?")


class Inventory(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["Name", "Where"])
        self.setColumnWidth(0, 140)
        self.setIndentation(14)
        proj = QTreeWidgetItem(self, ["▾ Projects"])
        doom = QTreeWidgetItem(proj, ["netdoom-3p", "3 nodes · halt-all"])
        for name, where, state, note, local in NODES:
            it = QTreeWidgetItem(doom, [f"{state_dot(state)} {name}", where + ("  [local]" if local else "")])
            it.setForeground(0, QBrush(QColor(*theme.state_colour(state))))
            if note:
                it.setToolTip(0, note)
            if name == "p2":
                self.setCurrentItem(it)
        QTreeWidgetItem(proj, ["serial-pair", "2 nodes · offline"])
        solo = QTreeWidgetItem(self, ["▾ Standalone VMs"])
        QTreeWidgetItem(solo, ["○ win311-test", "10.0.4.30:55860"])
        QTreeWidgetItem(solo, ["● dos622-16m", "127.0.0.1:37923  [local]"])
        cfg = QTreeWidgetItem(self, ["▾ VM configs"])
        for n in ("dos622_486_16m", "dos622_486_6m", "dos622_486_sb16", "dos622_486_sb16_ne2k"):
            QTreeWidgetItem(cfg, [n, "domain store"])
        self.expandAll()


def make_tasks():
    t = table(["Time", "Target", "Task", "Result"], [
        ("12:01:44", "p2", "stopped: breakpoint exec @0008:810F", "—"),
        ("12:01:44", "netdoom-3p", "halt-all: paused p1, p3", "ok 112 ms"),
        ("11:58:02", "netdoom-3p", "snapshot bundle “lvl2-start” (3 nodes)", "ok 1.4 s"),
        ("11:57:10", "p3", "launched 86Box pid 415816 on 127.0.0.1:36731", "ok"),
    ])
    t.setMaximumHeight(110)
    return t


# -- pages -------------------------------------------------------------------------------------

def page_summary():
    w = QWidget()
    g = QGridLayout(w)
    g.addWidget(group("Console preview", GuestScreen(paused=True, caption="p2 · 640x400")), 0, 0, 2, 1)
    g.addWidget(group("State", kv_grid([
        ("State", "PAUSED"), ("Why", "breakpoint exec @0008:810F (hit 3)"),
        ("Emu speed", "100 % (before stop)"), ("TSC rate", "33.0 MHz"),
        ("Uptime", "00:04:41"), ("Halt-all", "on (project)"),
    ], cols=1)), 0, 1)
    g.addWidget(group("Hardware", kv_grid([
        ("Machine", "MS-4144 (486)"), ("CPU", "i486DX2/66"), ("RAM", "16 MB"),
        ("Video", "Trident TVGA8900D"), ("Sound", "SB16"), ("Net", "NE2000 → vde:netdoom"),
    ], cols=1)), 1, 1)
    g.addWidget(group("Connection", kv_grid([
        ("Monitor", "10.0.4.22:55860 (remote)"), ("Banner", "86Box v5.1 RMM/1"),
        ("Latency", "0.4 ms"), ("Process", "remote — not launched here"),
        ("VM dir", "/vms/netdoom/p2"), ("Config", "dos622_486_sb16_ne2k"),
    ], cols=1)), 0, 2)
    media = table(["Slot", "Image", ""], [
        ("fdd.0", "(empty)", "Insert…"), ("fdd.1", "(empty)", "Insert…"),
        ("cdrom.0", "netdoom_cd.iso", "Eject"), ("hdd.0", "dos622_c.img  504 MB", "—"),
    ])
    g.addWidget(group("Media", media), 1, 2)
    g.setColumnStretch(0, 3)
    g.setColumnStretch(1, 2)
    g.setColumnStretch(2, 2)
    return w


def regs_widget():
    """Fixed-width grid: a value changing never resizes it (the current demo's jitter bug)."""
    regs = [("EAX", "00001F00"), ("EBX", "00000004"), ("ECX", "0000FFFF"), ("EDX", "000003F8"),
            ("ESI", "00000120"), ("EDI", "00008000"), ("EBP", "0000FFE0"), ("ESP", "0000FFD8"),
            ("CS", "0008"), ("DS", "0010"), ("ES", "0010"), ("SS", "0010"),
            ("EIP", "0000810F"), ("EFL", "00000246"), ("CR0", "00000011"), ("CPL", "0")]
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(6, 4, 6, 4)
    for i, (k, v) in enumerate(regs):
        r, c = divmod(i, 2)
        kl = QLabel(k)
        kl.setObjectName("muted")
        vl = QLabel(v)
        vl.setFont(mono(10))
        vl.setFixedWidth(80)
        if k in ("EAX", "EIP"):
            vl.setStyleSheet(f"color: {hexc(theme.ERR)}; background: transparent;")   # changed since last stop
        g.addWidget(kl, r, c * 2)
        g.addWidget(vl, r, c * 2 + 1)
    flags = QLabel("O d I t S Z a P c   (IOPL 0)")
    flags.setFont(mono(10))
    g.addWidget(flags, 8, 0, 1, 4)
    w.setFixedWidth(250)
    return w


def page_screen():
    """The guest screen with registers beside it; the console lives in its own dock."""
    w = QWidget()
    v = QVBoxLayout(w)
    top = QSplitter()
    top.addWidget(group("p2 — screen", GuestScreen(paused=True)))
    side = QWidget()
    sv = QVBoxLayout(side)
    sv.setContentsMargins(0, 0, 0, 0)
    sv.addWidget(group("Registers", regs_widget()))
    banner = QLabel("Stopped: breakpoint exec @0008:810F   ·   F5 resume  F10 step  F11 step over")
    banner.setObjectName("warnbox")
    banner.setWordWrap(True)
    sv.addWidget(banner)            # always present; text changes, the row never appears/disappears
    sv.addStretch(1)
    top.addWidget(side)
    top.setSizes([900, 260])
    v.addWidget(top)
    return w


def console_widget():
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(4, 4, 4, 4)
    out = QPlainTextEdit()
    out.setLineWrapMode(QPlainTextEdit.NoWrap)
    out.setReadOnly(True)
    out.setFont(mono(10))
    out.setPlainText("\n".join([
        "p2> debug.break.set addr=0x810F",
        "  ok  id=1",
        "p2> machine.resume",
        "[12:01:44] machine.stopped reason=breakpoint addr=0x810f",
        "p2> mem.phys.read addr=0x8100 size=32",
        "  00008100  B8 00 1F 8E D8 BB 04 00  8B 47 04 CD 21 EB F2 90  .........G..!...",
        "  00008110  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  ................",
    ]))
    v.addWidget(out)
    row = QHBoxLayout()
    tgt = QComboBox()
    tgt.addItems(["@p2", "@p1", "@p3", "@* (all)"])
    row.addWidget(tgt)
    line = QLineEdit("cpu.regs.get")
    line.setFont(mono(10))
    row.addWidget(line, 1)
    v.addLayout(row)
    return w


GROUP_SNAPS = [(0.06, "boot-ok"), (0.28, "lvl1-start"), (0.50, "desync-repro"), (0.70, "lvl2-start")]
NOW = 0.86                                   # the group's playhead


class TrackPlayer(QWidget):
    """Video-editor style snapshot timeline: one track per node under a group ruler.

    Group snapshots are columns: a dotted line from the ruler down through every locked track to
    the scrubber, whose dot jumps the whole group there. An unlocked node (p2 here) gets its own
    play track under its lane; group snapshots skip it until it is re-locked."""

    GUTTER, ROW, HEAD = 150, 30, 30

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 250)
        self.setMouseTracking(True)

    def x(self, f):
        return self.GUTTER + f * (self.width() - self.GUTTER - 24)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(*theme.PANEL))
        W, G = self.width(), self.GUTTER
        acc, warn, mut = QColor(*theme.ACCENT), QColor(*theme.WARN), QColor(*theme.MUTED)
        grp_fill = QColor(*theme.SNAP_GROUP)
        small, bold = QFont("Sans", 8), QFont("Sans", 9, QFont.Bold)
        # lanes: (name, top, height, locked)
        lanes = [("p1", 1), ("p2", 2), ("p3", 1)]
        y = self.HEAD + 8
        rows = []
        for name, h in lanes:
            rows.append((name, y, h * self.ROW))
            y += h * self.ROW + 6
        scrub_y = y + 18
        # ruler
        p.setFont(bold)
        p.setPen(QColor(*theme.TEXT))
        p.drawText(QRectF(8, 4, G - 12, 20), Qt.AlignVCenter, "netdoom-3p")
        p.setFont(small)
        p.setPen(mut)
        p.drawText(QRectF(8, 18, G - 12, 14), Qt.AlignVCenter, "group: 2 of 3 locked")
        p.setPen(QPen(QColor(*theme.BORDER), 1))
        p.drawLine(QPointF(G, self.HEAD), QPointF(W - 24, self.HEAD))
        for f, tag in GROUP_SNAPS:
            cx = self.x(f)
            p.setPen(Qt.NoPen)
            p.setBrush(acc)
            p.drawPolygon(QPolygonF([QPointF(cx, self.HEAD - 9), QPointF(cx + 6, self.HEAD - 3),
                                     QPointF(cx, self.HEAD + 3), QPointF(cx - 6, self.HEAD - 3)]))
            p.setPen(QColor(*theme.TEXT))
            p.drawText(QPointF(cx + 9, self.HEAD - 6), tag)
        # lanes
        for name, top, h in rows:
            locked = name != "p2"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(*theme.BG))
            p.drawRect(QRectF(0, top, W, h))
            icon("lock" if locked else "unlock").paint(p, 8, top + 5, 20, 20)
            p.setFont(bold)
            p.setPen(QColor(*theme.TEXT))
            p.drawText(QRectF(32, top, 40, 30), Qt.AlignVCenter, name)
            p.setFont(small)
            p.setPen(mut if locked else warn)
            p.drawText(QRectF(60, top, G - 64, 30), Qt.AlignVCenter, "locked" if locked else "decoupled")
            mid = top + 15
            if locked:
                p.setPen(Qt.NoPen)
                p.setBrush(grp_fill)
                p.drawRoundedRect(QRectF(self.x(0), mid - 8, self.x(NOW) - self.x(0), 16), 4, 4)
                p.setBrush(acc)
                for f, _ in GROUP_SNAPS:
                    p.drawEllipse(QPointF(self.x(f), mid), 4, 4)
            else:
                # group track up to where p2 was rewound to, then a ghost of what the group kept doing
                p.setPen(Qt.NoPen)
                p.setBrush(grp_fill)
                p.drawRoundedRect(QRectF(self.x(0), mid - 8, self.x(0.50) - self.x(0), 16), 4, 4)
                p.setBrush(acc)
                for f, _ in GROUP_SNAPS:
                    if f <= 0.50:
                        p.drawEllipse(QPointF(self.x(f), mid), 4, 4)
                p.setPen(QPen(acc, 1.2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(self.x(0.50), mid - 8, self.x(NOW) - self.x(0.50), 16), 4, 4)
                p.setPen(mut)
                p.drawText(QPointF(self.x(0.52), mid + 4), "group went on without p2")
                # p2's own play track
                own = top + 45
                p.setPen(QPen(warn, 1.5))
                p.drawLine(QPointF(self.x(0.50), mid + 8), QPointF(self.x(0.50), own - 8))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(253, 226, 190))
                p.drawRoundedRect(QRectF(self.x(0.50), own - 8, self.x(0.66) - self.x(0.50), 16), 4, 4)
                p.setBrush(warn)
                p.drawEllipse(QPointF(self.x(0.58), own), 4, 4)
                p.setPen(QColor(*theme.TEXT))
                p.drawText(QPointF(self.x(0.58) - 18, own + 20), "p2-try1")
                p.setPen(QPen(warn, 2.5))                # p2's own playhead (playing forward)
                p.drawLine(QPointF(self.x(0.66), own - 11), QPointF(self.x(0.66), own + 11))
                p.setPen(warn)
                p.drawText(QPointF(self.x(0.66) + 6, own + 4), "▶ p2 alone")
        # each group snapshot is a column, projected through every lane down to the scrubber
        col = QColor(acc)
        col.setAlpha(150)
        p.setPen(QPen(col, 1, Qt.DotLine))
        for f, _ in GROUP_SNAPS:
            p.drawLine(QPointF(self.x(f), self.HEAD + 3), QPointF(self.x(f), scrub_y - 6))
        # group playhead
        p.setPen(QPen(QColor(*theme.ERR), 2))
        p.drawLine(QPointF(self.x(NOW), self.HEAD), QPointF(self.x(NOW), scrub_y + 8))
        p.setPen(QColor(*theme.ERR))
        p.drawText(QPointF(self.x(NOW) + 4, self.HEAD + 1), "now")
        # scrubber
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*theme.BUTTON))
        p.drawRoundedRect(QRectF(self.x(0), scrub_y - 4, self.x(1) - self.x(0), 8), 4, 4)
        p.setBrush(acc)
        p.drawRoundedRect(QRectF(self.x(0), scrub_y - 4, self.x(NOW) - self.x(0), 8), 4, 4)
        for f, _ in GROUP_SNAPS:
            p.setBrush(QColor(255, 255, 255))
            p.setPen(QPen(acc, 2.5))
            p.drawEllipse(QPointF(self.x(f), scrub_y), 7, 7)
        p.setPen(QPen(warn, 2.5))                         # p2's detached head on the bar
        p.setBrush(warn)
        p.drawEllipse(QPointF(self.x(0.66), scrub_y), 4, 4)
        p.setBrush(QColor(*theme.ERR))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(self.x(NOW), scrub_y), 6, 6)
        p.setPen(mut)
        p.setFont(small)
        p.drawText(QRectF(8, scrub_y - 8, G - 12, 16), Qt.AlignVCenter, "click ○ = jump group")
        p.end()


def timeline_widget():
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(4, 4, 4, 4)
    top = QHBoxLayout()
    sc = QComboBox()
    sc.addItems(["Tracks: netdoom-3p", "Tree: netdoom-3p", "Tracks: p2 only"])
    top.addWidget(sc)
    note = QLabel("p2 is decoupled: group snapshots and restores skip it until it is re-locked.")
    note.setObjectName("warnbox")
    top.addWidget(note, 1)
    v.addLayout(top)
    v.addWidget(TrackPlayer(), 1)
    bar = QHBoxLayout()

    def tb(kind, tip):
        b = QToolButton()
        b.setIcon(icon(kind))
        b.setIconSize(QSize(18, 18))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        bar.addWidget(b)

    for kind, tip in (("prev", "Group: back to previous snapshot"), ("pause", "Group: pause"),
                      ("play", "Group: play"), ("next", "Group: forward to next snapshot (if a later one exists)"),
                      ("snapshot", "Group: snapshot the locked nodes")):
        tb(kind, tip)
    sep = QLabel("│")
    sep.setObjectName("muted")
    bar.addWidget(sep)
    bar.addWidget(QLabel("p2:"))
    for kind, tip in (("prev", "p2 only: back a snapshot"), ("pause", "p2 only: pause"), ("play", "p2 only: play"),
                      ("snapshot", "p2 only: snapshot on p2's own track")):
        tb(kind, tip)
    bar.addStretch(1)
    relock = QPushButton("Re-lock p2 here")
    relock.setToolTip("p2 keeps its own state; from now on group snapshots include it again")
    bar.addWidget(relock)
    roll = QPushButton("Roll group back to lvl2-start…")
    roll.setObjectName("danger")
    roll.setToolTip("Restore every node to lvl2-start and discard p2's own track (p2-try1)")
    bar.addWidget(roll)
    v.addLayout(bar)
    return w



def mem_map_bar():
    """1 MB real-mode map plus extended RAM, drawn proportionally (low MB magnified)."""
    regions = [  # (start, end, label, colour)
        (0x00000, 0x9FFFF, "conventional RAM 640K", (196, 216, 248)),
        (0xA0000, 0xBFFFF, "VGA A000", (250, 214, 160)),
        (0xC0000, 0xC7FFF, "VGA BIOS", (240, 180, 180)),
        (0xC8000, 0xDFFFF, "free UMB", (236, 236, 232)),
        (0xE0000, 0xEFFFF, "shadow", (220, 220, 210)),
        (0xF0000, 0xFFFFF, "BIOS", (240, 180, 180)),
    ]
    scene = QGraphicsScene()
    x, W = 0.0, 900.0
    low_w = W * 0.75
    for s, e, label, c in regions:
        w = low_w * (e - s + 1) / 0x100000
        scene.addRect(QRectF(x, 0, w, 34), QPen(QColor(*theme.BORDER)), QBrush(QColor(*c)))
        if w > 50:
            t = scene.addText(label)
            t.setFont(QFont("Sans", 7))
            t.setPos(x + 2, 6)
        if w > 30:
            a = scene.addText(f"{s:05X}")
            a.setFont(mono(7))
            a.setPos(x, 34)
        x += w
    scene.addRect(QRectF(x, 0, W - x, 34), QPen(QColor(*theme.BORDER)), QBrush(QColor(206, 230, 210)))
    t = scene.addText("extended RAM 100000–FFFFFF (15 MB)")
    t.setFont(QFont("Sans", 7))
    t.setPos(x + 4, 6)
    e = scene.addText("FFFFFF")
    e.setFont(mono(7))
    e.setPos(W - 44, 34)
    view = QGraphicsView(scene)
    view._scene = scene                  # the view does not own it
    view.setFixedHeight(64)
    view.setRenderHint(QPainter.Antialiasing)
    return view


def sub_with_badge(live, *widgets):
    w = QWidget()
    v = QVBoxLayout(w)
    row = QHBoxLayout()
    row.addWidget(badge(live))
    row.addStretch(1)
    v.addLayout(row)
    for x in widgets:
        v.addWidget(x)
    return w


def explorer_tabs():
    tabs = QTabWidget()
    mm = table(["Base", "End", "Size", "Owner", "R", "W", "X", "Mapping"], [
        ("00000000", "0009FFFF", "640K", "ram_low", "✓", "✓", "✓", "ram"),
        ("000A0000", "000BFFFF", "128K", "Trident TVGA8900D", "✓", "✓", "", "svga->mapping"),
        ("000C0000", "000C7FFF", "32K", "Trident VGA BIOS", "✓", "", "✓", "rom"),
        ("000C8000", "000DFFFF", "96K", "(unmapped)", "", "", "", "—"),
        ("000E0000", "000EFFFF", "64K", "shadow (chipset)", "✓", "", "✓", "ram_mid"),
        ("000F0000", "000FFFFF", "64K", "MS-4144 BIOS", "✓", "", "✓", "bios"),
        ("00100000", "00FFFFFF", "15M", "ram_high", "✓", "✓", "✓", "ram"),
        ("FFFE0000", "FFFFFFFF", "128K", "BIOS (high alias)", "✓", "", "✓", "bios_high"),
    ], mono_cols=(0, 1))
    tabs.addTab(sub_with_badge(False, mem_map_bar(), mm,
                               QLabel("Double-click a row → Hex view at its base")), "Memory map")
    hexv = QPlainTextEdit()
    hexv.setLineWrapMode(QPlainTextEdit.NoWrap)
    hexv.setReadOnly(True)
    hexv.setFont(mono(10))
    hexv.setPlainText("\n".join(
        f"{0x8100 + i * 16:08X}  " + " ".join(f"{(0x8100 + i * 16 + j) * 37 & 0xFF:02X}" for j in range(16))
        for i in range(12)))
    addr = QHBoxLayout()
    addr.addWidget(QLabel("Address"))
    a = QLineEdit("phys 0x00008100")
    a.setFont(mono(10))
    addr.addWidget(a)
    sp = QComboBox()
    sp.addItems(["physical", "linear", "seg:off"])
    addr.addWidget(sp)
    addr.addWidget(QPushButton("Go"))
    addr.addWidget(QPushButton("Write back (0 edits)"))
    addr.addWidget(QPushButton("Watch range…"))
    aw = QWidget()
    aw.setLayout(addr)
    tabs.addTab(sub_with_badge(True, aw, hexv), "Hex")
    io = table(["Ports", "Device", "Read", "Write", "Last value", ""], [
        ("0020–0021", "PIC 1 (8259)", "pic_read", "pic_write", "—", "Read"),
        ("0040–0043", "PIT (8254)", "pit_read", "pit_write", "—", "Read"),
        ("0060, 0064", "Keyboard (8042)", "kbc_read", "kbc_write", "0x1C", "Read"),
        ("01F0–01F7", "IDE primary", "ide_readb", "ide_writeb", "0x50", "Read"),
        ("0220–022F", "Sound Blaster 16", "sb_read", "sb_write", "—", "Read"),
        ("0300–031F", "NE2000", "ne2k_read", "ne2k_write", "—", "Read"),
        ("03C0–03DF", "Trident TVGA8900D", "tvga_in", "tvga_out", "—", "Read"),
        ("03F8–03FF", "COM1 (16550)", "serial_read", "serial_write", "0x60", "Read"),
    ], mono_cols=(0,), stretch=1)
    warn = QLabel("io.read has side effects (reading 0x60 consumes a scancode). Reads only while paused.")
    warn.setObjectName("warnbox")
    tabs.addTab(sub_with_badge(False, io, warn), "IO map")
    disk = QWidget()
    dv = QVBoxLayout(disk)
    dsel = QComboBox()
    dsel.addItems(["hdd.0  IDE pri master  dos622_c.img  504 MB  (1024/16/63)", "cdrom.0  netdoom_cd.iso"])
    dv.addWidget(dsel)
    scene = QGraphicsScene()
    parts = [(0.01, "MBR", (240, 180, 180)), (0.62, "C: FAT16  312 MB", (196, 216, 248)),
             (0.37, "free 190 MB", (236, 236, 232))]
    x = 0.0
    for frac, label, c in parts:
        scene.addRect(QRectF(x, 0, 900 * frac, 30), QPen(QColor(*theme.BORDER)), QBrush(QColor(*c)))
        if frac > 0.05:
            t = scene.addText(label)
            t.setFont(QFont("Sans", 8))
            t.setPos(x + 4, 4)
        x += 900 * frac
    pv = QGraphicsView(scene)
    pv._scene = scene
    pv.setFixedHeight(48)
    dv.addWidget(pv)
    dv.addWidget(table(["#", "Type", "Start LBA", "Sectors", "Boot"], [
        ("1", "06 FAT16", "63", "639,009", "✓"), ("2", "—", "", "", "")], mono_cols=(2, 3)))
    sec = QPlainTextEdit()
    sec.setLineWrapMode(QPlainTextEdit.NoWrap)
    sec.setReadOnly(True)
    sec.setFont(mono(9))
    sec.setPlainText("LBA 0  (MBR)\n000  FA 33 C0 8E D0 BC 00 7C  8B F4 50 07 50 1F FB FC  .3.....|..P.P...\n"
                     "1B0  00 00 00 00 00 00 00 00  00 00 00 00 00 00 80 01  ................\n"
                     "1F0  00 00 00 00 00 00 00 00  00 00 00 00 00 00 55 AA  ..............U.")
    dv.addWidget(sec)
    tabs.addTab(sub_with_badge(False, disk), "Disk map")
    dev = QTreeWidget()
    dev.setHeaderLabels(["Device", "Bus", "Resources"])
    for bus, items in [("ISA", [("Sound Blaster 16", "220h IRQ5 DMA1/5"), ("NE2000", "300h IRQ10")]),
                       ("VLB", [("Trident TVGA8900D", "3C0h A0000 C0000")]),
                       ("Onboard", [("IDE primary", "1F0h IRQ14"), ("COM1", "3F8h IRQ4"),
                                    ("8042 KBC", "60h/64h IRQ1")])]:
        b = QTreeWidgetItem(dev, [bus])
        for n, r in items:
            QTreeWidgetItem(b, [n, bus, r])
    dev.expandAll()
    dev.setColumnWidth(0, 220)
    tabs.addTab(sub_with_badge(False, dev), "Devices")
    return tabs


def page_monitor():
    w = QWidget()
    v = QVBoxLayout(w)
    head = QHBoxLayout()
    h = QLabel("p2 — paused at breakpoint exec @0008:810F")
    h.setObjectName("h1")
    head.addWidget(h)
    head.addStretch(1)
    v.addLayout(head)
    top = QHBoxLayout()
    top.addWidget(group("Registers", regs_widget()))
    dis = QPlainTextEdit()
    dis.setLineWrapMode(QPlainTextEdit.NoWrap)
    dis.setReadOnly(True)
    dis.setFont(mono(10))
    dis.setPlainText("\n".join([
        "   0008:8100  B8 00 1F      mov  ax, 1F00h",
        "   0008:8103  8E D8         mov  ds, ax",
        "   0008:8105  BB 04 00      mov  bx, 0004h",
        "   0008:8108  EB 05         jmp  810Fh",
        "   0008:810A  90            nop",
        "●▶ 0008:810F  8B 47 04      mov  ax, [bx+04h]      ; ds:0008 = 1F00",
        "   0008:8112  CD 21         int  21h",
        "   0008:8114  EB F2         jmp  8108h",
        "   0008:8116  C3            ret",
    ]))
    dw = QWidget()
    dl = QVBoxLayout(dw)
    dl.setContentsMargins(0, 0, 0, 0)
    br = QHBoxLayout()
    br.addWidget(badge(False))
    br.addWidget(QLabel("(client-side disassembler over mem.lin.read is an option)"))
    br.addStretch(1)
    dl.addLayout(br)
    dl.addWidget(dis)
    top.addWidget(group("Disassembly @ CS:EIP", dw), 1)
    bps = table(["", "Kind", "Where", "Cond", "Hits", "Scope"], [
        ("●", "exec", "0008:810F", "", "3", "p2"),
        ("○", "io write", "03F8", "al==0x0D", "0", "all nodes"),
        ("●", "mem write", "phys 0x41C–0x41D", "", "0", "p2"),
    ], mono_cols=(2,))
    bw = QWidget()
    bl = QVBoxLayout(bw)
    bl.setContentsMargins(0, 0, 0, 0)
    bl.addWidget(badge(True))
    bl.addWidget(bps)
    row = QHBoxLayout()
    for t in ("+ Break", "+ Watch", "Disable all"):
        row.addWidget(QPushButton(t))
    bl.addLayout(row)
    gb = group("Breakpoints / watchpoints", bw)
    gb.setFixedWidth(430)
    top.addWidget(gb)
    v.addLayout(top, 2)
    v.addWidget(explorer_tabs(), 3)
    return w


def snap_tree_view():
    """Static family tree in the look of snapview.SnapsPanel: bundle snaps blue, single-node grey."""
    scene = QGraphicsScene()
    boxes = {  # id: (x, y, title, sub, bundle, here)
        "start": (0, 80, "power on", "", True, False),
        "a": (180, 80, "boot-ok", "p1 p2 p3", True, False),
        "b": (360, 20, "lvl1-start", "p1 p2 p3", True, False),
        "c": (360, 150, "p2-debug", "p2 only", False, False),
        "d": (540, 20, "lvl2-start", "p1 p2 p3", True, True),
        "e": (540, 150, "desync-repro", "p1 p2 p3", True, False),
    }
    edges = [("start", "a"), ("a", "b"), ("a", "c"), ("b", "d"), ("b", "e")]
    pen = QPen(QColor(*theme.MUTED), 1.5)
    for s, d in edges:
        xs, ys = boxes[s][0] + 140, boxes[s][1] + 25
        xd, yd = boxes[d][0], boxes[d][1] + 25
        scene.addLine(xs, ys, xd, yd, pen)
    for k, (x, y, title, sub, bundle, here) in boxes.items():
        c = theme.SNAP_GROUP if bundle else theme.SNAP_OTHER
        border = QPen(QColor(*theme.ACCENT), 2.5) if here else QPen(QColor(*theme.BORDER), 1)
        scene.addRect(QRectF(x, y, 140, 50), border, QBrush(QColor(*c)))
        t = scene.addText(title + ("  ◀ now" if here else ""))
        t.setFont(QFont("Sans", 9, QFont.Bold))
        t.setPos(x + 4, y + 2)
        s2 = scene.addText(sub)
        s2.setFont(QFont("Sans", 8))
        s2.setDefaultTextColor(QColor(*theme.MUTED))
        s2.setPos(x + 4, y + 24)
    v = QGraphicsView(scene)
    v._scene = scene
    v.setRenderHint(QPainter.Antialiasing)
    return v


def page_snapshots():
    w = QWidget()
    v = QVBoxLayout(w)
    head = QHBoxLayout()
    head.addWidget(QLabel("Scope:"))
    a = QRadioButton("This VM (p2)")
    b = QRadioButton("Project bundle (netdoom-3p)")
    b.setChecked(True)
    head.addWidget(a)
    head.addWidget(b)
    head.addStretch(1)
    for t in ("Take bundle", "Restore", "Rename", "Delete", "Export bundle…", "Import…"):
        head.addWidget(QPushButton(t))
    v.addLayout(head)
    sp = QSplitter()
    left = QSplitter(Qt.Vertical)
    left.addWidget(group("Family tree", snap_tree_view()))
    lst = QTreeWidget()
    lst.setHeaderLabels(["Snapshot", "Taken", "Size", "Note"])
    for tag, when, size, note, members in [
        ("lvl2-start", "11:58:02", "41 MB", "all three in level 2 lobby", ["p1 · lvl2-start", "p2 · lvl2-start", "p3 · lvl2-start"]),
        ("desync-repro", "11:40:51", "40 MB", "p3 desyncs ~20 s after this", ["p1", "p2", "p3"]),
        ("lvl1-start", "11:31:15", "39 MB", "", ["p1", "p2", "p3"]),
        ("boot-ok", "11:22:03", "36 MB", "DOS + IPX loaded", ["p1", "p2", "p3"]),
    ]:
        it = QTreeWidgetItem(lst, [tag, when, size, note])
        for m in members:
            QTreeWidgetItem(it, ["   " + m, when, "", ""])
    lst.topLevelItem(0).setExpanded(True)
    lst.setColumnWidth(0, 170)
    left.addWidget(group("Bundle snapshots (members under each)", lst))
    sp.addWidget(left)
    det = QWidget()
    dv = QVBoxLayout(det)
    dv.addWidget(group("Details", kv_grid([
        ("Tag", "lvl2-start"), ("Scope", "bundle · 3 nodes"), ("Taken", "2026-10-17 11:58:02"),
        ("Halted for", "1.4 s (halt-all, then save on each)"), ("Size", "41 MB (p1 13, p2 14, p3 14)"),
        ("Parent", "lvl1-start"), ("Stored", "per node, in each VM dir"),
    ], cols=1)))
    note = QPlainTextEdit("all three in level 2 lobby, p2 ready")
    note.setLineWrapMode(QPlainTextEdit.NoWrap)
    note.setMaximumHeight(80)
    dv.addWidget(group("Note", note))
    dv.addWidget(group("Preview (p1 / p2 / p3)", None, _thumbs()))
    dv.addStretch(1)
    sp.addWidget(det)
    sp.setSizes([760, 420])
    v.addWidget(sp)
    return w


def _thumbs():
    h = QHBoxLayout()
    for n in ("p1", "p2", "p3"):
        s = GuestScreen(DOS_SCREEN[:6], caption=n)
        s.setMinimumSize(120, 90)
        h.addWidget(s)
    return h


# 86Box Settings, mirrored 1:1 (see 86box_ui_parity.md). Row: (label, kind, value, tag)
# kind: combo / cfgcombo (combo + Configure) / check / spin / text / radio / "=" (sub-heading)
# tag: reset = config.load, applies at reset · live = changes now · list = choices need a new
#      RMM method · host = a list from the 86Box host · new = needs new RMM support
VM_SETTINGS = {
    "Machine": [
        ("Machine type", "combo", "[1992] i486 (Socket 3)", "reset"), ("Machine", "cfgcombo", "MSI MS-4144", "reset"),
        ("CPU type", "combo", "Intel i486DX2", "reset"), ("Frequency", "combo", "66 MHz", "list"),
        ("FPU", "combo", "Internal", "list"), ("Wait states", "combo", "Default", "list"),
        ("PIT mode", "combo", "Auto", "reset"), ("Memory", "spin", "16384 KB", "live"),
        ("Time synchronization", "radio", "Disabled|Enabled (local time)|Enabled (UTC)", "reset"),
        ("", "check", "Softfloat FPU (high performance impact)", "reset"),
        ("", "check", "Use the 486 interpreter for 286 and 386 processors", "reset"),
        ("", "check", "Dynamic recompiler", "reset"),
        ("", "check", "Accurate FPU environment on the dynamic recompiler", "reset"),
        ("CPU frame size", "radio", "Larger frames (less smooth)|Smaller frames (smoother)", "reset"),
    ],
    "Display": [
        ("Video", "cfgcombo", "Trident TVGA8900D", "reset"), ("Video #2", "cfgcombo", "None", "list"),
        ("", "check", "Voodoo 1 or 2 Graphics", "reset"), ("", "check", "IBM 8514/A Graphics", "reset"),
        ("", "check", "XGA Graphics", "reset"), ("", "check", "IBM PS/55 Display Adapter Graphics", "reset"),
        ("VGA screen type", "combo", "Color", "new"), ("Grayscale conversion", "combo", "BT601 (NTSC/PAL)", "new"),
        ("Monitor EDID", "radio", "Default|Custom…", "reset"),
        ("", "check", "Overscan", "new"), ("", "check", "Inverted VGA monitor", "new"),
        ("", "check", "Change contrast for monochrome display", "new"),
    ],
    "Input devices": [
        ("Keyboard", "cfgcombo", "AT keyboard", "list"), ("Mouse", "cfgcombo", "PS/2 mouse", "list"),
        ("Tablet", "cfgcombo", "None", "list"), ("Joystick", "combo", "2-axis, 2-button", "list"),
        ("Joystick mapping", "text", "Joystick 1…  2…  3…  4…  (host devices)", "host"),
    ],
    "Sound": [
        ("Sound card 1", "cfgcombo", "Sound Blaster 16", "reset"), ("Sound card 2", "cfgcombo", "None", "list"),
        ("Sound card 3", "cfgcombo", "None", "list"), ("Sound card 4", "cfgcombo", "None", "list"),
        ("FM synth driver", "combo", "Nuked (more accurate)", "list"), ("", "check", "Use FLOAT32 sound", "reset"),
        ("Audio output device", "combo", "Default", "host"), ("Sample rate", "combo", "48000 Hz", "reset"),
        ("", "check", "Enable audio input (mic/line-in)", "reset"), ("Audio input device", "combo", "Default", "host"),
        ("Audio input sample rate", "combo", "48000 Hz", "reset"),
        ("MIDI Out device", "cfgcombo", "None", "host"), ("MIDI In device", "cfgcombo", "None", "host"),
        ("", "check", "Standalone MPU-401", "reset"), ("Sound gain", "spin", "0 dB", "live"),
        ("", "check", "Mute", "live"),
    ],
    "Network": [
        ("=", "", "NIC 1", ""), ("Mode", "combo", "VDE", "reset"), ("Adapter", "cfgcombo", "NE2000", "reset"),
        ("VDE socket", "text", "/tmp/vde_netdoom.ctl", "reset"), ("Interface (pcap)", "combo", "—", "host"),
        ("TAP bridge device", "text", "", "reset"), ("Switch: shared secret / hub mode / hostname", "text", "", "reset"),
        ("=", "", "NIC 2–4", ""), ("Mode", "combo", "None", "reset"),
    ],
    "Ports (COM & LPT)": [
        ("Internal LPT ECP DMA", "combo", "Auto", "list"),
        ("LPT1", "cfgcombo", "None", "list"), ("LPT2", "cfgcombo", "(off)", "list"),
        ("COM1", "cfgcombo", "None", "list"), ("COM2", "cfgcombo", "None", "list"),
        ("COM3", "cfgcombo", "(off)", "list"), ("COM4", "cfgcombo", "(off)", "list"),
    ],
    "Storage controllers": [
        ("Floppy disk controller", "cfgcombo", "Internal", "list"), ("CD-ROM controller", "cfgcombo", "None", "list"),
        ("=", "", "Hard disk controllers", ""), ("Controller 1", "cfgcombo", "Internal", "list"),
        ("Controller 2", "cfgcombo", "None", "list"),
        ("=", "", "SCSI", ""), ("Controller 1", "cfgcombo", "None", "list"), ("", "check", "Cassette", "reset"),
    ],
    "Hard disks": [
        ("Disks", "text", "1: IDE (0:0)  dos622_c.img  504 MB  1024/16/63", "reset"),
        ("Bus / channel", "combo", "IDE · 0:0", "reset"), ("Model", "combo", "[Generic] RAM Disk (max. speed)", "list"),
        ("Audio", "combo", "None", "list"),
        ("New…", "text", "file · C/H/S · size · type · bus · channel · model · VHD format · block size", "new"),
        ("Existing… / Remove", "text", "", "reset"),
    ],
    "Floppy & CD-ROM drives": [
        ("=", "", "Floppy drives", ""), ("Drive 1 type", "combo", '3.5" 1.44M', "list"),
        ("", "check", "Turbo timings", "reset"), ("", "check", "Check BPB", "reset"), ("Audio", "combo", "None", "list"),
        ("=", "", "CD-ROM drives", ""), ("Bus / channel", "combo", "ATAPI · 0:1", "reset"),
        ("Speed", "combo", "8x", "reset"), ("Type", "combo", "86BOX CD-ROM", "list"),
        ("", "check", "Use EDC/ECC emulation", "reset"),
    ],
    "Other removable devices": [
        ("MO drives: bus / channel / type", "combo", "None", "list"),
        ("Removable disk (ZIP): bus / channel / type", "combo", "None", "list"),
        ("Tape drives: bus / channel / type", "combo", "None", "list"),
    ],
    "Other peripherals": [
        ("ISA RTC", "cfgcombo", "None", "list"), ("", "check", "ISABugger device", "reset"),
        ("", "check", "POST card", "reset"), ("", "check", "86Box Unit Tester", "reset"),
        ("", "check", "Novell NetWare 2.x Key Card", "reset"),
        ("", "check", "Software-initiated poweroff device (IBM 5140)", "reset"),
        ("ISA memory cards 1–4", "cfgcombo", "None", "list"), ("ISA ROM cards 1–4", "cfgcombo", "None", "list"),
        ("VFIO passthrough + custom ROM", "text", "(host IOMMU groups)", "host"),
    ],
    "Remote monitor": [
        ("", "check", "Enable remote machine monitor (RMM) over TCP", "new"), ("Port", "spin", "55860", "new"),
        ("Listen on", "combo", "This interface: 10.0.4.22 (eth0)", "new"), ("Allow only from", "text", "10.0.4.10", "new"),
        ("", "check", "Pause the machine when a monitor connects", "new"),
        ("Note", "text", "Set on the 86Box host: config.load refuses rmm_* keys, so a client cannot widen its own access",
         ""),
    ],
}

TAGS = {"reset": ("at reset", "badgeLive"), "live": ("live", "badgeLive"), "list": ("needs list", "badgeNeed"),
        "host": ("host list", "badgeNeed"), "new": ("needs RMM", "badgeNeed")}


def settings_form(rows):
    w = QWidget()
    g = QGridLayout(w)
    g.setColumnStretch(1, 1)
    r = 0
    for label, kind, value, tag in rows:
        if label == "=":
            h = QLabel(value)
            h.setObjectName("h1")
            g.addWidget(h, r, 0, 1, 4)
            r += 1
            continue
        if label:
            g.addWidget(QLabel(label + ":"), r, 0)
        if kind in ("combo", "cfgcombo"):
            c = QComboBox()
            c.addItem(value)
            g.addWidget(c, r, 1)
            if kind == "cfgcombo":
                g.addWidget(QPushButton("Configure"), r, 2)
        elif kind == "check":
            c = QCheckBox(value)
            c.setChecked(value in ("Dynamic recompiler", "Standalone MPU-401") or "Enable remote" in value)
            g.addWidget(c, r, 1)
        elif kind == "spin":
            e = QLineEdit(value)
            e.setFixedWidth(120)
            g.addWidget(e, r, 1)
        elif kind == "radio":
            box = QHBoxLayout()
            for k, opt in enumerate(value.split("|")):
                rb = QRadioButton(opt)
                rb.setChecked(k == 0)
                box.addWidget(rb)
            box.addStretch(1)
            g.addLayout(box, r, 1)
        else:
            t = QLabel(value)
            t.setObjectName("muted")
            t.setWordWrap(True)
            g.addWidget(t, r, 1)
        if tag:
            text, obj = TAGS[tag]
            b = QLabel(text)
            b.setObjectName(obj)
            g.addWidget(b, r, 3)
        r += 1
    g.setRowStretch(r, 1)
    return w


def page_configure():
    w = QWidget()
    v = QVBoxLayout(w)
    head = QHBoxLayout()
    h = QLabel("VM settings — p2  (dos622_486_sb16_ne2k)")
    h.setObjectName("h1")
    head.addWidget(h)
    head.addStretch(1)
    pend = QLabel("2 changes pending · apply at next hard reset")
    pend.setObjectName("badgeNeed")
    head.addWidget(pend)
    for t in ("Validate", "Apply (reset)", "Save to VM store", "New VM from this…"):
        head.addWidget(QPushButton(t))
    v.addLayout(head)
    body = QHBoxLayout()
    cats = QListWidget()
    cats.addItems(list(VM_SETTINGS))
    cats.setFixedWidth(200)
    stack = QStackedWidget()
    for rows in VM_SETTINGS.values():
        stack.addWidget(settings_form(rows))
    cats.currentRowChanged.connect(stack.setCurrentIndex)
    cats.setCurrentRow(3)                       # Sound: the category the user asked about
    body.addWidget(cats)
    body.addWidget(group("Sound", stack), 3)
    cats.currentTextChanged.connect(lambda t: stack.parentWidget().setTitle(t))
    prev = QPlainTextEdit()
    prev.setLineWrapMode(QPlainTextEdit.NoWrap)
    prev.setReadOnly(True)
    prev.setFont(mono(9))
    prev.setPlainText("\n".join([
        "86box.cfg (as config.dump)", "", "[Sound]", "sndcard = sb16", "fm_driver = nuked",
        "midi_device = none", "mpu401_standalone = 1", "sound_type = float", "", "[General]",
        "sound_gain = 0", "sound_muted = 0", "", "pending (config.load, at reset):",
        "  [Sound] sound_sample_rate 44100 → 48000", "  [Sound] mpu401_standalone 0 → 1"]))
    body.addWidget(group("Config", prev), 2)
    v.addLayout(body)
    legend = QHBoxLayout()
    for tag in TAGS:
        text, obj = TAGS[tag]
        b = QLabel(text)
        b.setObjectName(obj)
        legend.addWidget(b)
    lg = QLabel("at reset = config.load + hard reset · live = applies now · needs list / host list / needs RMM = "
                "86Box can't report the choices yet (86box_ui_parity.md)")
    lg.setObjectName("muted")
    legend.addWidget(lg, 1)
    v.addLayout(legend)
    return w


def dlg_new_project():
    d = QDialog()
    d.setWindowTitle(f"New project — {MOCK}")
    v = QVBoxLayout(d)
    f = QFormLayout()
    f.addRow("Name", QLineEdit("netdoom-3p"))
    f.addRow("Description", QLineEdit("3-player IPX Doom over a shared VDE switch"))
    v.addLayout(f)
    nodes = table(["Node", "Source", "VM config", "Host : port", "On exit"], [
        ("p1", "Attach remote", "—", "10.0.4.21 : 55860", "—"),
        ("p2", "Attach remote", "—", "10.0.4.22 : 55860", "—"),
        ("p3", "Launch on this host", "dos622_486_sb16_ne2k", "127.0.0.1 : auto", "quit"),
    ], stretch=2)
    nodes.setMinimumHeight(130)
    nb = QHBoxLayout()
    for t in ("+ Attach remote…", "+ Launch local…", "Remove"):
        nb.addWidget(QPushButton(t))
    nb.addStretch(1)
    nw = QWidget()
    nwl = QVBoxLayout(nw)
    nwl.addWidget(nodes)
    nwl.addLayout(nb)
    v.addWidget(group("Nodes", nw))
    pol = QWidget()
    pf = QFormLayout(pol)
    hp = QComboBox()
    hp.addItems(["Halt all nodes when one stops", "Only the node that stopped"])
    pf.addRow("Halt policy", hp)
    pf.addRow("", QCheckBox("Snapshots are taken as a bundle by default"))
    lk = QLineEdit("p1.com1 ⇄ p2.com1 (null-modem)   ·   p1,p2,p3 ne2000 → vde:netdoom")
    pf.addRow("Links", lk)
    v.addWidget(group("Policy & links", pol))
    v.addWidget(QLabel("Saved as rmm-group/1 JSON (same format as groups/pair_launch.json) plus a snapshot index."))
    bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    v.addWidget(bb)
    d.resize(820, 520)
    return d


def prefs_widget(buttons):
    d = QWidget()
    h = QHBoxLayout(d)
    cats = QListWidget()
    cats.addItems(["General", "Remote connections", "Local 86Box", "Console & polling", "Keys", "Appearance"])
    cats.setCurrentRow(2)
    cats.setFixedWidth(170)
    h.addWidget(cats)
    st = QWidget()
    f = QFormLayout(st)
    f.addRow("86Box binary", QLineEdit("GIT_SRC_ROOT/A/86Box/build/regular/src/86Box"))
    pin = QComboBox()
    pin.addItems(["Build A pin (cluster)", "Build B pin (cluster)", "Path above"])
    f.addRow("Build", pin)
    f.addRow("VM store", QLineEdit("~/.rmm_86box/domains"))
    f.addRow("VM dirs", QLineEdit("/tmp/rmm_groups/<project>/<node>"))
    f.addRow("", QCheckBox("Allow launching local 86Box (off = remote-only console)"))
    ex = QComboBox()
    ex.addItems(["Quit 86Box when the console closes", "Leave it running"])
    f.addRow("On console exit", ex)
    f.addRow("Default monitor listen", QLineEdit("127.0.0.1"))
    note = QLabel("Default is the remote console: attach to 86Box on other hosts by host:port.\n"
                  "Launching 86Box here is optional, per VM.")
    note.setObjectName("muted")
    f.addRow(note)
    v = QVBoxLayout()
    v.addWidget(group("Local 86Box", st))
    v.addStretch(1)
    v.addWidget(QDialogButtonBox(buttons))
    h.addLayout(v, 1)
    return d


def dlg_preferences():
    d = QDialog()
    d.setWindowTitle(f"Settings — {MOCK}")
    QVBoxLayout(d).addWidget(prefs_widget(QDialogButtonBox.Ok | QDialogButtonBox.Cancel))
    d.resize(760, 420)
    return d


def dlg_86box_remote():
    """The proposed page in 86Box's own Settings dialog (old core UI), drawn here for review."""
    d = QDialog()
    d.setWindowTitle(f"86Box Settings — proposed page — {MOCK}")
    h = QHBoxLayout(d)
    cats = QListWidget()
    cats.addItems(["Machine", "Display", "Input devices", "Sound", "Network", "Ports (COM & LPT)",
                   "Storage controllers", "Hard disks", "Floppy & CD-ROM drives", "Other removable devices",
                   "Other peripherals", "Remote monitor"])
    cats.setCurrentRow(11)
    cats.setFixedWidth(200)
    h.addWidget(cats)
    st = QWidget()
    f = QFormLayout(st)
    en = QCheckBox("Enable remote machine monitor (RMM) over TCP")
    en.setChecked(True)
    f.addRow(en)
    port = QSpinBox()
    port.setRange(1, 65535)
    port.setValue(55860)
    f.addRow("Port", port)
    lst = QComboBox()
    lst.addItems(["Local only (127.0.0.1)", "Any address (0.0.0.0)", "This interface: 10.0.4.22 (eth0)"])
    lst.setCurrentIndex(2)
    f.addRow("Listen on", lst)
    f.addRow("Allow only from", QLineEdit("10.0.4.10"))
    hint = QLabel("IP or CIDR (10.0.4.0/24); empty = any peer that can reach the listen address.\n"
                  "Other peers are disconnected at accept and logged.")
    hint.setObjectName("muted")
    f.addRow("", hint)
    f.addRow("", QCheckBox("Pause the machine when a monitor connects"))
    w = QLabel("⚠ The monitor protocol has no authentication. A connected client can pause the machine, "
               "read and write guest memory and I/O ports, and load snapshots. Listen beyond 127.0.0.1 only "
               "on a trusted network, and set “Allow only from”.")
    w.setObjectName("warnbox")
    w.setWordWrap(True)
    f.addRow(w)
    cfg = QLabel("86box.cfg [General]:  rmm_enabled = 1 · rmm_port = 55860 · rmm_listen = 10.0.4.22 · "
                 "rmm_allow = 10.0.4.10\ncommand line:  --monitor-port 55860 --monitor-listen 10.0.4.22 "
                 "--monitor-allow 10.0.4.10")
    cfg.setFont(mono(8))
    cfg.setObjectName("muted")
    f.addRow(cfg)
    v = QVBoxLayout()
    v.addWidget(group("Remote monitor", st))
    v.addStretch(1)
    v.addWidget(QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel))
    h.addLayout(v, 1)
    d.resize(880, 470)
    return d


DIALOGS = [("new_project", dlg_new_project), ("settings", dlg_preferences), ("86box_remote_monitor", dlg_86box_remote)]


# -- window ------------------------------------------------------------------------------------

class Mockup(QMainWindow):
    """Pages in the middle; everything else is a dock (snap to any edge, tab together, or float
    as its own window) or a toolbar (drag by its grip to any edge, or float)."""

    PAGES = [("Summary", page_summary), ("Screen", page_screen), ("Monitor / Debug", page_monitor),
             ("Snapshots", page_snapshots), ("VM Settings", page_configure)]

    def __init__(self, keep_layout=True):
        super().__init__()
        self.setWindowTitle(f"RMM Console — {MOCK}")
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AllowTabbedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AnimatedDocks)
        self._dialogs = []
        self.tabs = QTabWidget()
        for name, fn in self.PAGES:
            self.tabs.addTab(fn(), name)
        self.setCentralWidget(self.tabs)
        self.toolbars = self._toolbars()
        self.docks = self._docks()
        self._menus()
        mk = QLabel(MOCK)
        mk.setObjectName("mock")
        self.statusBar().addPermanentWidget(mk)
        self.statusBar().showMessage("netdoom-3p · p1 running · p2 PAUSED (bp) · p3 running [local]   ·   "
                                     "3 monitors connected   ·   right-click a toolbar or dock title for layout")
        self.resize(1440, 900)
        self._default_state = self.saveState()
        self.keep_layout = keep_layout
        if keep_layout:
            st = QSettings("rmm_86box", "console_mockup")
            if st.value("geometry"):
                self.restoreGeometry(st.value("geometry"))
            if st.value("state"):
                self.restoreState(st.value("state"))

    def closeEvent(self, ev):
        if self.keep_layout:
            st = QSettings("rmm_86box", "console_mockup")
            st.setValue("geometry", self.saveGeometry())
            st.setValue("state", self.saveState())
        super().closeEvent(ev)

    # -- toolbars ----------------------------------------------------------------------------

    def _bar(self, name, area=Qt.TopToolBarArea):
        tb = QToolBar(name)
        tb.setObjectName(name)                    # saveState() keys on object names
        tb.setMovable(True)
        tb.setFloatable(True)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(area, tb)
        return tb

    def _toolbars(self):
        machine = self._bar("Machine")
        for kind, tip in (("play", "Resume (F5)"), ("pause", "Pause (F6)"), ("stop", "Power off"), (None, None),
                          ("step", "Step instruction (F10)"), ("stepover", "Step over (F11)"),
                          ("stepout", "Run to return (Shift+F11)"), (None, None), ("reset", "Hard reset")):
            if kind is None:
                machine.addSeparator()
            else:
                machine.addAction(icon(kind), tip).setToolTip(tip)
        snaps = self._bar("Snapshots")
        snaps.addAction(icon("snapshot"), "Take snapshot (Ctrl+S)").setToolTip("Take snapshot (Ctrl+S)")
        rb = QToolButton()
        rb.setIcon(icon("restore"))
        rb.setToolTip("Restore snapshot")
        rb.setPopupMode(QToolButton.MenuButtonPopup)
        m = QMenu(rb)
        for t in ("lvl2-start  (bundle, 11:58)", "desync-repro  (bundle, 11:40)", "lvl1-start  (bundle, 11:31)",
                  "p2-debug  (p2 only)"):
            m.addAction(t)
        rb.setMenu(m)
        snaps.addWidget(rb)
        nodes = self._bar("Nodes")
        nodes.addAction(icon("attach"), "Attach to remote 86Box…").setToolTip("Attach to remote 86Box (host:port)")
        nodes.addAction(icon("launch"), "Launch 86Box on this host…").setToolTip("Launch 86Box on this host")
        sel = QComboBox()
        sel.addItems(["netdoom-3p (3 nodes)", "dos622-16m", "win311-test"])
        nodes.addWidget(sel)
        return [machine, snaps, nodes]

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
        inv = self._dock("Inventory", Inventory(), Qt.LeftDockWidgetArea)
        con = self._dock("Console", console_widget(), Qt.BottomDockWidgetArea)
        tl = self._dock("Snapshot timeline", timeline_widget(), Qt.BottomDockWidgetArea)
        tasks = self._dock("Recent tasks", make_tasks(), Qt.BottomDockWidgetArea)
        sett = self._dock("Settings", prefs_widget(QDialogButtonBox.Apply | QDialogButtonBox.Reset),
                          Qt.RightDockWidgetArea)
        self.splitDockWidget(con, tl, Qt.Horizontal)       # console | timeline side by side
        self.tabifyDockWidget(tl, tasks)                    # tasks tabbed behind the timeline
        tl.raise_()
        sett.hide()                                         # View → Settings, or float it out
        self.resizeDocks([inv], [330], Qt.Horizontal)
        self.resizeDocks([con], [330], Qt.Vertical)
        return {"inventory": inv, "console": con, "timeline": tl, "tasks": tasks, "settings": sett}

    def break_out(self, *names):
        """Float docks as their own windows, laid out beside the main one (View → Break out)."""
        x = self.geometry().right() - 40
        for i, n in enumerate(names):
            d = self.docks[n]
            d.show()
            d.setFloating(True)
            d.resize(720, 360)
            d.move(x - 720 + i * 60, self.geometry().top() + 120 + i * 300)

    def dock_all(self):
        for d in self.docks.values():
            d.setFloating(False)

    # -- menus -------------------------------------------------------------------------------

    def _menus(self):
        mb = self.menuBar()
        spec = {
            "&File": ["New VM…", "New Project…", "-", "Open Project…", "Recent Projects ▸", "Save Project",
                      "Save Project As…", "-", "Import VM config…", "Export bundle…", "-", "Quit"],
            "&Edit": ["Copy screen", "Copy registers", "Copy console output", "-", "Paste into guest"],
            "&View": [],
            "S&creen": ["Scale ▸ 0.5x 1x 1.5x 2x 3x 4x 5x 6x 7x 8x", "Filter ▸ Nearest / Linear", "HiDPI scaling", "-",
                        "Force 4:3 display ratio", "Force machine display ratio", "Specify dimensions…",
                        "Fullscreen stretch ▸ Full / 4:3 / Square pixels / Integer / 4:3 integer", "-",
                        "Show non-primary monitors", "CGA composite settings…", "Overscan", "Grayscale ▸",
                        "-", "Take screenshot", "Take raw screenshot", "Copy screenshot", "Copy raw screenshot"],
            "&Media": ["Floppy 1 (3.5\" 1.44M): (empty) ▸ New image… / Existing… / Existing (write-protected)… / "
                       "Reload previous / Export to 86F… / Eject",
                       "CD-ROM 1 (ATAPI 0:1): netdoom_cd.iso ▸ Image… / Folder… / Host drive / Reload / Eject",
                       "MO / Removable disk / Tape ▸ New… / Existing… / Eject",
                       "Cassette ▸ New… / Existing… / Record / Play / Rewind / Fast forward / Eject",
                       "Cartridge ▸ Image… / Eject", "NIC 1 (NE2000, VDE) ▸ Connected", "-", "Clear image history"],
            "V&M": ["Attach to remote…", "Launch on this host…", "-", "Power on", "Hard reset", "Ctrl+Alt+Del",
                    "Ctrl+Alt+Esc", "Non-maskable interrupt", "ACPI shutdown", "Quit 86Box", "-", "Fast forward",
                    "Force interpretation", "Update mouse every CPU frame", "Keyboard requires capture",
                    "Right Ctrl is left Alt", "-", "Settings…", "MCA devices…", "Open printer tray…"],
            "&Debug": ["Resume  F5", "Pause", "Step  F10", "Step over  F11", "Run to return", "-",
                       "Breakpoints…", "Watchpoints…", "-", "Begin trace", "End trace", "-", "Halt policy ▸",
                       "Open Monitor on pause"],
            "&Snapshots": ["Take (this VM)", "Take bundle (project)", "Restore…", "-", "Snapshot manager…"],
            "&Project": ["Add node…", "Remove node", "Links…", "-", "Properties…"],
            "Se&ttings": ["Preferences…", "Sound gain…", "Mute", "86Box remote monitor page (proposed)…", "Key bindings…"],
            "&Help": ["Console commands", "RMM protocol", "About"],
        }
        hooks = {"New Project…": 0, "86Box remote monitor page (proposed)…": 2}
        for title, items in spec.items():
            m = mb.addMenu(title)
            if title == "&View":
                self._view_menu(m)
                continue
            for it in items:
                if it == "-":
                    m.addSeparator()
                    continue
                label, _, rest = it.partition(" ▸ ")
                if rest:                         # "Name ▸ a / b / c" (or space-separated) is a submenu
                    sub = m.addMenu(label)
                    for x in (rest.split(" / ") if " / " in rest else rest.split()):
                        sub.addAction(x)
                    continue
                act = QAction(it, self)
                if it in hooks:
                    act.triggered.connect(lambda _=False, i=hooks[it]: self._open(i))
                elif it == "Preferences…":
                    act.triggered.connect(lambda: self.break_out("settings"))
                elif it == "Quit":
                    act.triggered.connect(self.close)
                elif it in ("New VM…", "Settings…"):
                    act.triggered.connect(lambda: self.tabs.setCurrentIndex(4))
                elif it == "Snapshot manager…":
                    act.triggered.connect(lambda: self.tabs.setCurrentIndex(3))
                m.addAction(act)

    def _view_menu(self, m):
        m.addSection("Panels")
        for d in self.docks.values():
            m.addAction(d.toggleViewAction())
        m.addSection("Toolbars")
        for tb in self.toolbars:
            m.addAction(tb.toggleViewAction())
        m.addSeparator()
        a = m.addAction("Break out console + timeline + settings")
        a.triggered.connect(lambda: self.break_out("console", "timeline", "settings"))
        m.addAction("Dock everything back").triggered.connect(self.dock_all)
        m.addAction("Reset layout").triggered.connect(lambda: self.restoreState(self._default_state))
        m.addSeparator()
        m.addAction("Full-screen guest")

    def _open(self, i):
        d = DIALOGS[i][1]()
        d.setStyleSheet(theme.stylesheet() + EXTRA_QSS)
        self._dialogs.append(d)
        d.show()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", type=int, default=2)
    ap.add_argument("--save-png", metavar="DIR")
    a = ap.parse_args(argv)
    if a.save_png:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;*.warning=false")
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.stylesheet() + EXTRA_QSS)
    win = Mockup(keep_layout=not a.save_png)
    win.tabs.setCurrentIndex(a.page)
    if a.save_png:
        out = a.save_png
        os.makedirs(out, exist_ok=True)
        win.show()
        for i, (name, _) in enumerate(Mockup.PAGES):
            win.tabs.setCurrentIndex(i)
            app.processEvents()
            win.grab().save(os.path.join(out, f"{i}_{name.split()[0].lower()}.png"))
        for i, tb in enumerate(win.toolbars):
            tb.grab().save(os.path.join(out, f"toolbar_{i}_{tb.objectName().lower()}.png"))
        win.tabs.setCurrentIndex(2)
        win.break_out("console", "timeline", "settings")
        app.processEvents()
        win.grab().save(os.path.join(out, "broken_out_main.png"))
        for n in ("console", "timeline", "settings"):
            win.docks[n].grab().save(os.path.join(out, f"broken_out_{n}.png"))
        for name, fn in DIALOGS:
            d = fn()
            d.show()
            app.processEvents()
            d.grab().save(os.path.join(out, f"dlg_{name}.png"))
        return 0
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
