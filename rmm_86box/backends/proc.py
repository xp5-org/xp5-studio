# SPDX-License-Identifier: MIT
"""The process behind a machine this library started: how it ended, its log, how to stop it.
Every backend that launches something hands back a Proc, so a caller (the Qt manager, rmmctl, a
script) reads one shape whatever the emulator is.
"""

import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = sys.platform.startswith("win")
WIN_CRASH = {0xC0000005: "access violation", 0xC0000409: "stack buffer overrun", 0xC000001D: "illegal instruction",
             0xC0000094: "integer divide by zero", 0xC00000FD: "stack overflow", 0x80000003: "breakpoint trap"}


def describe_exit(code):
    """('exit' | 'crash', text) for a returncode."""
    if code is None:
        return None, "running"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return ("exit" if -code in (signal.SIGTERM, signal.SIGINT) else "crash"), name
    if (IS_WINDOWS and (code & 0xC0000000) == 0xC0000000) or (code & 0xFFFFFFFF) in WIN_CRASH:
        return "crash", WIN_CRASH.get(code & 0xFFFFFFFF, f"exception 0x{code & 0xFFFFFFFF:08X}")
    if code in (128 + 11, 128 + 6, 128 + 4, 128 + 7, 128 + 8):     # shell-style signal codes
        return "crash", f"exit {code} ({signal.Signals(code - 128).name})"
    return "exit", f"exit {code}"


class Proc:
    """One emulator process. `label` is how messages name it ("86Box", "QEMU", ...); `log` is a
    file whose tail says why it died, when the emulator writes one."""

    def __init__(self, popen, label, log=None, vm_dir=None, on_exit="quit"):
        self.popen = popen
        self.label = label
        self.log = log
        self.vm_dir = vm_dir
        self.on_exit = on_exit                    # quit | keep: what stopping the machine does to it

    @classmethod
    def spawn(cls, argv, label, cwd=None, env=None, log=None, vm_dir=None, on_exit="quit", stdout=None):
        popen = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stdout or subprocess.DEVNULL, stderr=subprocess.STDOUT if stdout else subprocess.DEVNULL,
                                 start_new_session=not IS_WINDOWS)
        return cls(popen, label, log=log, vm_dir=vm_dir, on_exit=on_exit)

    @property
    def pid(self):
        return self.popen.pid

    def alive(self):
        return self.popen.poll() is None

    def ended(self, wait=0.0):
        """(kind, text) once the process has ended, else None. kind is 'exit' or 'crash'."""
        deadline = time.time() + wait
        while self.popen.poll() is None:
            if time.time() >= deadline:
                return None
            time.sleep(0.05)
        return describe_exit(self.popen.returncode)

    def log_tail(self, n=4, grep=None):
        if not self.log:
            return []
        try:
            lines = open(self.log, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            return []
        if grep:
            lines = [ln for ln in lines if grep in ln]
        return lines[-n:]

    def bind_failed(self):
        return False

    def stop(self, rmm=None, timeout=5.0):
        """machine.quit over the adapter when connected (the emulator then exits cleanly and
        86Box writes nvr/), else SIGTERM, then SIGKILL."""
        if not self.alive():
            return
        if rmm is not None and rmm.connected:
            try:
                rmm.call("machine.quit", _timeout=3)
            except Exception:  # noqa: BLE001 - fall back to a signal
                pass
            if self.ended(timeout):
                return
        self.popen.terminate()
        if not self.ended(timeout):
            self.popen.kill()
            self.ended(timeout)
