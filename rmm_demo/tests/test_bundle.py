# SPDX-License-Identifier: MIT
"""Project bundles (rmmgui/bundle.py): snapshot selection, and a real QEMU round trip (export the
guest's qcow2 chain with its internal snapshots, import onto new paths, loadvm there).

    python3 -m unittest tests.test_bundle      (from rmm_demo/; the QEMU test needs qemu-img and qemu-system-i386)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, "..")):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui import bundle  # noqa: E402
from rmmgui.group import GroupSpec  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402

TREE = {"heads": {"a": 3, "b": 3}, "snapshots": [
    {"id": 1, "tag": "S1", "nodes": ["a", "b"], "parents": {"a": None, "b": None}, "scope": "group",
     "status": {"a": "ok", "b": "ok"}},
    {"id": 2, "tag": "S2", "nodes": ["a", "b"], "parents": {"a": 1, "b": 1}, "scope": "group",
     "status": {"a": "gone", "b": "ok"}},
    {"id": 3, "tag": "S3", "nodes": ["a", "b"], "parents": {"a": 2, "b": 2}, "scope": "group",
     "status": {"a": "ok", "b": "replaced"}}]}


class Select(unittest.TestCase):
    def test_full_keeps_every_live_record(self):
        t, keep = bundle.select(TREE, "full")
        self.assertEqual(keep, {1: ["a", "b"], 2: ["b"], 3: ["a"]})
        self.assertEqual([r["id"] for r in t["snapshots"]], [1, 2, 3])
        self.assertEqual(t["snapshots"][2]["parents"], {"a": 2, "b": 2})

    def test_latest_is_one_record_started_from_nothing(self):
        t, keep = bundle.select(TREE, "latest")
        self.assertEqual(keep, {3: ["a"]})
        self.assertEqual(t["heads"], {"a": 3, "b": 3})
        self.assertEqual(t["snapshots"][0]["parents"], {"a": None, "b": None})

    def test_nothing_live_or_bad_mode(self):
        dead = {"heads": {}, "snapshots": [dict(TREE["snapshots"][0], status={"a": "gone", "b": "gone"})]}
        with self.assertRaises(bundle.BundleError):
            bundle.select(dead, "full")
        with self.assertRaises(bundle.BundleError):
            bundle.select(TREE, "some")

    def test_rewrite_argv_longest_path_first(self):
        m = {"/d/a.qcow2": "/n/a", "/d/a.qcow2.bak": "/n/b"}
        self.assertEqual(bundle.rewrite_argv(["-drive", "file=/d/a.qcow2.bak,if=none"], m),
                         ["-drive", "file=/n/b,if=none"])


@unittest.skipUnless(shutil.which("qemu-img") and shutil.which("qemu-system-i386"), "needs QEMU")
class QemuRoundTrip(unittest.TestCase):
    def test_export_latest_then_import(self):
        from rmmgui import qemunode
        tmp = tempfile.mkdtemp(prefix="bundle_t_")
        self.addCleanup(shutil.rmtree, tmp, True)
        base, over = f"{tmp}/base.qcow2", f"{tmp}/over.qcow2"
        subprocess.check_call(["qemu-img", "create", "-q", "-f", "qcow2", base, "8M"])
        subprocess.check_call(["qemu-img", "create", "-q", "-f", "qcow2", "-b", base, "-F", "qcow2", over])
        port = 57611
        argv = ["qemu-system-i386", "-display", "none", "-nodefaults", "-S", "-m", "16",
                "-drive", f"file={over},format=qcow2,if=none,id=d0", "-device", "ide-hd,drive=d0",
                "-qmp", f"tcp:127.0.0.1:{port},server=on,wait=off"]
        proc = subprocess.Popen(argv)
        self.addCleanup(proc.kill)
        spec = GroupSpec({"schema": "rmm-group/1", "name": "q", "nodes": {"w": {"backend": "qemu", "port": port}}})
        lines = []
        g = HostGroup(spec, tmp, lambda t, tone, n: lines.append(t))
        self.addCleanup(g.stop)
        g.start()
        deadline = time.time() + 20
        while time.time() < deadline and not g.nodes["w"].connected:
            time.sleep(0.2)
        self.assertTrue(g.nodes["w"].connected, lines)
        g.snap(None, "S1")
        g.snap(None, "S2")
        app = types.SimpleNamespace(group=g, project_data=lambda: {"group": spec.raw, "tree": g.tree.to_json(),
                                                                    "files": {}, "lock": g.lock_state()})
        out = f"{tmp}/p.zip"
        m = bundle.export_bundle(app, out, "latest", lambda t, tone="text": lines.append(t))
        info = m["nodes"]["w"]
        self.assertEqual(info["tags"], ["S2"])
        self.assertEqual([f["format"] for f in info["files"]], ["qcow2", "qcow2"])
        self.assertEqual(info["argv"][0], "qemu-system-i386")
        # the live guest still holds both snapshots; the bundle's copy only S2
        self.assertEqual(set(g.nodes["w"].rmm.snapshot_tags()), {"S1", "S2"})
        dest = f"{tmp}/unpacked"
        with zipfile.ZipFile(out) as zf:
            zf.extractall(dest)
        copy = os.path.join(dest, info["files"][0]["arc"])
        tags = bundle.snap_tags(copy)
        self.assertEqual(tags, ["S2"])
        with open(f"{dest}/manifest.json") as f:
            self.assertEqual(json.load(f)["mode"], "latest")
        # import onto new paths with the original guest gone: overlay rebased, QEMU started by the node
        g.stop()
        proc.kill()
        proc.wait()
        for f in (base, over):
            os.unlink(f)
        got = {}
        app2 = types.SimpleNamespace(
            say=lambda t, tone="text": lines.append(t),
            group=types.SimpleNamespace(nodes={}, _fanout=lambda *a: None),
            replace_group=lambda sp, tree, files, lock: got.update(spec=sp, tree=tree))
        bundle.import_bundle(app2, out, f"{tmp}/imp", lambda t, tone="text": lines.append(t))
        deadline = time.time() + 10
        while time.time() < deadline and "spec" not in got:
            time.sleep(0.1)
        self.assertIn("spec", got, lines)
        argv = got["spec"].node("w").raw["qemu_argv"]
        self.assertTrue(all(over not in a for a in argv))
        g2 = HostGroup(got["spec"], tmp, lambda t, tone, n: lines.append(t))
        self.addCleanup(g2.stop)
        g2.start()
        deadline = time.time() + 20
        while time.time() < deadline and not g2.nodes["w"].connected:
            time.sleep(0.2)
        self.assertTrue(g2.nodes["w"].connected, lines)
        self.assertEqual(g2.nodes["w"].rmm.snapshot_tags(), ["S2"])
        self.assertFalse(qemunode.hmp_failed(g2.nodes["w"].rmm.hmp("loadvm S2")))


if __name__ == "__main__":
    unittest.main()


def _snapfile(data, disk_blocks, tag="T"):
    """A minimal RMMSNAP1 file: header, IDNT, DISK (one disk, 4K blocks), DATA, END."""
    import struct
    bs = 4096
    hdr = struct.pack("<8sIIQ64s", b"RMMSNAP1", 1, 88, 7, tag.encode())
    disk = struct.pack("<I", 1) + struct.pack("<IIQQII", 0, len(disk_blocks), 1000, 0xABCD, 1, bs)
    for b, fill in disk_blocks:
        disk += struct.pack("<I", b) + bytes([fill]) * bs
    out = hdr
    for sid, body in ((b"IDNT", b"machine x\n"), (b"DISK", disk), (b"DATA", data), (b"END ", b"")):
        out += struct.pack("<4sIQ", sid, 0, len(body)) + body
    return out


class SnapDelta(unittest.TestCase):
    def test_roundtrip_and_size(self):
        from rmmgui import snapdelta
        ram = bytearray(os.urandom(4096) * 256)                       # 1 MB, compressible and non-zero
        parent = _snapfile(bytes(ram), [(1, 1), (2, 2)], "P")
        ram[5000:5010] = b"x" * 10                                     # one page changes
        ram += b"tail"                                                 # and RAM grows (odd length)
        child = _snapfile(bytes(ram), [(1, 1), (2, 9), (7, 3)], "C")   # block 2 rewritten, block 7 new
        d = snapdelta.encode(parent, child)
        self.assertLess(len(d), 3 * 4096 + 600)
        self.assertEqual(snapdelta.decode(parent, d), child)
        self.assertEqual(snapdelta.decode(parent, snapdelta.encode(parent, parent)), parent)

    def test_wrong_parent_or_junk(self):
        from rmmgui import snapdelta
        a = _snapfile(b"A" * 8192, [(1, 1)])
        b = _snapfile(b"B" * 8192, [(5, 5)])
        d = snapdelta.encode(a, _snapfile(b"A" * 8192, [(1, 1), (2, 2)]))
        with self.assertRaises(snapdelta.DeltaError):
            snapdelta.decode(b, d)                                     # block 1 is not in b's disk
        with self.assertRaises(snapdelta.DeltaError):
            snapdelta.encode(b"junk", a)
        # no DISK/DATA in the parent: sections are kept whole and still round-trip
        import struct
        bare = _snapfile(b"", [])
        self.assertEqual(snapdelta.decode(bare, snapdelta.encode(bare, a)), a)

    def test_real_files_from_a_bundle(self):
        from rmmgui import snapdelta
        z = os.path.expanduser("~/Desktop/five_lockstep.rmmbundle.zip")
        if not os.path.exists(z):
            self.skipTest("no sample bundle")
        with zipfile.ZipFile(z) as zf:
            raw = zf.read("snaps/a/bundle_1_s1.rmmsnap")
            other = zf.read("snaps/b/bundle_1_s1.rmmsnap")
        for p, c in ((raw, raw), (raw, other)):
            try:
                d = snapdelta.encode(p, c)
            except snapdelta.DeltaError:
                continue
            self.assertEqual(snapdelta.decode(p, d), c)
        self.assertLess(len(snapdelta.encode(raw, raw)), 2000)


class UnpackDeltas(unittest.TestCase):
    def test_chain_rebuilds_in_order(self):
        from rmmgui import snapdelta
        ram = bytearray(os.urandom(4096) * 8)
        f1 = _snapfile(bytes(ram), [(1, 1)], "S1")
        ram[0:4] = b"abcd"
        f2 = _snapfile(bytes(ram), [(1, 1), (2, 2)], "S2")
        ram[9000:9004] = b"wxyz"
        f3 = _snapfile(bytes(ram), [(1, 1), (2, 5), (3, 3)], "S3")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/snaps/a")
            for name, data in (("1.rmmsnap", f1), ("2.rmmdelta", snapdelta.encode(f1, f2)),
                               ("3.rmmdelta", snapdelta.encode(f2, f3))):
                with open(f"{d}/snaps/a/{name}", "wb") as f:
                    f.write(data)
            info = {"snaps": {"1": "snaps/a/1.rmmsnap", "2": "snaps/a/2.rmmsnap", "3": "snaps/a/3.rmmsnap"},
                    "packed": {"3": {"arc": "snaps/a/3.rmmdelta", "parent": 2},
                              "2": {"arc": "snaps/a/2.rmmdelta", "parent": 1}}}
            bundle._unpack_deltas(d, info, lambda *a: None)
            self.assertEqual(open(f"{d}/snaps/a/2.rmmsnap", "rb").read(), f2)
            self.assertEqual(open(f"{d}/snaps/a/3.rmmsnap", "rb").read(), f3)
            self.assertFalse(os.path.exists(f"{d}/snaps/a/3.rmmdelta"))
