# SPDX-License-Identifier: MIT
"""Host groups: several VMs described in one JSON file and driven as one unit, roughly the way
docker compose describes linked containers. No Qt here; the window, the console and the
tests all use it.

    {
      "schema": "rmm-group/1",
      "name": "nullmodem_pair",
      "vars": {"vmroot": "/tmp/rmm_groups/{group}"},          # {tokens}, like a testlist CONFIG
      "policy": {"halt_all": true,                            # one VM stops -> pause the others
                 "halt_on": ["breakpoint", "watchpoint", "crash", "disconnect"],
                 "halt_unlocked": false},                     # ... unlocked ones too (hostgroup.py)
      "defaults": {"host": "127.0.0.1", "poll": {"rate": 5, "step": 1}},
      "templates": {                                          # reusable node bodies
        "dos486": {"vm_config": "ms4144_486",                 # a VM config testlist (box_cfg + seed)
                   "launch": {"binary": "/path/to/86Box"}}
      },
      "nodes": {                                              # insertion order = start order
        "a": {"extends": "dos486", "port": 55860,
              "launch": {"vm_dir": "{vmroot}/{node}"},
              "box_cfg": {"Ports (COM & LPT)": {"serial1_device": "null_modem"}},
              "run": ["input.text.type dir a:"]},
        "b": {"extends": "dos486", "port": "auto", "depends_on": ["a"],
              "launch": {"vm_dir": "{vmroot}/{node}"}},
        "probe": {"port": 55900},                             # no launch: attach to a running VM
        "m": {"mock": true},                                  # the in-process reference mock
        "q": {"qemu": {"profile": "isapc", "fda": "/img/dos.img",       # a headless QEMU started
                       "run_dir": "{vmroot}/{node}"}},                  # here (rmm_86box.backends.qemu)
        "s": {"qemu": {"profile": "ss5", "qmp": "/run/s/qmp.sock"}}     # one already running: attach
      },
      "run": ["@* machine.status"]                            # console lines once all are up
    }

A node with "backend": "simh" is a dgnova this GUI launches from its "simh" object ({"disk_image" or
"script_path", "memory", "binary", "cwd", "rdos_boot"}); it has no port, or attaches to one served by
rmm_86box.backends.serve with {"attach": true, "port": N} (rmm_86box/backends/simh.py).

A node with "backend": "vice" is a VICE launched from its "vice" object ({"image" (autostart),
"args", "binary", "snapdir", "cwd"}) or attached with {"attach": true, "port": <binary monitor
port>}. Pause, snapshots (monitor dump / undump to .vsf) and the screen poll go over VICE's binary
monitor (rmm_86box/backends/vice.py).

A node with a `qemu` block is a QEMU machine instead of an 86Box: no port, launch, vm_config, domain
or box_cfg. The block names a profile (isapc, q800, ss5 or an alias) and either `qmp` (a unix socket
path or "host:port" to attach to) or what to start it with: run_dir (default: a temp dir, removed at
stop), binary, memory_mib, args, env, vnc, gdb_port, on_exit (quit | keep), wait, and the profile's
media options (fda, hda, cdrom, scsi, bios, boot, ...). rmm_86box/backends/qemu/.

A node with "backend": "qemu" and a port is the older shape: a QEMU somebody else started with
-qmp tcp:..., driven through savevm / loadvm (rmm_86box/backends/qemu/legacy.py). Whoever started
that QEMU owns its disks: all writable ones must be qcow2.

Merging: defaults, then the `extends` chain (a template or an earlier node, recursively), then
the node itself. Dicts merge key by key, anything else replaces, and null deletes a key (as a
VM config's box_cfg_over does). After the merge, `{tokens}` in every string are filled from
`vars` plus {group}, {node}, {port} and, in a release bundle, {appdir} (its folder); unknown tokens are left as they are.

A node's machine, when the GUI launches it (`launch` present), comes from (later wins):
  vm_config  a testrunnerapp VM config testlist (__testlist__vm_<id>.py, or its <id> found in
             `vm_config_dir`): its box_cfg, its vm_seed (nvr/ copied in) and its box_binary;
  domain     a rmm_86box.kvm domain, inline or by name in the domain store;
  box_cfg    {section: {key: value}} on top, the same shape as a testlist's box_cfg.
"""

import json
import os
import re

from rmm_86box.spec import (MachineSpec, SpecError, TOKEN, fill, free_port, load_vm_config, merge,
                            port_in_use, read_testlist_config)

from . import backends

SCHEMA = "rmm-group/1"

# Why a node stopped, as halt_on names it. "segfault" and "halt" are accepted spellings.
STOP_REASONS = ("breakpoint", "watchpoint", "step", "pause", "crash", "disconnect", "exit")
ALIASES = {"segfault": "crash", "sigsegv": "crash", "halt": "pause", "bp": "breakpoint", "wp": "watchpoint"}
DEFAULT_HALT_ON = ["breakpoint", "watchpoint", "crash", "disconnect"]

NODE_KEYS = {"extends", "host", "port", "mock", "launch", "vm_config", "domain", "box_cfg", "run",
             "depends_on", "poll", "title", "description", "tags", "vm_store", "rpc_timeout", "backend",
             "qemu_argv", "qemu_cwd", "simh", "vice", "qemu"}
LAUNCH_KEYS = {"binary", "vm_dir", "roms", "args", "env", "log", "on_exit", "wait"}


GroupError = SpecError                             # one error type for the library and the GUI


# -- merging and tokens: rmm_86box.spec (imported above) ------------------------------------------------

def norm_reasons(names):
    out = []
    for n in names:
        n = ALIASES.get(str(n).strip().lower(), str(n).strip().lower())
        if n not in STOP_REASONS:
            raise GroupError(f"halt_on: unknown reason {n!r} (one of {', '.join(STOP_REASONS)})")
        if n not in out:
            out.append(n)
    return out


# -- the spec -------------------------------------------------------------------------------------

class Policy:
    """What the group does when one member stops."""

    def __init__(self, halt_all=False, halt_on=None, halt_unlocked=False):
        self.halt_all = bool(halt_all)
        self.halt_on = norm_reasons(DEFAULT_HALT_ON if halt_on is None else halt_on)
        self.halt_unlocked = bool(halt_unlocked)    # halt-all reaches unlocked nodes too (hostgroup.py)

    @classmethod
    def from_spec(cls, d):
        d = d or {}
        unknown = set(d) - {"halt_all", "halt_on", "halt_unlocked"}
        if unknown:
            raise GroupError(f"policy: unknown keys {sorted(unknown)}")
        return cls(d.get("halt_all", False), d.get("halt_on"), d.get("halt_unlocked", False))

    def to_spec(self):
        out = {"halt_all": self.halt_all, "halt_on": list(self.halt_on)}
        if self.halt_unlocked:
            out["halt_unlocked"] = True
        return out

    def triggers(self, reason):
        return self.halt_all and reason in self.halt_on

    def __repr__(self):
        return (f"halt_all {'on' if self.halt_all else 'off'}, halt_on {','.join(self.halt_on) or '-'}"
                + (", unlocked nodes too" if self.halt_unlocked else ""))


class NodeSpec(MachineSpec):
    """One resolved node: the library's MachineSpec plus what the group and the GUI add.
    `raw` is its merged, token-filled dict."""

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self.mock = bool(raw.get("mock"))
        self.run = list(raw.get("run") or [])
        self.depends_on = list(raw.get("depends_on") or [])
        self.title = raw.get("title") or name

    @property
    def mode(self):
        b = backends.find(self.backend)
        if self.mock:
            return "mock"
        if self.qemu is not None:
            return "qemu-attach" if self.qemu.get("qmp") else "qemu"
        if b is not None and b.mode:
            return b.mode
        return "launch" if self.launch is not None else "attach"

    def __repr__(self):
        return f"NodeSpec({self.name!r}, {self.mode}, port={self.port})"


class GroupSpec:
    """A parsed group file. `raw` is the file as written (what `save` writes back); `nodes` are
    resolved NodeSpecs in start order."""

    def __init__(self, raw, path=None):
        if not isinstance(raw, dict):
            raise GroupError("a group file is a JSON object")
        schema = raw.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise GroupError(f"schema {schema!r}: this GUI reads {SCHEMA!r}")
        unknown = set(raw) - {"schema", "name", "description", "vars", "policy", "defaults", "templates",
                              "nodes", "run", "vm_config_dir"}
        if unknown:
            raise GroupError(f"unknown top-level keys {sorted(unknown)}")
        self.raw = raw
        self.path = path
        self.name = raw.get("name") or (os.path.splitext(os.path.basename(path))[0] if path else "group")
        self.policy = Policy.from_spec(raw.get("policy"))
        self.run = list(raw.get("run") or [])
        nodes = raw.get("nodes") or {}
        if not isinstance(nodes, dict) or not nodes:
            raise GroupError("nodes: an object of at least one node")
        base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
        self.vars = {"group": self.name, "group_dir": base}
        self.vars.update(raw.get("vars") or {})
        self.vm_config_dir = fill(raw.get("vm_config_dir") or "", self.vars) or None
        self.nodes = [self._resolve(n, nodes) for n in self._order(nodes)]
        self._check()

    # -- loading --------------------------------------------------------------------------

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except ValueError as e:
                raise GroupError(f"{path}: {e}") from None
        return cls(raw, path)

    @classmethod
    def adhoc(cls, nodes, name="adhoc", policy=None):
        """A group from command-line nodes: {name: {"port": N} | {"mock": True}}."""
        return cls({"schema": SCHEMA, "name": name, "policy": policy or {}, "nodes": nodes})

    def _body_from(self, name, src, nodes, seen):
        body = src[name]
        if name in seen:
            raise GroupError(f"extends loop: {' -> '.join(seen + (name,))}")
        parent = body.get("extends")
        if parent:
            templates = self.raw.get("templates") or {}
            psrc = templates if parent in templates else nodes
            if parent not in psrc:
                raise GroupError(f"{name}: extends {parent!r}: no such template or node")
            body = merge(self._body_from(parent, psrc, nodes, seen + (name,)), body)
        return body

    def _order(self, nodes):
        """File order, moved only as far as depends_on needs."""
        out, state = [], {}

        def visit(n, path):
            if state.get(n) == "done":
                return
            if state.get(n) == "on":
                raise GroupError(f"depends_on loop: {' -> '.join(path + [n])}")
            if not isinstance(nodes[n], dict):
                raise GroupError(f"{n}: a node is an object")
            state[n] = "on"
            for d in nodes[n].get("depends_on") or []:
                if d not in nodes:
                    raise GroupError(f"{n}: depends_on {d!r}: no such node")
                visit(d, path + [n])
            state[n] = "done"
            out.append(n)

        for n in nodes:
            visit(n, [])
        return out

    def _resolve(self, name, nodes):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            raise GroupError(f"node name {name!r}: letters, digits, _ . - (it is typed as @{name})")
        body = self._body_from(name, nodes, nodes, ())
        unknown = set(body) - NODE_KEYS - {k for k in body if k.startswith("x-")}
        if unknown:
            raise GroupError(f"{name}: unknown keys {sorted(unknown)}")
        raw = merge(self.raw.get("defaults") or {}, body)
        raw.pop("extends", None)
        if raw.get("launch") is not None:
            if not isinstance(raw["launch"], dict):
                raise GroupError(f"{name}: launch is an object")
            bad = set(raw["launch"]) - LAUNCH_KEYS
            if bad:
                raise GroupError(f"{name}: launch: unknown keys {sorted(bad)}")
        from rmm_86box.backends.box86 import app_dir
        tokens = dict(self.vars, node=name)
        if app_dir():
            tokens.setdefault("appdir", app_dir())
        if isinstance(raw.get("port"), int):
            tokens["port"] = raw["port"]
        raw = fill(raw, tokens)
        return NodeSpec(name, raw)

    def _check(self):
        seen = {}
        for n in self.nodes:
            if n.mock:
                continue
            b = backends.find(n.backend)
            if b is None:
                raise GroupError(f"{n.name}: backend {n.backend!r} is not one of {', '.join(backends.names())}")
            bad = b.validate(n)
            if bad:
                raise GroupError(f"{n.name}: {bad}")
            if not b.uses_port or n.qemu is not None:     # a qemu block has no TCP port
                continue
            if n.port in (None, ""):
                raise GroupError(f"{n.name}: port is required (a number, or \"auto\" with launch)")
            if n.port == "auto":
                if n.launch is None:
                    raise GroupError(f"{n.name}: port \"auto\" needs launch (an attached VM has a fixed port)")
                continue
            if not isinstance(n.port, int) or not 0 < n.port < 65536:
                raise GroupError(f"{n.name}: port {n.port!r} is not a TCP port")
            key = (n.host, n.port)
            if key in seen:
                raise GroupError(f"{n.name} and {seen[key]} both use {n.host}:{n.port}; 86Box serves one "
                                 "client per port, and two instances cannot listen on one port")
            seen[key] = n.name
            if n.launch is not None and not n.launch.get("vm_dir"):
                raise GroupError(f"{n.name}: launch.vm_dir is required")

    def node(self, name):
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(name)

    # -- writing --------------------------------------------------------------------------

    def resolved(self):
        """The group with every node fully merged and filled: what will actually run."""
        return {"schema": SCHEMA, "name": self.name, "policy": self.policy.to_spec(),
                "nodes": {n.name: n.raw for n in self.nodes}, "run": self.run}

    def save(self, path, resolved=False):
        data = self.resolved() if resolved else dict(self.raw, policy=self.policy.to_spec())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return path


__all__ = ["GroupSpec", "NodeSpec", "Policy", "GroupError", "merge", "fill", "norm_reasons",
           "load_vm_config", "read_testlist_config", "free_port", "port_in_use", "SCHEMA", "STOP_REASONS"]
