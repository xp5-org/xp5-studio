"""Read RMM snapshot files (snapshot.export, RMM_SNAP_DIR) offline and diff two of them by dump region.

Layout (GIT_SRC_DIR/A/86Box/src/rmm_snap.c, "files" and "relocation"): a header (magic RMMSNAP1, version, hdrlen,
tsc, tag[64]), then sections {id[4], zero u32, len u64} IDNT BILD RELC DISK DATA, then END. RELC is the reloc blob:
header {magic "RLC1", nreg, nmod, nptr, nlab}, nreg regions {base, len, local} (one per rmm_snap_raw call, in visitor
order), nmod modules, nptr pointers, nreg label indexes, nlab labels of 32 bytes. DATA is the regions' bytes back to
back, so region k starts at the sum of the lengths before it (checked: the sum must equal len(DATA)).

    python3 -m rmm_86box.snapfile info FILE
    python3 -m rmm_86box.snapfile diff A B [--json OUT] [--max 40]

Used by the puppet's save -> restore -> save check (puppet_plan.md 7.3): two files of the same machine state must
match region by region.
"""

import argparse
import json
import struct
import sys

HDR = struct.Struct("<8sIIQ64s")
SECT = struct.Struct("<4sIQ")
RELC_HDR = struct.Struct("<5I")
REG = struct.Struct("<QQQ")
MOD_SIZE = 24 + 192
PTR_SIZE = 24
LABEL = 32


class SnapFileError(ValueError):
    pass


class SnapFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) < HDR.size or raw[:7] != b"RMMSNAP":
            raise SnapFileError(f"{path} is not a snapshot file")
        magic, self.version, hdrlen, self.tsc, tag = HDR.unpack_from(raw, 0)
        self.tag = tag.split(b"\0", 1)[0].decode(errors="replace")
        self.sections = {}
        at = hdrlen
        while at + SECT.size <= len(raw):
            sid, _, n = SECT.unpack_from(raw, at)
            at += SECT.size
            sid = sid.decode(errors="replace")
            if sid == "END ":
                break
            if at + n > len(raw):
                raise SnapFileError(f"{path}: section {sid} runs past the end")
            self.sections[sid] = (at, n)
            at += n
        for k in ("IDNT", "RELC", "DATA"):
            if k not in self.sections:
                raise SnapFileError(f"{path}: no {k} section (truncated or damaged)")
        self._raw = raw
        self.ident = self.sect("IDNT").decode(errors="replace")
        self.build = self.sect("BILD").decode(errors="replace") if "BILD" in self.sections else ""
        self.ptrs = {}
        self.regions = self._regions()

    def sect(self, sid):
        off, n = self.sections[sid]
        return self._raw[off:off + n]

    @property
    def data_off(self):
        return self.sections["DATA"][0]

    def _regions(self):
        blob = self.sect("RELC")
        magic, nreg, nmod, nptr, nlab = RELC_HDR.unpack_from(blob, 0)
        if magic != 0x31434C52:
            raise SnapFileError(f"{self.path}: RELC magic {magic:08x}")
        at = RELC_HDR.size
        regs = [REG.unpack_from(blob, at + i * REG.size) for i in range(nreg)]
        at += nreg * REG.size + nmod * MOD_SIZE
        for i in range(nptr):
            # where the pointer is (region, off) -> what it points at (kind 1 module / 2 region, target, offset)
            reg, off, kind, target, b = struct.unpack_from("<4IQ", blob, at + i * PTR_SIZE)
            if kind:
                self.ptrs[(reg, off)] = (kind, target, b)
        at += nptr * PTR_SIZE
        labidx = struct.unpack_from(f"<{nreg}I", blob, at)
        at += nreg * 4
        labels = [blob[at + i * LABEL:at + (i + 1) * LABEL].split(b"\0", 1)[0].decode(errors="replace")
                  for i in range(nlab)]
        out, off = [], 0
        for i, (base, n, local) in enumerate(regs):
            lab = labels[labidx[i]] if labidx[i] < nlab else "?"
            out.append({"index": i, "label": lab or "(core)", "off": off, "len": n, "local": local})
            off += n
        dlen = self.sections["DATA"][1]
        if off != dlen:
            # SCAN_REGS caps the table; past it the offsets are unknown. Say so instead of mislabelling.
            raise SnapFileError(f"{self.path}: regions add up to {off} bytes, DATA has {dlen}")
        return out

    def region_bytes(self, r):
        a = self.data_off + r["off"]
        return self._raw[a:a + r["len"]]

    def by_label(self):
        """{label: total bytes} in first-seen order."""
        out = {}
        for r in self.regions:
            out[r["label"]] = out.get(r["label"], 0) + r["len"]
        return out


def diff(a, b, max_runs=8, ptr_equiv=True, word=8):
    """Region-by-region compare of two SnapFiles of the same machine. Returns a dict: same (bool), ident_same,
    layout_same, regions: list of {label, index, len, ndiff, runs:[(off, len, a_hex, b_hex)]} for each region that
    differs. ptr_equiv: a pointer-sized word that RELC lists in both files as pointing at the same region (or
    module) and offset counts as equal, so a list a restore re-allocated (new heap addresses, same shape) is not
    a difference. `ptr_words` counts the words forgiven that way."""
    res = {"a": a.path, "b": b.path, "ident_same": a.ident == b.ident, "tsc": (a.tsc, b.tsc), "regions": []}
    la = [(r["label"], r["len"]) for r in a.regions]
    lb = [(r["label"], r["len"]) for r in b.regions]
    res["layout_same"] = la == lb
    if not res["layout_same"]:
        first = next((i for i, (x, y) in enumerate(zip(la, lb)) if x != y), min(len(la), len(lb)))
        res["layout_first_difference"] = {"index": first, "a": la[first] if first < len(la) else None,
                                          "b": lb[first] if first < len(lb) else None}
        res["same"] = False
        return res
    for ra, rb in zip(a.regions, b.regions):
        xa, xb = a.region_bytes(ra), b.region_bytes(rb)
        if xa == xb:
            continue
        ri = ra["index"]
        n = len(xa)
        mask = bytearray(x != y for x, y in zip(xa, xb))
        if ptr_equiv:
            for w in sorted({k // word for k in range(n) if mask[k]}):
                pa, pb = a.ptrs.get((ri, w * word)), b.ptrs.get((ri, w * word))
                if pa is not None and pa == pb:
                    mask[w * word:(w + 1) * word] = bytes(min(word, n - w * word))
                    res["ptr_words"] = res.get("ptr_words", 0) + 1
        if not any(mask):
            continue
        runs, nd, i = [], 0, 0
        while i < n:
            if mask[i]:
                j = i
                while j < n and mask[j]:
                    j += 1
                nd += j - i
                if len(runs) < max_runs:
                    runs.append((i, j - i, xa[i:min(j, i + 8)].hex(), xb[i:min(j, i + 8)].hex()))
                i = j
            else:
                i += 1
        res["regions"].append({"label": ra["label"], "index": ra["index"], "len": ra["len"], "ndiff": nd,
                               "runs": runs})
    res["same"] = res["ident_same"] and not res["regions"]
    return res


def summarize(d, max_lines=40):
    lines = []
    if not d["ident_same"]:
        lines.append("IDNT differs")
    if not d["layout_same"]:
        lines.append(f"region layout differs at {d['layout_first_difference']}")
    by = {}
    for r in d["regions"]:
        e = by.setdefault(r["label"], [0, 0, []])
        e[0] += 1
        e[1] += r["ndiff"]
        e[2].append(r)
    for lab, (nreg, nd, rs) in by.items():
        first = rs[0]
        run = first["runs"][0] if first["runs"] else None
        where = f" first at region {first['index']}+0x{run[0]:x} ({run[1]} B: {run[2]} -> {run[3]})" if run else ""
        lines.append(f"{lab}: {nd} bytes differ in {nreg} region(s){where}")
    return lines[:max_lines]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rmm_86box.snapfile", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info")
    i.add_argument("file")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--json")
    d.add_argument("--max", type=int, default=40)
    a = ap.parse_args(argv)
    if a.cmd == "info":
        s = SnapFile(a.file)
        print(f"tag={s.tag} tsc={s.tsc} regions={len(s.regions)} data={s.sections['DATA'][1]}")
        print(s.ident.rstrip())
        for lab, n in s.by_label().items():
            print(f"  {n:10d}  {lab}")
        return 0
    res = diff(SnapFile(a.a), SnapFile(a.b))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    for ln in summarize(res, a.max):
        print(ln)
    print("SNAPDIFF SAME" if res["same"] else f"SNAPDIFF DIFFERS ({len(res['regions'])} regions)")
    return 0 if res["same"] else 1


if __name__ == "__main__":
    sys.exit(main())
