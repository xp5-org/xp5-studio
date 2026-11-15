# SPDX-License-Identifier: MIT
"""Toolbar icons, drawn with QPainter, so they need no icon theme or emoji font (the hosts'
fonts draw several media symbols as boxes). icon(kind) -> QIcon; kinds are listed in KINDS."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from . import theme

KINDS = ("play", "pause", "stop", "step", "stepover", "stepout", "reset", "snapshot", "snapall", "restore",
         "prev", "next", "lock", "unlock", "delete", "attach", "launch", "halt", "pauseall", "playall", "turbo")

_cache = {}


def icon(kind, size=40):
    if (kind, size) in _cache:
        return _cache[kind, size]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    k = size / 40.0
    p.scale(k, k)
    col = {"play": theme.OK, "pause": theme.WARN, "stop": theme.ERR, "reset": theme.ERR,
           "lock": theme.ACCENT, "unlock": theme.WARN,
           "halt": theme.ERR, "turbo": theme.WARN, "pauseall": theme.WARN, "playall": theme.OK}.get(kind, (60, 60, 60))
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
    elif kind == "snapall":                      # camera with a second one behind
        p.drawRoundedRect(QRectF(11, 6, 26, 17), 3, 3)
        p.setBrush(QColor(255, 255, 255))
        p.drawRoundedRect(QRectF(3, 14, 28, 21), 3, 3)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(5, 16, 24, 17), 2, 2)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(17, 24.5), 5.5, 5.5)
        p.setBrush(c)
        p.drawEllipse(QPointF(17, 24.5), 3, 3)
    elif kind == "halt":                         # octagon with a bar: halt-all
        pts = [QPointF(20 + 15 * x, 20 + 15 * y) for x, y in
               ((-0.41, -1), (0.41, -1), (1, -0.41), (1, 0.41), (0.41, 1), (-0.41, 1), (-1, 0.41), (-1, -0.41))]
        p.drawPolygon(QPolygonF(pts))
        p.setBrush(QColor(255, 255, 255))
        p.drawRect(QRectF(11, 17, 18, 6))
    elif kind == "turbo":                        # lightning bolt
        p.drawPolygon(QPolygonF([QPointF(23, 4), QPointF(9, 23), QPointF(19, 23), QPointF(16, 36),
                                 QPointF(31, 16), QPointF(21, 16)]))
    elif kind in ("pauseall", "playall"):        # the single icon twice, offset
        for dx, alpha in ((-6, 110), (4, 255)):
            cc = QColor(c)
            cc.setAlpha(alpha)
            p.setBrush(cc)
            if kind == "pauseall":
                p.drawRoundedRect(QRectF(10 + dx, 9, 6, 22), 1.5, 1.5)
                p.drawRoundedRect(QRectF(21 + dx, 9, 6, 22), 1.5, 1.5)
            else:
                tri(10 + dx, 8, 30 + dx, 20, 32)
    p.end()
    ic = QIcon(pm)
    _cache[kind, size] = ic
    return ic
