# SPDX-License-Identifier: MIT
"""Node backends: which emulator a group node drives. The registry, the per-emulator launch and
control code, and the validation all live in the library (rmm_86box.backends), so rmmctl and other
tools see the same list this GUI does; this module only re-exports it for group.py and hostgroup.py.

Add an emulator: write rmm_86box/backends/<name>.py (start, validate, adapter) and register() it there.
"""

from rmm_86box.backends import Backend, Machine, find, get, names, register  # noqa: F401
