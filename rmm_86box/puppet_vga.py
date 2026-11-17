"""Video checks on the Linux puppet (puppet_plan.md P3), host side of the `vga` personality (pp_vga).

For each mode the guest sets the mode through the card's own BIOS (INT10 in vm86, or VBE 4F02 with the linear
frame buffer), draws a block pattern, CRCs VRAM and times vblank. The host then

  - recomputes the VRAM bytes from the printed layout and the seed: the CRC must match (write path, planar read
    map select, chain-4, CGA interleave, LFB pitch);
  - takes an RMM screenshot and samples the centre of every 8x8 block (every cell in text modes): each must be the
    colour the card's palette makes of that block (AC + DAC read back from the card for 16-colour modes, a fixed
    DAC palette the guest loads for 8 bpp, the channel masks for direct colour), within COLOR_TOL per channel;
  - checks the vblank rate of the standard modes (70 Hz for 200/350/400-line modes, 60 Hz for 480-line).

The pattern function and the colour rules are mirrored from linuxpuppet/linux/personalities/vga/pp_vga.c.
"""

import base64
import os
import struct
import time
import zlib

from .linuxpuppet import PuppetError

PP_VGA = "/pp/vga/bin/pp_vga"
M32 = 0xFFFFFFFF
COLOR_TOL = 3
HZ_TOL = 1.5
STD_MODES = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0D, 0x0E, 0x10, 0x11, 0x12, 0x13]
STD_HZ = {m: (59.94 if m in (0x11, 0x12) else 70.09) for m in STD_MODES}
INT_KEYS = ("w", "h", "pitch", "bpp", "ncolors", "planes", "len", "rs", "rp", "gs", "gp", "bs", "bp", "mono")
HEX_KEYS = ("mode", "base", "bda_mode")


# -- pattern (mirror of pp_vga.c) --------------------------------------------------------------------------
def hblk(bx, by, seed):
    v = ((bx * 0x9E3779B1) ^ ((by + 1) * 0x85EBCA77) ^ (seed * 0xC2B2AE3D)) & M32
    v ^= v >> 15
    v = (v * 0x2C1B3C6D) & M32
    v ^= v >> 12
    v = (v * 0x297A2D39) & M32
    v ^= v >> 15
    return v


def text_cell(v, mono):
    if mono:
        fg = (0, 7, 8, 15)[v & 3]
        bg = 7 if (v >> 2) & 1 else 0
        if (fg & 7) == bg:
            fg = 0 if bg else 15
    else:
        fg, bg = v & 15, (v >> 4) & 7
        if fg == bg:
            fg ^= 8
    return (0xDB if (v >> 8) & 1 else 0x20), fg | (bg << 4)


def chunky_dac():
    """The 6-bit DAC palette pp_vga loads for 8 bpp modes: every index a different colour."""
    return bytes(c for i in range(256) for c in ((i & 7) * 9, ((i >> 3) & 7) * 9, ((i >> 6) & 3) * 21))


def _bcol(L, by, seed):
    nbx = (L["w"] + 7) // 8
    if L["kind"] == "direct":
        return [hblk(x, by, seed) for x in range(nbx)]
    return [hblk(x, by, seed) % L["ncolors"] for x in range(nbx)]


def _pack(L, c):
    px = ((((c & 0xFF) >> (8 - L["rs"])) << L["rp"]) | ((((c >> 8) & 0xFF) >> (8 - L["gs"])) << L["gp"])
          | ((((c >> 16) & 0xFF) >> (8 - L["bs"])) << L["bp"]))
    return px.to_bytes(4, "little")[:(L["bpp"] + 7) // 8]


def vram_bytes(L, seed):
    """The bytes pp_vga's CRC covers, in the same order."""
    k, w, h, pitch = L["kind"], L["w"], L["h"], L["pitch"]
    if k == "text":
        out = bytearray()
        for y in range(h):
            for x in range(w):
                out += bytes(text_cell(hblk(x, y, seed), L["base"] == 0xB0000))
        return bytes(out)
    rows = {}
    for by in range((h + 7) // 8):
        bc = _bcol(L, by, seed)
        if k in ("cga4", "cga2"):
            sh = 2 if k == "cga4" else 1
            ppb = 8 // sh
            row = bytearray()
            for x in range(pitch):
                b = 0
                for i in range(ppb):
                    b = (b << sh) | bc[(x * ppb + i) // 8]
                row.append(b)
            rows[by] = bytes(row)
        elif k == "planar":
            rows[by] = [bytes(0xFF if x < len(bc) and (bc[x] >> p) & 1 else 0 for x in range(pitch))
                        for p in range(L["planes"])]
        elif k == "chunky":
            rows[by] = bytes(bc[x // 8] for x in range(w)).ljust(pitch, b"\0")
        else:
            rows[by] = b"".join(_pack(L, bc[x // 8]) for x in range(w)).ljust(pitch, b"\0")
    if k in ("cga4", "cga2"):
        return b"".join(rows[y // 8] for y in range(0, 200, 2)) + b"".join(rows[y // 8] for y in range(1, 200, 2))
    if k == "planar":
        return b"".join(rows[y // 8][p] for p in range(L["planes"]) for y in range(h))
    return b"".join(rows[y // 8] for y in range(h))


# -- colours ------------------------------------------------------------------------------------------------
def c6to8(v):
    return int((v & 0x3F) / 63.0 * 255.0)


def cnto8(q, n):
    return q if n >= 8 else int(q / float((1 << n) - 1) * 255.0)


def ac_index(r, c):
    """Attribute controller: 4-bit pixel/attribute colour -> DAC index (colour plane enable, P5-4 / P7-6)."""
    ac = r["ac"]
    p = ac[c & ac[0x12]] & 0x3F
    if ac[0x10] & 0x80:
        p = (p & 0x0F) | ((ac[0x14] & 3) << 4)
    return p | ((ac[0x14] & 0x0C) << 4)


def dac_rgb(r, i):
    i &= r["pel"]
    d = r["dac"]
    return c6to8(d[3 * i]), c6to8(d[3 * i + 1]), c6to8(d[3 * i + 2])


def expected_rgb(L, r, c):
    k = L["kind"]
    if k == "chunky":
        return dac_rgb(r, c)
    if k == "direct":
        out = []
        for sh, n in ((0, L["rs"]), (8, L["gs"]), (16, L["bs"])):
            out.append(cnto8(((c >> sh) & 0xFF) >> (8 - n), n))
        return tuple(out)
    return dac_rgb(r, ac_index(r, c))


def expected_range(L, r, c):
    """Per channel (lo, hi): an n-bit direct-colour channel may reach 8 bits by shift, bit replication, scaling or a
    gamma LUT, which is the card's choice, so anything in the value's 8-bit bucket is right. Palette modes: exact."""
    if L["kind"] != "direct":
        return tuple((v, v) for v in expected_rgb(L, r, c))
    out = []
    for sh, n in ((0, L["rs"]), (8, L["gs"]), (16, L["bs"])):
        q = ((c >> sh) & 0xFF) >> (8 - n)
        lo = q << (8 - n)
        out.append((lo, max(lo | ((1 << (8 - n)) - 1), cnto8(q, n))))
    return tuple(out)


def _off(got, rng):
    return max(max(lo - g, g - hi, 0) for g, (lo, hi) in zip(got, rng))


def expansion(L, samples):
    """Which n-bit -> 8-bit rule the screen shows for direct colour: shift, replicate, scale, or mixed."""
    rules = {"shift": lambda q, n: q << (8 - n), "replicate": lambda q, n: ((q << (8 - n)) | (q >> (2 * n - 8))) & 0xFF,
             "scale": cnto8}
    ok = {k: 0 for k in rules}
    for c, got in samples:
        for k, f in rules.items():
            ok[k] += all(abs(f(((c >> sh) & 0xFF) >> (8 - n), n) - g) <= 1
                         for g, (sh, n) in zip(got, ((0, L["rs"]), (8, L["gs"]), (16, L["bs"]))))
    best = [k for k, v in ok.items() if v == len(samples)]
    return "/".join(best) if best else "mixed"


# -- screenshots --------------------------------------------------------------------------------------------
def capture(rpc, wait_ms=500):
    """Two captures, the second after the guest's last write has surely been scanned out. (w, h, rgb, fresh)."""
    rpc.call("capture.screenshot", step=8, wait_ms=wait_ms, timeout=30)
    time.sleep(0.3)
    f = rpc.call("capture.screenshot", step=1, wait_ms=wait_ms, timeout=60)
    return f["w"], f["h"], base64.b64decode(f["data"]), f.get("fresh")


def write_png(path, w, h, rgb):
    raw = b"".join(b"\0" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & M32)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def compare_shot(L, r, seed, shot, tol=COLOR_TOL, keep=6):
    """Sample the screenshot at every block (cell) centre. Returns {samples, bad, first:[...]}."""
    W, H, rgb, _ = shot
    w, h = L["w"], L["h"]
    bad, first, n = 0, [], 0

    def px(x, y):
        o = (y * W + x) * 3
        return rgb[o], rgb[o + 1], rgb[o + 2]

    if L["kind"] == "text":
        mono = L["base"] == 0xB0000
        for cy in range(h):
            for cx in range(w):
                ch, at = text_cell(hblk(cx, cy, seed), mono)
                want = expected_rgb(L, r, at & 15 if ch == 0xDB else at >> 4)
                got = px(int((cx + 0.5) * W / w), int((cy + 0.5) * H / h))
                n += 1
                if max(abs(a - b) for a, b in zip(got, want)) > tol:
                    bad += 1
                    if len(first) < keep:
                        first.append({"cell": [cx, cy], "want": want, "got": got})
        return {"samples": n, "bad": bad, "first": first}
    seen = []
    for by in range((h + 7) // 8):
        bc = _bcol(L, by, seed)
        yc = by * 8 + min(4, (h - by * 8) // 2)
        for bx, c in enumerate(bc):
            xc = bx * 8 + min(4, (w - bx * 8) // 2)
            want = expected_range(L, r, c)
            got = px(int((xc + 0.5) * W / w), int((yc + 0.5) * H / h))
            n += 1
            if _off(got, want) > tol:
                bad += 1
                if len(first) < keep:
                    first.append({"block": [bx, by], "want": want, "got": got})
            elif len(seen) < 200:
                seen.append((c, got))
    res = {"samples": n, "bad": bad, "first": first}
    if L["kind"] == "direct" and seen:
        res["expansion"] = expansion(L, seen)
    if bad:
        res["shift"] = best_shift(L, r, seed, shot, tol)
    return res


def best_shift(L, r, seed, shot, tol=COLOR_TOL, span=3):
    """When blocks are off: is the picture just displaced? Try whole-block offsets on block centres sampled at
    1:1 scale (the screen size may be wrong too). Returns the best (dx, dy) in blocks and its match fraction."""
    W, H, rgb, _ = shot
    w, h = L["w"], L["h"]
    kx, ky = max(1, round(W / w)), max(1, round(H / h))
    cols = {by: _bcol(L, by, seed) for by in range(-span, (h + 7) // 8 + span) if 0 <= by < (h + 7) // 8}
    best = None
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            ok = tot = 0
            for by in range(0, (h + 7) // 8, 3):
                for bx in range(0, (w + 7) // 8, 3):
                    sx, sy = ((bx * 8 + 4) * kx), ((by * 8 + 4) * ky)
                    if sx >= W or sy >= H or not 0 <= by + dy < len(cols) or not 0 <= bx + dx < len(cols[0]):
                        continue
                    o = (sy * W + sx) * 3
                    want = expected_range(L, r, cols[by + dy][bx + dx])
                    tot += 1
                    ok += _off(rgb[o:o + 3], want) <= tol
            if tot and (best is None or ok / tot > best[2]):
                best = (dx, dy, ok / tot)
    return {"dx_blocks": best[0], "dy_blocks": best[1], "match": round(best[2], 3)} if best else None


# -- guest output -------------------------------------------------------------------------------------------
def parse(text):
    r = {"lines": text.splitlines(), "fail": []}
    for ln in r["lines"]:
        if not ln.startswith("VGA "):
            continue
        f = ln.split()
        key = f[1]
        if key == "FAIL":
            r["fail"].append(ln[9:])
        elif key == "layout":
            kv = dict(x.split("=", 1) for x in f[2:])
            L = {"kind": kv["kind"]}
            for k in INT_KEYS + ("banked", "gran"):
                L[k] = int(kv.get(k, 0))
            for k in HEX_KEYS:
                L[k] = int(kv[k], 16)
            r["layout"] = L
        elif key == "crc":
            r["crc"] = int(f[2], 16)
        elif key == "ac":
            r["ac"] = [int(x, 16) for x in f[2:]]
        elif key == "pel":
            r["pel"] = int(f[2], 16)
        elif key == "dac":
            r["dac"] = bytes.fromhex(f[2])
        elif key == "vbl" and f[2].startswith("n="):          # older pp_vga: total only
            kv = dict(x.split("=", 1) for x in f[2:])
            r["vbl"] = {"n": int(kv["n"]), "total_us": int(kv["us"])}
            r["hz"] = round(int(kv["n"]) * 1e6 / max(1, int(kv["us"])), 3)
        elif key == "vbl":
            iv = sorted(int(x) for x in f[2].split("=", 1)[1].split(",") if x)
            med = iv[len(iv) // 2]
            r["vbl"] = {"n": len(iv), "median_us": med, "min_us": iv[0], "max_us": iv[-1],
                        "missed": sum(1 for x in iv if x > 1.5 * med)}
            r["hz"] = round(1e6 / max(1, med), 3)
        elif key == "int10":
            r["int10"] = {k: int(v, 16) for k, v in (x.split("=", 1) for x in f[2:])}
        elif key in ("pattern", "done"):
            kv = dict(x.split("=", 1) for x in f[2:] if "=" in x)
            r[key + "_ms"] = int(kv.get("ms", 0))
        elif key == "vbe":
            if f[2] == "none":
                r["vbe"] = None
            else:
                kv = dict(x.split("=", 1) for x in f[2:5])
                r["vbe"] = {"ver": int(kv["ver"], 16), "mem_kb": int(kv["mem_kb"]), "cap": int(kv["cap"], 16),
                            "oem": ln.split("oem=", 1)[1]}
        elif key == "vmode":
            kv = dict(x.split("=", 1) for x in f[2:])
            r.setdefault("vmodes", []).append({k: int(v, 16) if k in ("m", "attr", "lfb", "winseg") else int(v)
                                               for k, v in kv.items()})
    return r


def run_vga(pup, args, timeout=180.0):
    w = pup.run([PP_VGA] + [str(a) for a in args], timeout=timeout)
    text = w["out"].decode(errors="replace")
    r = parse(text)
    r["run_ms"] = w["run_ms"]
    if w["state"] != "exited" or w["code"] != 0 or r["fail"]:
        tail = " | ".join(r["lines"][-6:])
        raise PuppetError(f"pp_vga {' '.join(map(str, args))}: {w['state']} {w['code']}: {tail}")
    return r


# -- checks -------------------------------------------------------------------------------------------------
def check_mode(pup, mode, seed, out_dir=None, vbl=30, hz=None, regseq=None, tag=None):
    args = ["mode", hex(mode), seed, f"vbl={vbl}"] + ([f"regseq={regseq}"] if regseq else [])
    r = run_vga(pup, args)
    L = r["layout"]
    res = {"mode": f"{mode:#x}", "layout": L, "crc": f"{r['crc']:08x}", "guest_ms": r.get("done_ms"),
           "pattern_ms": r.get("pattern_ms"), "problems": []}
    if "int10" in r:
        res["int10_ax"] = f"{r['int10']['ax']:#x}"
    exp = zlib.crc32(vram_bytes(L, seed)) & M32
    res["crc_expected"] = f"{exp:08x}"
    if r["crc"] != exp:
        res["problems"].append(f"VRAM CRC {r['crc']:08x}, the pattern gives {exp:08x}")
    if L["kind"] == "chunky" and r["dac"] != chunky_dac():
        diff = next(i for i in range(768) if r["dac"][i] != chunky_dac()[i])
        res["problems"].append(f"DAC read back differs from what was loaded, first at entry {diff // 3}")
    if L["bda_mode"] != (mode & 0x7F) and mode < 0x100 and not regseq:
        res["problems"].append(f"BDA mode byte {L['bda_mode']:#x} after INT10 AH=0 AL={mode:#x}")
    shot = capture(pup.rpc)
    res["shot"] = {"w": shot[0], "h": shot[1], "fresh": shot[3]}
    if out_dir:
        p = os.path.join(out_dir, f"vga_{tag or ''}{mode:03x}.png")
        write_png(p, shot[0], shot[1], shot[2])
        res["shot"]["png"] = p
    cmp = compare_shot(L, r, seed, shot)
    res["screen"] = cmp
    res["palette"] = {"ac": r.get("ac"), "pel": r.get("pel"), "dac": r.get("dac", b"").hex()}
    if cmp["bad"]:
        try:                                    # the CRTC/sequencer state that produced this picture, for triage
            res["regs"] = regs_program(pup, ext=True).splitlines()
        except PuppetError as e:
            res["regs"] = [f"dump failed: {e}"]
        res["problems"].append(f"screenshot {shot[0]}x{shot[1]} (mode {L['w']}x{L['h']}): {cmp['bad']}/{cmp['samples']} "
                               f"samples off, best fit {cmp.get('shift')}, first {cmp['first'][:2]}")
    if "hz" in r:
        res["hz"] = r["hz"]
        res["vbl"] = r["vbl"]
        if hz is not None and abs(r["hz"] - hz) > HZ_TOL:
            res["problems"].append(f"vblank {r['hz']} Hz, want {hz} Hz")
        elif hz is None and not 40.0 <= r["hz"] <= 160.0:
            res["problems"].append(f"vblank {r['hz']} Hz out of 40..160")
    res["ok"] = not res["problems"]
    return res


def text_back(pup):
    return run_vga(pup, ["text"])


def modes(pup, mode_list=None, seed=0x3E5D, out_dir=None, vbl=30):
    out = []
    try:
        for m in (mode_list or STD_MODES):
            out.append(check_mode(pup, m, seed + m, out_dir, vbl=vbl, hz=STD_HZ.get(m)))
    finally:
        text_back(pup)
    return out


def vbe_testable(vm, mem_kb):
    """None if pp_vga can check the mode, else why not."""
    model, bpp, attr = vm["model"], vm["bpp"], vm["attr"]
    if not attr & 1:
        return "not supported by the hardware (attr bit 0)"
    if model in (4, 6) and not attr & 0x80 and (vm.get("wina", 0) & 7) != 7:
        return "no linear frame buffer and window A not readable + writable"
    if model == 4 and bpp != 8 or model == 6 and bpp not in (15, 16, 24, 32) or model not in (0, 3, 4, 6):
        return f"model {model} bpp {bpp} not handled"
    pitch = vm["lpitch"] or vm["pitch"]
    if model == 3 and vm["pitch"] * vm["h"] > 0x10000:
        return "planar mode larger than the A000 window"
    if model in (4, 6) and pitch * vm["h"] > mem_kb * 1024:
        return "needs more VRAM than the card reports"
    return None


def vbe(pup, seed=0x7BE0, out_dir=None, max_modes=0, vbl=20):
    info = run_vga(pup, ["info"])
    res = {"vbe": info.get("vbe"), "modes": [], "skipped": []}
    if not info.get("vbe"):
        return res
    todo = []
    for vm in info.get("vmodes", []):
        why = vbe_testable(vm, info["vbe"]["mem_kb"]) if "model" in vm else "4F01 failed"
        (res["skipped"].append({"m": f"{vm['m']:#x}", "why": why}) if why else todo.append(vm))
    if max_modes:
        todo = todo[:max_modes]
    try:
        for vm in todo:
            try:
                res["modes"].append(check_mode(pup, vm["m"], seed + vm["m"], out_dir, vbl=vbl, tag="vbe_"))
            except PuppetError as e:
                res["modes"].append({"mode": f"{vm['m']:#x}", "ok": False, "problems": [str(e)]})
    finally:
        text_back(pup)
    return res


def regs_program(pup, ext=False):
    r = run_vga(pup, ["regs"] + (["ext"] if ext else []))
    return "\n".join(ln for ln in r["lines"] if ln.startswith("R ")) + "\n"


def regseq(pup, mode=0x13, seed=0x5E9, out_dir=None):
    """Set MODE through the BIOS, dump the VGA registers as a REGSEQ program, go back to text mode, play the
    program instead of INT10: same VRAM CRC, same screen, and a second dump equal to the first."""
    res = {"mode": f"{mode:#x}"}
    try:
        res["bios"] = check_mode(pup, mode, seed, out_dir, vbl=0, tag="bios_")
        prog = regs_program(pup)
        res["program_lines"] = prog.count("\n")
        text_back(pup)
        pup.put("/pp/tmp/vga_mode.seq", prog.encode())
        res["regseq"] = check_mode(pup, mode, seed, out_dir, vbl=0, regseq="/pp/tmp/vga_mode.seq", tag="regseq_")
        prog2 = regs_program(pup)
    finally:
        text_back(pup)
    a, b = prog.splitlines(), prog2.splitlines()
    res["reg_diffs"] = [f"{x} -> {y}" for x, y in zip(a, b) if x != y][:20]
    if out_dir:
        with open(os.path.join(out_dir, f"vga_mode{mode:02x}.seq"), "w") as f:
            f.write(prog)
    res["problems"] = (res["bios"]["problems"] + res["regseq"]["problems"]
                       + ([f"registers after the replay differ: {res['reg_diffs'][:5]}"] if res["reg_diffs"] else []))
    if res["bios"]["crc"] != res["regseq"]["crc"]:
        res["problems"].append(f"VRAM CRC via BIOS {res['bios']['crc']} vs via REGSEQ {res['regseq']['crc']}")
    res["ok"] = not res["problems"]
    return res
