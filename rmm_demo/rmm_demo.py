# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""xp5-studio (formerly the 86Box RMM demo): a small Qt (PySide6) window on top of the rmm_86box library, for one VM or a host group.

Shows how a program loads the library and uses it: a live guest screen (capture.screenshot,
which never pauses the guest), a console with Tab completion, registers and snapshots, and the
VM's config in KVM terms, stored and loaded through the monitor. Read-only screen: no mouse
or keyboard capture into the guest (type into it with input.text.type / input.key.send, or
paste with Ctrl+Shift+V).

    rmm_demo.py --port 55860              a VM with [General] rmm_port = 55860
    rmm_demo.py --mock                    the reference mock, no VM (--mock 3: three of them)
    rmm_demo.py --attach a=55860 --attach b=55861 --halt-all
                                          two running VMs as a group
    rmm_demo.py --group pair.json         a host group file (rmm-group/1, see rmmgui/group.py
                                          and groups/*.json): attach, launch 86Box, or mock

Console: `@b line` runs a line on node b, `@*` on all, `use b` changes the default node.
`nodes`, `halt`, `resume`, `group show|save|load|export|halt-all|halt-on`, `copy screen|regs|out|last`.
`snap [TAG]` snapshots every node together (`@b snap` only b); `restore TAG` rolls them back
together; `snaps` prints the snapshot family tree, F12 (or `snaps view`) draws it.
Halt-all (F11, or "policy" in the file): when one node stops on a breakpoint, watchpoint or
crash, the others are paused too.

Keys: see rmmgui/app.py. Tab complete, Enter run, Ctrl+C/X/V copy/cut/paste, F1-F8 on the
selected node, F9 resume all, F10 pause all. Close the window (or `quit`) to exit; 86Box
instances the demo started are quit with it (launch.on_exit "keep" leaves them running).
"""

import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, ".."), HERE):     # source checkout: the library beside us
    if os.path.isdir(os.path.join(p, "rmm_86box")) and p not in sys.path:
        sys.path.insert(0, p)

from rmmgui.group import GroupError, GroupSpec  # noqa: E402


def sample_domains():
    """[(file name, reader)] for domains/*.json, from a directory or from the zipapp."""
    src = os.path.join(HERE, "domains")
    if os.path.isdir(src):
        return [(f, lambda f=f: open(os.path.join(src, f), "rb").read())
                for f in sorted(os.listdir(src)) if f.endswith(".json")]
    if zipfile.is_zipfile(HERE):                        # running as rmm_demo.pyz
        z = zipfile.ZipFile(HERE)
        return [(n.split("/", 1)[1], lambda n=n: z.read(n))
                for n in z.namelist() if n.startswith("domains/") and n.endswith(".json")]
    return []


def seed_domains(store):
    """Sample domains shipped with the demo, copied into the store once."""
    os.makedirs(store, exist_ok=True)
    for name, read in sample_domains():
        dst = os.path.join(store, name)
        if not os.path.exists(dst):
            with open(dst, "wb") as f:
                f.write(read())


def load_project(path):
    """(GroupSpec, the project dict) from a saved project file."""
    import json
    from rmmgui.app import PROJECT_SCHEMA
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") != PROJECT_SCHEMA:
        raise GroupError(f"{path}: schema {data.get('schema')!r}, want {PROJECT_SCHEMA}")
    return GroupSpec(data["group"], data.get("group_path") or path), data


def group_from_args(a):
    if a.project:
        spec, a.project_data = load_project(a.project)
        return spec
    if a.group:
        return GroupSpec.load(a.group)
    nodes = {}
    for spec in a.attach:
        name, eq, port = spec.partition("=")
        if not eq:
            raise GroupError(f"--attach {spec!r}: NAME=PORT")
        nodes[name] = {"port": int(port), "host": a.host}
    for i in range(a.mock):
        nodes["mock" if a.mock == 1 else f"mock{i + 1}"] = {"mock": True}
    if not nodes and a.empty:                       # a blank studio, as File > New project starts one
        nodes["m"] = {"mock": True}
    if not nodes:
        nodes["vm"] = {"port": a.port, "host": a.host}
    poll = {"rate": a.poll_rate, "step": a.step, "enabled": not a.no_poll}
    return GroupSpec({"schema": "rmm-group/1", "name": "adhoc", "nodes": nodes,
                      "defaults": {"poll": poll, "rpc_timeout": a.rpc_timeout}})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=55860)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mock", type=int, nargs="?", const=1, default=0,
                    help="run against the reference mock (no VM); a number starts that many")
    ap.add_argument("--attach", action="append", default=[], metavar="NAME=PORT",
                    help="a running VM as a group node (repeatable)")
    ap.add_argument("--empty", action="store_true",
                    help="a blank studio (what the xp5-studio program opens): the reference mock stands in until "
                         "you open a project or group file, or add nodes with Attach / Launch / QEMU")
    ap.add_argument("--group", help="a host group file (JSON, schema rmm-group/1)")
    ap.add_argument("--project", help="a saved project file (rmm-project/1: group + snapshot tree); the GUI "
                                      "launches every node in it, nothing else has to start them")
    ap.add_argument("--halt-all", dest="halt_all", action="store_true", default=None,
                    help="start with halt-all on (pause every node when one stops)")
    ap.add_argument("--no-halt-all", dest="halt_all", action="store_false")
    ap.add_argument("--check", action="store_true",
                    help="parse --group, print the resolved nodes as JSON and exit (no window)")
    ap.add_argument("--rpc-timeout", type=float, default=10.0,
                    help="seconds to wait for each 86Box reply (raise it for a valgrind'ed 86Box)")
    ap.add_argument("--poll-rate", type=float, default=10.0, help="frames per second to ask for")
    ap.add_argument("--no-poll", action="store_true", help="start with polling off")
    ap.add_argument("--step", type=int, default=1, help="capture every Nth pixel (1..8)")
    ap.add_argument("--vm-store", default=os.path.join(os.path.expanduser("~"), ".rmm_86box", "domains"))
    ap.add_argument("--snaps-view", action="store_true", help="open on the Snapshots tab (F12)")
    ap.add_argument("--no-system-clipboard", action="store_true", help="copy/paste inside the window only")
    ap.add_argument("--display", help="X display to open on (e.g. :10)")
    ap.add_argument("--size", default="1280x820")
    ap.add_argument("--title", default="xp5-studio")
    ap.add_argument("--run", action="append", default=[], help="console line to run once connected (repeatable)")
    ap.add_argument("--exit-after", type=float, default=0,
                    help="close after N seconds once queued --run lines are done (unattended runs)")
    ap.add_argument("--save-png", help="with --exit-after: save the window to this PNG first")
    a = ap.parse_args(argv)
    if a.display:
        os.environ["DISPLAY"] = a.display
    a.project_data = None
    a.size = tuple(int(v) for v in a.size.lower().split("x"))
    try:
        spec = group_from_args(a)
    except (GroupError, OSError, ValueError) as e:
        print(f"rmm_demo: {e}", file=sys.stderr)
        return 2
    if a.check:
        import json
        print(json.dumps(spec.resolved(), indent=2))
        return 0
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;*.warning=false")
    seed_domains(a.vm_store)
    from rmmgui.app import App                       # Qt from here on
    App(a, spec).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
