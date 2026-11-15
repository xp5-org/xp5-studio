#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the deployable set, stdlib only (no setuptools/pip needed, no network):

    make_dist.py OUT_DIR

    OUT_DIR/rmm_86box-<ver>-py3-none-any.whl   the library, pip-installable, also importable
                                               straight from the file (PYTHONPATH=<whl>)
    OUT_DIR/rmm_demo.pyz                       the Qt (PySide6) demo + the library in one file:
                                               /opt/venv/bin/python rmm_demo.pyz --port N
    OUT_DIR/rmm_demo/                          the same, unpacked (rmm_demo.py, rmmgui/, rmm_86box/,
                                               domains/, groups/)

Both bundle rmm_86box/_vendor/rmm_mock.py and rmm_registry.py (owned by this repo), so --mock works
anywhere. Archive members are written in sorted order with a fixed timestamp, so the same
sources give the same bytes (and the same md5 in the tool registry).
"""

import base64
import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "rmm_86box")
DEMO = os.path.join(HERE, "rmm_demo")
STAMP = (2026, 1, 1, 0, 0, 0)


def version():
    for line in open(os.path.join(PKG, "__init__.py")):
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"')
    raise SystemExit("no __version__ in rmm_86box/__init__.py")


def package_files():
    """(archive name, source path) for the library, vendored mock included."""
    out = []
    for dirpath, dirs, files in os.walk(PKG):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f.endswith(".py"):
                src = os.path.join(dirpath, f)
                out.append((os.path.relpath(src, HERE).replace(os.sep, "/"), src))
    return sorted(out)


def put(z, name, data):
    zi = zipfile.ZipInfo(name, STAMP)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    z.writestr(zi, data)


def wheel(out):
    ver = version()
    dist = f"rmm_86box-{ver}.dist-info"
    files = [(n, open(s, "rb").read()) for n, s in package_files()]
    files.append((f"{dist}/METADATA", (
        "Metadata-Version: 2.1\nName: rmm-86box\nVersion: " + ver + "\n"
        "Summary: Client for 86Box's remote machine monitor (RMM)\nRequires-Python: >=3.8\n"
        "Description-Content-Type: text/markdown\n\n" + open(os.path.join(HERE, "README.md")).read()).encode()))
    files.append((f"{dist}/WHEEL", b"Wheel-Version: 1.0\nGenerator: make_dist.py\nRoot-Is-Purelib: true\nTag: py3-none-any\n"))
    files.append((f"{dist}/entry_points.txt", b"[console_scripts]\nrmm-86box = rmm_86box.__main__:main\nrmmctl = rmm_86box.ctl:main\n"))
    record = []
    for n, data in files:
        h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{n},sha256={h},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    files.append((f"{dist}/RECORD", ("\n".join(record) + "\n").encode()))
    path = os.path.join(out, f"rmm_86box-{ver}-py3-none-any.whl")
    with zipfile.ZipFile(path, "w") as z:
        for n, data in files:
            put(z, n, data)
    return path


def demo_files():
    out = [(n, s) for n, s in package_files()]
    out.append(("rmm_demo.py", os.path.join(DEMO, "rmm_demo.py")))
    gui = os.path.join(DEMO, "rmmgui")
    for root, dirs, names in os.walk(gui):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in names:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                out.append(("rmmgui/" + os.path.relpath(p, gui).replace(os.sep, "/"), p))
    for sub in ("domains", "groups"):
        for f in sorted(os.listdir(os.path.join(DEMO, sub))):
            if f.endswith(".json"):
                out.append((f"{sub}/{f}", os.path.join(DEMO, sub, f)))
    return sorted(out)


def pyz(out):
    path = os.path.join(out, "rmm_demo.pyz")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"#!/opt/venv/bin/python\n")         # where PySide6 lives on the runner hosts
        with zipfile.ZipFile(f, "w") as z:
            for n, s in demo_files():
                put(z, n, open(s, "rb").read())
            put(z, "__main__.py", b"import sys\nfrom rmm_demo import main\nsys.exit(main())\n")
    try:
        os.chmod(tmp, 0o755)                      # convenience only; run it with the venv python
    except OSError:
        pass                                      # e.g. NFS from a host with another uid mapping
    os.replace(tmp, path)
    return path


def unpacked(out):
    d = os.path.join(out, "rmm_demo")
    shutil.rmtree(d, ignore_errors=True)
    for n, s in demo_files():
        dst = os.path.join(d, n)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(s, dst)
    return d


def main(argv):
    if len(argv) != 1:
        print(__doc__)
        return 2
    out = os.path.abspath(argv[0])
    os.makedirs(out, exist_ok=True)
    for p in (wheel(out), pyz(out), unpacked(out)):
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
