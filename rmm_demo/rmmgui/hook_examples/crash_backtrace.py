# SPDX-License-Identifier: MIT
"""A symbolic backtrace at a stop: put a breakpoint on the path a crash takes (the program's
error / abort routine, its INT 0 or exception handler, the OS fault code) and attach this.

    sym load game.json ; sym find
    sym break __fatal_runtime_error_   -> break #1      (or debug.break.set addr=...)
    hook b1 crash_backtrace.py [snap=crash] [log=crash.txt] [depth=16] [scan=64] [far=1] [resume=1]

Each stop prints where it is, the registers, then two views of the stack:
  frames   the (E)BP chain: [bp] = caller's bp, [bp+2] (or +4) = return address
  scan     every stack word that points just after a CALL into a known function, which also finds
           callers in code built without frame pointers (the classic "dps" heuristic)
Real mode reads the stack at SS*16; protected mode assumes a flat SS (base 0), as DOS4G and Win32 do.
  snap=TAG    save a snapshot TAG<hit> first, so the state can be restored and looked at again
  log=FILE    append the report
  far=1       16-bit far calls (large / medium model): frames hold a CS too
  resume=1    keep running after the report (default: stay paused at the crash)
"""

import time

import rmm_86box  # noqa: F401  (hit.rmm is an rmm_86box.Rmm)


def _u(b, i, n):
    return int.from_bytes(b[i:i + n], "little")


def _after_call(hit, a, wide):
    """True when the bytes just before a look like a CALL that returns to a."""
    try:
        b = hit.read(a - 7, 7, linear=True)
    except Exception:  # noqa: BLE001 - unmapped
        return False
    if (b[-5] == 0xE8 and wide) or (b[-3] == 0xE8 and not wide) or b[-5] == 0x9A or b[-7] == 0x9A:
        return True
    for k in (2, 3, 4, 6):                                  # FF /2 (near indirect), FF /3 (far indirect)
        if b[-k] == 0xFF and (b[-k + 1] >> 3) & 7 in (2, 3):
            return True
    return False


def on_hit(hit):
    a = hit.args
    if a.get("snap"):
        hit.snap(f"{a['snap']}{hit.count}")
    r = hit.regs()
    pm = bool(r.get("cr0", 0) & 1)
    wide = pm                                               # 32-bit stack words in protected mode
    pc = hit.pc()
    lines = [f"--- {hit.name} stopped in {hit.sym(pc)}  (file {hit.file_addr(pc) or '?'})  "
             f"cs:eip={r['cs']:04X}:{r['eip']:08X}  {'protected' if pm else 'real'} mode",
             "    " + " ".join(f"{k}={r[k]:08X}" for k in ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp")),
             f"    ds={r['ds']:04X} es={r['es']:04X} ss={r['ss']:04X} eflags={r['eflags']:08X}"]
    ss_base = 0 if pm else r["ss"] * 16
    cs_base = r.get("cs_base", r["cs"] * 16)
    w = 4 if wide else 2
    far = bool(a.get("far")) and not pm

    lines.append("  frames (bp chain):")
    bp = r["ebp"] if wide else r["ebp"] & 0xFFFF
    seen = set()
    for i in range(int(a.get("depth", 16))):
        if not bp or bp in seen:
            break
        seen.add(bp)
        try:
            fr = hit.read(ss_base + bp, w * 3, linear=True)
        except Exception:  # noqa: BLE001
            break
        nbp, ret = _u(fr, 0, w), _u(fr, w, w)
        ret_lin = (_u(fr, 2 * w, 2) * 16 + ret) if far else (ret if wide and pm else cs_base + ret)
        lines.append(f"    #{i}  {hit.sym(ret_lin):<32} file {hit.file_addr(ret_lin) or '?':<14} bp={bp:X}")
        if nbp <= bp:
            break
        bp = nbp

    lines.append("  scan (stack words returning just after a CALL):")
    sp = r["esp"] if wide else r["esp"] & 0xFFFF
    n = int(a.get("scan", 64))
    try:
        stk = hit.read(ss_base + sp, n * w + 2, linear=True)
    except Exception:  # noqa: BLE001
        stk = b""
    found = 0
    for i in range(0, max(0, len(stk) - w - 1), w):
        v = _u(stk, i, w)
        cands = [v] if pm else [cs_base + v, _u(stk, i + 2, 2) * 16 + v]
        for c in cands:
            if c and hit.symbols.at(c) and _after_call(hit, c, wide):
                lines.append(f"    sp+{i:<4X} {hit.sym(c):<32} file {hit.file_addr(c) or '?'}")
                found += 1
                break
    if not found:
        lines.append("    (none" + ("" if hit.symbols.tables else ": no symbols loaded, sym load FILE") + ")")

    for ln in lines:
        hit.say(ln)
    if a.get("log"):
        with open(a["log"], "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "\n".join(lines) + "\n\n")
    hit.state["reports"] = hit.state.get("reports", 0) + 1
    return "resume" if a.get("resume") else "stay"
