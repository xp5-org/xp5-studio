#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""xp5-studio: the program a release bundle's user double-clicks. Opens the Qt console blank (the
reference mock stands in until a project or group file is opened, or a node is added with
Attach / Launch / QEMU) and starts file dialogs in the bundle folder, which holds 86Box, roms/
and projects/. Any rmm_demo.py option still works: `xp5-studio --project projects/x.rmmproj.json`.

From a source checkout: python xp5_studio.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
DEMO = os.path.join(HERE, "rmm_demo")
for p in (HERE, DEMO):                              # source checkout: the library and the GUI beside us
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import rmm_demo  # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a.split("=")[0] in ("--project", "--group", "--attach", "--mock", "--port", "--empty") for a in argv):
        argv.append("--empty")
    for i, a in enumerate(argv):                    # paths given on the command line stay valid after the chdir below
        if a in ("--project", "--group") and i + 1 < len(argv):
            argv[i + 1] = os.path.abspath(argv[i + 1])
    for sub in ("projects", ""):
        d = os.path.join(HERE, sub)
        if os.path.isdir(d):
            os.chdir(d)                             # file dialogs open here
            break
    return rmm_demo.main(argv)


if __name__ == "__main__":
    sys.exit(main())
