# SPDX-License-Identifier: MIT
"""Project bundles (rmm-bundle/1): one zip with the whole project, snapshots included, so it can be
carried to another host and opened there. No Qt here.

A project file (app.save_project) keeps the tree and the paths of snapshot files; a bundle keeps
the bytes. Each node contributes what holds its snapshots:

  86Box   its VM dir (cfg, NVR, disk and floppy images) and, per kept snapshot, a snapshot.export
          file (`snaps/<node>/`). Import launches the node on the unpacked VM dir and
          snapshot.load's every file (restore=false), so snapshot.list shows them again.
  QEMU    the qcow2 chain of every disk the guest has open (query-block), as it is: the snapshots
          are internal to those files. Import rebases overlays onto the unpacked backing files and
          starts the QEMU from its recorded command line (`qemu_argv`), found from /proc when the
          export runs on the QEMU's host.

Two scopes: "full" keeps every snapshot still live; "latest" keeps only the newest live one (the
tree shrinks to that record and, on QEMU, the other tags are deleted from the copies, never from
the running guest's disks).
"""

import collections
import contextlib
import copy
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile

from rmm_86box import RpcError

from . import snapdelta
from .snaps import GONE, OK

SCHEMA = "rmm-bundle/1"
MODES = ("full", "latest")
LOCAL = ("127.0.0.1", "localhost", "::1")
SKIP_VM = re.compile(r"(\.log|\.tmp|\.lock)$")
LOAD_WAIT = 90.0


class BundleError(ValueError):
    pass


class Progress:
    """What the running export/import is doing, for the Summary page (app.bundle). Written by the
    worker thread, read by the GUI tick: plain attributes only."""

    def __init__(self, op, path, mode="", say=None):
        self.op, self.path, self.mode = op, path, mode
        self.phase, self.detail, self.state = "starting", "", "running"      # running | done | failed
        self.t0, self.t1 = time.time(), None
        self.log = collections.deque(maxlen=14)
        self.out_file = None                     # growing file whose size is the progress (the .zip.tmp)
        self.warnings = 0
        self._say = say or (lambda text, tone="text": None)

    def say(self, text, tone="text"):
        self.log.append((time.strftime("%H:%M:%S"), text, tone))
        self._say(text, tone)

    def step(self, phase, detail=""):
        self.phase, self.detail = phase, detail
        self.say(phase + (f": {detail}" if detail else ""), "muted")

    def end(self, state, detail=""):
        self.state, self.detail, self.t1 = state, detail, time.time()

    def written(self):
        with contextlib.suppress(OSError, TypeError):
            return os.path.getsize(self.out_file)
        return 0

    def elapsed(self):
        return (self.t1 or time.time()) - self.t0


# -- which snapshots go in --------------------------------------------------------------------------

def select(tree_json, mode):
    """(tree_json to keep, {record id: [nodes that hold it]}). `latest` is the newest record
    any node still holds (status ok); its tree is that one record, started from nothing."""
    if mode not in MODES:
        raise BundleError(f"mode {mode!r}: want one of {', '.join(MODES)}")
    recs = [r for r in tree_json.get("snapshots", []) if any(s == OK for s in r.get("status", {}).values())]
    if mode == "latest":
        recs = sorted(recs, key=lambda r: r["id"])[-1:]
    if not recs:
        raise BundleError("no snapshot is live on any node: take one first")
    keep = {r["id"]: [n for n in r["nodes"] if r["status"].get(n) == OK] for r in recs}
    out = copy.deepcopy(dict(tree_json, snapshots=recs))
    if mode == "latest":
        r = out["snapshots"][0]
        r["parents"] = {n: None for n in r["nodes"]}
        out["heads"] = {n: (r["id"] if n in r["nodes"] else None) for n in tree_json.get("heads", {})}
    else:
        ids = set(keep)
        out["heads"] = {n: (h if h in ids else None) for n, h in tree_json.get("heads", {}).items()}
        for r in out["snapshots"]:
            r["parents"] = {n: (p if p in ids else None) for n, p in r.get("parents", {}).items()}
    return out, keep


# -- QEMU helpers -----------------------------------------------------------------------------------

def qemu_chain(vm):
    """[{path, format, backing}] for every file-backed disk (and its backing files) the guest has open."""
    seen, out = set(), []
    for dev in vm.qmp("query-block") or []:
        img = (dev.get("inserted") or {}).get("image")
        top = True
        while img:
            path = img.get("filename", "")
            if path and not path.startswith("json:") and os.path.isabs(path) and path not in seen:
                seen.add(path)
                out.append({"path": path, "format": img.get("format", "raw"), "top": top,
                            "backing": img.get("full-backing-filename") or img.get("backing-filename")})
            top = False
            img = img.get("backing-image")
    return out


def qemu_argv(port):
    """(argv, cwd) of the local QEMU whose command line names this QMP port, else (None, None)."""
    want = re.compile(rf":{port}(,|$)")
    me = os.getpid()
    if not os.path.isdir("/proc/self"):           # macOS, BSD: ps and lsof (split on spaces: a path with one is lost)
        with contextlib.suppress(OSError):
            for ln in subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout.splitlines():
                pid, _, cmd = ln.strip().partition(" ")
                argv = cmd.split()
                if (pid.isdigit() and int(pid) != me and argv and "qemu" in os.path.basename(argv[0])
                        and any(want.search(a) for a in argv[1:])):
                    cwd = None
                    for l in subprocess.run(["lsof", "-a", "-d", "cwd", "-p", pid, "-Fn"], capture_output=True,
                                            text=True).stdout.splitlines():
                        if l.startswith("n"):
                            cwd = l[1:]
                    return argv, cwd
        return None, None
    with contextlib.suppress(OSError):
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            except OSError:
                continue
            if argv and "qemu" in os.path.basename(argv[0]) and any(want.search(a) for a in argv[1:]):
                cwd = None
                with contextlib.suppress(OSError):
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                return argv, cwd
    return None, None


def qimg(*args, check=True):
    p = subprocess.run(["qemu-img", *args], capture_output=True, text=True)
    if check and p.returncode:
        raise BundleError(f"qemu-img {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def snap_tags(path):
    """Internal snapshot tags of a qcow2 file."""
    out = qimg("snapshot", "-l", "-U", path, check=False).stdout
    tags = []
    for ln in out.splitlines():
        cols = ln.split()
        if len(cols) >= 2 and cols[0].isdigit():
            tags.append(cols[1])
    return tags


def rewrite_argv(argv, mapping):
    """argv with every old disk path swapped for its new one (longest paths first)."""
    olds = sorted(mapping, key=len, reverse=True)
    out = []
    for a in argv:
        for o in olds:
            a = a.replace(o, mapping[o])
        out.append(a)
    return out


# -- export -----------------------------------------------------------------------------------------

def vm_dir_of(node):
    if node.proc:
        return node.proc.vm_dir
    return os.path.dirname(node.rmm.call("config.dump", section="General").get("path", ""))


def export_bundle(app, path, mode="full", say=None):
    """Write the running project to `path` (a .zip). Pauses the nodes while it copies and resumes
    those that were running. Returns the manifest."""
    prog = Progress("export", path, mode, say)
    app.bundle = prog
    say = prog.say
    grp = app.group
    full = grp.tree.to_json()
    tree, keep = select(full, mode)
    dropped = {}
    for r in full["snapshots"]:
        for nd, st in r["status"].items():
            if st == OK:
                dropped.setdefault(nd, set()).add(r["tag"])
    say(f"bundle ({mode}): snapshots {', '.join('#%d' % i for i in sorted(keep))}", "muted")
    nodes = [n for n in grp.nodes.values() if not n.spec.mock]
    was = {}
    for n in nodes:
        if n.connected:
            with contextlib.suppress(RpcError, OSError):
                was[n.name] = n.rmm.state() == "running"
    work = tempfile.mkdtemp(prefix="rmm_bundle_")
    tmp = prog.out_file = path + ".tmp"
    manifest = {"schema": SCHEMA, "mode": mode, "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "project": app.project_data(), "nodes": {}, "warnings": []}
    manifest["project"]["tree"] = tree
    manifest["project"]["files"] = {}
    paused = []

    def resume():
        """Guests run again as soon as nothing more is read from them (not after the zip is closed)."""
        if paused:
            prog.phase = "resuming guests"
            grp._fanout(paused, lambda n: n.resume())
            paused.clear()
            say("guests resumed", "muted")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zf:
            added = {}                                   # real path -> arcname (a shared base goes in once)

            def add(src, arc):
                if src in added:
                    return added[src]
                size = os.path.getsize(src)
                prog.detail = f"{os.path.basename(src)} ({size >> 20} MB)"
                if size > 16 << 20:
                    say(f"  adding {prog.detail}", "muted")
                # disk images are stored as they are (deflating ~2 GB took a minute, saved a third);
                # snapshot files compress ~50x and stay deflated
                big = size > 16 << 20 and not arc.startswith("snaps/")
                zf.write(src, arc, compress_type=zipfile.ZIP_STORED if big else zipfile.ZIP_DEFLATED)
                added[src] = arc
                return arc

            # 1. what the guests never write (backing files) goes in while they still run
            late = []
            prog.phase = "copying disks the guests do not write (guests still running)"
            for n in nodes:
                info = manifest["nodes"][n.name] = {"backend": n.spec.backend, "launched": n.proc is not None}
                warn = info.setdefault("warnings", [])
                if n.spec.backend == "qemu":
                    try:
                        todo = _qemu_prep(n, tree, dropped, work, add, info, warn, say)
                        late.append((n, info, warn, lambda n=n, i=info, w=warn, t=todo: _qemu_rest(n, t, work, add, i, w, say)))
                    except (RpcError, OSError, BundleError) as e:
                        warn.append(f"not bundled: {e}")
                        say(f"[{n.name}] bundle: {e}", "err")
                else:
                    late.append((n, info, warn, lambda n=n, i=info, w=warn: _export_86box(n, tree, keep, work, add, i, w, say)))
            # 2. now stop them: a consistent image of the active disks and the snapshot files
            prog.step("pausing guests")
            paused.extend(n for n in nodes if was.get(n.name))
            grp._fanout(paused, lambda n: n.pause(induced=True))
            for n, info, warn, job in late:
                prog.phase, prog.detail = f"exporting {n.name} (guests paused)", ""
                try:
                    job()
                except (RpcError, OSError, BundleError) as e:
                    warn.append(f"not bundled: {e}")
                    say(f"[{n.name}] bundle: {e}", "err")
            resume()
            for n, info, warn, _ in late:
                manifest["warnings"] += [f"{n.name}: {w}" for w in warn]
            # snap file index of the bundle: record id -> node -> arcname (import rewrites to disk)
            for n, info in manifest["nodes"].items():
                for rid, arc in (info.get("snaps") or {}).items():
                    manifest["project"]["files"].setdefault(rid, {})[n] = arc
            prog.phase, prog.detail = "writing manifest", ""
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        os.replace(tmp, path)
    except BaseException as e:
        prog.end("failed", f"{type(e).__name__}: {e}")
        say(f"bundle export failed: {e}", "err")
        raise
    finally:
        resume()
        shutil.rmtree(work, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    prog.warnings = len(manifest["warnings"])
    prog.end("done", f"{os.path.getsize(path) >> 20} MB")
    say(f"bundle written: {path}" + (f"  ({len(manifest['warnings'])} warning(s))" if manifest["warnings"] else ""),
        "warn" if manifest["warnings"] else "ok")
    return manifest


def _export_86box(n, tree, keep, work, add, info, warn, say):
    if n.spec.host not in LOCAL:
        raise BundleError(f"host {n.spec.host} is not this machine: its files cannot be copied from here")
    vm_dir = vm_dir_of(n)
    if not vm_dir or not os.path.isdir(vm_dir):
        raise BundleError("no VM dir (config.dump gave no cfg path)")
    snaps, packed, fulls, saved = {}, {}, {}, 0         # fulls: rid -> its exported file, kept for the children
    for rid, held in sorted(keep.items()):
        if n.name not in held:
            continue
        rec = next(r for r in tree["snapshots"] if r["id"] == rid)
        drop = f"{vm_dir}/rmm_snaps"
        os.makedirs(drop, exist_ok=True)
        f = f"{drop}/bundle_{rid}_{rec['tag']}.rmmsnap"
        try:
            n.rmm.call("snapshot.export", tag=rec["tag"], path=f, _timeout=120)
        except (RpcError, OSError) as e:
            warn.append(f"#{rid} {rec['tag']}: export failed ({e})")
            rec["status"][n.name] = GONE
            continue
        arc = f"snaps/{n.name}/{os.path.basename(f)}"
        snaps[str(rid)] = arc                       # where import puts the whole file
        fulls[rid] = f
        parent = rec.get("parents", {}).get(n.name)
        if parent in fulls:
            d = _pack_delta(fulls[parent], f, os.path.join(work, f"{n.name}_{rid}.rmmdelta"), warn)
            if d:
                packed[str(rid)] = {"arc": arc[:-len(".rmmsnap")] + ".rmmdelta", "parent": parent}
                add(d, packed[str(rid)]["arc"])
                saved += os.path.getsize(f) - os.path.getsize(d)
                continue
        add(f, arc)
    info["snaps"], info["packed"] = snaps, packed
    info["vm_dir"] = f"vm/{n.name}"
    for root, dirs, files in os.walk(vm_dir):
        dirs[:] = [d for d in dirs if d != "rmm_snaps"]
        for name in files:
            if not SKIP_VM.search(name):
                src = os.path.join(root, name)
                add(src, f"vm/{n.name}/{os.path.relpath(src, vm_dir)}")
    for f in (os.path.join(vm_dir, "rmm_snaps", os.path.basename(p)) for p in snaps.values()):
        with contextlib.suppress(OSError):
            os.unlink(f)
    say(f"[{n.name}] bundled VM dir + {len(snaps)} snapshot file(s)"
        + (f", {len(packed)} as deltas (-{saved >> 20} MB before compression)" if packed else ""), "muted")


def _pack_delta(parent_file, child_file, out, warn):
    """Write the delta of child against parent to `out`; None when it does not pay or does not round-trip."""
    try:
        with open(parent_file, "rb") as f:
            pb = f.read()
        with open(child_file, "rb") as f:
            cb = f.read()
        d = snapdelta.encode(pb, cb)
        if len(d) >= len(cb) or snapdelta.decode(pb, d) != cb:
            return None
    except (snapdelta.DeltaError, OSError, ValueError, KeyError, struct.error) as e:
        warn.append(f"{os.path.basename(child_file)}: kept whole, no delta ({e})")
        return None
    with open(out, "wb") as f:
        f.write(d)
    return out


def _qemu_prep(n, tree, dropped, work, add, info, warn, say):
    """Plan the node's disks and add every file the guest does not write, while it still runs.
    Returns the files left for the paused phase."""
    chain = qemu_chain(n.rmm)
    if not chain:
        raise BundleError("query-block lists no file-backed disk")
    mine = {r["tag"] for r in tree["snapshots"] if n.name in r["nodes"]}
    if not mine:
        warn.append("no kept snapshot lives on this node")
    drop = dropped.get(n.name, set()) - mine        # tree snapshots left out of this bundle
    info["files"], info["tags"] = [], sorted(mine)
    todo = []
    for i, c in enumerate(chain):
        c["arc"] = f"qemu/{n.name}/{i}_{os.path.basename(c['path'])}"
        c["drop"] = drop
        info["files"].append({"orig": c["path"], "arc": c["arc"], "format": c["format"], "backing": c["backing"]})
        if c["top"] or (c["format"] == "qcow2" and drop & set(snap_tags(c["path"]))):
            todo.append(c)
        else:
            add(c["path"], c["arc"])
    return todo


def _qemu_rest(n, todo, work, add, info, warn, say):
    for c in todo:
        src, drop = c["path"], c["drop"]
        if c["format"] == "qcow2":
            chk = qimg("check", "-U", "-f", "qcow2", src, check=False)
            if chk.returncode in (2, 3):
                warn.append(f"{os.path.basename(src)}: qemu-img check reports damage; copy while the guest is "
                            "stopped (quit it) for a clean image")
            if drop & set(snap_tags(src)):
                cp = os.path.join(work, f"{n.name}_{os.path.basename(c['arc'])}")
                shutil.copyfile(src, cp)
                for t in sorted(drop & set(snap_tags(cp))):
                    qimg("snapshot", "-d", t, cp)
                src = cp
        add(src, c["arc"])
    argv, cwd = qemu_argv(n.port) if n.spec.host in LOCAL else (None, None)
    if argv:
        info["argv"], info["cwd"] = argv, cwd
    else:
        warn.append("QEMU command line not found (not local, or already gone): start it by hand on the "
                    "unpacked images")
    say(f"[{n.name}] bundled {len(info['files'])} disk file(s), tags {', '.join(info['tags']) or '-'}", "muted")


# -- import -----------------------------------------------------------------------------------------

def read_manifest(path):
    with zipfile.ZipFile(path) as zf:
        try:
            m = json.loads(zf.read("manifest.json"))
        except KeyError:
            raise BundleError(f"{path}: no manifest.json (not an RMM bundle)") from None
    if m.get("schema") != SCHEMA:
        raise BundleError(f"{path}: schema {m.get('schema')!r}, want {SCHEMA!r}")
    return m


def import_bundle(app, path, dest, say=None):
    """Unpack into `dest` (a new or empty directory), start the project from it and load the
    snapshots back. The nodes are left paused: restore a snapshot to continue."""
    prog = Progress("import", path, "", say)
    app.bundle = prog
    say = prog.say
    m = read_manifest(path)
    if os.path.exists(dest) and os.listdir(dest):
        raise BundleError(f"{dest} is not empty")
    os.makedirs(dest, exist_ok=True)
    dest = os.path.abspath(dest)
    prog.step("unpacking", f"{path} -> {dest}")
    prog.out_file = None
    with zipfile.ZipFile(path) as zf:
        zf.extractall(dest)
    proj = m["project"]
    group = copy.deepcopy(proj["group"])
    nodes = group.get("nodes") or {}
    for name, info in m["nodes"].items():
        body = nodes.get(name)
        if body is None:
            continue
        if info.get("vm_dir") and body.get("launch", True) is not None and _launches(group, name):
            body.setdefault("launch", {})["vm_dir"] = os.path.join(dest, info["vm_dir"])
        elif info.get("vm_dir"):
            say(f"[{name}] was attached, not launched: it stays attach-only (its VM dir is in the bundle "
                f"at {dest}/{info['vm_dir']})", "warn")
        if info.get("files"):
            mapping = {f["orig"]: os.path.join(dest, f["arc"]) for f in info["files"]}
            for f in info["files"]:
                if f["format"] == "qcow2" and f["backing"] and f["backing"] in mapping:
                    qimg("rebase", "-u", "-f", "qcow2", "-b", mapping[f["backing"]], "-F", "qcow2",
                         mapping[f["orig"]])
            if info.get("argv"):
                body["qemu_argv"] = rewrite_argv(info["argv"], mapping)
                if info.get("cwd") and os.path.isdir(info["cwd"]):
                    body["qemu_cwd"] = info["cwd"]
            else:
                say(f"[{name}] no QEMU command line in the bundle: start it on {dest}/qemu/{name}/ by hand", "warn")
    for name, info in m["nodes"].items():
        _unpack_deltas(dest, info, say)
    files = {rid: {n: os.path.join(dest, arc) for n, arc in per.items()} for rid, per in proj.get("files", {}).items()}
    for w in m.get("warnings", []):
        say(f"bundle warning: {w}", "warn")
    from .group import GroupSpec
    spec = GroupSpec(group, None)

    def go():
        try:
            prog.step("starting nodes")
            app.replace_group(spec, proj.get("tree"), files, proj.get("lock"))
            _load_snaps(app, m, files, say, prog)
            prog.end("done")
        except Exception as e:
            prog.end("failed", f"{type(e).__name__}: {e}")
            say(f"bundle import failed: {e}", "err")
    threading.Thread(target=go, name="import-bundle", daemon=True).start()
    return m


def _unpack_deltas(dest, info, say):
    """Rebuild each delta'd snapshot file next to where its whole file belongs, parents first."""
    for rid in sorted(info.get("packed", {}), key=int):
        pk = info["packed"][rid]
        parent = os.path.join(dest, info["snaps"][str(pk["parent"])])
        delta, out = os.path.join(dest, pk["arc"]), os.path.join(dest, info["snaps"][rid])
        try:
            with open(parent, "rb") as f, open(delta, "rb") as g:
                body = snapdelta.decode(f.read(), g.read())
        except (snapdelta.DeltaError, OSError, struct.error, KeyError) as e:
            raise BundleError(f"snapshot #{rid}: cannot rebuild from its delta ({e})") from None
        with open(out, "wb") as f:
            f.write(body)
        os.unlink(delta)
    if info.get("packed"):
        say(f"rebuilt {len(info['packed'])} snapshot file(s) from deltas", "muted")


def _launches(group, name):
    """Whether the node's resolved spec has a launch block (via its templates)."""
    from .group import GroupSpec
    with contextlib.suppress(Exception):
        return GroupSpec(group, None).node(name).launch is not None
    return False


def _load_snaps(app, m, files, say, prog):
    grp = app.group
    deadline = time.time() + LOAD_WAIT
    prog.phase = "waiting for the nodes to connect"
    todo = [grp.nodes[n] for n in m["nodes"] if n in grp.nodes]
    while time.time() < deadline and not all(n.connected and n.status for n in todo):
        time.sleep(0.2)
    prog.phase = "pausing nodes"
    grp._fanout([n for n in todo if n.connected], lambda n: n.pause(induced=True))
    recs = {r["id"]: r for r in m["project"]["tree"]["snapshots"]}
    for n in todo:
        if n.spec.backend == "qemu" or not n.connected:
            if not n.connected:
                say(f"[{n.name}] not connected: its snapshots were not loaded", "err")
            continue
        for rid in sorted(int(k) for k in m["nodes"][n.name].get("snaps", {})):
            path = files.get(str(rid), {}).get(n.name)
            prog.phase, prog.detail = f"loading snapshots into {n.name}", f"#{rid}"
            tag = recs[rid]["tag"]
            try:
                n.induced_until = time.time() + 3.0
                n.rmm.call("snapshot.load", path=path, tag=tag, restore=False, _timeout=120)
                say(f"[{n.name}] loaded #{rid} {tag}", "ok")
            except (RpcError, OSError) as e:
                say(f"[{n.name}] load #{rid} {tag}: {e}", "err")
    say("project imported: nodes are paused; restore a snapshot (restore TAG) to continue", "ok")
