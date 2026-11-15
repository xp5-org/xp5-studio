Symbol fixtures for tests/test_symbols.py.

Real tool output (Open Watcom 2.0, `wcl` / `wcl386` / `wasm` + `wlink`, and GCC + GNU ld / nm):
  game.c -> game16.exe/.map (DOS small model), game32.exe/.map (DOS/4G LE), gament.exe/.map (Win32 PE)
  memloop.asm -> memloop.com/.map  (MEMLOOP.COM, byte for byte what prereqs/memwrite_src/mkcom.py writes)
  gnu.map, gnu.nm                  (gcc -no-pie -Wl,-Map of the same program, nm -n)

Hand-written in the documented layouts (no such toolchain here): ms16.map (MS LINK 16-bit),
ms32.map (MS LINK 32-bit with Rva+Base), bc.map (Borland TLINK), ghidra16.txt (Ghidra
ImportSymbolsScript lines), ghidra_stock.json (Ghidra ExportFunctionInfoScript output).

game16.json has the shape rmmgui/ghidra/ExportRmmSymbols.py writes (image base 1000:0000,
function byte patterns, a `player` struct), generated from game16.map + game16.exe, not by Ghidra.
