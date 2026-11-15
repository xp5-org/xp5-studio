# SPDX-License-Identifier: MIT
"""Start the RMM reference mock (rmm_86box/_vendor/rmm_mock.py, the spec's Python model of the monitor)
in-process, for trying the library or the demo with no VM.

rmm_mock.py and rmm_registry.py live in rmm_86box/_vendor/ (owned by this repo),
so this works from the wheel or the .pyz; a source checkout finds them there too.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.join(HERE, "_vendor")


def _mock_module():
    try:
        from ._vendor import rmm_mock          # wheel / pyz (zipimport handles it)
        return rmm_mock
    except ImportError:
        pass
    if os.path.exists(os.path.join(CHECKOUT, "rmm_mock.py")):
        if CHECKOUT not in sys.path:
            sys.path.insert(0, CHECKOUT)
        import rmm_mock
        return rmm_mock
    raise RuntimeError(f"rmm_mock.py not found (not vendored, and no {CHECKOUT})")


def start_mock(port=0, hw=None):
    """A running MockServer; .port is where to connect, .stop() ends it. hw picks extra mock
    hardware, e.g. "joy_ms" (a joystick and a Microsoft serial mouse on COM1)."""
    srv = _mock_module().MockServer(port, hw)
    srv.start()
    return srv
