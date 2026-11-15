# SPDX-License-Identifier: MIT
"""Moved to the library: rmm_86box.backends.box86 (86box.cfg, Launched) and backends.proc
(describe_exit). Kept so old imports work."""

from rmm_86box.backends.box86 import Launched, node_cfg, read_cfg, write_cfg  # noqa: F401
from rmm_86box.backends.proc import IS_WINDOWS, WIN_CRASH, describe_exit  # noqa: F401
