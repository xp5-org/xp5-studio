# SPDX-License-Identifier: MIT
"""Start and stop a local, headless QEMU: its run dir holds the sockets and the log.

    p = QemuProcess("isapc", fda="/img/dos.img").start()
    p.qmp_path, p.pid, p.ended()        # ended(): None while it runs, else the exit code
    p.stop()                            # QMP quit, then SIGTERM, then SIGKILL
"""

import os
import shutil
import subprocess
import tempfile

from . import profiles


class QemuProcess:
    def __init__(self, profile, run_dir=None, binary=None, memory_mib=None, vnc=False, gdb_port=None,
                 paused=True, args=(), env=None, **opts):
        self.profile = profiles.get(profile) if isinstance(profile, str) else profile
        self.own_dir = run_dir is None
        self.run_dir = run_dir or tempfile.mkdtemp(prefix=f"rmm-qemu-{self.profile.name}-")
        os.makedirs(self.run_dir, exist_ok=True)
        self.opts = opts
        self.argv = profiles.argv(self.profile, self.run_dir, binary=binary, memory_mib=memory_mib, vnc=vnc,
                                  gdb_port=gdb_port, paused=paused, args=args, **opts)
        self.memory_mib = memory_mib or self.profile.memory_mib
        self.env = env
        self.log = os.path.join(self.run_dir, "qemu.log")
        self.proc = None

    @property
    def qmp_path(self):
        return profiles.sockets(self.run_dir)["qmp"]

    @property
    def vnc_path(self):
        return profiles.sockets(self.run_dir)["vnc"]

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def start(self):
        for path in profiles.sockets(self.run_dir).values():
            if os.path.exists(path):
                os.unlink(path)               # a stale socket from a killed run
        env = dict(os.environ, **(self.env or {}))
        with open(self.log, "ab") as log:
            self.proc = subprocess.Popen(self.argv, cwd=self.run_dir, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return self

    def ended(self):
        return self.proc.poll() if self.proc else None

    def tail(self, n=20):
        try:
            with open(self.log, "rb") as f:
                return f.read().decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            return []

    def stop(self, qmp=None, timeout=5.0):
        """Ask QEMU to quit (over `qmp` when given), then terminate, then kill."""
        if self.proc and self.proc.poll() is None:
            self._end(qmp, timeout)
        if self.own_dir:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        return self.ended()

    def _end(self, qmp, timeout):
        steps = [self.proc.terminate, self.proc.kill]
        if qmp is not None and qmp.connected:
            try:
                qmp.execute("quit", timeout=timeout)
                steps.insert(0, None)
            except Exception:
                pass
        for sig in steps:
            if sig:
                sig()
            try:
                self.proc.wait(timeout)
                break
            except subprocess.TimeoutExpired:
                continue
