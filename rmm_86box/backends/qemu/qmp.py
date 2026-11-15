# SPDX-License-Identifier: MIT
"""A small blocking QMP client: one socket, a reader thread, replies matched by id, events
to a callback. Stdlib only.

    q = Qmp("/run/vm/qmp.sock")          # or ("127.0.0.1", 4444)
    q.connect(wait=5)                     # greeting + qmp_capabilities
    q.execute("query-status")             # -> {"running": True, "status": "running", ...}
    q.on_event = lambda name, data: ...   # STOP, RESUME, RESET, SHUTDOWN, ...
"""

import itertools
import json
import socket
import threading
import time

from ...rpc import RpcError


class Qmp:
    def __init__(self, address, timeout=10.0):
        self.address = address            # unix socket path, or (host, port)
        self.timeout = timeout
        self.greeting = None
        self.on_event = None               # fn(name, data) on the reader thread
        self.on_close = None               # fn() once the socket is gone
        self._sock = None
        self._ids = itertools.count(1)
        self._wait = {}                    # id -> [Event, reply]
        self._send_lock = threading.Lock()

    @property
    def connected(self):
        return self._sock is not None

    def connect(self, wait=0):
        """Connect, retrying for `wait` seconds while QEMU is still creating the socket."""
        end = time.time() + wait
        while True:
            try:
                fam = socket.AF_UNIX if isinstance(self.address, str) else socket.AF_INET
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(self.address)
                break
            except OSError:
                s.close()
                if time.time() >= end:
                    raise
                time.sleep(0.1)
        f = s.makefile("rb")
        self.greeting = json.loads(f.readline() or b"null")
        if not self.greeting or "QMP" not in self.greeting:
            s.close()
            raise OSError(f"{self.address}: not a QMP monitor")
        s.settimeout(None)
        self._sock = s
        threading.Thread(target=self._reader, args=(f,), name="qmp-reader", daemon=True).start()
        self.execute("qmp_capabilities")
        return self.greeting["QMP"].get("version", {}).get("qemu", {})

    def close(self):
        s, self._sock = self._sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def execute(self, command, timeout=None, **arguments):
        """The command's `return`; a QMP error raises RpcError (code -32000, data = its class)."""
        if not self._sock:
            raise RpcError("QMP not connected")
        i = next(self._ids)
        slot = self._wait[i] = [threading.Event(), None]
        msg = {"execute": command, "id": i}
        if arguments:
            msg["arguments"] = arguments
        with self._send_lock:
            self._sock.sendall(json.dumps(msg).encode() + b"\n")
        if not slot[0].wait(timeout or self.timeout):
            self._wait.pop(i, None)
            raise RpcError(f"{command}: no reply from QEMU")
        r = slot[1]
        if r is None:
            raise RpcError(f"{command}: QEMU went away")
        if "error" in r:
            raise RpcError(f"{command}: {r['error'].get('desc')}", code=-32000, data=r["error"].get("class"))
        return r.get("return")

    def hmp(self, line):
        """One human-monitor line (savevm, info snapshots, ...): its text output."""
        return self.execute("human-monitor-command", **{"command-line": line})

    def _reader(self, f):
        try:
            for line in f:
                msg = json.loads(line)
                if "event" in msg:
                    if self.on_event:
                        self.on_event(msg["event"], msg.get("data", {}))
                    continue
                slot = self._wait.pop(msg.get("id"), None)
                if slot:
                    slot[1] = msg
                    slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            s, self._sock = self._sock, None
            if s:
                s.close()
            f.close()
            for slot in self._wait.values():
                slot[0].set()
            self._wait.clear()
            if self.on_close:
                self.on_close()
