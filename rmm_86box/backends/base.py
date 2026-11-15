# SPDX-License-Identifier: MIT
"""Backend: the Rmm surface over something that is not 86Box's monitor.

A subclass lists the RMM methods it honours in HANDLERS ({rmm name: python method}) and
any methods of its own in EXTRA (rmm_schema rows). `methods` holds exactly those, with the
params, results and docs of 86Box's schema where the names match, so completion and the GUI
show what this machine can do. call() dispatches; a name not offered is -32601.
"""

import threading

from .. import client
from .._vendor import rmm_schema
from ..registry import Method, Methods
from ..rpc import RpcError


def Unsupported(method):
    return RpcError(f"{method}: not offered by this backend", code=-32601)


class Backend:
    HANDLERS = {}                     # rmm method name -> name of the python method that runs it
    EXTRA = {}                        # name -> (params, returns, errors, doc), rows the 86Box schema lacks
    KIND = "?"

    def __init__(self):
        self.methods = self._method_table()
        self.last_result = None
        self.server_info = None
        self.vm = None
        self._listeners = []
        self._lock = threading.Lock()

    @classmethod
    def _method_table(cls):
        m = Methods()
        rows = {n: m.by_name[n] for n in cls.HANDLERS if n in m.by_name}
        for name, (ps, ret, errors, doc) in cls.EXTRA.items():
            params, enums = rmm_schema.parse_spec(ps)
            rows[name] = Method(name, params, ret.split(), doc, enums=enums,
                                errors=[int(c) for c in errors.split()])
        m.by_name = rows
        m.from_server = True          # this table is the whole truth: no static fallback rows
        return m

    # -- what a subclass provides ------------------------------------------------------------

    def connect(self, wait=0):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    @property
    def connected(self):
        raise NotImplementedError

    # -- the Rmm surface -----------------------------------------------------------------------

    def __enter__(self):
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method, _timeout=None, **params):
        attr = self.HANDLERS.get(method)
        if attr is None:
            raise Unsupported(method)
        try:
            with self._lock:
                r = getattr(self, attr)(**params)
        except TypeError as e:
            raise RpcError(f"{method}: {e}", code=-32602) from None
        self.last_result = r
        return r

    def on_event(self, callback):
        """callback(name, params) with RMM event names (machine.stopped, machine.resumed,
        machine.reset, machine.exit, rpc.disconnected)."""
        self._listeners.append(callback)
        return callback

    def _emit(self, name, params=None):
        for cb in list(self._listeners):
            cb(name, params or {})

    def discover(self):
        """rpc.discover in 86Box's shape, from `methods`."""
        return {"methods": [{"name": m.name, "params": m.params, "returns": m.returns, "doc": m.doc,
                             "errors": m.errors, "enums": m.enums} for m in self.methods.by_name.values()],
                "events": [{"name": n, "fields": f} for n, f in self.methods.events.items()],
                "errors": [{"code": c, "name": n} for c, n in self.methods.errors.items()]}

    # the same helpers and attribute chains as Rmm (rmm.machine.status(), rmm.input.tap(...))
    __getattr__ = client.Rmm.__getattr__
    __dir__ = client.Rmm.__dir__
    _children = client.Rmm._children
    state = client.Rmm.state
    paused = client.Rmm.paused
    screenshot = client.Rmm.screenshot
