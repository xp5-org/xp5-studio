# SPDX-License-Identifier: MIT
"""The xp5-studio GUI, in parts:

    group.py      host-group JSON files (rmm-group/1): parse, merge, tokens, check, save
    launch.py     start 86Box for a node: write 86box.cfg, --monitor-port, exit/crash status
    node.py       one VM: connection, console, frame poller, status, its own worker thread
    hostgroup.py  the running group: @targets, halt-all policy, pause/resume fan-out, group snap/restore
    snaps.py      the snapshot family tree (records, heads, branches) and its layout
    commands.py   console builtins the GUI adds (use, nodes, halt, resume, group, copy, snap, restore, snaps, ...)
    demos.py      `demo timetravel | vga | step | vm | life`
    editline.py   the input line: selection, cut/copy/paste, history, Tab cycling
    clipboard.py  system clipboard (Qt, then wl-copy/xclip/xsel/pbcopy/clip), else in-process
    theme.py      colours and sizes
    view.py       the panels: node tabs, machine, screen, console (PySide6 widgets)
    snapview.py   the Snapshots tab: the family tree in a QGraphicsView (PySide6)
    app.py        the window: wiring, keys, the 30 Hz refresh timer (PySide6)

Only view.py, snapview.py and app.py import PySide6, so everything else is testable headless.
"""
