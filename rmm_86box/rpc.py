# SPDX-License-Identifier: MIT
"""Thread-safe JSON-RPC 2.0 client for the 86Box remote machine monitor (stdlib only).

Wire format: one JSON object per line over TCP (127.0.0.1 only; 86Box takes one client at a
time). The server sends a legacy banner first ("OK 86Box RMM <ver> <paused|running>"); after
that the first byte of each line picks the dialect, '{' being JSON-RPC.

Unlike rmm_client/rpc.py (the spec's wire-level client, which reads replies inline), this one
runs a reader thread, so several threads can share the single connection: a GUI polling
frames, a console sending commands, and event callbacks all at once.

    c = RpcClient(55860); c.connect(wait=20)
    c.call("machine.status")                     -> result dict, or raises RpcError
    c.on_event(lambda name, params: ...)         -> called on the reader thread
    c.wait_event("machine.stopped", timeout=5)   -> params, or None
"""

import itertools
import json
import queue
import socket
import threading
import time


class RpcError(Exception):
    """`code` is the JSON-RPC error code: -32700/-32600/-32601/-32602/-32603, or an app code:
    1 not paused, 2 slot empty, 3 no such slot, 4 busy/absent/disabled, 5 write dropped,
    7 no such snapshot, 8 cannot snapshot, 9 linear address not mapped, 10 config refused,
    11 protocol too old (rpc.hello).
    None: not a JSON-RPC reply at all (or the connection went away)."""

    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self):
        return f"[{self.code}] {self.message}" if self.code is not None else self.message


class RpcClient:
    def __init__(self, port, host="127.0.0.1", timeout=10.0):
        self.addr = (host, int(port))
        self.timeout = timeout
        self.sock = None
        self.banner = None
        self.events = queue.Queue()          # every notification, as {"method", "params"}
        self._ids = itertools.count(1)
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending = {}                    # id -> [threading.Event, response]
        self._legacy = queue.Queue()          # replies to legacy-dialect lines
        self._callbacks = []
        self._reader = None
        self._closed = threading.Event()

    # -- lifecycle ------------------------------------------------------------------------

    def connect(self, wait=0):
        """Connect (retrying for `wait` seconds while 86Box starts) and read the banner."""
        deadline = time.time() + float(wait)
        while True:
            try:
                self.sock = socket.create_connection(self.addr, timeout=self.timeout)
                break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.5)
        # The banner is read under the connect timeout: 86Box serves one client at a time, so a
        # port another client holds accepts the connection (backlog) and then says nothing.
        self._closed.clear()
        self._rfile = self.sock.makefile("rb")
        try:
            self.banner = self._rfile.readline().decode(errors="replace").rstrip("\r\n")
        except OSError as e:
            self.close()
            raise RpcError(f"no banner from {self.addr[0]}:{self.addr[1]} within {self.timeout}s "
                           "(is another client attached?)") from e
        self.sock.settimeout(None)
        if not self.banner.startswith("OK"):
            self.close()
            raise RpcError(f"bad banner: {self.banner!r}")
        self._reader = threading.Thread(target=self._read_loop, name="rmm-reader", daemon=True)
        self._reader.start()
        return self.banner

    @property
    def connected(self):
        return self.sock is not None and not self._closed.is_set()

    def close(self):
        self._closed.set()
        s, self.sock = self.sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)     # wakes the reader, which closes its file on exit
            except OSError:
                pass
            s.close()
        rf = getattr(self, "_rfile", None)
        if rf and not (self._reader and self._reader.is_alive()):
            try:
                rf.close()                       # no reader to do it: the fd would stay open with rf
            except OSError:
                pass
        self._fail_pending("connection closed")

    def __enter__(self):
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- reader ---------------------------------------------------------------------------

    def _read_loop(self):
        rf = self._rfile
        try:
            for raw in rf:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line.startswith("{"):
                    self._legacy.put(line)
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if "method" in obj and "id" not in obj:
                    self._dispatch_event(obj)
                    continue
                with self._plock:
                    slot = self._pending.get(obj.get("id"))
                if slot:
                    slot[1] = obj
                    slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            try:
                rf.close()                       # releases the socket's fd (makefile holds it open)
            except (OSError, ValueError):
                pass
            self._closed.set()
            self._fail_pending("connection closed by 86Box")
            self._dispatch_event({"method": "rpc.disconnected", "params": {}})

    def _dispatch_event(self, obj):
        if self.events.qsize() >= 1000:          # nobody is draining: keep the newest
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
        self.events.put(obj)
        for cb in list(self._callbacks):
            try:
                cb(obj.get("method"), obj.get("params") or {})
            except Exception:  # noqa: BLE001 - a bad callback must not kill the reader
                pass

    def _fail_pending(self, why):
        with self._plock:
            for slot in self._pending.values():
                if slot[1] is None:
                    slot[1] = {"error": {"code": None, "message": why}}
                slot[0].set()

    # -- requests -------------------------------------------------------------------------

    def _send(self, text):
        if not self.connected:
            raise RpcError("not connected")
        with self._wlock:
            s = self.sock                    # close() may clear it from another thread
            if s is None:
                raise RpcError("not connected")
            s.sendall((text + "\n").encode())

    def call(self, method, params=None, timeout=None, **kw):
        """Call `method` with `params` (a dict) and/or keyword params; returns the result or
        raises RpcError. Safe from any thread."""
        p = dict(params or {}, **kw)
        rid = next(self._ids)
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if p:
            req["params"] = p
        slot = [threading.Event(), None]
        with self._plock:
            self._pending[rid] = slot
        try:
            self._send(json.dumps(req))
            if not slot[0].wait(self.timeout if timeout is None else timeout):
                raise RpcError(f"{method}: no reply within {self.timeout if timeout is None else timeout}s")
        finally:
            with self._plock:
                self._pending.pop(rid, None)
        resp = slot[1]
        if "error" in resp:
            e = resp["error"] or {}
            raise RpcError(e.get("message", ""), e.get("code"), e.get("data"))
        return resp.get("result")

    def notify(self, method, **params):
        req = {"jsonrpc": "2.0", "method": method}
        if params:
            req["params"] = params
        self._send(json.dumps(req))

    def legacy(self, line, timeout=None):
        """One legacy-dialect line ("status", "savevm x", ...); returns the raw reply line."""
        self._send(line)
        try:
            return self._legacy.get(timeout=self.timeout if timeout is None else timeout)
        except queue.Empty:
            raise RpcError(f"{line!r}: no reply") from None

    # -- events ---------------------------------------------------------------------------

    def on_event(self, callback):
        """callback(name, params) for every notification, on the reader thread. Also
        "rpc.disconnected" when the socket closes."""
        self._callbacks.append(callback)
        return callback

    def wait_event(self, name, timeout=5.0, where=None):
        """Params of the next `name` notification within `timeout`, else None. Other events
        taken off the queue meanwhile are put back (behind any that arrived since)."""
        deadline, skipped = time.time() + timeout, []
        try:
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return None
                try:
                    ev = self.events.get(timeout=left)
                except queue.Empty:
                    return None
                p = ev.get("params") or {}
                if ev.get("method") == name and (where is None or where(p)):
                    return p
                skipped.append(ev)
        finally:
            for ev in skipped:
                self.events.put(ev)

    def drain_events(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out
