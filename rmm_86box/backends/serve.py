# SPDX-License-Identifier: MIT
"""Serve any backend's adapter as a TCP JSON-RPC port that outlives the caller, in the dialect
RpcClient speaks (a banner line "OK <label> RMM <ver> <state>", then one JSON object per line).

SIMH is the reason: its only control is the pty its simulator was started on, so a tool that exits
(rmmctl) must leave something holding it. This is that something:

    python3 -m rmm_86box.backends.serve NAME --node '{"backend": "simh", "simh": {...}}' [--port N]

It starts the machine from the node dict (the same one a group file holds), prints
`READY <port>` on stdout once it listens, answers every method the adapter does, forwards the
adapter's events as notifications, and exits when the machine does (machine.quit included).
One client at a time, like 86Box.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

from .. import __version__
from ..rpc import RpcError
from ..spec import MachineSpec, free_port
from . import get


def _reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return (json.dumps(msg) + "\n").encode()


class Server:
    def __init__(self, machine, host="127.0.0.1", port=0):
        self.machine = machine
        self.rmm = machine.rmm
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.done = threading.Event()
        self._client = None
        self._wlock = threading.Lock()
        self.rmm.on_event(self._forward)

    def _send(self, data):
        c = self._client
        if c is None:
            return
        with self._wlock:
            try:
                c.sendall(data)
            except OSError:
                pass

    def _forward(self, name, params):
        if name == "rpc.disconnected":
            return
        self._send((json.dumps({"jsonrpc": "2.0", "method": name, "params": params}) + "\n").encode())

    def _banner(self):
        try:
            state = self.rmm.state()
        except OSError:
            state = "exited"
        return f"OK {self.machine.backend} RMM {__version__} {state}\n".encode()

    def _handle(self, line):
        try:
            req = json.loads(line)
            method, params, rid = req["method"], req.get("params") or {}, req.get("id")
        except (ValueError, KeyError, TypeError):
            return
        if method == "machine.quit":             # answer first: stopping the emulator can outlast the caller's timeout
            self._send(_reply(rid, {}))
            self.done.set()
            try:
                self._client.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            return
        try:
            result = self.rmm.call(method, **params)
            out = _reply(rid, result if result is not None else {})
        except RpcError as e:
            out = _reply(rid, error={"code": e.code if e.code is not None else -32603, "message": e.message})
        except OSError as e:
            out = _reply(rid, error={"code": -32603, "message": str(e)})
        except TypeError as e:
            out = _reply(rid, error={"code": -32602, "message": str(e)})
        if rid is not None:
            self._send(out)

    def _serve_client(self, conn):
        self._client = conn
        self._send(self._banner())
        try:
            for raw in conn.makefile("rb"):
                self._handle(raw.decode(errors="replace"))
        except OSError:
            pass
        finally:
            self._client = None
            conn.close()

    def run(self):
        self.sock.settimeout(0.5)
        while not self.done.is_set():
            if not self.rmm.connected and self.machine.proc is not None and not self.machine.proc.alive():
                break
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._serve_client(conn)             # one client at a time
        self.sock.close()


def spawn(spec, log_dir, wait=20.0):
    """Start `serve` for spec as a detached process -> (Popen, port, out_path, err_path) once it prints
    READY. For a caller that exits (rmmctl) and must leave the machine controllable. The caller owns
    the Popen; ValueError text is why it did not come up (the child's stderr tail included)."""
    os.makedirs(log_dir, exist_ok=True)
    out_path = os.path.join(log_dir, spec.name + ".out")
    err_path = os.path.join(log_dir, spec.name + ".err")
    env = dict(os.environ)
    lib_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["PYTHONPATH"] = lib_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, "-m", "rmm_86box.backends.serve", spec.name, "--node", json.dumps(spec.raw)]
    if isinstance(spec.port, int):
        argv += ["--port", str(spec.port)]
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                start_new_session=not sys.platform.startswith("win"))
    deadline = time.time() + wait
    while time.time() < deadline:
        with open(out_path) as f:
            for line in f:
                if line.startswith("READY "):
                    return proc, int(line.split()[1]), out_path, err_path
                if line.startswith("ERROR"):
                    proc.wait(5)
                    raise ValueError(line.strip())
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    with open(err_path) as f:
        tail = "".join(f.readlines()[-6:]).strip()
    if proc.poll() is None:
        proc.terminate()
    raise ValueError(f"{spec.name}: the {spec.backend} server did not come up in {wait:g}s" + (f"\n{tail}" if tail else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rmm_86box.backends.serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--node", required=True, help="the node dict as JSON, as an rmm-group/1 file holds it")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="TCP port to serve on (default: a free one)")
    a = ap.parse_args(argv)
    spec = MachineSpec.of(a.name, json.loads(a.node))
    backend = get(spec.backend)
    bad = backend.validate(spec)
    if bad:
        print(f"ERROR {a.name}: {bad}", flush=True)
        return 2
    machine = backend.start(spec, say=lambda text, tone="text": None)
    machine.rmm.connect(wait=30)
    srv = Server(machine, a.host, a.port or free_port(a.host))
    print(f"READY {srv.port}", flush=True)
    try:
        srv.run()
    finally:
        machine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
