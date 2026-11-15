# SPDX-License-Identifier: MIT
"""rmmgui.symbols: every map / listing / JSON shape in tests/data/symbols loads; tables are placed by
seg= / psp= / anchors / exe= / byte search; then the `sym` word and the shipped hook examples on the
reference mock (its MEMLOOP guest is tests/data/symbols/memloop.com, with a real Watcom map).

    /opt/venv/bin/python -m unittest tests.test_symbols      (from rmm_demo/)
"""

import os
import random
import shutil
import struct
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "symbols")
for p in (os.path.join(HERE, ".."), os.path.join(HERE, "..", "..")):
    sys.path.insert(0, os.path.abspath(p))

try:                                                          # its path setup finds this copy's library
    from test_rmmgui import LOOP_BP, MockGroup, wait_for  # noqa: E402
except ImportError:                                           # run as tests.test_symbols
    from tests.test_rmmgui import LOOP_BP, MockGroup, wait_for  # noqa: E402
from rmmgui import symbols as Y  # noqa: E402
from rmmgui.symbols import SymbolError, Symbols  # noqa: E402


def f(name):
    return os.path.join(DATA, name)


def slurp(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def spit(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class FakeMem:
    """Guest memory for find/verify without a VM: read(addr, n) like Rmm.read_mem(linear=True)."""

    def __init__(self, size=0x800000):
        self.ram = bytearray(size)

    def put(self, addr, data):
        self.ram[addr:addr + len(data)] = data

    def read(self, addr, n):
        return bytes(self.ram[addr:addr + n])


def load_mz(mem, path, load_seg):
    """What DOS does with an .EXE: copy the image to load_seg:0000 and add load_seg to every
    relocated segment word."""
    d = slurp(path, "rb")
    nrel, hdr, reloff = struct.unpack_from("<H", d, 6)[0], struct.unpack_from("<H", d, 8)[0] * 16, \
        struct.unpack_from("<H", d, 0x18)[0]
    img = bytearray(d[hdr:])
    for i in range(nrel):
        off, seg = struct.unpack_from("<HH", d, reloff + i * 4)
        a = seg * 16 + off
        struct.pack_into("<H", img, a, (struct.unpack_from("<H", img, a)[0] + load_seg) & 0xFFFF)
    mem.put(load_seg * 16, img)


class Formats(unittest.TestCase):
    def t(self, name, **kw):
        return Symbols().load(f(name), **kw)

    def test_watcom_dos16(self):
        t = self.t("game16.map")
        self.assertEqual((t.mode, t.fmt), ("seg", "Open Watcom map"))
        hurt, hero = t.lookup("hurt"), t.lookup("hero")             # hurt_ / _hero: decoration ignored
        self.assertEqual((hurt.name, hurt.seg, hurt.off, hurt.kind), ("hurt_", 0, 8, "func"))
        self.assertEqual((hero.name, hero.seg, hero.off, hero.kind), ("_hero", 0x46, 0x22, "data"))
        self.assertFalse(t.placed)

    def test_watcom_dos4g_sections(self):
        t = self.t("game32.map", exe=f("game32.exe"))
        self.assertEqual(t.mode, "sections")
        self.assertEqual(t.exe.kind, "LE")
        hurt = t.lookup("hurt_")
        self.assertEqual((hurt.seg, hurt.off, hurt.kind), (1, 0x10, "func"))
        pat = t.pattern(hurt)                                       # sub [abs32], eax: the fixup is masked
        self.assertEqual(Y.fmt_pattern(pat[10:16]), "29 05 ?? ?? ?? ??")
        self.assertEqual(t.file_addr(hurt), "0001:00000010")

    def test_watcom_pe_flat(self):
        t = self.t("gament.map", exe=f("gament.exe"))
        self.assertEqual((t.mode, t.image_base, t.placed), ("flat", 0x400000, True))
        self.assertEqual(t.lin(t.lookup("hurt_")), 0x401010)
        self.assertEqual(Y.fmt_pattern(t.pattern(t.lookup("hurt_"))[10:16]), "29 05 ?? ?? ?? ??")   # .reloc

    def test_ms_link_borland_gnu_nm_ghidra(self):
        ms16 = self.t("ms16.map")
        self.assertEqual({s.name: (s.seg, s.off, s.kind) for s in ms16.syms},
                         {"_hero": (0x17, 0x42, "data"), "_hurt": (0, 0x10, "func"), "_main": (0, 0x34, "func")})
        ms32 = self.t("ms32.map")
        self.assertEqual((ms32.mode, ms32.image_base), ("flat", 0x400000))
        self.assertEqual({s.name: s.off for s in ms32.syms},
                         {"_hurt": 0x401000, "_main": 0x401020, "_hero": 0x403000, "_helper": 0x401100})
        bc = self.t("bc.map")
        self.assertEqual({s.name: (s.seg, s.off) for s in bc.syms},
                         {"_main": (0, 0x291), "_hurt": (0, 0x250), "_hero": (0x123, 0x94)})
        gnu = self.t("gnu.map")
        self.assertEqual((gnu.fmt, gnu.lin(gnu.lookup("hurt"))), ("GNU ld map", 0x401106))
        nm = self.t("gnu.nm")
        self.assertEqual((nm.lookup("hurt").kind, nm.lookup("hero").kind), ("func", "data"))
        g16 = self.t("ghidra16.txt")
        self.assertEqual((g16.mode, g16.origin), ("seg", 0x1000))  # Ghidra puts an MZ at 1000:0000
        stock = self.t("ghidra_stock.json")
        self.assertEqual([(s.name, s.off, s.kind) for s in stock.syms], [("hurt", 0x401000, "func"),
                                                                         ("main", 0x401020, "func")])

    def test_ghidra_export_json_structs(self):
        t = self.t("game16.json")
        self.assertEqual((t.mode, t.origin), ("seg", 0x1000))
        self.assertEqual(t.lookup("hero.health").off, t.lookup("hero").off + 4)
        self.assertEqual(t.lookup("hurt_").size, 0x11)

    def test_not_symbols(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "x.txt")
        spit(p, "hello\nworld\n")
        with self.assertRaises(SymbolError):
            Symbols().load(p)


class Placement(unittest.TestCase):
    def test_psp_seg_anchor_agree_and_map_matches_ghidra(self):
        S = Symbols()
        m = S.load(f("game16.map"), psp=0x1F00)                     # .EXE image at PSP + 10h
        self.assertEqual(m.lin(m.lookup("hurt_")), 0x1F10 * 16 + 8)
        j = S.load(f("game16.json"), name="gh", psp=0x1F00)
        self.assertEqual(j.lin(j.lookup("hurt_")), 0x1F10 * 16 + 8)   # origin 1000 vs 0: same place
        self.assertEqual(j.lin(j.lookup("hero.health")), m.lin(m.lookup("_hero")) + 4)
        a = Symbols().load(f("game16.map"))
        a.anchor("main_", 0x1F10 * 16 + 0x19)
        self.assertEqual(a.base, 0x1F10)
        with self.assertRaises(SymbolError):
            a.anchor("main_", 0x1F10 * 16 + 0x1A + 0x10 * 3 + 1)      # not paragraph-alignable
        self.assertEqual(S.name_of(0x1F10 * 16 + 8 + 3, with_table=True), "game16!hurt_+0x3")
        self.assertEqual(j.file_addr(0x1F10 * 16 + 8 + 3), "1000:000B")
        self.assertEqual(m.file_addr(0x1F10 * 16 + 8 + 3), "0000:000B")

    def test_find_mz_by_exe_and_by_ghidra_patterns(self):
        mem = FakeMem()
        load_mz(mem, f("game16.exe"), 0x2345)
        m = Symbols().load(f("game16.map"), exe=f("game16.exe"))
        m.find(mem.read, 0, 0xA0000)
        self.assertEqual(m.base, 0x2345)
        self.assertEqual(m.verify(mem.read)[1], 0)
        j = Symbols().load(f("game16.json"))
        j.find(mem.read, 0, 0xA0000)
        self.assertEqual(j.lin(j.lookup("hurt_")), 0x2345 * 16 + 8)

    def test_find_le_object_with_fixups_applied(self):
        t = Symbols().load(f("game32.map"), exe=f("game32.exe"))
        mem = FakeMem()
        _rb, vsize, pages, _p = t.exe.objects[0]
        img = bytearray(t.exe.data[pages[0]:pages[0] + vsize])
        rnd = random.Random(7)
        for (obj, off) in t.exe.masked:                              # DOS/4GW patched these
            if obj == 1 and off < len(img):
                img[off] = rnd.randrange(256)
        mem.put(0x123000, img)
        t.find(mem.read, 0x100000, 0x200000)
        self.assertEqual(t.sec_base[1], 0x123000)
        self.assertEqual(t.lin(t.lookup("main_")), 0x123000 + 0x27)

    def test_parse_names_numbers_and_errors(self):
        S = Symbols()
        S.load(f("game16.map"), psp=0x1F00)
        self.assertEqual(S.parse("0x810F"), 0x810F)
        self.assertEqual(S.parse("810F"), 0x810F)
        self.assertEqual(S.parse("0800:010F"), 0x810F)
        self.assertEqual(S.parse("hurt_"), 0x1F108)
        self.assertEqual(S.parse("game16!hurt_+4"), 0x1F10C)
        self.assertEqual(S.parse("beef"), 0xBEEF)                     # hex when no symbol has the name
        with self.assertRaises(SymbolError):
            S.parse("zork")
        un = Symbols()
        un.load(f("game16.map"))
        with self.assertRaisesRegex(SymbolError, "not placed"):
            un.addr("hurt_")


# -- on the reference mock -------------------------------------------------------------------------



class OnMock(MockGroup):
    def word(self, n, mod, *words):
        out = []
        n.put_call(lambda: out.append(mod.console(n, list(words))))
        self.assertTrue(wait_for(lambda: out), self.lines)
        return out[0]

    def sym(self, n, *words):
        return self.word(n, Y, *words)

    def hook(self, n, *words):
        from rmmgui import hooks
        return self.word(n, hooks, *words)

    def tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_sym_load_find_lookup_break(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        n.rmm.write_mem(0x0800 * 16 + 0x100, slurp(f("memloop.com"), "rb"))   # what DOS loaded at PSP 0800
        n.rmm.write_mem(0x0500 * 16 + 0x100, slurp(f("memloop.com"), "rb"))   # a stale copy below it (a DOS
                                                                              # buffer: real 86Box had one)
        out = self.sym(n, "load", f("memloop.map"), f"exe={f('memloop.com')}")
        self.assertIn("memloop", out[0])
        self.assertIn("not placed", out[1])
        out = self.sym(n, "find")
        self.assertIn("load segment 0800", out[0])
        self.assertIn("5 functions match, 0 differ", out[0])
        self.assertIn("2 copies at 0500, 0800; chose the one cs:eip is in", out[1])
        self.assertEqual(self.sym(n, "memloop")[0].split("=")[1].split()[0], hex(LOOP_BP).replace("0x", "0x0000"))
        self.assertIn("= memloop!memloop_jmp", self.sym(n, "8113")[0])
        self.assertIn("file address 0000:0113", self.sym(n, "8113")[0])
        out = self.sym(n, "break", "memloop")
        self.assertIn(f"at {LOOP_BP:#x} (memloop)", out[0])
        bp = int(out[0].split("#")[1].split()[0])
        self.assertEqual(n.hit_rules[("break", bp)]["label"], "memloop")
        d = self.tmp()
        path = os.path.join(d, "where.py")
        spit(path, textwrap.dedent("""
            def on_hit(hit):
                hit.state["where"] = (hit.sym(), hit.file_addr(), hit.pc(), hit.addr_of("memloop_jmp"))
                return "stay"
        """))
        self.assertIn("armed", self.hook(n, f"b{bp}", path)[0])
        h = n.hit_rules[("break", bp)]["hook"]
        n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: "where" in h.state), self.lines)
        self.assertEqual(h.state["where"], ("memloop", "0000:010F", LOOP_BP, LOOP_BP + 4))
        self.assertIn("load segment 0800", self.sym(n)[0])
        self.assertIn("error", self.sym(n, "nosuch")[0])

    def test_who_writes_example_on_a_watch(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        d = self.tmp()
        self.sym(n, "load", f("memloop.map"), "seg=0x0800")             # a .COM: load segment = PSP
        bda = os.path.join(d, "bda.txt")
        spit(bda, "counter 000004F0 l\n")                    # Ghidra ImportSymbols line
        self.sym(n, "load", bda)
        out = self.sym(n, "watch", "counter", "2", "w")
        self.assertIn("watch #", out[0])
        wid = int(out[0].split("#")[1].split()[0])
        path = os.path.join(d, "ww.py")
        self.assertIn("from who_writes", self.hook(n, "new", path, f"w{wid}", "from=who_writes")[0])
        tally = os.path.join(d, "writers.txt")
        self.assertIn("armed", self.hook(n, f"w{wid}", path, "every=3", f"out={tally}")[0])
        h = n.hit_rules[("watch", wid)]["hook"]
        if n.rmm.state() == "paused":
            n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: h.runs >= 4), self.lines)
        self.assertEqual(h.errors, 0, h.last_error)
        self.assertEqual(list(h.state["writers"]), ["memloop!memloop"])   # pc-1: inside the inc, not the jmp
        self.assertEqual(h.state["writers"]["memloop!memloop"]["file"], "0000:0112")
        self.hook(n, "off", f"w{wid}")
        self.assertTrue(wait_for(lambda: os.path.exists(tally)), self.lines)
        self.assertTrue(slurp(tally).startswith("0000:0112  memloop!memloop  "))
        self.assertTrue(any("new writer memloop!memloop+0x3" in t for _, _, t in self.lines), self.lines)

    def test_crash_backtrace_example_stays_paused(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        self.sym(n, "load", f("memloop.map"), "seg=0x0800")
        bp = int(self.sym(n, "break", "memloop_jmp")[0].split("#")[1].split()[0])
        d = self.tmp()
        path = os.path.join(d, "bt.py")
        self.hook(n, "new", path, f"b{bp}", "from=crash_backtrace")
        log = os.path.join(d, "crash.txt")
        self.hook(n, f"b{bp}", path, f"log={log}", "snap=crash")
        h = n.hit_rules[("break", bp)]["hook"]
        if n.rmm.state() == "paused":
            n.rmm.call("machine.resume")
        self.assertTrue(wait_for(lambda: h.state.get("reports")), self.lines)
        self.assertEqual(h.errors, 0, h.last_error)
        self.assertIn("stayed paused", h.last)
        text = slurp(log)
        self.assertIn("stopped in memloop_jmp  (file 0000:0113)", text)
        self.assertIn("real mode", text)
        tags = [s["tag"] for s in n.rmm.call("snapshot.list")["snapshots"]]
        self.assertTrue(any(t.startswith("crash") for t in tags), tags)

    def test_ghidra_script_written_out(self):
        g = self.make({"a": {"mock": True}})
        n = g.nodes["a"]
        d = self.tmp()
        out = self.sym(n, "ghidra-script", d)
        self.assertIn("ExportRmmSymbols.py", out[0])
        src = slurp(os.path.join(d, "ExportRmmSymbols.py"))
        compile(src, "ExportRmmSymbols.py", "exec")
        self.assertIn("getRelocationTable", src)


if __name__ == "__main__":
    unittest.main()
