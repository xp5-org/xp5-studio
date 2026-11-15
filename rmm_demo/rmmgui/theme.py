# SPDX-License-Identifier: MIT
"""Colours and sizes: light and plain. Tones are the names node/group code uses for a line."""

BG = (244, 244, 242)
PANEL = (255, 255, 255)
BORDER = (204, 204, 200)
TEXT = (32, 32, 32)
MUTED = (112, 112, 112)
ACCENT = (32, 96, 200)
OK = (24, 128, 60)
WARN = (190, 110, 0)
ERR = (186, 34, 34)
INPUT_BG = (250, 250, 236)
BUTTON = (232, 232, 228)
SELECT = (196, 216, 248)
HALT_BG = (255, 236, 200)
SCREEN_BG = (20, 20, 20)
SNAP_GROUP = (222, 234, 252)      # a snapshot of every node
SNAP_OTHER = (240, 240, 236)      # one saved from a node's own console
RATES = [0.5, 1, 2, 3, 5, 10, 15, 20, 30]
PAD = 10
BG_POLL_RATE = 1.0          # frames per second for nodes other than the selected one

TONES = {"text": TEXT, "ok": OK, "warn": WARN, "err": ERR, "accent": ACCENT, "muted": MUTED}


def state_colour(state):
    return {"running": OK, "paused": WARN}.get(state, ERR)


def hexc(c):
    return "#%02x%02x%02x" % tuple(c)


def stylesheet():
    """Qt style sheet for the whole window; layout lives in the widgets, looks live here."""
    return f"""
    QWidget {{ background: {hexc(BG)}; color: {hexc(TEXT)}; }}
    QFrame#panel, QFrame#nodeTab {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)}; }}
    QFrame#nodeTab {{ background: {hexc(BUTTON)}; border-radius: 4px; }}
    QFrame#nodeTab[selected="true"] {{ background: {hexc(PANEL)}; border: 2px solid {hexc(ACCENT)}; }}
    QFrame#panel QLabel, QFrame#nodeTab QLabel {{ background: transparent; border: none; }}
    QLabel#title, QLabel#muted {{ color: {hexc(MUTED)}; }}
    QFrame#halt {{ background: {hexc(HALT_BG)}; border: 1px solid {hexc(WARN)}; }}
    QFrame#haltIdle {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)}; color: {hexc(MUTED)}; }}
    QFrame#halt QLabel, QFrame#haltIdle QLabel {{ background: transparent; border: none; }}
    QFrame#haltIdle QLabel {{ color: {hexc(MUTED)}; }}
    QPushButton {{ background: {hexc(BUTTON)}; border: 1px solid {hexc(BORDER)}; border-radius: 3px; padding: 3px 8px; }}
    QPushButton:checked {{ background: {hexc(OK)}; color: {hexc(PANEL)}; }}
    QPushButton#danger {{ background: {hexc(WARN)}; color: {hexc(PANEL)}; }}
    QPushButton:disabled {{ color: {hexc(MUTED)}; }}
    QPlainTextEdit, QGraphicsView {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)};
                                     selection-background-color: {hexc(SELECT)}; selection-color: {hexc(TEXT)}; }}
    QLineEdit {{ background: {hexc(INPUT_BG)}; border: 1px solid {hexc(BORDER)}; padding: 2px 4px;
                 selection-background-color: {hexc(SELECT)}; selection-color: {hexc(TEXT)}; }}
    QSplitter::handle {{ background: {hexc(BG)}; }}
    QMenuBar {{ background: {hexc(PANEL)}; border-bottom: 1px solid {hexc(BORDER)}; }}
    QMenuBar::item:selected, QMenu::item:selected {{ background: {hexc(SELECT)}; }}
    QMenu {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)}; }}
    QMenu::item:disabled {{ color: {hexc(MUTED)}; }}
    QToolBar {{ background: {hexc(PANEL)}; border-bottom: 1px solid {hexc(BORDER)}; spacing: 4px; }}
    QToolButton {{ padding: 3px; border: 1px solid transparent; border-radius: 3px; }}
    QToolButton:hover {{ border-color: {hexc(BORDER)}; background: {hexc(BUTTON)}; }}
    QToolButton:checked {{ border-color: {hexc(ACCENT)}; background: {hexc(SELECT)}; }}
    QTabWidget::pane {{ border: 1px solid {hexc(BORDER)}; background: {hexc(PANEL)}; }}
    QTabBar::tab {{ background: {hexc(BUTTON)}; border: 1px solid {hexc(BORDER)}; padding: 5px 14px; margin-right: 2px; }}
    QTabBar::tab:selected {{ background: {hexc(PANEL)}; border-bottom-color: {hexc(PANEL)}; color: {hexc(ACCENT)}; }}
    QTreeWidget, QTableWidget, QListWidget {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)};
                   selection-background-color: {hexc(SELECT)}; selection-color: {hexc(TEXT)}; }}
    QHeaderView::section {{ background: {hexc(BUTTON)}; border: none; border-right: 1px solid {hexc(BORDER)};
                   padding: 3px 6px; }}
    QGroupBox {{ background: {hexc(PANEL)}; border: 1px solid {hexc(BORDER)}; border-radius: 4px;
                margin-top: 14px; padding-top: 6px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {hexc(MUTED)}; }}
    QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ background: transparent; }}
    QLabel#badgeLive {{ background: #d8f0de; color: {hexc(OK)}; border-radius: 3px; padding: 1px 6px; }}
    QLabel#badgeNeed {{ background: #fde8cc; color: {hexc(WARN)}; border-radius: 3px; padding: 1px 6px; }}
    QLabel#warnbox {{ background: {hexc(HALT_BG)}; border: 1px solid {hexc(WARN)}; padding: 6px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {hexc(MUTED)};
                   background: white; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {hexc(ACCENT)}; }}
    QStatusBar {{ background: {hexc(PANEL)}; border-top: 1px solid {hexc(BORDER)}; }}
    QComboBox, QSpinBox {{ background: {hexc(INPUT_BG)}; border: 1px solid {hexc(BORDER)}; padding: 1px 4px; }}
    """
