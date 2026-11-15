# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""Lock / unlock on two real 86Box instances (groups/pair_launch.json), no window.

    lockstep_real.py --floppy MEMLOOP.IMG [--binary 86Box] [--vmroot DIR] [--group FILE]

HostGroup launches both 86Box (the group's own launch blocks), each boots DOS off A: and runs
MEMLOOP.COM from B:. A marker byte at 0000:04FC (the free end of the BIOS intra-application
area; MEMLOOP uses 04F0-04F3) is written in each phase, so which state a node is in can be read
back exactly (MEMLOOP's own counter wraps in milliseconds; it only shows the loop runs):

    a=A1 b=B1  group snap S1 (both paused)
    a=A2 b=B2  unlock b; group snap S2 holds only a
               b alone back to S1: b reads B1, a still A2 and running
    b=B3       b snapped alone (btry1, parent S1); lock b; group snap S3 holds a and b,
               b's parent is btry1: S3 is a=A2, b=B3 (b continued from S1, a did not)
    b=B4       unlock b, b snapped alone (btry2)
               rollback to S2 is refused (b is not in it) and changes nothing
               rollback to S1: a=A1 b=B1, btry2 deleted from b's 86Box, btry1 kept
               (from an earlier unlock), every node locked, MEMLOOP runs on both

Exit 0 when every check passed. Unattended: __testlist__rmm_demo_lockstep.py runs it.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, "..")):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui.group import GroupSpec  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402
from rmmgui.snaps import SnapError  # noqa: E402

MARK = 0x4FC
ICA = 0x4F0                     # MEMLOOP's word counter; its CS at 0x4F2
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.2f}] {msg}", flush=True)


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)
    log(f"ok    {msg}")


def wait(cond, secs, what):
    deadline = time.time() + secs
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.25)
    raise Fail(f"timed out after {secs}s: {what}")


def mark(n, val=None):
    """Read (or write, then read) the marker byte; leaves the node as it found it."""
    with n.rmm.paused():
        if val is not None:
            n.rmm.call("mem.phys.write", addr=MARK, data=f"{val:02x}")
        return int(n.rmm.call("mem.phys.read", addr=MARK, len=1)["data"], 16)


def counter(n):
    with n.rmm.paused():
        return int.from_bytes(bytes.fromhex(n.rmm.call("mem.phys.read", addr=ICA, len=4)["data"]), "little")


def loop_runs(n):
    """MEMLOOP is running: CS is published and the counter moves while the node runs."""
    c1 = counter(n)
    check(c1 >> 16 != 0, f"{n.name}: MEMLOOP's CS is published")
    was = n.rmm.state()
    if was != "running":
        n.resume()
    time.sleep(0.4)
    c2 = counter(n)
    if was != "running":
        n.pause()
    check((c1 & 0xFFFF) != (c2 & 0xFFFF), f"{n.name}: MEMLOOP counter moves ({c1 & 0xFFFF:#06x} -> {c2 & 0xFFFF:#06x})")


def screen(n):
    return "\n".join(n.rmm.text_screen())


def type_line(n, text):
    n.rmm.call("input.text.type", text=text)
    time.sleep(0.3)
    n.rmm.call("input.key.send", combo="enter")


def boot_memloop(n, floppy):
    wait(lambda: "A:\\>" in screen(n), 120, f"{n.name}: DOS prompt A:\\>")
    log(f"{n.name}: DOS is up")
    n.rmm.call("fdd.1.insert", path=floppy, wp=True)
    type_line(n, "b:")
    wait(lambda: "B:\\>" in screen(n), 30, f"{n.name}: B:\\> prompt")
    type_line(n, "memloop")
    wait(lambda: "RMMLOOP" in screen(n), 30, f"{n.name}: MEMLOOP banner")
    loop_runs(n)


def tags_on(n):
    return sorted(s["tag"] for s in n.rmm.call("snapshot.list")["snapshots"])


def run(g, floppy):
    a, b = g.nodes["a"], g.nodes["b"]
    wait(lambda: all(n.connected and n.status for n in (a, b)), 90, "both 86Box connected")
    for n in (a, b):
        boot_memloop(n, floppy)

    g.halt([a, b])
    mark(a, 0xA1)
    mark(b, 0xB1)
    s1, failed = g.snap(None, "S1")
    check(s1.nodes == ["a", "b"] and not failed, "group snap S1 on a and b")
    g.resume([a, b])
    mark(a, 0xA2)
    mark(b, 0xB2)

    g.unlock("b")
    s2, _ = g.snap(None, "S2")
    check(s2.nodes == ["a"], f"b unlocked: group snap S2 holds only a ({s2.nodes})")

    _, done, _ = g.restore("S1", [b], run=False)
    check(done == ["b"], "b alone restored to S1")
    check(mark(b) == 0xB1, "b reads its S1 marker")
    check(mark(a) == 0xA2 and a.rmm.state() == "running", "a untouched: A2 and still running")
    b.resume()
    loop_runs(b)
    mark(b, 0xB3)
    btry1, _ = g.snap([b], "btry1")
    check(btry1.scope == "nodes" and btry1.parents == {"b": s1.id}, f"btry1 on b's own track, parent S1 ({btry1.parents})")

    g.relock("b")
    s3, _ = g.snap(None, "S3")
    check(s3.nodes == ["a", "b"], "b locked again: group snap S3 holds a and b")
    check(s3.parents == {"a": s2.id, "b": btry1.id}, f"S3 parents: a from S2, b from btry1 ({s3.parents})")
    g.restore("S3", run=False)
    check((mark(a), mark(b)) == (0xA2, 0xB3), "S3 is a=A2, b=B3: b continued from S1, a did not")
    g.resume()

    g.unlock("b")
    mark(b, 0xB4)
    g.snap([b], "btry2")
    before = (mark(a), mark(b))
    try:
        g.rollback_group("S2")
        raise Fail("rollback to S2 (not on b) was not refused")
    except SnapError as e:
        log(f"refused as it should be: {e}")
    check((mark(a), mark(b)) == before and g.unlocked() == ["b"], "the refused rollback changed nothing")

    rec, done, failed, dropped = g.rollback_group("S1")
    check(sorted(done) == ["a", "b"] and not failed, "rollback to S1 restored a and b")
    check(dropped == {"b": ["btry2"]}, f"rollback deleted b's btry2 only ({dropped})")
    check((mark(a), mark(b)) == (0xA1, 0xB1), "after rollback a=A1, b=B1")
    tags = tags_on(b)
    check("btry2" not in tags and "btry1" in tags and "S1" in tags, f"b's 86Box lists {tags}")
    check(g.unlocked() == [], "every node is locked again")
    for n in (a, b):
        loop_runs(n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default=os.path.join(DEMO, "groups", "pair_launch.json"))
    ap.add_argument("--floppy", required=True, help="a 1.44M image with MEMLOOP.COM")
    ap.add_argument("--binary", help="86Box binary (default: the VM config's box_binary)")
    ap.add_argument("--vmroot", default=None, help="VM dirs go under it (default: a temp dir)")
    a = ap.parse_args(argv)
    if not os.environ.get("DISPLAY"):
        try:
            d = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, '/testsrc/pyhelpers'); "
                                "import displayhelpers; print(displayhelpers.resolve_display())"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
            if d:
                os.environ["DISPLAY"] = d
        except (OSError, subprocess.SubprocessError):
            pass
    log(f"DISPLAY={os.environ.get('DISPLAY')}")
    raw = json.load(open(a.group))
    raw.setdefault("vars", {})["vmroot"] = a.vmroot or tempfile.mkdtemp(prefix="rmm_lockstep_")
    raw["policy"] = {"halt_all": False}
    if a.binary:
        for t in raw.get("templates", {}).values():
            if "launch" in t:
                t["launch"]["binary"] = a.binary
    spec = GroupSpec(raw, a.group)
    g = HostGroup(spec, tempfile.mkdtemp(prefix="rmm_store_"),
                  lambda t, tone, n: log(f"  {'[' + n.name + '] ' if n else ''}{t}") if tone in ("ok", "warn", "err") else None)
    rc = 1
    try:
        g.start()
        run(g, os.path.abspath(a.floppy))
        log("PASS: lock / unlock / rollback on real 86Box")
        rc = 0
    except Fail as e:
        log(f"FAIL: {e}")
    except Exception as e:  # noqa: BLE001 - report, then still quit both 86Box
        log(f"FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for n in g.nodes.values():
            log(f"{n.name}: {n.state}  port {n.port}  {n.conn_msg}")
            if n.proc:
                for ln in n.proc.log_tail(6):
                    log(f"  {n.name} log: {ln}")
        g.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
