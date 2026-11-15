# SPDX-License-Identifier: MIT
"""Recent tasks: what happened to which node or group, and how it ended, like vSphere's task
strip. The model has no Qt (App.say() feeds it every ok / warn / err line); TasksView shows the
newest first in a table whose rows never change height.
"""

import collections
import re
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from . import theme as T

RESULT = {"ok": "ok", "warn": "warning", "err": "error"}
TARGET = re.compile(r"^\[([A-Za-z0-9_.-]+)\] ")


class TaskLog(collections.deque):
    """(t, target, text, result, tone); `total` counts every entry ever added."""

    def __init__(self, maxlen=500):
        super().__init__(maxlen=maxlen)
        self.total = 0

    def add(self, target, text, tone="ok"):
        m = TARGET.match(text)
        if m:                                    # "[b] text" from a multi-node say()
            target, text = m.group(1), text[m.end():]
        if text.startswith("error: "):
            text = text[7:]
        self.append((time.time(), target or "-", text, RESULT.get(tone, tone), tone))
        self.total += 1


class TasksView(QTableWidget):
    def __init__(self, log):
        super().__init__(0, 4)
        self.log = log
        self.setHorizontalHeaderLabels(["Time", "Target", "Task", "Result"])
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setAlternatingRowColors(True)
        h = self.horizontalHeader()
        for c, w in ((0, 70), (1, 90), (3, 70)):
            h.setSectionResizeMode(c, QHeaderView.Fixed)
            self.setColumnWidth(c, w)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.seen = 0

    def refresh(self):
        new = self.log.total - self.seen
        if new <= 0:
            return
        self.seen = self.log.total
        for t, target, text, result, tone in list(self.log)[-new:]:
            self.insertRow(0)
            for c, v in enumerate((time.strftime("%H:%M:%S", time.localtime(t)), target, text, result)):
                it = QTableWidgetItem(v)
                if c == 2:
                    it.setToolTip(v)
                if c == 3:
                    it.setForeground(QColor(*T.TONES.get(tone, T.TEXT)))
                self.setItem(0, c, it)
        while self.rowCount() > self.log.maxlen:
            self.removeRow(self.rowCount() - 1)
        self.item(0, 0).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
