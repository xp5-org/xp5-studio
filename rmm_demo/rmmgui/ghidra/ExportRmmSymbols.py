# SPDX-License-Identifier: MIT
# Export this program's functions, labels, typed data and struct layouts to JSON for xp5-studio's
# `sym load` (rmmgui/symbols.py), with each function's first bytes as a pattern so `sym find`
# can locate the image in guest memory. Relocated bytes are written as ?? .
#
# GUI:       Script Manager -> ExportRmmSymbols.py -> pick the output file
# Headless:  analyzeHeadless PROJ_DIR PROJ -import GAME.EXE -postScript ExportRmmSymbols.py /out/game.json
#            analyzeHeadless PROJ_DIR PROJ -process GAME.EXE -noanalysis -postScript ExportRmmSymbols.py /out/game.json
# Then, in the studio console:  sym load /out/game.json   (then: sym find, or psp=XXXX / base=X)
#
# Runs under Jython 2.7 (Ghidra's default) and PyGhidra (Python 3): no f-strings, nothing beyond
# GhidraScript's flat API and ghidra.program.model.
#
# @category Export
# @menupath Tools.Export RMM symbols (JSON)
# @runtime Jython

from __future__ import print_function

import json

from ghidra.program.model.data import Structure, TypeDef
from ghidra.program.model.symbol import SourceType, SymbolType

SIG_LEN = 16
GENERATOR = "ExportRmmSymbols.py"


def addr_text(a):
    """1000:0234 for real-mode (segmented) programs, 00401234 for flat ones."""
    return a.toString(False)


def masked_bytes(prog, start, n, reloc_addrs):
    out = []
    mem = prog.getMemory()
    for i in range(n):
        a = start.add(i)
        if not mem.contains(a):
            break
        if a in reloc_addrs:
            out.append("??")
            continue
        try:
            out.append("%02X" % (mem.getByte(a) & 0xFF))
        except Exception:  # uninitialized memory: the pattern stops here
            break
    return " ".join(out)


def relocated_addresses(prog):
    """Every byte a relocation touches (MZ segment fixups, PE base relocations, LE fixups)."""
    out = set()
    it = prog.getRelocationTable().getRelocations()
    while it.hasNext():
        r = it.next()
        try:
            n = r.getLength()
        except Exception:  # older Ghidra: no getLength(); its original bytes give the size
            b = r.getBytes()
            n = len(b) if b is not None else 4
        for i in range(max(n, 1)):
            out.add(r.getAddress().add(i))
    return out


def struct_of(dt):
    while isinstance(dt, TypeDef):
        dt = dt.getBaseDataType()
    return dt if isinstance(dt, Structure) else None


def export(prog, path):
    relocs = relocated_addresses(prog)
    fm = prog.getFunctionManager()
    funcs = []
    for f in fm.getFunctions(True):
        if f.isExternal():
            continue
        e = f.getEntryPoint()
        funcs.append({
            "name": f.getName(True) if f.getParentNamespace() and not f.getParentNamespace().isGlobal() else f.getName(),
            "entry": addr_text(e),
            "size": int(f.getBody().getNumAddresses()),
            "signature": f.getSignature().getPrototypeString(),
            "thunk": bool(f.isThunk()),
            "bytes": masked_bytes(prog, e, SIG_LEN, relocs),
        })
    func_entries = set(x["entry"] for x in funcs)

    data, types = [], {}
    listing = prog.getListing()
    for d in listing.getDefinedData(True):
        sym = d.getPrimarySymbol()
        if sym is None or sym.getSource() == SourceType.DEFAULT:
            continue                                     # DAT_xxxx: no name worth exporting
        dt = d.getDataType()
        data.append({"name": sym.getName(), "address": addr_text(d.getAddress()), "size": int(d.getLength()),
                     "datatype": dt.getName()})
        st = struct_of(dt)
        if st is not None and dt.getName() not in types:
            fields = []
            for c in st.getDefinedComponents():
                fields.append({"name": c.getFieldName() or ("field_0x%x" % c.getOffset()),
                               "offset": int(c.getOffset()), "size": int(c.getLength()),
                               "type": c.getDataType().getName()})
            types[dt.getName()] = {"size": int(st.getLength()), "fields": fields}

    labels = []
    data_addrs = set(x["address"] for x in data)
    for s in prog.getSymbolTable().getAllSymbols(True):
        if s.getSymbolType() != SymbolType.LABEL or s.getSource() == SourceType.DEFAULT or s.isExternal():
            continue
        a = addr_text(s.getAddress())
        if a in func_entries or a in data_addrs:
            continue
        labels.append({"name": s.getName(), "address": a})

    memory = []
    for b in prog.getMemory().getBlocks():
        memory.append({"name": b.getName(), "start": addr_text(b.getStart()), "end": addr_text(b.getEnd()),
                       "r": bool(b.isRead()), "w": bool(b.isWrite()), "x": bool(b.isExecute()),
                       "initialized": bool(b.isInitialized())})

    out = {
        "generator": GENERATOR,
        "program": {
            "name": prog.getName(),
            "executable_format": prog.getExecutableFormat(),
            "executable_path": prog.getExecutablePath(),
            "md5": prog.getExecutableMD5(),
            "language": str(prog.getLanguageID()),
            "image_base": addr_text(prog.getImageBase()),
        },
        "memory": memory,
        "functions": funcs,
        "labels": labels,
        "data": data,
        "types": types,
    }
    with open(path, "w") as fp:
        json.dump(out, fp, indent=1)
    return len(funcs), len(labels), len(data), len(types)


def main():
    args = getScriptArgs()  # noqa: F821 - GhidraScript flat API
    if args:
        path = args[0]
    else:
        path = askFile("RMM symbols JSON", "Export").getAbsolutePath()  # noqa: F821
    n = export(currentProgram, path)  # noqa: F821
    print("ExportRmmSymbols: %d functions, %d labels, %d data, %d struct types -> %s" % (n + (path,)))


main()
