#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Assembles the one-click release folder and its .zip / .7z (stdlib, plus py7zr or a 7z binary for .7z).

    make_bundle.py --dist <nuitka .dist dir> --binary <86Box> --roms <roms dir> --out <dir>
                   [--platform linux|windows|macos] [--image <floppy.img> ...]

The folder (xp5-studio-<platform>/) holds `xp5-studio` (double-click: an empty studio), the 86Box
build beside it, roms/, images/ and projects/ (the demo projects, rewritten to find 86Box, roms/ and
images/ through {appdir}); the frozen program's libraries sit in the same folder.
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HERE, "rmm_demo", "projects")


def project(src, dst, images):
    """A demo project that finds the bundle's 86Box/roms/images wherever the folder is unpacked."""
    p = json.load(open(src))
    g = p["group"]
    for n in g["nodes"].values():
        L = n.get("launch") or {}
        L.pop("binary", None)
        L.pop("roms", None)
        for sect in (n.get("box_cfg") or {}).values():
            for k, v in sect.items():
                if isinstance(v, str) and os.path.basename(v) in images:
                    sect[k] = "{appdir}/images/" + os.path.basename(v)
    (g.get("vars") or {}).pop("binary", None)
    json.dump(p, open(dst, "w"), indent=1)


def readme(plat, exe):
    return (f"xp5-studio ({plat})\n\nDouble-click {exe}: it opens an empty studio (nothing to install; no Python needed).\n"
            "File > Open project... and pick one from projects/, e.g. conwaybench.rmmproj.json.\n"
            "86Box (with the snapshot/rmm patch) and roms/ sit beside it; the studio finds them itself.\n")


def archive_zip(folder, out):
    base = os.path.dirname(folder)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(root, f)
                zi = zipfile.ZipInfo.from_file(full, os.path.relpath(full, base))
                zi.date_time = (2000, 1, 1, 0, 0, 0)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as fh:
                    z.writestr(zi, fh.read())


def archive_7z(folder, out):
    if os.path.exists(out):
        os.unlink(out)
    for exe in ("7z", "7za", "7zr"):
        if shutil.which(exe):
            subprocess.run([exe, "a", "-bd", out, os.path.basename(folder)], cwd=os.path.dirname(folder), check=True,
                           stdout=subprocess.DEVNULL)
            return
    import py7zr
    with py7zr.SevenZipFile(out, "w") as z:
        z.writeall(folder, os.path.basename(folder))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dist", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--roms", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--platform", default="linux")
    ap.add_argument("--image", action="append", default=[])
    a = ap.parse_args(argv)
    win = a.platform == "windows"
    mac = a.platform == "macos"
    name = f"xp5-studio-{a.platform}"
    folder = os.path.join(a.out, name)
    shutil.rmtree(folder, ignore_errors=True)
    if mac:       # --dist is the xp5_studio.app pyside6-deploy left; the bundle folder holds it as xp5-studio.app
        os.makedirs(folder)
        shutil.copytree(a.dist, os.path.join(folder, "xp5-studio.app"), symlinks=True)
        exe = "xp5-studio.app"
    else:
        shutil.copytree(a.dist, folder, symlinks=True)
        exe = "xp5-studio.exe" if win else "xp5-studio"
    for cand in () if mac else ("xp5_studio.exe", "xp5_studio.bin"):
        if os.path.exists(os.path.join(folder, cand)):
            os.rename(os.path.join(folder, cand), os.path.join(folder, exe))
            break
    else:
        if not mac:
            sys.exit(f"{a.dist}: no xp5_studio.exe/.bin in it")
    if mac:       # --binary is the 86Box.app, already self-contained (build_bundle_macos.sh ran macdeployqt on it)
        shutil.copytree(a.binary, os.path.join(folder, "86Box.app"), symlinks=True)
    else:
        shutil.copy2(a.binary, os.path.join(folder, "86Box.exe" if win else "86Box"))
    shutil.copytree(a.roms, os.path.join(folder, "roms"), symlinks=True)
    os.makedirs(os.path.join(folder, "images"))
    names = []
    for im in a.image:
        shutil.copy2(im, os.path.join(folder, "images"))
        names.append(os.path.basename(im))
    os.makedirs(os.path.join(folder, "projects"))
    for f in sorted(os.listdir(PROJECTS)):
        if f.endswith(".rmmproj.json"):
            project(os.path.join(PROJECTS, f), os.path.join(folder, "projects", f), names)
    with open(os.path.join(folder, "README.txt"), "w") as fh:
        fh.write(readme(a.platform, exe))
    if not win:
        for f in () if mac else (exe, "86Box"):
            p = os.path.join(folder, f)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    archive_zip(folder, folder + ".zip")
    archive_7z(folder, folder + ".7z")
    print(folder)


if __name__ == "__main__":
    main()
