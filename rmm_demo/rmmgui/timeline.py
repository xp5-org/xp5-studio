# SPDX-License-Identifier: MIT
"""The Snapshot timeline dock: the group's snapshots as a video-editor track view, with
lock / unlock (hostgroup.py) per node.

    ruler     one diamond per group snapshot, spaced by when it was taken; "now" at the right
    lanes     one per node. A locked lane follows the group: its dots are the group snapshots it
              is in (a ring marks its head). An unlocked lane's group bar stops at the snapshot
              it branched from; below it, its own track runs for as long as it has run alone,
              with its own snapshots on it
    scrubber  click a column's circle: the locked nodes jump there (restore)

Clicks: a lane's lock icon unlocks / re-locks it; a dot on an unlocked lane (or on any own
track) restores that node alone to it; a dot on a locked lane picks the snapshot (Restore and
Roll back use it). The transport bar has the group's buttons, then the selected node's.

Every size here is fixed by the node count alone (each lane reserves its own-track row), so
unlocking, snapping or restoring never moves the dock or the window.
"""

import collections
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QToolButton, QToolTip, QVBoxLayout, QWidget)

from . import theme as T
from .icons import icon
from .snapview import SnapsPanel
from .view import Elided

GUTTER, RIGHT, HEAD, MAIN, OWN, GAP, SCRUB = 150, 30, 38, 24, 22, 6, 40
MIN_COL = 46                     # px between two group snapshot columns at least
OWN_FILL = (253, 226, 190)


# -- the model one paint draws (no Qt) ----------------------------------------------------------

def track_model(g, now=None):
    """What the track view shows, in seconds and record ids:
    {"t0", "now", "cols": [group records by time], "head": the locked nodes' common head,
     "lanes": [{"name", "locked", "state", "head", "recs": [records holding it],
                "fork": its fork or None, "bp": branch point id, "own": [own records]}]}"""
    now = now or time.time()
    tree = g.tree
    with tree.lock:
        recs = sorted(tree.recs.values(), key=lambda r: (r.t, r.id))
        heads = dict(tree.heads)
    cols = [r for r in recs if r.scope == "group"]
    t0 = min([g.t0] + [r.t for r in recs])
    locked = g.locked
    forks = dict(g.forks)
    lanes = []
    for name, n in list(g.nodes.items()):
        fork = forks.get(name)
        lanes.append({"name": name, "locked": name in locked, "state": n.state, "head": heads.get(name),
                      "recs": [r for r in recs if name in r.nodes], "fork": fork,
                      "bp": g.branch_point(name) if fork else None,
                      "own": g.own_records(name) if fork else []})
    c = collections.Counter(heads.get(x) for x in g.nodes if x in locked)
    head = c.most_common(1)[0][0] if c else None
    return {"t0": t0, "now": max(now, t0 + 1.0), "cols": cols, "head": head, "lanes": lanes}


def col_neighbours(m):
    """(previous, next) group record around the locked nodes' head, for the transport."""
    cols = [r for r in m["cols"] if r.live()]
    ids = [r.id for r in cols]
    if m["head"] in ids:
        i = ids.index(m["head"])
        return (cols[i - 1] if i > 0 else None), (cols[i + 1] if i + 1 < len(cols) else None)
    return (cols[-1] if cols else None), None


def rollback_target(g, m):
    """The snapshot Roll back offers: the picked one if every node holds it, else the first
    unlocked node's branch point, else the newest one every node holds."""
    every = [r for r in m["cols"] if all(r.live(x) and x in r.nodes for x in g.nodes)]
    ok = {r.id for r in every}
    sel = g.tree.selected
    if sel in ok:
        return g.tree.recs[sel]
    for ln in m["lanes"]:
        if ln["fork"] and ln["bp"] in ok:
            return g.tree.recs[ln["bp"]]
    return every[-1] if every else None


# -- the track view ---------------------------------------------------------------------------

class TrackPlayer(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.hits = []                           # (QRectF, tooltip, fn or None), drawn order
        self.setMouseTracking(True)
        self._rows = 0
        self.small = QFont(self.font())
        self.small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))
        self.bold = QFont(self.font())
        self.bold.setBold(True)
        self.resize_for(1)

    def resize_for(self, rows):
        if rows != self._rows:
            self._rows = rows
            self.setMinimumSize(420, HEAD + rows * (MAIN + OWN + GAP) + SCRUB)

    # -- geometry ------------------------------------------------------------------------------

    def _xs(self, m):
        """{record id: x} for the group columns, x(t) for anything else, and the "now" x."""
        left, right = GUTTER + 8, self.width() - RIGHT
        usable = max(1.0, right - left)
        span = m["now"] - m["t0"]
        raw = [left + (r.t - m["t0"]) / span * usable for r in m["cols"]]
        xs = []
        for x in raw:                            # at least MIN_COL apart, in time order
            xs.append(max(x, xs[-1] + MIN_COL) if xs else x)
        if xs and xs[-1] > right - 24:           # too many to space by time: evenly instead
            k = len(xs)
            xs = [left + (i + 0.5) * (usable - 24) / k for i in range(k)]
        col = {r.id: x for r, x in zip(m["cols"], xs)}
        anchors = [(m["t0"], left)] + [(r.t, x) for r, x in zip(m["cols"], xs)] + [(m["now"], right)]

        def tx(t):
            if t <= anchors[0][0]:
                return anchors[0][1]
            for (ta, xa), (tb, xb) in zip(anchors, anchors[1:]):
                if t <= tb:
                    return xa if tb <= ta else xa + (t - ta) / (tb - ta) * (xb - xa)
            return right
        return col, tx, right, usable / span

    # -- painting ------------------------------------------------------------------------------

    def paintEvent(self, _):
        g = self.app.group
        m = track_model(g)
        self.resize_for(len(m["lanes"]))
        col, tx, now_x, pps = self._xs(m)
        left = GUTTER + 8
        hits = []
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        acc, warn, mut, err = QColor(*T.ACCENT), QColor(*T.WARN), QColor(*T.MUTED), QColor(*T.ERR)
        text = QColor(*T.TEXT)
        p.fillRect(self.rect(), QColor(*T.PANEL))
        recs = g.tree.recs
        locked_n = sum(1 for ln in m["lanes"] if ln["locked"])
        # header
        p.setFont(self.bold)
        p.setPen(text)
        p.drawText(QRectF(8, 2, GUTTER - 12, 18), Qt.AlignVCenter, self._elide(p, g.spec.name, GUTTER - 12))
        p.setFont(self.small)
        p.setPen(mut)
        hd = m["head"]
        sub = f"{locked_n} of {len(m['lanes'])} locked" + (f" · from {recs[hd].tag}" if hd in recs else "")
        p.drawText(QRectF(8, 19, GUTTER - 12, 14), Qt.AlignVCenter, self._elide(p, sub, GUTTER - 12))
        p.setPen(QPen(QColor(*T.BORDER), 1))
        p.drawLine(QPointF(left, HEAD - 4), QPointF(now_x, HEAD - 4))
        # ruler: a diamond and the tag per group snapshot
        xs_sorted = sorted(col.values())
        for r in m["cols"]:
            cx = col[r.id]
            nxt = [x for x in xs_sorted if x > cx]
            room = (nxt[0] if nxt else now_x + RIGHT) - cx - 12
            p.setPen(Qt.NoPen)
            p.setBrush(acc if r.live() else mut)
            y = HEAD - 4
            p.drawPolygon(QPolygonF([QPointF(cx, y - 7), QPointF(cx + 5, y - 2), QPointF(cx, y + 3),
                                     QPointF(cx - 5, y - 2)]))
            p.setPen(text if r.live() else mut)
            p.drawText(QRectF(cx + 7, y - 18, max(10, room), 13), Qt.AlignVCenter | Qt.AlignLeft,
                       self._elide(p, r.tag, max(10, room)))
            hits.append((QRectF(cx - 7, y - 18, 14 + max(10, room), 22), self._desc(g, r), lambda r=r: self._pick(r)))
        # lanes
        y = HEAD + 2
        for ln in m["lanes"]:
            hits += self._lane(p, g, m, ln, y, col, tx, now_x, pps, acc, warn, mut, text)
            y += MAIN + OWN + GAP
        scrub_y = y + 14
        # the group columns, through every lane down to the scrubber
        c = QColor(acc)
        c.setAlpha(110)
        p.setPen(QPen(c, 1, Qt.DotLine))
        for r in m["cols"]:
            p.drawLine(QPointF(col[r.id], HEAD), QPointF(col[r.id], scrub_y - 8))
        # now
        p.setPen(QPen(err, 2))
        p.drawLine(QPointF(now_x, HEAD - 6), QPointF(now_x, scrub_y + 8))
        p.setFont(self.small)
        p.setPen(err)
        p.drawText(QPointF(now_x - 22, HEAD - 22), "now")
        # scrubber
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*T.BUTTON))
        p.drawRoundedRect(QRectF(left, scrub_y - 4, now_x - left, 8), 4, 4)
        for r in m["cols"]:
            cx = col[r.id]
            here = r.id == hd
            p.setBrush(acc if here else QColor(255, 255, 255))
            p.setPen(QPen(acc if r.live() else mut, 2.2))
            p.drawEllipse(QPointF(cx, scrub_y), 7, 7)
            on = [x for x in r.nodes if x in g.locked and r.live(x)]
            if r.live() and on:
                hits.append((QRectF(cx - 9, scrub_y - 9, 18, 18),
                             f"click: jump the locked nodes ({', '.join(on)}) to {r.ref} {r.tag}",
                             lambda r=r: self.app.submit(f"restore {r.ref}")))
            else:
                hits.append((QRectF(cx - 9, scrub_y - 9, 18, 18), self._desc(g, r), None))
        for ln in m["lanes"]:                    # each unlocked node's own head, on the bar
            if ln["fork"]:
                hx = self._own_head_x(ln, m, col, now_x, pps)
                p.setPen(Qt.NoPen)
                p.setBrush(warn)
                p.drawEllipse(QPointF(hx, scrub_y), 4, 4)
        p.setPen(mut)
        p.setFont(self.small)
        p.drawText(QRectF(8, scrub_y - 8, GUTTER - 12, 16), Qt.AlignVCenter, "click ○: jump the group")
        p.end()
        self.hits = hits

    def _own_head_x(self, ln, m, col, now_x, pps):
        bx = col.get(ln["bp"], GUTTER + 8)
        return min(now_x, bx + max(8.0, (m["now"] - ln["fork"]["t"]) * pps))

    def _lane(self, p, g, m, ln, top, col, tx, now_x, pps, acc, warn, mut, text):
        hits = []
        name, W = ln["name"], self.width()
        left = GUTTER + 8
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*T.BG))
        p.drawRect(QRectF(0, top, W, MAIN + OWN))
        icon("lock" if ln["locked"] else "unlock").paint(p, 6, top + 3, 18, 18)
        verb = "unlock" if ln["locked"] else "lock"
        hits.append((QRectF(4, top + 1, 22, 22),
                     f"{name} is {'locked' if ln['locked'] else 'unlocked'}: click to {verb} it"
                     + ("" if ln["locked"] else " again (it keeps its state; the next group snapshot includes it)"),
                     lambda: self.app.toggle_lock(name)))
        p.setFont(self.bold)
        p.setPen(text)
        p.drawText(QRectF(28, top, 60, MAIN), Qt.AlignVCenter, self._elide(p, name, 60))
        p.setFont(self.small)
        p.setPen(QColor(*T.state_colour(ln["state"])))
        p.drawText(QRectF(28, top + MAIN - 2, GUTTER - 32, OWN), Qt.AlignVCenter, ln["state"])
        p.setPen(mut if ln["locked"] else warn)
        p.drawText(QRectF(90, top, GUTTER - 92, MAIN), Qt.AlignVCenter, "locked" if ln["locked"] else "unlocked")
        mid, own_y = top + MAIN / 2, top + MAIN + OWN / 2
        fill = QColor(*T.SNAP_GROUP)
        if ln["locked"]:
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(left, mid - 7, now_x - left, 14), 4, 4)
        else:
            bx = col.get(ln["bp"], left)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            if bx > left:
                p.drawRoundedRect(QRectF(left, mid - 7, bx - left, 14), 4, 4)
            p.setPen(QPen(acc, 1.1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(bx, mid - 7, max(4.0, now_x - bx), 14), 4, 4)
            p.setPen(mut)
            if now_x - bx > 160:
                p.drawText(QRectF(bx + 8, mid - 7, now_x - bx - 16, 14), Qt.AlignVCenter,
                           f"the group went on without {name}")
            # its own track
            hx = self._own_head_x(ln, m, col, now_x, pps)
            p.setPen(QPen(warn, 1.5))
            p.drawLine(QPointF(bx, mid + 7), QPointF(bx, own_y - 7))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(*OWN_FILL))
            p.drawRoundedRect(QRectF(bx, own_y - 7, max(6.0, hx - bx), 14), 4, 4)
            p.setPen(QPen(warn, 2.5))
            p.drawLine(QPointF(hx, own_y - 9), QPointF(hx, own_y + 9))
            p.setFont(self.small)
            p.setPen(warn)
            tag = f"▶ {name} alone" if ln["state"] == "running" else f"‖ {name} {ln['state']}"
            if W - RIGHT + 24 - hx > 70:
                p.drawText(QPointF(hx + 5, own_y + 4), tag)
            else:
                p.drawText(QRectF(hx - 90, own_y - 7, 84, 14), Qt.AlignVCenter | Qt.AlignRight, tag)
            for r in ln["own"]:
                x = min(hx, bx + max(6.0, (r.t - ln["fork"]["t"]) * pps))
                live = r.live(name)
                p.setPen(QPen(warn, 1.5))
                p.setBrush(warn if live else QColor(*T.PANEL))
                p.drawEllipse(QPointF(x, own_y), 4.5, 4.5)
                if r.id == ln["head"]:
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(x, own_y), 7.5, 7.5)
                hits.append((QRectF(x - 7, own_y - 7, 14, 14),
                             self._desc(g, r) + (f"\nclick: restore {name} alone to it" if live else ""),
                             (lambda r=r: self.app.submit(f"@{name} restore {r.ref}")) if live else None))
        # group snapshot dots on this lane
        for r in m["cols"]:
            cx = col[r.id]
            holds = name in r.nodes
            live = holds and r.live(name)
            if not holds:
                p.setPen(QPen(mut, 1, Qt.DotLine))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx, mid), 3, 3)
                continue
            p.setPen(Qt.NoPen if live else QPen(mut, 1.2))
            p.setBrush(acc if live else QColor(*T.PANEL))
            p.drawEllipse(QPointF(cx, mid), 4.5, 4.5)
            if r.id == ln["head"]:
                p.setPen(QPen(acc, 1.6))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx, mid), 7.5, 7.5)
            if ln["locked"]:
                tip, fn = self._desc(g, r) + "\nclick: pick it (Restore / Roll back use it)", lambda r=r: self._pick(r)
            elif live:
                tip, fn = (self._desc(g, r) + f"\nclick: rewind {name} alone to it",
                           lambda r=r: self.app.submit(f"@{name} restore {r.ref}"))
            else:
                tip, fn = self._desc(g, r), None
            hits.append((QRectF(cx - 7, mid - 7, 14, 14), tip, fn))
        # a locked node's side branch (snapshots it took alone, or kept from when it was unlocked)
        if ln["locked"]:
            for r in ln["recs"]:
                if r.scope == "group":
                    continue
                x = tx(r.t)
                live = r.live(name)
                p.setPen(QPen(warn if live else mut, 1.2))
                p.setBrush(QColor(*OWN_FILL) if live else QColor(*T.PANEL))
                p.drawEllipse(QPointF(x, own_y), 3.5, 3.5)
                if r.id == ln["head"]:
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(x, own_y), 6.5, 6.5)
                hits.append((QRectF(x - 6, own_y - 6, 12, 12),
                             self._desc(g, r) + (f"\nclick: restore {name} alone to it" if live else ""),
                             (lambda r=r: self.app.submit(f"@{name} restore {r.ref}")) if live else None))
        return hits

    @staticmethod
    def _elide(p, s, w):
        return p.fontMetrics().elidedText(s, Qt.ElideRight, int(w))

    @staticmethod
    def _desc(g, r):
        return g.tree.describe(r, list(g.nodes))

    def _pick(self, r):
        self.app.group.tree.selected = r.id

    # -- mouse ---------------------------------------------------------------------------------

    def _hit(self, pos):
        for rect, tip, fn in reversed(self.hits):
            if rect.contains(pos):
                return tip, fn
        return None, None

    def mouseMoveEvent(self, ev):
        tip, fn = self._hit(ev.position())
        self.setCursor(Qt.PointingHandCursor if fn else Qt.ArrowCursor)
        if tip:
            QToolTip.showText(ev.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        _, fn = self._hit(ev.position())
        if fn:
            fn()
            self.update()


# -- the dock ---------------------------------------------------------------------------------

def tool(kind, tip, slot):
    b = QToolButton()
    b.setIcon(icon(kind))
    b.setIconSize(QSize(18, 18))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.NoFocus)
    b.clicked.connect(slot)
    return b


class FixedButton(QPushButton):
    """A push button of fixed width whose text is cut with "…" to fit."""

    def __init__(self, width, slot, name=None):
        super().__init__()
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        if name:
            self.setObjectName(name)
        self.clicked.connect(slot)
        self._full = None

    def set(self, text, tip, enabled=True):
        if text != self._full:
            self._full = text
            self.setText(self.fontMetrics().elidedText(text, Qt.ElideRight, self.width() - 16))
        self.setToolTip(tip)
        self.setEnabled(enabled)


class TimelinePanel(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        top = QHBoxLayout()
        self.view_pick = QComboBox()
        self.view_pick.addItems(["Tracks", "Tree"])
        self.view_pick.setFixedWidth(90)
        self.view_pick.setToolTip("Tracks: the timeline with lock / unlock. Tree: the family tree")
        top.addWidget(self.view_pick)
        self.note = Elided()
        top.addWidget(self.note, 1)
        self.relock = FixedButton(130, lambda: app.toggle_lock(self._node()))
        top.addWidget(self.relock)
        self.roll = FixedButton(170, self._roll, name="danger")
        top.addWidget(self.roll)
        v.addLayout(top)
        self.stack = QStackedWidget()
        self.player = TrackPlayer(app)
        sc = QScrollArea()
        sc.setWidget(self.player)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        self.stack.addWidget(sc)
        self.tree = SnapsPanel(app)
        self.stack.addWidget(self.tree)
        self.view_pick.currentIndexChanged.connect(self.stack.setCurrentIndex)
        v.addWidget(self.stack, 1)
        a = app
        bar = QHBoxLayout()
        bar.setSpacing(2)
        lab = QLabel("Group")
        lab.setObjectName("muted")
        bar.addWidget(lab)
        self.g_prev = tool("prev", "", lambda: self._go(self._prev))
        self.g_next = tool("next", "", lambda: self._go(self._next))
        bar.addWidget(self.g_prev)
        bar.addWidget(tool("pauseall", "Group: pause the locked nodes (F10)", lambda: a.submit("halt")))
        bar.addWidget(tool("playall", "Group: resume the locked nodes (F9)", lambda: a.submit("resume")))
        bar.addWidget(self.g_next)
        bar.addWidget(tool("snapall", "Group: snapshot the locked nodes (Shift+F7)", lambda: a.snap_button("snap")))
        sep = QLabel("│")
        sep.setObjectName("muted")
        bar.addWidget(sep)
        self.node_pick = QComboBox()
        self.node_pick.setFixedWidth(80)
        self.node_pick.setToolTip("The node these buttons act on (the selected node)")
        self.node_pick.activated.connect(lambda i: a.select_node(self.node_pick.itemText(i)))
        bar.addWidget(self.node_pick)
        self.n_lock = tool("unlock", "", lambda: a.toggle_lock(self._node()))
        self.n_prev = tool("prev", "", lambda: self._go(self._node_prev))
        bar.addWidget(self.n_lock)
        bar.addWidget(self.n_prev)
        self.n_pause = tool("pause", "", lambda: a.submit(f"@{self._node()} halt"))
        self.n_play = tool("play", "", lambda: a.submit(f"@{self._node()} resume"))
        self.n_snap = tool("snapshot", "", lambda: a.snap_button(f"@{self._node()} snap"))
        for b in (self.n_pause, self.n_play, self.n_snap):
            bar.addWidget(b)
        bar.addStretch(1)
        v.addLayout(bar)
        self._prev = self._next = self._node_prev = None
        self._roll_to = None
        self._sig = None
        self.tick = 0

    def _node(self):
        return self.app.group.current

    def _go(self, target):
        if target is None:
            return
        line, _ = target
        self.app.submit(line)

    def _roll(self):
        if self._roll_to is not None:
            self.app.rollback(self._roll_to.ref)

    def refresh(self):
        a = self.app
        g = a.group
        self.tick += 1
        if self.stack.currentIndex() == 1:
            self.tree.refresh()
        else:
            tree = g.tree
            with tree.lock:
                sig = (len(tree.recs), tuple(tree.heads.items()), tree.selected,
                       tuple((r.id, tuple(r.status.values())) for r in tree.recs.values()))
            sig += (tuple(sorted(g.locked)), tuple(n.state for n in g.nodes.values()), self.player.width())
            if sig != self._sig or self.tick % 15 == 0:      # "now" moves: about twice a second
                self._sig = sig
                self.player.update()
        m = track_model(g)
        names = list(g.nodes)
        if [self.node_pick.itemText(i) for i in range(self.node_pick.count())] != names:
            self.node_pick.clear()
            self.node_pick.addItems(names)
        if self.node_pick.currentText() != g.current:
            self.node_pick.setCurrentText(g.current)
        un = g.unlocked()
        self.note.set(f"{', '.join(un)} unlocked: group snapshots, restores and pause / resume skip "
                      f"{'it' if len(un) == 1 else 'them'} until re-locked" if un else
                      "every node is locked: group snapshots and restores take them all together",
                      T.WARN if un else T.MUTED)
        prev, nxt = col_neighbours(m)
        on = lambda r: [x for x in r.nodes if x in g.locked]  # noqa: E731
        self._prev = (f"restore {prev.ref}", prev) if prev and on(prev) else None
        self._next = (f"restore {nxt.ref}", nxt) if nxt and on(nxt) else None
        self.g_prev.setEnabled(self._prev is not None)
        self.g_prev.setToolTip(f"Group: back to {prev.ref} {prev.tag}" if self._prev else "Group: no earlier snapshot")
        self.g_next.setEnabled(self._next is not None)
        self.g_next.setToolTip(f"Group: forward to {nxt.ref} {nxt.tag}" if self._next else "Group: no later snapshot")
        name = g.current
        n = g.nodes[name]
        locked = name in g.locked
        self.n_lock.setIcon(icon("unlock" if locked else "lock"))
        self.n_lock.setToolTip(f"Unlock {name}: take it out of the group's snapshots and restores" if locked
                               else f"Re-lock {name} as it is now")
        recs = [r for r in next(ln["recs"] for ln in m["lanes"] if ln["name"] == name) if r.live(name)]
        ids = [r.id for r in recs]
        head = g.tree.heads.get(name)
        before = recs[ids.index(head) - 1] if head in ids and ids.index(head) > 0 else None
        self._node_prev = (f"@{name} restore {before.ref}", before) if before else None
        self.n_prev.setEnabled(before is not None)
        self.n_prev.setToolTip(f"{name} alone: back to {before.ref} {before.tag}" if before else
                               f"{name}: no earlier snapshot")
        self.n_pause.setEnabled(n.state == "running")
        self.n_pause.setToolTip(f"Pause {name} alone")
        self.n_play.setEnabled(n.state == "paused")
        self.n_play.setToolTip(f"Resume {name} alone")
        self.n_snap.setEnabled(n.connected)
        self.n_snap.setToolTip(f"Snapshot {name} alone" + ("" if locked else " (on its own track)"))
        self.relock.set(f"Re-lock {name} here" if not locked else f"Unlock {name}",
                        f"{name} keeps its state; from now on group snapshots include it again" if not locked
                        else f"Take {name} out of group snapshots, restores and pause / resume, to rewind it alone")
        self._roll_to = rollback_target(g, m)
        r = self._roll_to
        self.roll.set(f"Roll group back to {r.tag}…" if r else "Roll group back…",
                      (f"Restore every node to {r.ref} {r.tag}" + (" and discard what the unlocked nodes saved "
                                                                    "since; locks them again" if un else ""))
                      if r else "No snapshot every node holds yet", r is not None)
