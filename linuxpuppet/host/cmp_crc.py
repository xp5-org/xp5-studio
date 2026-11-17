#!/usr/bin/env python3
"""Compare "sector <lba> crc32 <hex>" lines in a puppet run's output with the same sectors of a disk image.
usage: cmp_crc.py <disk.img> <run.json> <lba> [<lba> ...]      exit 0 when every lba matches"""
import json, re, sys, zlib

img, runjson, lbas = sys.argv[1], sys.argv[2], [int(a) for a in sys.argv[3:]]
out = json.load(open(runjson))["run"]["out"]
failed = 0
with open(img, "rb") as f:
    for lba in lbas:
        f.seek(lba * 512)
        want = zlib.crc32(f.read(512))
        m = re.search(r"sector %d crc32 ([0-9a-f]+)" % lba, out)
        got = int(m.group(1), 16) if m else None
        print("lba", lba, "guest", m.group(1) if m else None, "image", hex(want))
        if got != want:
            failed += 1
sys.exit(1 if failed else 0)
