# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""count_conns.py PATTERN MIN [WAIT]: wait until the process whose command line contains PATTERN
holds at least MIN established TCP connections (read from /proc, no ss/lsof needed).
Exit 0 on success, 1 if the process is gone or WAIT seconds (default 60) pass first."""
import os
import subprocess
import sys
import time


def pid_of(pattern):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) != os.getpid()]


def established(pid):
    inodes = set()
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if tgt.startswith("socket:["):
            inodes.add(tgt[8:-1])
    n = 0
    for fam in ("tcp", "tcp6"):
        with open(f"/proc/net/{fam}") as f:
            next(f)
            for ln in f:
                c = ln.split()
                if c[3] == "01" and c[9] in inodes:
                    n += 1
    return n


def main():
    pattern, need = sys.argv[1], int(sys.argv[2])
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 60
    end = time.time() + wait
    n = 0
    while time.time() < end:
        pids = pid_of(pattern)
        if not pids:
            print(f"no process matches {pattern!r}")
            return 1
        n = established(pids[0])
        if n >= need:
            print(f"pid {pids[0]} holds {n} established connections")
            return 0
        time.sleep(1)
    print(f"pid {pids[0]} holds only {n} of {need} connections")
    return 1


sys.exit(main())
