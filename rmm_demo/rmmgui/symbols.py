# SPDX-License-Identifier: MIT
"""Symbol tables for a node: names for guest addresses, from the files tools already write.

Nothing here is a new file format. A table loads from whatever the toolchain or disassembler made:

  linker map files   Open Watcom wlink (DOS 16-bit seg:off, DOS4G/LE sections, PE flat),
                     Microsoft LINK (16-bit, and 32-bit with its Rva+Base column),
                     Borland TLINK, GNU ld -Map
  text listings      nm / nm -n ("00401000 T main"); Ghidra's ImportSymbolsScript format
                     ("main 00401000 f"), which is also what most "export symbols" scripts write
  JSON               Ghidra's stock ExportFunctionInfoScript ([{"name", "entry"}]), the bundled
                     ghidra/ExportRmmSymbols.py (functions with sizes and byte patterns, labels,
                     data with types, struct layouts), or any list / dict of {name, address|entry|addr}

Addresses come in three shapes, kept as the file wrote them:

  flat      00401000                 linear = addr - image_base + base   (base defaults to image_base)
  seg       1000:0010 (real mode)    linear = (load_seg + seg - origin) * 16 + off
                                     map files count segments from the image start (origin 0);
                                     Ghidra loads an MZ at 1000:0000 (origin 0x1000); load_seg is
                                     where the image starts in the guest: PSP + 0x10 for an .EXE
  sections  0001:00000010            linear = section_base[1] + off    (DOS4G LE objects, PE sections)

A table is "placed" once its base is known: given (seg= / psp= / base= / secN=), taken from the
executable (exe=FILE: an MZ's relocations, a PE's image base and sections), anchored on one symbol
(main@1F00:0010), or found by matching the functions' first bytes in guest memory (find).
"""

import json
import os
import pkgutil
import re
import struct

DEFAULT_ORIGIN_GHIDRA_MZ = 0x1000
SIG_LEN = 16
MAX_GAP = 0x4000            # a name covers at most this far past a symbol with no size


class SymbolError(Exception):
    pass


class Symbol:
    __slots__ = ("name", "kind", "seg", "off", "size", "type", "module", "sig", "local")

    def __init__(self, name, off, seg=None, kind="label", size=0, type="", module="", sig=None, local=False):
        self.name, self.off, self.seg, self.kind = name, off, seg, kind
        self.size, self.type, self.module, self.sig, self.local = size, type, module, sig, local

    def __repr__(self):
        where = f"{self.seg:04X}:{self.off:04X}" if self.seg is not None else f"{self.off:08X}"
        return f"<{self.kind} {self.name} {where}>"


# -- address text ------------------------------------------------------------------------------

_HEX = re.compile(r"^[0-9A-Fa-f]+$")


def parse_file_addr(v):
    """An address as a file wrote it -> (seg or None, off). Accepts ints, '0x401000', '00401000h',
    '1000:0010', Ghidra's 'ram:00401000' / 'segment_2::1000:0010' space prefixes."""
    if isinstance(v, int):
        return None, v
    s = str(v).strip().rstrip("hH") if not str(v).strip().lower().startswith("0x") else str(v).strip()
    parts = [p for p in s.split(":") if p != ""]
    while len(parts) > 1 and not _HEX.match(parts[0].replace("0x", "")):
        parts = parts[1:]                                    # address space name
    if len(parts) == 2 and all(_HEX.match(p) for p in parts):
        return int(parts[0], 16), int(parts[1], 16)
    if len(parts) == 1:
        p = parts[0]
        return None, int(p, 16) if not p.lower().startswith("0x") else int(p, 0)
    raise ValueError(f"not an address: {v!r}")


def parse_pattern(text):
    """'55 8B EC ?? 0E' (or '558BEC??0E') -> [0x55, 0x8B, 0xEC, None, 0x0E]."""
    if not text:
        return None
    t = text.replace(" ", "")
    if len(t) % 2:
        raise ValueError(f"odd byte pattern {text!r}")
    return [None if t[i:i + 2] in ("??", "..") else int(t[i:i + 2], 16) for i in range(0, len(t), 2)]


def fmt_pattern(pat):
    return " ".join("??" if b is None else f"{b:02X}" for b in pat)


# -- loaders -----------------------------------------------------------------------------------

def _kind_of(word):
    w = (word or "").lower()
    if w in ("f", "func", "function", "code", "t"):
        return "func"
    if w in ("d", "b", "r", "data", "bss", "rodata", "g", "v"):
        return "data"
    return "label"


def load_json(text):
    obj = json.loads(text)
    meta, recs = {}, []
    if isinstance(obj, list):
        recs = [(r, None) for r in obj]
        meta["format"] = "json list" + (" (Ghidra ExportFunctionInfoScript)" if obj and isinstance(obj[0], dict) and "entry" in obj[0] else "")
    elif isinstance(obj, dict):
        prog = obj.get("program") or {}
        meta.update(prog)
        meta["format"] = "json" + (f" ({obj['generator']})" if obj.get("generator") else "")
        for key, kind in (("functions", "func"), ("labels", "label"), ("data", "data"), ("symbols", None)):
            recs += [(r, kind) for r in obj.get(key) or []]
        meta["types"] = obj.get("types") or {}
        if "image_base" in obj:
            meta["image_base"] = obj["image_base"]
    else:
        raise SymbolError("JSON symbols: expected a list of {name, address} or a dict of lists")
    syms = []
    for r, kind in recs:
        if not isinstance(r, dict) or "name" not in r:
            continue
        a = next((r[k] for k in ("address", "entry", "addr", "start", "value", "offset") if k in r), None)
        if a is None:
            continue
        seg, off = parse_file_addr(a)
        k = kind or ("func" if "entry" in r and not r.get("kind") else _kind_of(r.get("kind") or r.get("type")))
        s = Symbol(str(r["name"]), off, seg, k,
                   int(r.get("size") or 0), str(r.get("datatype") or r.get("type") or ""), str(r.get("module", "")),
                   parse_pattern(r.get("bytes") or r.get("pattern") or r.get("signature_bytes")))
        syms.append(s)
    return syms, meta


# MS LINK / Borland TLINK / Watcom seg:off publics:  "0000:0010       _main", "0001:00000010+ hurt_",
# "0001:00000000       _hurt     00401000 f   g.obj", "0000:0000  Abs  __acrtused"
_RE_SEGLINE = re.compile(r"^\s*([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4,8})([*+s]?)\s+(?:(Abs|Imp|Idle)\s+)?(\S+)(.*)$")
# Watcom PE (flat) memory map:  "00401010+      hurt_"
_RE_FLATLINE = re.compile(r"^([0-9A-Fa-f]{8})([*+s]?)\s+(\S+)\s*$")
# GNU ld -Map:  "                0x0000000000401106                hurt"
_RE_GNULINE = re.compile(r"^\s+0x([0-9A-Fa-f]+)\s+([A-Za-z_.$?@][\w.$?@]*)\s*$")
_RE_NM = re.compile(r"^([0-9A-Fa-f]{4,16})\s+([A-Za-z?-])\s+(\S+)\s*$")
_RE_GHIDRA_TXT = re.compile(r"^(\S+)\s+((?:0x)?[0-9A-Fa-f]{4,16}|[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4,8})(?:\s+([fl]))?\s*$")
# segment tables: Watcom "_TEXT  CODE  AUTO  0000:0008  00000455" / "_TEXT CODE AUTO 00401010 00003d88",
# MS / Borland " 00000H 0016FH 00170H _TEXT  CODE"
_RE_WSEG = re.compile(r"^(\S+(?: CODE)?)\s+(\S+)\s+(\S+)\s+(?:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4,8})|([0-9A-Fa-f]{8}))\s+([0-9A-Fa-f]{8})\s*$")
_RE_MSSEG = re.compile(r"^\s*([0-9A-Fa-f]{5,8})H\s+([0-9A-Fa-f]{5,8})H\s+([0-9A-Fa-f]{5,8})H\s+(\S+)\s+(\S+)")


def load_map(text):
    """A linker map (Watcom, MS LINK, Borland, GNU ld) -> (symbols, meta)."""
    meta = {"format": "map"}
    if "Open Watcom Linker" in text or "WATCOM" in text[:400]:
        meta["format"] = "Open Watcom map"
    elif "Publics by Value" in text or "Publics by Name" in text:
        meta["format"] = "Microsoft LINK map" if ("Preferred load address" in text or "Rva+Base" in text) \
            else "MS LINK / Borland TLINK map"
    elif re.search(r"^Memory Configuration|^Linker script and memory map", text, re.M):
        meta["format"] = "GNU ld map"
    m = re.search(r"Preferred load address is ([0-9A-Fa-f]+)", text)
    if m:
        meta["image_base"] = int(m.group(1), 16)
    ranges = []                 # (is_code, how, seg, start, size); how: lin | flat | seg | sec
    syms, seen = [], set()
    in_publics = False
    wide = False                # seg:off with 8-digit offsets: sections (DOS4G objects, PE sections)
    for line in text.splitlines():
        low = line.lower()
        if "publics by" in low or "static symbols" in low or "memory map" in low or re.match(r"^address\s+symbol", low):
            in_publics = True
            continue
        if "entry point" in low or low.startswith("line numbers"):
            in_publics = False
            continue
        m = _RE_WSEG.match(line)
        if m and m.group(2) not in ("Class", "====="):
            is_code, size = "CODE" in m.group(2).upper(), int(m.group(7), 16)
            if m.group(6):
                ranges.append((is_code, "flat", None, int(m.group(6), 16), size))
            else:
                w8 = len(m.group(5)) == 8
                wide |= w8
                ranges.append((is_code, "sec" if w8 else "seg", int(m.group(4), 16), int(m.group(5), 16), size))
            continue
        m = _RE_MSSEG.match(line)
        if m:
            ranges.append(("CODE" in m.group(5).upper(), "lin", None, int(m.group(1), 16), int(m.group(3), 16)))
            continue
        if "C=" in line and "S=" in line:                # Borland detailed segment map line
            continue
        m = _RE_SEGLINE.match(line)
        if m:
            seg, off, name, rest = int(m.group(1), 16), int(m.group(2), 16), m.group(5), m.group(6).split()
            w8 = len(m.group(2)) == 8
            if re.match(r"^[0-9A-Fa-f]+H$", name):        # MS LINK 32 segment table: 0001:00000000 00001000H .text CODE
                wide |= w8
                ranges.append((bool(rest) and "CODE" in rest[-1].upper(), "sec" if w8 else "seg", seg, off,
                               int(name[:-1], 16)))
                continue
            if m.group(4) in ("Abs", "Imp"):
                continue
            wide |= w8
            flat = int(rest[0], 16) if rest and re.match(r"^[0-9A-Fa-f]{8}$", rest[0]) else None
            kind = "func" if len(rest) > 1 and rest[1] == "f" else "label"
            mod = rest[-1] if len(rest) > 1 and rest[-1] not in ("f", "i") else ""
            if (name, seg, off) in seen:
                continue
            seen.add((name, seg, off))
            if flat is not None:
                if seg != 0:                               # seg 0 = absolute values, not addresses
                    syms.append(Symbol(name, flat, None, kind, module=mod, local="static" in low))
            else:
                syms.append(Symbol(name, off, seg, kind, local=m.group(3) == "+"))
            continue
        m = _RE_FLATLINE.match(line)
        if m and in_publics:
            key = (m.group(3), None, int(m.group(1), 16))
            if key not in seen:
                seen.add(key)
                syms.append(Symbol(m.group(3), int(m.group(1), 16), None, local=m.group(2) == "+"))
            continue
        m = _RE_GNULINE.match(line)
        if m and meta["format"] == "GNU ld map":
            key = (m.group(2), None, int(m.group(1), 16))
            if key not in seen and not m.group(2).startswith("."):
                seen.add(key)
                syms.append(Symbol(m.group(2), int(m.group(1), 16), None))
    if wide:
        meta["sections"] = True
    _classify(syms, ranges)
    return syms, meta


def _classify(syms, ranges):
    """Mark symbols inside a CODE segment as functions, the rest as data, where the map says."""
    for s in syms:
        if s.kind == "func":
            continue
        for is_code, how, seg, start, size in ranges:
            if how == "flat":
                if s.seg is not None:
                    continue
                a = s.off
            elif how == "sec":
                if s.seg != seg:
                    continue
                a = s.off
            else:
                if s.seg is None:
                    continue
                a, start = s.seg * 16 + s.off, (start if how == "lin" else seg * 16 + start)
            if start <= a < start + max(size, 1):
                s.kind = "func" if is_code else "data"
                break


def load_text(text):
    """nm output or Ghidra ImportSymbolsScript lines."""
    syms, fmt = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        m = _RE_NM.match(line)
        if m and fmt != "ghidra":
            fmt = "nm"
            t = m.group(2)
            if t in "UuwvN-?":
                continue
            syms.append(Symbol(m.group(3), int(m.group(1), 16), None, _kind_of(t), local=t.islower()))
            continue
        m = _RE_GHIDRA_TXT.match(line)
        if m:
            fmt = "ghidra"
            seg, off = parse_file_addr(m.group(2))
            syms.append(Symbol(m.group(1), off, seg, "func" if m.group(3) == "f" else "label"))
    return syms, {"format": {"nm": "nm listing", "ghidra": "Ghidra symbol list (name addr [f|l])"}.get(fmt, "text")}


def load_file(path):
    with open(path, "r", errors="replace") as f:
        text = f.read()
    head = text.lstrip()[:1]
    if head in "[{":
        try:
            return load_json(text)
        except json.JSONDecodeError as e:
            raise SymbolError(f"{path}: looks like JSON but does not parse: {e}") from e
    syms, meta = load_map(text)
    if not syms:
        syms, meta = load_text(text)
    if not syms:
        raise SymbolError(f"{os.path.basename(path)}: no symbols found (want a linker map, nm output, "
                          "a Ghidra name/address list, or JSON)")
    return syms, meta


# -- executables: where a map file's bytes are, for find / verify --------------------------------

class Exe:
    """Just enough of MZ / PE to read the bytes a symbol names, with relocated bytes masked."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        self.kind, self.image_base, self.sections, self.masked = "MZ", None, [], set()
        if d[:2] not in (b"MZ", b"ZM"):
            if not path.lower().endswith(".com"):
                raise SymbolError(f"{path}: not an MZ/PE executable (or a .COM)")
            self.kind, self.hdr = "COM", -0x100       # loaded at PSP:0100; a tiny map counts from the PSP
            return
        lfanew = struct.unpack_from("<I", d, 0x3C)[0] if len(d) > 0x40 else 0
        if lfanew and d[lfanew:lfanew + 4] == b"PE\0\0":
            self._pe(lfanew)
        elif lfanew and d[lfanew:lfanew + 2] in (b"LE", b"LX"):
            self._le(lfanew)
        else:
            self._mz()

    def _mz(self):
        d = self.data
        nrel, hdr_par, reloff = struct.unpack_from("<H", d, 6)[0], struct.unpack_from("<H", d, 8)[0], \
            struct.unpack_from("<H", d, 0x18)[0]
        self.hdr = hdr_par * 16
        for i in range(nrel):
            off, seg = struct.unpack_from("<HH", d, reloff + i * 4)
            a = seg * 16 + off
            self.masked.update((a, a + 1))

    def _pe(self, lfanew):
        d = self.data
        self.kind = "PE"
        nsec, optsize = struct.unpack_from("<H", d, lfanew + 6)[0], struct.unpack_from("<H", d, lfanew + 20)[0]
        opt = lfanew + 24
        magic = struct.unpack_from("<H", d, opt)[0]
        self.image_base = struct.unpack_from("<I" if magic == 0x10B else "<Q", d, opt + (28 if magic == 0x10B else 24))[0]
        ddir = opt + (96 if magic == 0x10B else 112)
        sec = opt + optsize
        for i in range(nsec):
            name = d[sec + i * 40:sec + i * 40 + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", d, sec + i * 40 + 8)
            self.sections.append((name, va, max(vsize, rawsize), rawptr, rawsize))
        rva, size = struct.unpack_from("<II", d, ddir + 5 * 8)            # base relocations
        p, end = self._rva_off(rva), None
        if p is not None and size:
            end = p + size
            while p + 8 <= end:
                page, blk = struct.unpack_from("<II", d, p)
                if blk < 8:
                    break
                for k in range((blk - 8) // 2):
                    e = struct.unpack_from("<H", d, p + 8 + k * 2)[0]
                    if e >> 12 in (3, 10):
                        a = page + (e & 0xFFF)
                        self.masked.update(range(a, a + (4 if e >> 12 == 3 else 8)))
                p += blk

    # LE (DOS/4GW, DOS32A, PMODE/W): objects are the map's sections, 1-based; fixups masked per object
    _LE_SRC_SIZE = {0: 1, 2: 2, 3: 4, 5: 2, 6: 6, 7: 4, 8: 4}

    def _le(self, h):
        d = self.data
        self.kind = "LE" if d[h:h + 2] == b"LE" else "LX"
        u32 = lambda o: struct.unpack_from("<I", d, h + o)[0]  # noqa: E731
        self.page_size = u32(0x28)
        npages, objtab, nobj, pagetab = u32(0x14), u32(0x40), u32(0x44), u32(0x48)
        fixpage, fixrec, self.data_pages = u32(0x68), u32(0x6C), u32(0x80)
        shift = u32(0x2C) if self.kind == "LX" else 0
        phys = []
        for i in range(npages):
            e = d[h + pagetab + i * 4:h + pagetab + i * 4 + 4]
            if self.kind == "LE":
                phys.append(self.data_pages + (((e[0] << 16) | (e[1] << 8) | e[2]) - 1) * self.page_size)
            else:
                phys.append(self.data_pages + (struct.unpack_from("<I", e)[0] << shift))
        self.objects = []                # (reloc_base, vsize, [file offset of each page], first logical page)
        for i in range(nobj):
            vsize, rbase, _fl, pidx, pcount, _r = struct.unpack_from("<IIIIII", d, h + objtab + i * 24)
            self.objects.append((rbase, vsize, phys[pidx - 1:pidx - 1 + pcount], pidx))
            self.sections.append((f"obj{i + 1}", rbase, vsize, None, None))
        offs = [u32(fixpage + i * 4) for i in range(npages + 1)]
        for oi, (_rb, _vs, pages, pidx) in enumerate(self.objects, 1):
            for k in range(len(pages)):
                lp = pidx - 1 + k
                p, end = h + fixrec + offs[lp], h + fixrec + offs[lp + 1]
                while p < end:
                    src, fl = d[p], d[p + 1]
                    p += 2
                    if src & 0x20:
                        cnt, srcoffs = d[p], None
                        p += 1
                    else:
                        srcoffs = [struct.unpack_from("<h", d, p)[0]]
                        p += 2
                    t = fl & 3
                    p += 2 if fl & 0x40 else 1                     # object / module / entry ordinal
                    if t == 0:
                        if src & 0xF != 2:
                            p += 4 if fl & 0x10 else 2
                    elif t == 1:
                        p += 4 if fl & 0x10 else (1 if fl & 0x80 else 2)
                    elif t == 2:
                        p += 4 if fl & 0x10 else 2
                    if t != 0 and fl & 0x04:
                        p += 4 if fl & 0x20 else 2
                    if srcoffs is None:
                        srcoffs = list(struct.unpack_from(f"<{cnt}h", d, p))
                        p += cnt * 2
                    size = self._LE_SRC_SIZE.get(src & 0xF, 4)
                    for so in srcoffs:
                        base = k * self.page_size + so
                        self.masked.update((oi, base + j) for j in range(size))

    def _rva_off(self, rva):

        for _n, va, vsize, rawptr, rawsize in self.sections:
            if va <= rva < va + vsize:
                return rawptr + (rva - va) if rva - va < rawsize else None
        return None

    def pattern(self, table, sym, n=SIG_LEN):
        """The first n bytes at sym as the file holds them; relocated bytes are None."""
        if self.kind in ("MZ", "COM"):
            if sym.seg is None:
                return None
            a = (sym.seg - table.origin) * 16 + sym.off
            off, key = self.hdr + a, a
        elif self.kind in ("LE", "LX"):
            if sym.seg is None or not 1 <= sym.seg <= len(self.objects):
                return None
            _rb, vsize, pages, _p = self.objects[sym.seg - 1]
            out = []
            for i in range(n):
                o = sym.off + i
                pg = o // self.page_size
                if o >= vsize or pg >= len(pages):
                    break
                f = pages[pg] + o % self.page_size
                out.append(None if (sym.seg, o) in self.masked or f >= len(self.data) else self.data[f])
            return out or None
        else:
            if sym.seg is not None:
                if not 1 <= sym.seg <= len(self.sections):
                    return None
                rva = self.sections[sym.seg - 1][1] + sym.off
            else:
                rva = sym.off - (table.image_base or self.image_base)
            off, key = self._rva_off(rva), rva
            if off is None:
                return None
        raw = self.data[off:off + n]
        return [None if key + i in self.masked else b for i, b in enumerate(raw)] if raw else None


# -- one loaded table --------------------------------------------------------------------------

class SymbolTable:
    def __init__(self, name, path, syms, meta, exe=None):
        self.name, self.path, self.meta = name, path, meta
        self.fmt = meta.get("format", "?")
        self.syms = syms
        self.types = meta.get("types") or {}
        segs = [s for s in syms if s.seg is not None]
        if segs and (meta.get("sections") or any(s.off > 0xFFFF for s in segs)):
            self.mode = "sections"
        elif segs:
            self.mode = "seg"
        else:
            self.mode = "flat"
        ib = meta.get("image_base")
        ib_seg, ib_off = parse_file_addr(ib) if ib is not None else (None, None)
        # map files count segments from the image start; Ghidra puts an MZ at 1000:0000
        ghidra_mz = self.mode == "seg" and "Ghidra" in self.fmt and min(s.seg for s in segs) >= DEFAULT_ORIGIN_GHIDRA_MZ
        self.origin = ib_seg if ib_seg is not None else (DEFAULT_ORIGIN_GHIDRA_MZ if ghidra_mz else 0)
        self.image_base = ib_off if (ib is not None and ib_seg is None) else None
        self.exe = Exe(exe) if exe else None
        if self.exe and self.exe.kind == "PE":
            if self.image_base is None:
                self.image_base = self.exe.image_base
        self.find_notes = []
        self.base = None                        # flat: where image_base sits; seg: load segment
        self.sec_base = {}
        self.placed_by = ""
        if self.mode == "flat":
            self.base = self.image_base or 0
            self.placed_by = "image base" if self.image_base else "addresses as written"
        elif self.mode == "sections" and self.exe and self.exe.kind == "PE":
            for i, (_n, va, *_r) in enumerate(self.exe.sections, 1):
                self.sec_base[i] = self.exe.image_base + va
            self.placed_by = "PE section table"
        self._expand_structs()
        self._index = None

    # names
    def _expand_structs(self):
        """data symbols whose type is a known struct get name.field children (from JSON types)."""
        if not self.types:
            return
        extra = []
        for s in self.syms:
            t = self.types.get(s.type) if s.kind == "data" else None
            for f in (t or {}).get("fields", []):
                extra.append(Symbol(f"{s.name}.{f['name']}", s.off + int(f["offset"]), s.seg, "data",
                                    int(f.get("size") or 0), str(f.get("type", ""))))
        self.syms += extra

    def lookup(self, name):
        exact = [s for s in self.syms if s.name == name]
        if exact:
            return exact[0]
        low = name.lower()
        norm = _norm(name)
        for test in (lambda s: s.name.lower() == low, lambda s: _norm(s.name) == norm):
            hit = [s for s in self.syms if test(s)]
            if hit:
                return hit[0]
        return None

    @property
    def placed(self):
        if self.mode == "flat":
            return True
        if self.mode == "seg":
            return self.base is not None
        return bool(self.sec_base)

    def lin(self, s):
        if self.mode == "flat" or s.seg is None:
            return s.off - (self.image_base or 0) + (self.base or 0)
        if self.mode == "seg":
            return None if self.base is None else ((self.base + s.seg - self.origin) << 4) + s.off
        b = self.sec_base.get(s.seg)
        return None if b is None else b + s.off

    def file_addr(self, s_or_lin):
        """How the map / Ghidra writes an address: 1000:0234, 0001:00000010 or 00401234."""
        if isinstance(s_or_lin, Symbol):
            s = s_or_lin
            if s.seg is None:
                return f"{s.off:08X}"
            return f"{s.seg:04X}:{s.off:04X}" if self.mode == "seg" else f"{s.seg:04X}:{s.off:08X}"
        a = s_or_lin
        if self.mode == "flat":
            return f"{a - (self.base or 0) + (self.image_base or 0):08X}"
        if self.mode == "seg":
            if self.base is None:
                return None
            near = self.at(a, any_kind=True)
            seg = near[0].seg if near else self.origin
            off = a - ((self.base + seg - self.origin) << 4)
            if not 0 <= off <= 0xFFFF:
                seg, off = self.origin + (a >> 4) - self.base, a & 0xF
            return f"{seg:04X}:{off:04X}"
        for sec, b in self.sec_base.items():
            if b <= a < b + 0x10000000:
                near = self.at(a, any_kind=True)
                if near and near[0].seg == sec:
                    return f"{sec:04X}:{a - b:08X}"
        return None

    def index(self):
        if self._index is None:
            rows = []
            for s in self.syms:
                a = self.lin(s)
                if a is not None and "." not in s.name:
                    rows.append((a, s))
            rows.sort(key=lambda r: (r[0], r[1].kind != "func", r[1].local))
            self._index = rows
        return self._index

    def at(self, linear, any_kind=False):
        """(symbol, delta) for the symbol linear falls in, or None."""
        rows = self.index()
        lo, hi = 0, len(rows)
        while lo < hi:
            mid = (lo + hi) // 2
            if rows[mid][0] <= linear:
                lo = mid + 1
            else:
                hi = mid
        i = lo - 1
        if i < 0:
            return None
        a = rows[i][0]
        while i > 0 and rows[i - 1][0] == a:                 # same address: prefer functions, globals
            i -= 1
        a, s = rows[i]
        d = linear - a
        limit = s.size if s.size else MAX_GAP
        if d < limit or any_kind and d < MAX_GAP:
            return s, d
        return None

    def set_base(self, **kw):
        """seg= psp= base= secN= -> placed. Returns what it now is."""
        self._index = None
        for k, v in kw.items():
            if k == "origin":
                self._need("seg")
                self.origin = v
            elif k == "seg":
                self._need("seg")
                self.base = v
            elif k == "psp":
                self._need("seg")
                self.base = v + 0x10
            elif k == "base":
                if self.mode == "seg":
                    self.base = v >> 4
                elif self.mode == "flat":
                    self.base = v
                else:
                    raise SymbolError("a sections table takes sec1=ADDR sec2=ADDR ... (base= is for flat ones)")
            elif re.match(r"^sec\d+$", k):
                self._need("sections")
                self.sec_base[int(k[3:])] = v
            else:
                raise SymbolError(f"{k}=: placement is seg= psp= base= secN= (origin= for the file's own first segment)")
        self.placed_by = "given (" + " ".join(f"{k}={v:#x}" for k, v in kw.items()) + ")"
        return self.where()

    def anchor(self, name, linear):
        s = self.lookup(name)
        if not s:
            raise SymbolError(f"{self.name} has no symbol {name!r}")
        self._index = None
        if self.mode == "flat":
            self.base = linear - s.off + (self.image_base or 0)
        elif self.mode == "seg":
            para = linear - s.off
            if para % 16:
                raise SymbolError(f"{name} at {linear:#x}: {s.seg:04X}:{s.off:04X} cannot land there "
                                  "(the segment would not be paragraph aligned)")
            self.base = (para >> 4) - s.seg + self.origin
        else:
            self.sec_base[s.seg] = linear - s.off
        self.placed_by = f"anchored {name}@{linear:#x}"
        return self.where()

    def _need(self, mode):
        if self.mode != mode:
            raise SymbolError(f"{self.name} has {self.mode} addresses; "
                              + {"seg": "use seg= or psp=", "flat": "use base=",
                                 "sections": "use sec1= sec2= ..."}[self.mode])

    def where(self):
        if self.mode == "seg":
            return (f"load segment {self.base:04X} (linear {self.base << 4:#x})" if self.base is not None
                    else "not placed: seg=, psp=, exe= + find, or SYMBOL@ADDR")
        if self.mode == "flat":
            if not self.image_base:
                return "addresses as written" + (f" + {self.base:#x}" if self.base else "")
            return f"image base {self.image_base:#x} at {self.base:#x}"
        if not self.sec_base:
            return "not placed: sec1=ADDR ..., exe= (PE), or SYMBOL@ADDR"
        return " ".join(f"sec{k}@{v:#x}" for k, v in sorted(self.sec_base.items()))

    def pattern(self, s):
        if s.sig:
            return s.sig
        if self.exe:
            return self.exe.pattern(self, s)
        return None

    def verify(self, read):
        """Count functions whose bytes in the guest match the file / export at the current base."""
        good = bad = 0
        for s in self._sig_syms(8):
            a = self.lin(s)
            pat = self.pattern(s)
            if a is None or not pat:
                continue
            try:
                got = read(a, len(pat))
            except Exception:  # noqa: BLE001 - unmapped page: a mismatch
                bad += 1
                continue
            if _match(got, pat, 0):
                good += 1
            else:
                bad += 1
        return good, bad

    def _sig_syms(self, n):
        out = []
        for s in self.syms:
            if s.kind == "func" or (s.kind == "label" and self.exe):
                pat = self.pattern(s)
                if pat and sum(b is not None for b in pat) >= 6:
                    out.append((sum(b is not None for b in pat), s))
        out.sort(key=lambda r: -r[0])
        return [s for _k, s in out[:n]]

    def find(self, read, lo, hi, prefer=None):
        """Scan guest memory [lo, hi) for the functions' byte patterns; place the table where they
        all agree. read(addr, n) -> bytes. A program's image is often in memory more than once (a
        DOS buffer still holding the file, an unpacker's source, a previous run): every full match is
        kept, and the one `prefer` (the CPU's cs_base + eip) falls in wins, else the first. Returns a
        description, raises SymbolError if there is none."""
        if self.mode == "sections":
            groups = {}
            for s in self._sig_syms(64):
                groups.setdefault(s.seg, []).append(s)
        else:
            groups = {None: self._sig_syms(6)}
        if not any(groups.values()):
            raise SymbolError(f"{self.name}: no byte patterns to look for (a Ghidra export carries them; "
                              "for a map file give exe=FILE)")
        notes = []
        for sec, cands in groups.items():
            cands = cands[:6]
            first, rest = cands[0], cands[1:]
            pat = self.pattern(first)
            run_at, run = _longest_run(pat)
            hits = _scan(read, lo, hi, bytes(run))
            oks = []
            for h in hits:
                start = h - run_at
                if not _match(read(start, len(pat)), pat, 0):
                    continue
                base = self._base_for(first, start)
                if base is None or base in oks:
                    continue
                agree = sum(1 for s in rest if self._check_at(read, s, base))
                if agree == len(rest) or (rest and agree >= max(1, len(rest) - 1)):
                    oks.append(base)
            if not oks:
                if sec is None:
                    raise SymbolError(f"{self.name}: {first.name}'s bytes ({fmt_pattern(pat)}) are not in "
                                      f"{lo:#x}-{hi:#x}" + (f" ({len(hits)} partial matches)" if hits else ""))
                continue
            pick, why = oks[0], ""
            if len(oks) > 1:
                near = sorted((d, b) for b in oks for d in [self._distance(sec, b, prefer)] if d is not None)
                if near:
                    pick, why = near[0][1], "the one cs:eip is in"
                else:
                    why = "the first; cs:eip is in none of them" if prefer is not None else "the first"
                notes.append(f"{len(oks)} copies at " + ", ".join(self._fmt_base(b) for b in oks) + f"; chose {why}")
            self._apply_base(sec, pick)
        self._index = None
        self.placed_by = f"found in guest memory {lo:#x}-{hi:#x}" + (f"; {'; '.join(notes)}" if notes else "")
        self.find_notes = notes
        return self.where()

    def _fmt_base(self, b):
        return f"{b:04X}" if self.mode == "seg" else f"{b:#x}"

    def _distance(self, sec, base, linear):
        """How far linear is past the nearest symbol at or below it, with the image placed at base
        (None: below the image or more than MAX_GAP past its symbols)."""
        if linear is None:
            return None
        saved = (self.base, dict(self.sec_base))
        self._apply_base(sec, base)
        try:
            below = [linear - a for a in (self.lin(s) for s in self.syms if sec is None or s.seg == sec)
                     if a is not None and a <= linear]
            return min(below) if below and min(below) < MAX_GAP else None
        finally:
            self.base, self.sec_base = saved

    def _base_for(self, s, start):
        if self.mode == "flat":
            return start - s.off + (self.image_base or 0)
        if self.mode == "seg":
            para = start - s.off
            return None if para % 16 else (para >> 4) - s.seg + self.origin
        return start - s.off

    def _apply_base(self, sec, base):
        if self.mode == "sections":
            self.sec_base[sec] = base
        else:
            self.base = base

    def _check_at(self, read, s, base):
        saved = (self.base, dict(self.sec_base))
        self._apply_base(s.seg, base)
        try:
            a = self.lin(s)
            pat = self.pattern(s)
            return a is not None and _match(read(a, len(pat)), pat, 0)
        except Exception:  # noqa: BLE001
            return False
        finally:
            self.base, self.sec_base = saved

    def summary(self):
        funcs = sum(1 for s in self.syms if s.kind == "func")
        data = sum(1 for s in self.syms if s.kind == "data")
        return (f"{self.name:<10} {len(self.syms)} symbols ({funcs} functions, {data} data) {self.mode}, "
                f"{self.fmt}; {self.where()}" + (f" [{self.placed_by}]" if self.placed_by else ""))


def _norm(name):
    """C name decoration off: _main, main_, _Foo@8, @Foo@8 -> main / foo."""
    n = name.lower()
    n = re.sub(r"@\d+$", "", n)
    return n.strip("_@")


def _longest_run(pat):
    best, cur, at, cur_at = [], [], 0, 0
    for i, b in enumerate(pat + [None]):
        if b is None:
            if len(cur) > len(best):
                best, at = cur, cur_at
            cur = []
        else:
            if not cur:
                cur_at = i
            cur.append(b)
    return at, best


def _match(got, pat, at):
    if len(got) - at < len(pat):
        return False
    return all(b is None or got[at + i] == b for i, b in enumerate(pat))


def _scan(read, lo, hi, needle, chunk=0x10000):
    hits, a = [], lo
    while a < hi and len(hits) < 64:
        n = min(chunk, hi - a)
        try:
            buf = read(a, n + len(needle) - 1 if a + n < hi else n)
        except Exception:  # noqa: BLE001 - a hole (unpaged linear range): skip it
            a += n
            continue
        i = buf.find(needle)
        while i != -1:
            hits.append(a + i)
            i = buf.find(needle, i + 1)
        a += n
    return hits


# -- the node's collection ---------------------------------------------------------------------

_RE_EXPR = re.compile(r"^(?:(?P<tbl>[\w.-]+)!)?(?P<name>[A-Za-z_.$?@][\w.$?@]*)(?:(?P<op>[+-])(?P<n>(?:0x)?[0-9A-Fa-f]+))?$")


def parse_number(text):
    """hex (0x810F, 810F, 810Fh) or seg:off (0800:010F) -> linear int; ValueError otherwise."""
    s = text.strip().replace("_", "")
    if ":" in s:
        seg, off = s.split(":", 1)
        return int(seg, 16) * 16 + int(off, 16)
    if s.lower().endswith("h") and _HEX.match(s[:-1]):
        return int(s[:-1], 16)
    return int(s, 0) if s.lower().startswith(("0x", "0o", "0b")) else int(s, 16)


class Symbols:
    """Every table loaded on one node, and the lookups the console, panel and hooks use."""

    def __init__(self):
        self.tables = {}

    def load(self, path, name=None, exe=None, **place):
        path = os.path.abspath(os.path.expanduser(path))
        syms, meta = load_file(path)
        name = name or os.path.splitext(os.path.basename(path))[0]
        t = SymbolTable(name, path, syms, meta, exe=os.path.abspath(os.path.expanduser(exe)) if exe else None)
        if place:
            t.set_base(**place)
        self.tables[name] = t
        return t

    def table(self, name=None):
        if name is None:
            if len(self.tables) == 1:
                return next(iter(self.tables.values()))
            raise SymbolError("name the table: " + (", ".join(self.tables) or "none loaded (sym load FILE)"))
        if name not in self.tables:
            raise SymbolError(f"no symbol table {name!r} (loaded: {', '.join(self.tables) or 'none'})")
        return self.tables[name]

    def find_symbol(self, name, tbl=None):
        tables = [self.table(tbl)] if tbl else list(self.tables.values())
        for t in tables:
            s = t.lookup(name)
            if s:
                return t, s
        return None, None

    def addr(self, expr):
        """'main', 'game!main', 'hero.health', 'hurt_+0x10' -> linear address (SymbolError if unknown
        or its table is not placed yet)."""
        m = _RE_EXPR.match(expr.strip())
        if not m:
            raise SymbolError(f"{expr!r}: a name, table!name, or name+offset")
        t, s = self.find_symbol(m.group("name"), m.group("tbl"))
        if not s:
            raise SymbolError(f"no symbol {m.group('name')!r}" + (f" in {m.group('tbl')}" if m.group("tbl") else "")
                              + (f" (tables: {', '.join(self.tables)})" if self.tables else " (no tables: sym load FILE)"))
        a = t.lin(s)
        if a is None:
            raise SymbolError(f"{s.name} is in {t.name}, which is not placed yet: {t.where()}")
        if m.group("op"):
            d = int(m.group("n"), 0) if m.group("n").lower().startswith("0x") else int(m.group("n"), 16)
            a += d if m.group("op") == "+" else -d
        return a

    def parse(self, text):
        """An address field: 0x810F / 810F / 0800:010F / a symbol expression -> linear int."""
        t = text.strip()
        if not t:
            raise SymbolError("empty address")
        if self.tables and _RE_EXPR.match(t) and not t.lower().startswith("0x"):
            try:
                return self.addr(t)
            except SymbolError:
                if not _HEX.match(t.rstrip("hH")):
                    raise
        try:
            return parse_number(t)
        except ValueError:
            raise SymbolError(f"{t!r}: not a number or a known symbol") from None

    def is_symbol(self, text):
        t = text.strip()
        if not self.tables or not _RE_EXPR.match(t) or t.lower().startswith("0x"):
            return False
        try:
            self.addr(t)
            return True
        except SymbolError:
            return False

    def at(self, linear):
        """(table, symbol, delta) or None."""
        best = None
        for t in self.tables.values():
            if not t.placed:
                continue
            r = t.at(linear)
            if r and (best is None or r[1] < best[2]):
                best = (t, r[0], r[1])
        return best

    def name_of(self, linear, with_table=None):
        r = self.at(linear) if linear is not None else None
        if not r:
            return None
        t, s, d = r
        pre = f"{t.name}!" if (with_table if with_table is not None else len(self.tables) > 1) else ""
        return pre + s.name + (f"+{d:#x}" if d else "")

    def file_addr(self, linear):
        r = self.at(linear)
        if not r:
            return None
        return r[0].file_addr(linear)

    def names(self):
        return [s.name for t in self.tables.values() for s in t.syms]


# -- the console word --------------------------------------------------------------------------

USAGE = """\
sym                                tables on this node: format, symbols, where each sits
sym load FILE [as=NAME] [exe=EXE] [seg=|psp=|base=|secN=ADDR] [SYMBOL@ADDR]
                                   a linker map (Watcom, MS LINK, Borland, GNU ld), nm output,
                                   a Ghidra symbol list or JSON export
sym base [NAME] seg=X | psp=X | base=X | secN=X | SYMBOL@ADDR    say where the image sits
sym find [NAME] [LO-HI]            place it by matching function bytes in guest memory
sym verify [NAME]                  check the bytes at the current placement
sym NAME|ADDR                      name -> address and file address; address -> name+offset
sym list [FILTER] [in=NAME]        matching symbols (first 60)
sym break NAME | sym watch NAME [LEN] [r|w|rw]   set it in 86Box, labelled with the name
sym drop NAME
sym ghidra-script [DIR]            write the Ghidra exporter (ExportRmmSymbols.py) to DIR (~/ghidra_scripts)
  Names work in the Monitor panel's address fields (break / watch Add, memory) and in hooks:
  hit.sym(addr), hit.addr_of("name"), hit.file_addr(addr)."""


SUBWORDS = ("load", "base", "find", "verify", "list", "break", "watch", "drop", "ghidra-script", "help")


def _expr_name(expr):
    """'game!hero.health+4' -> ('hero.health', 'game')."""
    m = _RE_EXPR.match(expr.strip())
    return (m.group("name"), m.group("tbl")) if m else (expr, None)


def _place_args(words):
    place, anchors, opts, rest = {}, [], {}, []
    for w in words:
        k, sep, v = w.partition("=")
        if sep and k in ("as", "exe", "in"):
            opts[k] = v
        elif sep:
            place[k] = parse_number(v)
        elif "@" in w and not w.startswith("@"):
            nm, _, a = w.partition("@")
            anchors.append((nm, parse_number(a)))
        else:
            rest.append(w)
    return place, anchors, opts, rest


def _reader(node):
    return lambda a, n: node.rmm.read_mem(a, n, linear=True)


def console(node, args):
    """The per-node `sym` builtin. Runs on the node's worker thread."""
    S = node.symbols
    try:
        if not args:
            if not S.tables:
                return ["no symbol tables on this node", *USAGE.splitlines()]
            return [t.summary() for t in S.tables.values()]
        w = args[0]
        if w in ("help", "-h", "--help"):
            return USAGE.splitlines()
        if w == "ghidra-script":
            d = os.path.abspath(os.path.expanduser(args[1] if len(args) > 1 else "~/ghidra_scripts"))
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, "ExportRmmSymbols.py")
            with open(dst, "wb") as f:
                f.write(pkgutil.get_data(__package__, "ghidra/ExportRmmSymbols.py"))
            return [f"wrote {dst}",
                    "  Ghidra: Script Manager -> ExportRmmSymbols.py (in Export), pick the .json; then: sym load FILE.json",
                    f"  headless: analyzeHeadless PROJDIR PROJ -import GAME.EXE -postScript {dst} OUT.json"]
        if w == "load":
            place, anchors, opts, rest = _place_args(args[1:])
            if not rest:
                raise SymbolError("sym load FILE [as=NAME] [exe=EXE] [seg=|psp=|base=|secN=] [SYM@ADDR]")
            t = S.load(rest[0], name=opts.get("as"), exe=opts.get("exe"), **place)
            for nm, a in anchors:
                t.anchor(nm, a)
            out = [f"loaded {t.summary()}"]
            if t.placed and (t.exe or any(s.sig for s in t.syms)):
                good, bad = t.verify(_reader(node))
                if good or bad:
                    out.append(f"  bytes at that placement: {good} functions match, {bad} differ"
                               + ("" if not bad else " (sym find to search for it)"))
            elif not t.placed:
                out.append(f"  {t.where()}")
            return out
        if w in ("base", "find", "verify", "drop"):
            rest = args[1:]
            name = rest[0] if rest and rest[0] in S.tables else None
            if name:
                rest = rest[1:]
            t = S.table(name)
            if w == "drop":
                del S.tables[t.name]
                return [f"dropped {t.name}"]
            if w == "verify":
                good, bad = t.verify(_reader(node))
                return [f"{t.name}: {good} functions match at {t.where()}, {bad} differ"
                        + ("" if good or bad else " (no byte patterns: give exe= or use a Ghidra export)")]
            if w == "base":
                place, anchors, _o, _r = _place_args(rest)
                if place:
                    t.set_base(**place)
                for nm, a in anchors:
                    t.anchor(nm, a)
                return [t.summary()]
            if rest and "-" in rest[0]:
                lo, hi = (parse_number(x) for x in rest[0].split("-", 1))
            elif t.mode == "seg":
                lo, hi = 0, 0xA0000
            else:
                ram = node.rmm.call("mem.info").get("ram_kb", 16384) * 1024
                lo, hi = 0, min(ram, 0x4000000)
            node.say(f"sym find {t.name}: scanning {lo:#x}-{hi:#x} ...", "muted")
            try:
                r = node.rmm.regs()
                pc = r.get("cs_base", r.get("cs", 0) * 16) + r.get("eip", 0)
            except Exception:  # noqa: BLE001 - no registers: the first copy wins
                pc = None
            where = t.find(_reader(node), lo, hi, prefer=pc)
            good, bad = t.verify(_reader(node))
            return [f"{t.name}: {where} ({good} functions match, {bad} differ)"] + [f"  {n}" for n in t.find_notes]
        if w == "list":
            place, _a, opts, rest = _place_args(args[1:])
            flt = rest[0].lower() if rest else ""
            tables = [S.table(opts["in"])] if "in" in opts else list(S.tables.values())
            out = []
            for t in tables:
                for s in t.syms:
                    if flt in s.name.lower():
                        a = t.lin(s)
                        out.append(f"{t.file_addr(s):>13}  {'--------' if a is None else f'{a:08X}'}  {s.kind:<5} "
                                   f"{s.name}" + (f"  [{s.size}]" if s.size else "") + (f"  {s.type}" if s.type else ""))
                        if len(out) >= 60:
                            return out + ["... (more: narrow the filter)"]
            return out or [f"no symbol matches {flt!r}"]
        if w in ("break", "watch"):
            if len(args) < 2:
                raise SymbolError(f"sym {w} NAME" + (" [LEN] [r|w|rw]" if w == "watch" else ""))
            a = S.addr(args[1])
            label = args[1]
            if w == "break":
                ident = node.rmm.call("debug.break.set", addr=a)["id"]
            else:
                t, s = S.find_symbol(*_expr_name(args[1]))
                length = next((int(x, 0) for x in args[2:] if x[0].isdigit()), (s.size if s and 0 < s.size <= 4 else 2))
                kind = next((x for x in args[2:] if x in ("r", "w", "rw")), "w")
                ident = node.rmm.call("debug.watch.set", addr=a, len=length, kind=kind)["id"]
            node.set_hit_rule(w, ident, label=label, on_hit="stop", by="console")
            ref = f"{w[0]}{ident}"
            return [f"{w} #{ident} at {a:#x} ({label}); attach a script: hook {ref} FILE.py"]
        # a lookup
        q = args[0]
        if S.is_symbol(q):
            a = S.addr(q)
            t, s = S.find_symbol(*_expr_name(q))
            return [f"{q} = {a:#010x}" + (f"  ({t.name} {t.file_addr(s)} {s.kind}"
                                         + (f" size {s.size}" if s.size else "") + (f" {s.type}" if s.type else "") + ")"
                                         if s else "")]
        a = parse_number(q)
        nm = S.name_of(a, with_table=True)
        return [f"{a:#010x} = {nm}  (file address {S.file_addr(a)})" if nm else f"{a:#010x}: no symbol covers it"]
    except (SymbolError, ValueError, OSError) as e:
        return [f"error: {e}"]
    except Exception as e:  # noqa: BLE001 - RpcError etc.: one line, not a dead worker
        return [f"error: {type(e).__name__}: {e}"]
