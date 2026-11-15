# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""Group snap / rollback across five VMs: two 86Box DOS nodes and three QEMU guests.

    lockstep_five.py --floppy MEMLOOP.IMG [--binary 86Box] [--vmroot DIR] [--group FILE]

groups/five_lockstep.json: a and b are 86Box the group launches itself (as lockstep_real.py);
w31, sol and m68k are QEMU guests this script does NOT start. The testlist (rmm-demo-lockstep5)
boots them and leaves them at their desktop / prompt, and the nodes attach by QMP port.

    S1  every node paused, one group snap: a=A1 b=B1, each QEMU's paused screen hashed
        run on: a=A2 b=B2, QEMUs run (the m68k cube animates, so its screen must have moved)
    S2  rollback S1: a=A1 b=B1, and each QEMU's paused screen is byte-identical to S1's
    unlock sol, snap sol alone (soltry), rollback S1 deletes soltry on sol and locks it again

A QEMU guest has no byte to poke from here (xp only reads), so its rollback is proven by the
screen: paused QEMU screendumps are deterministic, and loadvm restores the display with the RAM.
Exit 0 when every check passed.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lockstep_real as L  # noqa: E402  (also puts rmm_demo and rmm_lib on sys.path)
from lockstep_real import Fail, check, log, mark, wait  # noqa: E402
from rmmgui.group import GroupSpec  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402
from rmmgui.snaps import SnapError  # noqa: E402

QEMU = ("w31", "sol", "m68k")
ANIMATED = ("m68k",)            # CubeRotate spins: its screen differs between two instants


def shot(n):
    return hashlib.md5(n.rmm.screen_bytes()).hexdigest()


def run(g, floppy):
    a, b = g.nodes["a"], g.nodes["b"]
    q = {k: g.nodes[k] for k in QEMU}
    wait(lambda: all(n.connected and n.status for n in g.nodes.values()), 90, "all five nodes connected")
    for k, n in q.items():
        check(n.rmm.state() == "running", f"{k}: QEMU attached and running ({n.conn_msg})")
    for n in (a, b):
        L.boot_memloop(n, floppy)

    g.halt(list(g.nodes.values()))
    mark(a, 0xA1)
    mark(b, 0xB1)
    h1 = {k: shot(n) for k, n in q.items()}
    s1, failed = g.snap(None, "S1")
    check(s1.nodes == list(g.nodes) and not failed, f"group snap S1 on all five ({s1.nodes}, failed {failed})")
    for k, n in q.items():
        check("S1" in n.rmm.snapshot_tags(), f"{k}: QEMU lists S1 (savevm)")
    g.resume()
    mark(a, 0xA2)
    mark(b, 0xB2)
    time.sleep(2.0)
    g.halt(list(g.nodes.values()))
    h2 = {k: shot(n) for k, n in q.items()}
    for k in ANIMATED:
        check(h2[k] != h1[k], f"{k}: screen moved while running after S1")
    g.resume()

    rec, done, failed = g.restore("S1", run=False)
    check(sorted(done) == sorted(g.nodes) and not failed, f"restore S1 reached all five ({done}, failed {failed})")
    check((mark(a), mark(b)) == (0xA1, 0xB1), "after rollback a=A1, b=B1")
    for k, n in q.items():
        check(n.rmm.state() == "paused", f"{k}: paused after loadvm")
        check(shot(n) == h1[k], f"{k}: screen is S1's again")
    g.resume()
    for n in (a, b):
        L.loop_runs(n)

    g.unlock("sol")
    soltry, _ = g.snap([g.nodes["sol"]], "soltry")
    check(soltry.scope == "nodes" and "soltry" in q["sol"].rmm.snapshot_tags(), "sol alone snapped (soltry)")
    s2, _ = g.snap(None, "S2")
    check("sol" not in s2.nodes and len(s2.nodes) == 4, f"S2 skips unlocked sol ({s2.nodes})")
    rec, done, failed, dropped = g.rollback_group("S1")
    check(sorted(done) == sorted(g.nodes) and not failed, "rollback S1 restored all five")
    check(dropped == {"sol": ["soltry"]}, f"rollback deleted sol's soltry only ({dropped})")
    check("soltry" not in q["sol"].rmm.snapshot_tags() and g.unlocked() == [], "sol: soltry gone, every node locked")
    for k, n in q.items():
        check(shot(n) == h1[k], f"{k}: screen is S1's after the second rollback")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default=os.path.join(L.DEMO, "groups", "five_lockstep.json"))
    ap.add_argument("--floppy", required=True, help="a 1.44M image with MEMLOOP.COM")
    ap.add_argument("--binary", help="86Box binary (default: the VM config's box_binary)")
    ap.add_argument("--vmroot", default=None)
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
    raw = json.load(open(a.group))
    raw.setdefault("vars", {})["vmroot"] = a.vmroot or tempfile.mkdtemp(prefix="rmm_lockstep5_")
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
        log("PASS: group snap / rollback across 2x 86Box + 3x QEMU")
        rc = 0
    except (Fail, SnapError) as e:
        log(f"FAIL: {e}")
    except Exception as e:  # noqa: BLE001 - report, then still quit the 86Box
        log(f"FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for n in g.nodes.values():
            log(f"{n.name}: {n.state}  port {n.port}  {n.conn_msg}")
            if n.proc:
                for ln in n.proc.log_tail(6):
                    log(f"  {n.name} log: {ln}")
        g.stop()                # QEMU nodes only drop their QMP link; test_terminate_all ends the guests
    return rc


if __name__ == "__main__":
    sys.exit(main())
