# SPDX-License-Identifier: MIT
"""Widgets shared by the pages and docks (Qt). Each has refresh(), called from a timer in app.py,
that reads the app's state and updates itself; nothing here changes state except through the App
methods.

Fixed geometry: every label whose text changes with the machine's state (registers, stop reason,
rates, the prompt) has a fixed width or elides, and nothing shows or hides on a stop or resume,
so no panel moves when the guest breaks (tests/test_layout_stable.py checks this).
"""

import time

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFontDatabase, QImage, QPainter, QTextCharFormat, QColor, QTextCursor
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import theme as T

THUMB = (64, 40)


def mono_font(points=10):
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(points)
    return f


def frame_image(frame):
    """A poller Frame as a QImage that owns its pixels."""
    return QImage(frame.rgb, frame.w, frame.h, frame.w * 3, QImage.Format_RGB888).copy()


class ImageCache:
    """node name -> (Frame, QImage): convert a frame once, however often it is drawn."""

    def __init__(self):
        self.d = {}

    def get(self, node, frame):
        hit = self.d.get(node.name)
        if hit and hit[0] is frame:
            return hit[1]
        img = frame_image(frame)
        self.d[node.name] = (frame, img)
        return img


def button(text, slot=None, checkable=False, name=None):
    b = QPushButton(text)
    b.setFocusPolicy(Qt.NoFocus)
    b.setCheckable(checkable)
    if name:
        b.setObjectName(name)
    if slot:
        b.clicked.connect(slot)
    return b


def panel(title):
    """A framed panel with a muted title; returns (frame, body layout)."""
    f = QFrame()
    f.setObjectName("panel")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(8, 6, 8, 6)
    t = QLabel(title)
    t.setObjectName("title")
    lay.addWidget(t)
    return f, lay


def tone_colour(tone):
    return QColor(T.hexc(T.TONES.get(tone, T.TEXT)))


class Elided(QLabel):
    """A one-line label whose size never follows its text: long text is cut with "…" (the full
    text is the tooltip). width=None stretches with the layout; a number fixes it."""

    def __init__(self, text="", width=None, mono=False, name=None):
        super().__init__()
        self._full = ""
        self._colour = None
        if mono:
            self.setFont(mono_font())
        if name:
            self.setObjectName(name)
        if width:
            self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Ignored if not width else QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(self.fontMetrics().height() + 4)
        self.set(text)

    def set(self, text, colour=None):
        """Set text (and optionally a colour); a no-op when nothing changed."""
        text = text or ""
        if text == self._full and colour == self._colour:
            return
        self._full, self._colour = text, colour
        self.setToolTip(text if len(text) > 20 else "")
        if colour is not None:
            self.setStyleSheet(f"color: {T.hexc(colour)}; background: transparent;")
        self.update()

    def full_text(self):
        return self._full

    def fix_width(self, w):
        """A fixed width set after construction (the layout must hear about it)."""
        self.setFixedWidth(w)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.updateGeometry()

    def sizeHint(self):
        return QSize(self.width() if self.maximumWidth() == self.minimumWidth() else 40, self.height())

    def minimumSizeHint(self):
        return QSize(0, self.height())

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        r = self.contentsRect()
        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, self.fontMetrics().elidedText(self._full, Qt.ElideRight, r.width()))


class HaltBanner(QFrame):
    """The group's halt line. Always there at one height: idle it reads "running" in a muted
    style, so a halt or a resume never adds or removes a row above the pages."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("haltIdle")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        self.text = Elided()
        lay.addWidget(self.text, 1)
        self.setFixedHeight(self.text.height() + 8)
        self._halted = None

    def refresh(self):
        g = self.app.group
        h = g.halted
        halted = bool(h)
        if halted != self._halted:
            self._halted = halted
            self.setObjectName("halt" if halted else "haltIdle")
            self.style().unpolish(self)
            self.style().polish(self)
        if not h:
            pol = g.policy
            locked = g.locked
            self.text.set(f"{g.spec.name}: " + "  ".join(f"{n.name} {n.state}" + ("" if n.name in locked else " (unlocked)")
                                                         for n in g.nodes.values())
                          + f"      halt-all {'ON (' + ','.join(pol.halt_on) + ')' if pol.halt_all else 'off'}")
            return
        age = time.time() - h["t"]
        self.text.set(f"HALTED  {h['by']}: {h['detail'] or h['reason']}   paused: {', '.join(h['paused']) or '-'}"
                      f"   ({age:.0f}s ago)   Resume all / F9 continues every node")


# -- registers --------------------------------------------------------------------------------

REG_ROWS = [("eax", "ebx"), ("ecx", "edx"), ("esi", "edi"), ("ebp", "esp"), ("cs", "ds"), ("es", "ss"),
            ("eip", "eflags")]
SEG = {"cs", "ds", "es", "ss", "fs", "gs"}
FLAGS = [(11, "O"), (10, "D"), (9, "I"), (8, "T"), (7, "S"), (6, "Z"), (4, "A"), (2, "P"), (0, "C")]


def flags_text(fl):
    return " ".join(ch if fl & (1 << b) else ch.lower() for b, ch in FLAGS) + f"  IOPL {(fl >> 12) & 3}"


class RegsGrid(QWidget):
    """The selected node's registers in a fixed grid of fixed-width labels. While it runs every
    value reads "--------"; on a stop the values that changed since the previous stop turn red."""

    def __init__(self, app, across=2):
        super().__init__()
        self.app = app
        g = QGridLayout(self)
        g.setContentsMargins(4, 2, 4, 2)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(1)
        self.vals = {}
        regs = [k for pair in REG_ROWS for k in pair]
        nrows = (len(regs) + across - 1) // across
        for i, k in enumerate(regs):
            r, c = divmod(i, across)
            kl = QLabel("EFL" if k == "eflags" else k.upper())
            kl.setObjectName("muted")
            kl.setFixedWidth(30)
            v = Elided("", 76, mono=True)
            g.addWidget(kl, r, c * 2)
            g.addWidget(v, r, c * 2 + 1)
            self.vals[k] = v
        self.flags = Elided("", 240, mono=True)
        g.addWidget(self.flags, nrows, 0, 1, 4)
        self.state = Elided("", 240, name="muted")
        g.addWidget(self.state, nrows + 1, 0, 1, 4)
        self.setFixedSize(self.sizeHint().width() + 8, self.sizeHint().height())
        self._prev, self._last, self._shown = {}, {}, None

    def refresh(self):
        node = self.app.group.node()
        r = node.regs or {}
        stopped = node.state == "paused"
        key = (node.name, id(node.regs), stopped, node.state, node.stop_note)
        if key == self._shown:
            return
        if self._shown and self._shown[0] == node.name and self._shown[1] != id(node.regs):
            self._prev = self._last
        elif not self._shown or self._shown[0] != node.name:
            self._prev = {}
        self._shown, self._last = key, dict(r)
        good = stopped and r and "error" not in r
        for k, lab in self.vals.items():
            if not good:
                lab.set("-" * (4 if k in SEG else 8), T.MUTED)
                continue
            v = r.get(k, 0)
            text = f"{v:04X}" if k in SEG else f"{v:08X}"
            lab.set(text, T.ERR if self._prev and self._prev.get(k) != v else T.TEXT)
        self.flags.set(flags_text(r.get("eflags", 0)) if good else "", T.TEXT)
        if "error" in r:
            self.state.set(f"regs: {r['error']}")
        elif good:
            self.state.set(f"stopped: {node.stop_note or 'paused'}")
        else:
            self.state.set(f"{node.state}: values show when it stops (F5 pause, F6 step)")


# -- screen -----------------------------------------------------------------------------------


class FrameView(QWidget):
    """The guest screen, scaled to fit with its aspect kept."""

    def __init__(self):
        super().__init__()
        self.img = None
        self.msg = ""
        self.setMinimumSize(160, 100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def show_image(self, img, msg=""):
        if img is not self.img or msg != self.msg:
            self.img, self.msg = img, msg
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(*T.SCREEN_BG))
        if self.img is not None:
            sc = min(self.width() / self.img.width(), self.height() / self.img.height())
            w, h = max(1, int(self.img.width() * sc)), max(1, int(self.img.height() * sc))
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), self.img)
        p.setPen(QColor(220, 220, 220))
        p.drawText(self.rect().adjusted(10, 10, -10, -10), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, self.msg)


class ScreenPanel(QFrame):
    def __init__(self, app, images):
        super().__init__()
        self.app, self.images = app, images
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        bar = QHBoxLayout()
        self.title = Elided("Guest screen", 150, name="title")
        self.poll = button("Poll OFF", app.toggle_poll, True)
        self.poll.setFixedWidth(self.poll.sizeHint().width())
        self.rate = Elided("", 64)
        self.rate.setAlignment(Qt.AlignCenter)
        self.info = Elided("", name="muted")
        bar.addWidget(self.title)
        bar.addWidget(self.poll)
        bar.addWidget(button("-", lambda: app.step_rate(-1)))
        bar.addWidget(self.rate)
        bar.addWidget(button("+", lambda: app.step_rate(+1)))
        bar.addWidget(button("Grab", app.grab))
        bar.addWidget(self.info, 1)
        lay.addLayout(bar)
        self.view = FrameView()
        lay.addWidget(self.view, 1)
        self.controls = [self.poll] + [bar.itemAt(i).widget() for i in (2, 4, 5)]

    def refresh(self):
        node = self.app.group.node()
        self.title.set(f"{node.name}: screen")
        p = node.poller
        for w in self.controls:
            w.setEnabled(p is not None)
        if p is None:
            self.rate.set("—")
            self.info.set("—")
            self.view.show_image(None, node.conn_msg)
            return
        self.poll.setChecked(p.enabled)
        self.poll.setText(f"Poll {'ON' if p.enabled else 'OFF'}")
        self.rate.set(f"{p.rate:g} fps")
        f = p.latest
        info = f"actual {p.fps:4.1f} fps"
        if f:
            info += f"  {f.src_w}x{f.src_h}  seq {f.seq}"
        if p.error:
            info = p.error
        self.info.set(info, T.ERR if p.error else T.MUTED)
        msg = "" if f is not None else "no frame yet"
        if f is not None and not p.enabled:
            msg = "polling off (F2)"
        self.view.show_image(self.images.get(node, f) if f is not None else None, msg)


# -- console ------------------------------------------------------------------------------------

class OutputView(QPlainTextEdit):
    """Console output: native selection and scrolling; typing goes to the input line."""

    def __init__(self, input_line):
        super().__init__()
        self.input = input_line
        self.setReadOnly(True)
        self.setFont(mono_font())
        self.setMaximumBlockCount(4000)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setFocusPolicy(Qt.ClickFocus)
        self.seen, self.gen = 0, 0

    def keyPressEvent(self, ev):
        k, mods = ev.key(), ev.modifiers()
        if k in (Qt.Key_PageUp, Qt.Key_PageDown) or (mods & Qt.ControlModifier and k in (Qt.Key_Home, Qt.Key_End)):
            super().keyPressEvent(ev)
        elif mods & Qt.ControlModifier and k in (Qt.Key_C, Qt.Key_Insert, Qt.Key_A):
            super().keyPressEvent(ev)
        else:
            self.input.setFocus()
            QApplication.sendEvent(self.input, ev)

    def selected_text(self):
        return self.textCursor().selectedText().replace(" ", "\n")

    def sync(self, out):
        """Append what `out` (app.Output) gained since the last call."""
        if out.gen != self.gen:                         # cleared
            self.gen, self.seen = out.gen, 0
            self.clear()
        new = out.total - self.seen
        if new <= 0:
            return
        items = list(out)[-new:] if new < len(out) else list(out)
        self.seen = out.total
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        cur = QTextCursor(self.document())
        cur.movePosition(QTextCursor.End)
        for text, tone, _ in items:
            fmt = QTextCharFormat()
            fmt.setForeground(tone_colour(tone))
            if self.document().characterCount() > 1:
                cur.insertBlock()
            cur.insertText(text, fmt)
        if at_bottom:
            bar.setValue(bar.maximum())


class InputLine(QLineEdit):
    """The command line: history, Tab completion that cycles, and the console's editing keys."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setFont(mono_font())
        self.hist, self.hist_i = [], 0
        self.cands, self.ci, self.base, self.start = [], -1, None, 0
        self.output = None                              # set by ConsolePanel
        self.textEdited.connect(self._edited)

    def focusNextPrevChild(self, nxt):                  # Tab is completion, not focus
        return False

    def _edited(self, _):
        self.cands = []
        self.app.update_hint()

    def remember(self, line):
        if line and (not self.hist or self.hist[-1] != line):
            self.hist.append(line)
        self.hist_i = len(self.hist)

    def _set(self, text):
        self.setText(text)
        self.setCursorPosition(len(text))

    def _tab(self):
        a = self.app
        if self.cands and self.base is not None:
            self.ci = (self.ci + 1) % len(self.cands)
            self._set(self.base[:self.start] + self.cands[self.ci])
            a.hint = "  ".join(self.cands[:14]) + ("  ..." if len(self.cands) > 14 else "")
            return
        text, cur = self.text(), self.cursorPosition()
        c = a.complete(text[:cur])
        if len(c.items) == 1:
            self.setText(text[:c.start] + c.items[0] + text[cur:])
            self.setCursorPosition(c.start + len(c.items[0]))
            a.hint = c.hint or a.hint
        elif c.items:
            filled = c.apply(text[:cur])
            self.setText(filled + text[cur:])
            self.setCursorPosition(len(filled))
            self.cands, self.start, self.base, self.ci = c.items, c.start, self.text(), -1
            a.hint = "  ".join(c.items[:14]) + ("  ..." if len(c.items) > 14 else "")
        else:
            a.hint = c.hint or a.hint

    def _paste(self):
        """Every complete pasted line runs; the tail stays for editing."""
        s = self.app.clip.get().replace("\r\n", "\n").replace("\r", "\n")
        if not s:
            self.app.hint = "clipboard is empty"
            return
        if "\n" not in s:
            self.insert(s)
            return
        text, cur = self.text(), self.cursorPosition()
        if self.hasSelectedText():
            text = text[:self.selectionStart()] + text[self.selectionEnd():]
            cur = self.selectionStart()
        parts = s.split("\n")
        lines = [text[:cur] + parts[0]] + parts[1:-1]
        self._set(parts[-1] + text[cur:])
        self.setCursorPosition(len(parts[-1]))
        for ln in (x.strip() for x in lines if x.strip()):
            self.remember(ln)
            self.app.submit(ln)

    def _copy(self):
        out = self.output.selected_text() if self.output else ""
        text = out or self.selectedText() or self.text()
        if not text:
            self.app.hint = "nothing to copy: drag over console lines or select in the input line"
            return
        how = self.app.clip.put(text)
        self.app.hint = f"copied {len(text.splitlines()) or 1} line(s), {len(text)} chars ({how})"

    def keyPressEvent(self, ev):
        k, mods = ev.key(), ev.modifiers()
        ctrl, shift = bool(mods & Qt.ControlModifier), bool(mods & Qt.ShiftModifier)
        if k == Qt.Key_Tab and not ctrl:
            self._tab()
            return
        if k not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            self.cands = []
        if k in (Qt.Key_Return, Qt.Key_Enter):
            line = self.text().strip()
            self.clear()
            self.remember(line)
            self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())
            if line:
                self.app.submit(line)
        elif ctrl and k in (Qt.Key_C, Qt.Key_Insert):
            self._copy()
        elif ctrl and k == Qt.Key_X:
            t = self.selectedText()
            if t:
                self.app.clip.put(t)
                self.del_()
        elif (ctrl and k == Qt.Key_V and not shift) or (shift and k == Qt.Key_Insert):
            self._paste()
        elif ctrl and k == Qt.Key_K:
            self.app.clip.put(self.text()[self.cursorPosition():])
            self.setText(self.text()[:self.cursorPosition()])
        elif ctrl and k == Qt.Key_U:
            cur = self.cursorPosition()
            self.app.clip.put(self.text()[:cur])
            self.setText(self.text()[cur:])
            self.setCursorPosition(0)
        elif ctrl and k == Qt.Key_L:
            self.app.out.clear()
        elif ctrl and k == Qt.Key_Backspace:
            self.cursorWordBackward(True)
            self.del_()
        elif ctrl and k == Qt.Key_Delete:
            self.cursorWordForward(True)
            self.del_()
        elif ctrl and k in (Qt.Key_Home, Qt.Key_End):
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum() if k == Qt.Key_End else 0)
        elif k == Qt.Key_Up:
            if self.hist:
                self.hist_i = max(0, self.hist_i - 1)
                self._set(self.hist[self.hist_i])
        elif k == Qt.Key_Down:
            if self.hist:
                self.hist_i = min(len(self.hist), self.hist_i + 1)
                self._set(self.hist[self.hist_i] if self.hist_i < len(self.hist) else "")
        elif k in (Qt.Key_PageUp, Qt.Key_PageDown):
            QApplication.sendEvent(self.output, ev)
        elif k == Qt.Key_Escape:
            if self.output.textCursor().hasSelection() or self.hasSelectedText():
                self.deselect()
                c = self.output.textCursor()
                c.clearSelection()
                self.output.setTextCursor(c)
            else:
                self.clear()
        else:
            super().keyPressEvent(ev)


class ConsolePanel(QFrame):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        t = Elided("@node line   Tab complete   Ctrl+C/X/V copy/cut/paste   drag to select output   "
                   "F1-F8 node keys   F9 resume all   F10 pause all", name="title")
        self.input = InputLine(app)
        self.out = OutputView(self.input)
        self.input.output = self.out
        self.prompt = Elided("", mono=True)
        self.prompt.set("rmm> ", T.ACCENT)
        self.hint = Elided("", name="muted")
        row = QHBoxLayout()
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)
        lay.addWidget(t)
        lay.addWidget(self.out, 1)
        lay.addLayout(row)
        lay.addWidget(self.hint)
        self.setFocusProxy(self.input)
        self._names = None

    def refresh(self):
        a = self.app
        node = a.group.node()
        names = tuple(a.group.nodes)
        if names != self._names:                         # fixed width from the longest prompt
            self._names = names
            longest = max(["rmm", "..."] + list(names), key=len)
            self.prompt.fix_width(self.prompt.fontMetrics().horizontalAdvance(longest + ">  ") + 12)
        self.out.sync(a.out)
        self.prompt.set("...  " if node.busy else (f"{node.name}> " if len(names) > 1 else "rmm> "), T.ACCENT)
        self.hint.set(f"running: {node.busy}" if node.busy else a.hint)
