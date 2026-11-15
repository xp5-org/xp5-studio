# SPDX-License-Identifier: MIT
"""The console's input line: text, cursor, selection, history, and clipboard edits. No Qt
here. The Qt window uses QLineEdit (view.InputLine) instead; this is kept for its unit tests
and can go when those do.

The selection runs from `anchor` to `cursor` (anchor None = nothing selected). Typing or
pasting replaces it, as in any text field.
"""


class LineEditor:
    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.anchor = None
        self.hist = []
        self.hist_i = 0

    # -- selection -------------------------------------------------------------------------

    def selection(self):
        """(start, end) of the selection, or None."""
        if self.anchor is None or self.anchor == self.cursor:
            return None
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def selected_text(self):
        s = self.selection()
        return self.text[s[0]:s[1]] if s else ""

    def select_all(self):
        self.anchor, self.cursor = 0, len(self.text)

    def _move(self, pos, select):
        if select:
            if self.anchor is None:
                self.anchor = self.cursor
        else:
            self.anchor = None
        self.cursor = max(0, min(len(self.text), pos))

    def left(self, select=False, word=False):
        s = self.selection()
        if s and not select:
            self._move(s[0], False)
            return
        self._move(self._word_left() if word else self.cursor - 1, select)

    def right(self, select=False, word=False):
        s = self.selection()
        if s and not select:
            self._move(s[1], False)
            return
        self._move(self._word_right() if word else self.cursor + 1, select)

    def home(self, select=False):
        self._move(0, select)

    def end(self, select=False):
        self._move(len(self.text), select)

    def _word_left(self):
        i = self.cursor
        while i > 0 and self.text[i - 1] in " .=":
            i -= 1
        while i > 0 and self.text[i - 1] not in " .=":
            i -= 1
        return i

    def _word_right(self):
        i, n = self.cursor, len(self.text)
        while i < n and self.text[i] in " .=":
            i += 1
        while i < n and self.text[i] not in " .=":
            i += 1
        return i

    # -- edits ------------------------------------------------------------------------------

    def _cut_selection(self):
        s = self.selection()
        if not s:
            return False
        self.text = self.text[:s[0]] + self.text[s[1]:]
        self.cursor, self.anchor = s[0], None
        return True

    def insert(self, s):
        """Insert at the cursor, replacing a selection. Newlines become spaces (paste() is the
        multi-line path)."""
        s = s.replace("\r", "").replace("\n", " ").replace("\t", " ")
        self._cut_selection()
        self.text = self.text[:self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)

    def backspace(self, word=False):
        if self._cut_selection():
            return
        if self.cursor:
            start = self._word_left() if word else self.cursor - 1
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start

    def delete(self, word=False):
        if self._cut_selection():
            return
        end = self._word_right() if word else self.cursor + 1
        self.text = self.text[:self.cursor] + self.text[end:]

    def kill_to_end(self):
        """Ctrl+K: cut from the cursor to the end; returns the text removed."""
        gone = self.text[self.cursor:]
        self.text, self.anchor = self.text[:self.cursor], None
        return gone

    def kill_to_start(self):
        """Ctrl+U: cut from the start to the cursor; returns the text removed."""
        gone = self.text[:self.cursor]
        self.text, self.cursor, self.anchor = self.text[self.cursor:], 0, None
        return gone

    def clear(self):
        self.text, self.cursor, self.anchor = "", 0, None

    def set(self, text):
        self.text, self.cursor, self.anchor = text, len(text), None

    # -- clipboard ------------------------------------------------------------------------

    def cut(self):
        t = self.selected_text()
        if t:
            self._cut_selection()
        return t

    def paste(self, s):
        """Paste clipboard text. Every complete line (ended by a newline) is returned to be
        run, the first one joined to what is typed before the cursor; the tail stays in the
        line for editing, followed by what was after the cursor. A paste with no newline is
        just an insert."""
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in s:
            self.insert(s)
            return []
        self._cut_selection()
        before, after = self.text[:self.cursor], self.text[self.cursor:]
        parts = s.split("\n")
        lines = [before + parts[0]] + parts[1:-1]
        self.text = parts[-1] + after
        self.cursor, self.anchor = len(parts[-1]), None
        return [ln for ln in lines if ln.strip()]

    # -- history ---------------------------------------------------------------------------

    def submit(self):
        """Enter: the line to run ('' for none); it goes into history."""
        line = self.text.strip()
        self.clear()
        if line:
            if not self.hist or self.hist[-1] != line:
                self.hist.append(line)
            self.hist_i = len(self.hist)
        return line

    def remember(self, line):
        if line and (not self.hist or self.hist[-1] != line):
            self.hist.append(line)
        self.hist_i = len(self.hist)

    def up(self):
        if self.hist:
            self.hist_i = max(0, self.hist_i - 1)
            self.set(self.hist[self.hist_i])

    def down(self):
        if self.hist:
            self.hist_i = min(len(self.hist), self.hist_i + 1)
            self.set(self.hist[self.hist_i] if self.hist_i < len(self.hist) else "")


class TabCycler:
    """Tab completion over a Console-like `complete(text) -> Completion` (items, start, hint,
    apply). First Tab fills the common part; Tab again cycles the candidates."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.cands, self.i, self.base, self.start = [], -1, None, 0

    def tab(self, ed, complete):
        """Complete in place on LineEditor `ed`; returns a hint line (or "")."""
        if self.cands and self.base is not None:
            self.i = (self.i + 1) % len(self.cands)
            ed.set(self.base[:self.start] + self.cands[self.i])
            return "  ".join(self.cands[:14]) + ("  ..." if len(self.cands) > 14 else "")
        ed.anchor = None
        c = complete(ed.text[:ed.cursor])
        if len(c.items) == 1:
            ed.text = ed.text[:c.start] + c.items[0] + ed.text[ed.cursor:]
            ed.cursor = c.start + len(c.items[0])
            return c.hint
        if c.items:
            filled = c.apply(ed.text[:ed.cursor])
            ed.text, ed.cursor = filled + ed.text[ed.cursor:], len(filled)
            self.cands, self.start, self.base = c.items, c.start, ed.text
            return "  ".join(c.items[:14]) + ("  ..." if len(c.items) > 14 else "")
        return c.hint
