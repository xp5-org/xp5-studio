#!/usr/bin/env python3
"""Make the puppet's test disk: raw image, every sector a function of its LBA (the personalities compute the same bytes).
usage: mkdisk.py <out.img> [cyl hpc spt]   default 16 cylinders, 16 heads, 63 sectors/track (16128 sectors, 8257536 bytes)"""
import sys, struct

def sector(lba):
    out = bytearray(512)
    for i in range(512):
        x = (lba * 2654435761 + i * 40503) & 0xFFFFFFFF
        out[i] = ((x ^ (x >> 13)) >> 5) & 0xFF
    return bytes(out)

if __name__ == "__main__":
    path = sys.argv[1]
    cyl, hpc, spt = (int(a) for a in sys.argv[2:5]) if len(sys.argv) >= 5 else (16, 16, 63)
    with open(path, "wb") as f:
        for lba in range(cyl * hpc * spt):
            f.write(sector(lba))
    print(path, cyl, hpc, spt, cyl * hpc * spt, "sectors")
