# SPDX-License-Identifier: MIT
"""The VICE backend against a fake binary monitor (no VICE is installed here): registry and group
validation, then ViceRmm pause / snapshot / restore / delete / resume, memory read and a frame
through `capture.screenshot`.

    python3 -m unittest discover -s tests      (from rmm_demo/)
"""

import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, ".."), "/testsrc/pyhelpers"):
    sys.path.insert(0, os.path.abspath(p))

from rmm_86box import Frame, RpcError  # noqa: E402
from rmmgui import backends  # noqa: E402
from rmmgui.backends.vice import ViceRmm  # noqa: E402
from rmmgui.group import GroupError, GroupSpec  # noqa: E402

W, H = 6, 4                                      # fake display: 6x4 with a 2x2 border each side... inner 4x2


class FakeMonitor:
    """Speaks enough of the binary monitor: stops on any command but ping/exit, like VICE."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.stopped = False
        self.log = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _recv(self, c, n):
        buf = b""
        while len(buf) < n:
            d = c.recv(n - len(buf))
            if not d:
                raise OSError
            buf += d
        return buf

    def _send(self, c, rtype, rid, body=b"", err=0):
        c.sendall(struct.pack("<BBIBBI", 2, 2, len(body), rtype, err, rid) + body)

    def _serve(self):
        while True:                              # clients one after another, as rmmctl's commands connect
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self._conn(c)

    def _conn(self, c):
        try:
            while True:
                head = self._recv(c, 11)
                length, rid, cmd = struct.unpack("<IIB", head[2:])
                body = self._recv(c, length) if length else b""
                self.log.append(cmd)
                if cmd not in (0x81, 0xAA, 0xBB) and not self.stopped:
                    self.stopped = True
                    self._send(c, 0x62, 0xFFFFFFFF, struct.pack("<H", 0xC000))
                if cmd == 0xAA:
                    self.stopped = False
                    self._send(c, 0x63, 0xFFFFFFFF, struct.pack("<H", 0xC000))
                    self._send(c, 0xAA, rid)
                elif cmd == 0x01:
                    _, a, b = struct.unpack_from("<BHH", body)
                    data = bytes(range(a & 0xFF, (a & 0xFF) + (b - a + 1)))
                    self._send(c, 0x01, rid, struct.pack("<H", len(data)) + data)
                elif cmd == 0x41:
                    name = body[3:3 + body[2]].decode()
                    open(name, "wb").write(b"VSF")
                    self._send(c, 0x41, rid)
                elif cmd == 0x42:
                    name = body[1:1 + body[0]].decode()
                    self._send(c, 0x42, rid, err=0 if os.path.exists(name) else 1)
                elif cmd == 0x84:
                    inner = bytes((x * 10 + y) % 256 for y in range(H) for x in range(W) for _ in range(3))
                    hdr = struct.pack("<HHHHHHB", W, H, 1, 1, W - 2, H - 2, 24)
                    self._send(c, 0x84, rid, struct.pack("<I", 13) + hdr + struct.pack("<I", len(inner)) + inner)
                else:
                    self._send(c, cmd, rid)
        except OSError:
            pass


class Registry(unittest.TestCase):
    def test_registered(self):
        self.assertIn("vice", backends.names())

    def spec(self, nodes):
        return GroupSpec({"schema": "rmm-group/1", "name": "t", "nodes": nodes}, "t.json")

    def test_validation(self):
        with self.assertRaises(GroupError):
            self.spec({"a": {"backend": "vice"}})
        with self.assertRaises(GroupError):
            self.spec({"a": {"backend": "vice", "vice": {"attach": True}}})
        g = self.spec({"a": {"backend": "vice", "vice": {"image": "/x.prg"}},
                       "b": {"backend": "vice", "vice": {"attach": True, "port": 6502}}})
        self.assertEqual([n.mode for n in g.nodes], ["vice", "vice"])


class FakeVice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mon = FakeMonitor()
        self.rmm = ViceRmm(self.mon.port, snapdir=self.tmp, timeout=3)
        self.events = []
        self.rmm.on_event(lambda n, p: self.events.append(n))
        self.rmm.connect(wait=2)

    def tearDown(self):
        self.rmm.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pause_snapshot_resume(self):
        r = self.rmm
        self.assertEqual(r.call("machine.status")["state"], "running")
        with self.assertRaises(RpcError):
            r.call("snapshot.save", tag="s1")            # not paused
        r.call("machine.pause")
        self.assertEqual(r.call("machine.status")["state"], "paused")
        r.call("snapshot.save", tag="s1")
        self.assertEqual([s["tag"] for s in r.call("snapshot.list")["snapshots"]], ["s1"])
        r.call("snapshot.restore", tag="s1")
        with self.assertRaises(RpcError):
            r.call("snapshot.restore", tag="nope")
        r.call("snapshot.delete", tag="s1")
        self.assertEqual(r.call("snapshot.list")["snapshots"], [])
        r.call("machine.resume")
        self.assertEqual(r.call("machine.status")["state"], "running")
        self.assertIn("machine.stopped", self.events)
        self.assertIn("machine.resumed", self.events)

    def test_reads_keep_running(self):
        r = self.rmm
        self.assertEqual(r.call("mem.phys.read", addr=0x10, len=4)["data"], "10111213")
        f = Frame(r.call("capture.screenshot"))
        self.assertEqual((f.w, f.h, len(f.rgb)), (W - 2, H - 2, (W - 2) * (H - 2) * 3))
        self.assertFalse(self.mon.stopped)               # the poll resumed what its command stopped
        self.assertEqual(r.call("machine.status")["state"], "running")

    def test_unsupported(self):
        with self.assertRaises(RpcError) as e:
            self.rmm.call("cpu.regs.get")
        self.assertEqual(e.exception.code, -32601)


if __name__ == "__main__":
    unittest.main()
