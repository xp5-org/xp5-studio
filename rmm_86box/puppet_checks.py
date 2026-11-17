"""Snapshot and bulk-transfer checks driven through the Linux puppet (puppet_plan.md 2.4, section 7, section 5.1).

Each check is a function of a ready Puppet and returns a dict for the JSON report; a failed check raises
PuppetError. The CLI is rmm_86box.linuxpuppet (subcommands snap-busy, snap-inflight, snap-roundtrip, snap-load,
bulk). The busy load is the selftest personality's `work` mode: deterministic, so the host knows every CRC it
must print (linuxpuppet.work_crcs) whether or not a snapshot was restored in the middle.

    busy       child mid-run -> save -> let it finish (A) -> restore -> let it finish again (B): A == B == expected
    inflight   save with a command posted but not yet seen by the guest: the reply comes once per timeline,
               the same bytes in both, and the next seq follows
    roundtrip  save A -> restore A -> save B while paused: the two exported files match region by region
               (snapfile.diff names the device of every difference); the child still finishes correctly
    load       snapshot.load a busy-state file into a fresh 86Box: the child finishes with the right CRCs
    bulk       ECHO at payload-size edges and a 1 MB PUT/GET, with bytes/s (plan 5.1 baseline)
"""

import json
import os
import re
import time
import zlib

from . import snapfile
from .linuxpuppet import OP_ECHO, PAYLOAD_MAX, PuppetError, work_crcs

WORK_LINE = re.compile(r"SELFTEST work r=(\d+) crc=([0-9a-f]{8})$")
DONE_LINE = re.compile(r"SELFTEST work done rounds=(\d+) crc=([0-9a-f]{8})")


def work_argv(st, rounds, seed, ms):
    return [st, "work", str(rounds), str(seed), str(ms)]


def work_lines(text):
    return [ln for ln in text.splitlines() if WORK_LINE.match(ln)]


def check_work_output(text, exp, what):
    got, dup = {}, []
    for ln in text.splitlines():
        m = WORK_LINE.match(ln)
        if m:
            r = int(m.group(1))
            if r in got:
                dup.append(r)
            got[r] = int(m.group(2), 16)
    bad = [r for r in range(len(exp)) if got.get(r) != exp[r]]
    done = DONE_LINE.search(text)
    errs = []
    if dup:
        errs.append(f"rounds printed twice: {dup[:8]}")
    if bad:
        r = bad[0]
        g = got.get(r)
        errs.append(f"{len(bad)} rounds wrong or missing, first r={r}: guest "
                    f"{'%08x' % g if g is not None else 'missing'}, expected {exp[r]:08x}")
    if not done or int(done.group(2), 16) != exp[-1]:
        errs.append(f"no correct done line (want crc {exp[-1]:08x}): {done.group(0) if done else 'none'}")
    if errs:
        tail = "\n  ".join(text.splitlines()[-6:])
        raise PuppetError(f"{what}: " + "; ".join(errs) + f"\n  last output:\n  {tail}")
    return len(got)


def start_work(pup, st, rounds, seed, ms, min_lines=3, timeout=90.0):
    """EXEC the work child and return (pid, expected crcs, lines printed so far) once it is well under way."""
    exp = work_crcs(rounds, seed)
    pid = pup.exec(work_argv(st, rounds, seed, ms))
    out = f"/pp/run/{pid}.out"
    t0 = time.time()
    while True:
        w = pup.wait(pid, timeout=1.0, out_max=0)
        if w["state"] != "running":
            raise PuppetError(f"work child {pid} ended before the snapshot point ({w['state']} {w['code']}): "
                              f"make it longer (rounds/ms)")
        n = len(work_lines(pup.get(out).decode(errors="replace"))) if w["out_size"] else 0
        if n >= min_lines:
            if n >= rounds - 2:
                raise PuppetError(f"work child {pid} is at round {n} of {rounds}: too close to the end to save")
            return pid, exp, n
        if time.time() - t0 > timeout:
            raise PuppetError(f"work child {pid} printed {n} rounds in {timeout:.0f}s")


def finish_work(pup, pid, exp, what, timeout=300.0):
    t0 = time.time()
    while True:
        w = pup.wait(pid, timeout=5.0, out_max=0)
        if w["state"] != "running":
            break
        if time.time() - t0 > timeout:
            raise PuppetError(f"{what}: work child {pid} still running after {timeout:.0f}s")
    if (w["state"], w["code"]) != ("exited", 0):
        raise PuppetError(f"{what}: work child {pid} ended {w['state']} {w['code']}")
    text = pup.get(f"/pp/run/{pid}.out").decode(errors="replace")
    n = check_work_output(text, exp, what)
    return {"rounds": n, "run_ms": w["run_ms"], "wall_s": round(time.time() - t0, 2), "crc": f"{exp[-1]:08x}"}


def ping_ok(pup, cookie):
    r = pup.ping(cookie=cookie)
    if r["status"] or r["vals"][1] != cookie:
        raise PuppetError(f"PING {cookie:#x} after the check: {r}")
    return r["rtt_s"]


# -- checks ------------------------------------------------------------------------------------------------
def busy(pup, st, rounds=200, seed=0x5EED, ms=50, export=None, state=None, tag="pp_busy"):
    pid, exp, n0 = start_work(pup, st, rounds, seed, ms)
    res = {"pid": pid, "rounds": rounds, "seed": seed, "ms": ms, "lines_at_save": n0}
    res["save_s"] = pup.snap_save(tag)
    if export:
        res["export_s"] = pup.snap_export(tag, export)
        res["export_bytes"] = os.path.getsize(export)
        with open(state or export + ".json", "w") as f:
            json.dump({"pid": pid, "rounds": rounds, "seed": seed, "ms": ms, "st": st, "lines_at_save": n0,
                       "rsp_seq": pup.seq, "tag": tag}, f, indent=1)
    res["a"] = finish_work(pup, pid, exp, "first timeline (no restore)")
    h = pup.snap_restore(tag)
    res["restore_s"] = h["restore_s"]
    left = len(work_lines(pup.get(f"/pp/run/{pid}.out").decode(errors="replace")))
    if left < n0 or left >= rounds:
        # the output file lives in the RAM root: after the restore it must be back where it was at the save
        raise PuppetError(f"after restore the child's output has {left} rounds, at the save it had {n0}")
    res["b"] = finish_work(pup, pid, exp, "second timeline (after snapshot.restore)")
    pup.rm(f"/pp/run/{pid}.out")
    res["ping_ms"] = ping_ok(pup, 0xB05E) * 1e3
    return res


def inflight(pup, size=4096, tag="pp_inflight"):
    data = os.urandom(size)
    want = zlib.crc32(data) & 0xFFFFFFFF
    posted = {}

    def save(seq):
        posted["seq"] = seq
        t0 = time.time()
        pup.rpc.call("snapshot.save", timeout=180.0, tag=tag)
        posted["save_s"] = time.time() - t0

    r1 = pup.call(OP_ECHO, payload=data, in_flight=save)
    seq = posted["seq"]
    if r1["status"] or r1["vals"][0] != want or r1["data"] != data:
        raise PuppetError(f"ECHO seq {seq} in the first timeline: {r1['status']} crc {r1['vals'][0]:08x}")
    h = pup.snap_restore(tag, resume=False)
    if (h["cmd_seq"], h["rsp_seq"]) != (seq, (seq - 1) & 0xFFFFFFFF):
        raise PuppetError(f"the snapshot does not hold seq {seq} in flight: cmd.seq {h['cmd_seq']} "
                          f"rsp.seq {h['rsp_seq']}")
    pup.rpc.call("machine.resume")
    r2 = pup.collect(seq)
    if r2["status"] or r2["vals"] != r1["vals"] or r2["data"] != data:
        raise PuppetError(f"ECHO seq {seq} after the restore differs: status {r2['status']} vals {r2['vals']} "
                          f"vs {r1['vals']}, data {'same' if r2['data'] == data else 'differs'}")
    time.sleep(1.0)
    h2 = pup.header()
    if h2["rsp_seq"] != seq or h2["cmd_seq"] != seq:
        raise PuppetError(f"after the replayed reply: cmd.seq {h2['cmd_seq']} rsp.seq {h2['rsp_seq']}, want {seq}")
    return {"seq": seq, "size": size, "save_s": posted["save_s"], "restore_s": h["restore_s"],
            "rtt_first_ms": r1["rtt_s"] * 1e3, "rtt_replay_ms": r2["rtt_s"] * 1e3,
            "ping_ms": ping_ok(pup, 0x1F1F) * 1e3}


# A vs B differences that a restore makes on purpose. (label, offset in that region, length, why). The offset is
# where the card's svga_t field lands in the card's struct, so each card has its own row; a card missing here
# fails loudly with its offset, which is how a new row gets added.
RESTORE_SETS = [
    ("vga", 0x124, 1, "svga.fullchange = changeframecount: svga_rmm_snap_vram asks for a full redraw"),
    ("ati_rage128_pro", 0x11C, 1, "svga.fullchange = changeframecount: svga_rmm_snap_vram asks for a full redraw"),
]


def _unexplained(d):
    left = []
    for r in d["regions"]:
        runs = [x for x in r["runs"] if not any(r["label"] == lab and x[0] == off and x[1] == n
                                                 for lab, off, n, _ in RESTORE_SETS)]
        if runs:
            left.append(dict(r, runs=runs))
    return left


def roundtrip(pup, st, out_dir, rounds=200, seed=0xC0FFEE, ms=50):
    """save A -> restore A -> save B -> restore B -> save C, all in one pause. B == C exactly (restoring a
    restored state changes nothing); A vs B may differ only by pointers to re-allocated objects (snapfile
    ptr_equiv) and the RESTORE_SETS fields."""
    pid, exp, n0 = start_work(pup, st, rounds, seed, ms)
    f = {k: os.path.join(out_dir, f"rt_{k}.snap") for k in "abc"}
    res = {"pid": pid, "lines_at_save": n0}
    t0 = time.time()
    with pup._hold():                                  # one pause: nothing runs between the saves
        for k in "abc":
            if k != "a":
                pup.rpc.call("snapshot.restore", timeout=180.0, tag="pp_rt_" + prev)
            pup.rpc.call("snapshot.save", timeout=180.0, tag="pp_rt_" + k)
            pup.rpc.call("snapshot.export", timeout=180.0, tag="pp_rt_" + k, path=f[k])
            prev = k
    res["paused_s"] = round(time.time() - t0, 2)
    sa, sb, sc = (snapfile.SnapFile(f[k]) for k in "abc")
    ab, bc = snapfile.diff(sa, sb, max_runs=1 << 20), snapfile.diff(sb, sc)
    for name, d in (("ab", ab), ("bc", bc)):
        with open(os.path.join(out_dir, f"rt_diff_{name}.json"), "w") as fh:
            json.dump(d, fh, indent=1)
    left = _unexplained(ab)
    res["ab"] = snapfile.summarize(ab)
    res["ab_ptr_words"] = ab.get("ptr_words", 0)
    res["ab_allowed"] = [f"{r['label']}+0x{x[0]:x}" for r in ab["regions"] for x in r["runs"]
                         if any(r["label"] == lab and x[0] == off for lab, off, _, _ in RESTORE_SETS)]
    res["bc"] = snapfile.summarize(bc)
    res["bc_same_raw"] = snapfile.diff(sb, sc, ptr_equiv=False)["same"]
    res["by_label"] = sa.by_label()
    res["work"] = finish_work(pup, pid, exp, "after save/restore/save/restore/save")
    pup.rm(f"/pp/run/{pid}.out")
    for k in "abc":
        pup.rpc.call("snapshot.delete", tag="pp_rt_" + k)
    errs = []
    if not bc["same"]:
        errs.append("restore is not idempotent, B vs C:\n    " + "\n    ".join(res["bc"]))
    if left or not ab["layout_same"] or not ab["ident_same"]:
        errs.append("A vs B differs beyond re-allocated pointers and RESTORE_SETS:\n    " +
                    "\n    ".join(snapfile.summarize(dict(ab, regions=left))))
    if errs:
        raise PuppetError("save/restore/save: " + "\n  ".join(errs))
    return res


def load(pup, path, state=None):
    with open(state or path + ".json") as f:
        st = json.load(f)
    h = pup.snap_load(path)
    exp = work_crcs(st["rounds"], st["seed"])
    res = {"load_s": h["load_s"], "gen": h["gen"], "rsp_seq": h["rsp_seq"], "saved_rsp_seq": st["rsp_seq"]}
    res["work"] = finish_work(pup, st["pid"], exp, "fresh process after snapshot.load")
    pup.rm(f"/pp/run/{st['pid']}.out")
    res["ping_ms"] = ping_ok(pup, 0x10AD) * 1e3
    return res


def bulk(pup, big=1 << 20):
    sizes = [1, 7, 4095, 4096, 4097, 16384, 32768, PAYLOAD_MAX]
    res = {"echo": []}
    for n in sizes:
        rtts = []
        for _ in range(3):
            data = os.urandom(n)
            r = pup.echo(data)
            rtts.append(r["rtt_s"])
        best = min(rtts)
        res["echo"].append({"size": n, "best_ms": round(best * 1e3, 1), "kBps": round(2 * n / 1024 / best, 1)})
    data = os.urandom(big)
    p = pup.put("/pp/tmp/bulk.bin", data)
    t0 = time.time()
    back = pup.get("/pp/tmp/bulk.bin")
    g = time.time() - t0
    pup.rm("/pp/tmp/bulk.bin")
    if back != data:
        raise PuppetError(f"1 MB GET differs from the PUT ({len(back)} bytes back)")
    res["file"] = {"size": big, "put_kBps": round(big / 1024 / p["s"], 1), "get_kBps": round(big / 1024 / g, 1)}
    res["stats"] = dict(pup.stats)
    return res
