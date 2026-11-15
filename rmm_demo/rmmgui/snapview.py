# SPDX-License-Identifier: MIT
"""The Snapshots tab: the family tree (snaps.py) in a QGraphicsView, then the picked snapshot's
buttons and details. The scene is rebuilt only when the tree, a node's state or the selection
changes; the view pans with its scroll bars, the wheel, or dragging the background.
"""

import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel, QVBoxLayout)

from . import theme as T
from .snaps import layout
from .view import Elided, button

BOX = (150, 56)            # a snapshot in the tree
GAP = (20, 28)
START_H = 28               # the "start" pill


def col(c):
    return QColor(*c)


class SnapBox(QGraphicsRectItem):
    def __init__(self, rect, on_click):
        super().__init__(rect)
        self.on_click = on_click

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.on_click()
        else:
            super().mousePressEvent(ev)


class SnapsPanel(QFrame):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        t = Elided("snap: every node   @a snap: just a   restore TAG   click a box to pick it   "
                   "wheel / drag to pan   F12 back", name="title")
        lay.addWidget(t)
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setRenderHint(self.view.renderHints().Antialiasing)
        self.view.viewport().setStyleSheet(f"background: {T.hexc(T.PANEL)};")
        self.view.verticalScrollBar().valueChanged.connect(self._panned)
        self.view.horizontalScrollBar().valueChanged.connect(self._panned)
        lay.addWidget(self.view, 1)
        self.bar = QHBoxLayout()
        lay.addLayout(self.bar)
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)                 # a long line wraps; it never widens the window
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail.setFixedHeight(self.detail.fontMetrics().lineSpacing() * 3 + 4)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.detail)
        self._sig = None
        self._bar_sig = None
        self._programmatic = False

    def _panned(self, _):
        if not self._programmatic:
            self.app.snap_follow = False             # the user took over; Snap buttons re-enable it

    # -- refresh --------------------------------------------------------------------------------

    def refresh(self):
        a = self.app
        grp = a.group
        tree = grp.tree
        with tree.lock:
            recs = dict(tree.recs)
            heads = dict(tree.heads)
            sig = (tuple((r.id, r.tag, tuple(sorted(r.status.items())), tuple(sorted(r.failed.items())))
                         for r in recs.values()),
                   tuple(sorted(heads.items(), key=lambda kv: kv[0])), tree.selected,
                   tuple((n.name, n.state) for n in grp.nodes.values()))
        if sig != self._sig:
            self._sig = sig
            self._build(recs, heads)
        self._buttons(recs.get(tree.selected))

    def _build(self, recs, heads):
        a = self.app
        tree = a.group.tree
        sc = self.scene
        sc.clear()
        with tree.lock:
            pos, tw, th = layout(tree, BOX[0], BOX[1], GAP[0], GAP[1], START_H)
        P = {k: QPointF(x + 10, y + 10) for k, (x, y) in pos.items()}
        for r in recs.values():                       # edges first, boxes over them
            c = P[r.id] + QPointF(BOX[0] / 2, 0)
            p = r.parent()
            s = P[p] + QPointF(BOX[0] / 2, BOX[1] if p is not None else START_H)
            mid = s.y() + (c.y() - s.y()) / 2
            path = QPainterPath(s)
            path.lineTo(s.x(), mid)
            path.lineTo(c.x(), mid)
            path.lineTo(c)
            item = QGraphicsPathItem(path)
            item.setPen(QPen(col(T.MUTED if r.live() else T.BORDER), 2))
            sc.addItem(item)
        self._start_box(P[None], [n for n, h in heads.items() if h is None])
        for r in recs.values():
            self._snap_box(r, P[r.id], [n for n, h in heads.items() if h == r.id], tree.selected == r.id)
        for r in recs.values():                       # members that came from another box
            for q in r.other_parents():
                self._link(P[q], q is None, P[r.id])
        if not recs:
            self._text("No snapshots yet.  snap  saves every node under one tag;  @a snap  saves only a.",
                       QPointF(30, 70), T.MUTED)
        sc.setSceneRect(sc.itemsBoundingRect().adjusted(-10, -10, 10, 24))
        if a.snap_follow and recs:                    # keep the newest snapshot in view until panned
            self._programmatic = True
            newest = max(recs)
            self.view.ensureVisible(QRectF(P[newest], QPointF(P[newest].x() + BOX[0], P[newest].y() + BOX[1] + 30)),
                                    40, 40)
            self._programmatic = False

    def _text(self, s, at, colour, small=False, parent=None):
        t = QGraphicsSimpleTextItem(s, parent)
        f = QFont(self.font())
        f.setPointSize(8 if small else 10)
        t.setFont(f)
        t.setBrush(QBrush(col(colour)))
        if parent is None:
            t.setPos(at)
            self.scene.addItem(t)
        else:
            t.setPos(at)
        return t

    def _start_box(self, at, here):
        r = QRectF(at.x() + BOX[0] / 2 - 40, at.y(), 80, START_H)
        item = QGraphicsRectItem(r)
        item.setBrush(QBrush(col(T.BUTTON)))
        item.setPen(QPen(col(T.BORDER)))
        self.scene.addItem(item)
        self._text("start", QPointF(r.x() + 22, r.y() + 5), T.MUTED, parent=item)
        self._chips(r.center().x(), r.bottom() + 4, here)

    def _snap_box(self, rec, at, here, selected):
        grp = self.app.group
        tree = grp.tree
        live = rec.live()
        fill = {"group": T.SNAP_GROUP, "nodes": T.PANEL}.get(rec.scope, T.SNAP_OTHER) if live else T.BG

        def click():
            tree.selected = None if tree.selected == rec.id else rec.id
            if tree.selected:
                self.app.hint = (f"{rec.ref} {rec.tag}: Restore / Delete buttons below the tree, "
                                 f"or `restore {rec.ref}`")

        box = SnapBox(QRectF(at.x(), at.y(), BOX[0], BOX[1]), click)
        box.setBrush(QBrush(col(fill)))
        box.setPen(QPen(col(T.ACCENT if selected else (T.BORDER if live else T.BUTTON)), 3 if selected else 1))
        box.setCursor(Qt.PointingHandCursor)
        self.scene.addItem(box)
        x, y = at.x() + 8, at.y()
        self._text(rec.tag[:16], QPointF(x, y + 4), T.TEXT if live else T.MUTED, parent=box)
        ref = self._text(rec.ref, QPointF(0, 0), T.MUTED, small=True, parent=box)
        ref.setPos(at.x() + BOX[0] - ref.boundingRect().width() - 6, y + 7)
        yy = y + 24
        if rec.members_text(list(grp.nodes)) == "all" and live and all(rec.live(n) for n in rec.nodes):
            self._text("all nodes", QPointF(x, yy), T.ACCENT, small=True, parent=box)
        else:
            cx = x
            for n in rec.nodes:
                ok = rec.live(n)
                t = self._text(n if ok else f"({n})", QPointF(cx, yy), T.TEXT if ok else T.MUTED, small=True, parent=box)
                cx += t.boundingRect().width() + 6
        stamp = time.strftime("%H:%M:%S", time.localtime(rec.t))
        extra = "  failed: " + ",".join(rec.failed) if rec.failed else ("  console" if rec.scope == "console" else "")
        self._text(stamp + extra, QPointF(x, yy + 14), T.ERR if rec.failed else T.MUTED, small=True, parent=box)
        self._chips(at.x() + BOX[0] / 2, at.y() + BOX[1] + 4, here)

    def _chips(self, cx, y, names):
        """Small labels under a box: the nodes whose current state came from it."""
        if not names:
            return
        grp = self.app.group
        items = []
        for n in names:
            node = grp.nodes.get(n)
            c = T.state_colour(node.state) if node else T.MUTED
            t = QGraphicsSimpleTextItem(n)
            f = QFont(self.font())
            f.setPointSize(8)
            t.setFont(f)
            t.setBrush(QBrush(col(T.PANEL)))
            items.append((t, c, t.boundingRect().width() + 10))
        x = cx - (sum(w for _, _, w in items) + 4 * (len(items) - 1)) / 2
        for t, c, w in items:
            h = t.boundingRect().height() + 2
            r = QGraphicsRectItem(QRectF(x, y, w, h))
            r.setBrush(QBrush(col(c)))
            r.setPen(QPen(Qt.NoPen))
            self.scene.addItem(r)
            t.setParentItem(r)
            t.setPos(x + 5, y + 1)
            x += w + 4

    def _link(self, qa, q_start, ra):
        """Dashed accent line from box q to box r: side to side on one row, else bottom to top;
        a dot marks r's end."""
        qh = START_H if q_start else BOX[1]
        if abs(qa.y() - ra.y()) < 2:
            left = qa.x() < ra.x()
            p0 = QPointF(qa.x() + (BOX[0] if left else 0), qa.y() + qh / 2)
            p1 = QPointF(ra.x() + (0 if left else BOX[0]), ra.y() + BOX[1] / 2)
        else:
            p0 = QPointF(qa.x() + BOX[0] / 2 + 12, qa.y() + qh)
            p1 = QPointF(ra.x() + BOX[0] / 2 + 12, ra.y())
        path = QPainterPath(p0)
        path.lineTo(p1)
        item = QGraphicsPathItem(path)
        pen = QPen(col(T.ACCENT), 2)
        pen.setStyle(Qt.DashLine)
        item.setPen(pen)
        self.scene.addItem(item)
        dot = self.scene.addEllipse(QRectF(p1.x() - 4, p1.y() - 4, 8, 8), QPen(Qt.NoPen), QBrush(col(T.ACCENT)))
        dot.setZValue(1)

    # -- buttons and details ---------------------------------------------------------------------

    def _buttons(self, rec):
        a = self.app
        grp = a.group
        cur = grp.current
        sig = (cur, rec.id if rec else None, rec.live() if rec else None, tuple(rec.nodes) if rec else None,
               rec.live(cur) if rec and cur in rec.nodes else None, tuple(grp.nodes))
        if sig != self._bar_sig:
            self._bar_sig = sig
            while self.bar.count():
                it = self.bar.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            self.bar.addWidget(button("Snap all", lambda: a.snap_button("snap")))
            self.bar.addWidget(button(f"Snap {cur}", lambda: a.snap_button(f"@{cur} snap")))
            if rec is not None and rec.live():
                self.bar.addWidget(button(f"Restore {rec.tag} ({rec.members_text(list(grp.nodes))})",
                                          lambda: a.submit(f"restore {rec.ref}")))
                if cur in rec.nodes and len(rec.nodes) > 1 and rec.live(cur):
                    self.bar.addWidget(button(f"Restore {cur} only", lambda: a.submit(f"@{cur} restore {rec.ref}")))
                self.bar.addWidget(button("Delete", lambda: a.submit(f"snaps delete {rec.ref}"), name="danger"))
            self.bar.addStretch(1)
        if rec is None:
            self.detail.setText("Coloured tags under a box: the nodes whose current state came from it (last snap or "
                                "restore). Blue: every node.  Dashed: members that came from another box.")
            return
        par = "  ".join(f"{n} from {'start' if p is None else '#' + str(p)}" for n, p in rec.parents.items())
        st = "  ".join(f"{n}: {rec.status[n]}" + (f" tsc {rec.tsc[n]:,}" if rec.tsc.get(n) is not None else "")
                       for n in rec.nodes)
        st += "".join(f"   {n} failed: {why[:50]}" for n, why in rec.failed.items())
        self.detail.setText(f"{rec.ref} {rec.tag}   {rec.scope}   taken {time.strftime('%H:%M:%S', time.localtime(rec.t))}"
                            f"   {par}" + (f"   {rec.note}" if rec.note else "") + "\n" + st[:220])
