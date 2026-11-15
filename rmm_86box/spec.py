# SPDX-License-Identifier: MIT
"""What describes a machine, shared by the Qt manager, rmmctl and any tool built on the library:
the node dict of an `rmm-group/1` file (merged, token-filled), the helpers that merge and fill it,
VM config testlists, and port helpers. Stdlib only. rmmgui.group re-exports all of this.

A MachineSpec is a name and that dict. `backends.get(spec.backend).start(spec)` starts or attaches
it, whoever the caller is; see backends/__init__.py.
"""

import ast
import copy
import os
import re
import socket

TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_BACKEND = "86box"


class SpecError(ValueError):
    pass


# -- merging and tokens ---------------------------------------------------------------------------

def merge(base, over):
    """Deep merge, new object: dicts key by key, other values replace, None deletes."""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy.deepcopy(over)
    out = copy.deepcopy(base)
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def fill(value, tokens, depth=8):
    """{token} substitution through dicts and lists. Repeats so vars may use vars."""
    if isinstance(value, dict):
        return {k: fill(v, tokens, depth) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, tokens, depth) for v in value]
    if not isinstance(value, str):
        return value
    for _ in range(depth):
        new = TOKEN.sub(lambda m: str(tokens[m.group(1)]) if m.group(1) in tokens else m.group(0), value)
        if new == value:
            break
        value = new
    return value


# -- ports ----------------------------------------------------------------------------------------

def free_port(host="127.0.0.1"):
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def port_in_use(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, int(port))) == 0


# -- VM config testlists (testrunnerapp) ------------------------------------------------------------

def read_testlist_config(path):
    """CONFIG of a __testlist__*.py, read statically (CONFIG is a literal dict by contract)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "CONFIG" for t in n.targets):
            return ast.literal_eval(n.value)
    raise SpecError(f"{path}: no literal CONFIG")


def load_vm_config(ref, search_dir=None):
    """{"box_cfg", "vm_seed", "box_binary", "vm_id", "path"} of a VM config testlist, by path or
    by vm_id (looked up as __testlist__vm_<id>.py in search_dir). Its own {tokens} are filled
    from its scalar keys, as build_vars would."""
    path = ref
    if not os.path.isfile(path) and search_dir:
        path = os.path.join(search_dir, f"__testlist__vm_{ref}.py")
    if not os.path.isfile(path):
        raise SpecError(f"vm_config {ref!r}: no such testlist" + (f" (looked in {search_dir})" if search_dir else ""))
    cfg = read_testlist_config(path)
    if not cfg.get("vm_config"):
        raise SpecError(f"{path}: not a VM config (no \"vm_config\": True)")
    scalars = {k: v for k, v in cfg.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    resolved = fill(cfg, scalars)
    return {"vm_id": resolved.get("vm_id"), "path": path, "box_cfg": resolved.get("box_cfg") or {},
            "vm_seed": resolved.get("vm_seed"), "box_binary": resolved.get("box_binary")}


# -- the spec -------------------------------------------------------------------------------------

class MachineSpec:
    """One machine to start or attach. `raw` is its node dict as an rmm-group/1 file writes it,
    after `extends` merging and {token} filling; the attributes are the keys every backend reads."""

    def __init__(self, name, raw):
        self.name = name
        self.raw = raw
        self.host = raw.get("host", "127.0.0.1")
        self.port = raw.get("port")                 # int, "auto" or None
        self.qemu = raw.get("qemu")                 # dict or None: a QEMU machine (backends/qemu/adapter.py)
        self.backend = raw.get("backend", "qemu" if self.qemu is not None else DEFAULT_BACKEND)
        self.launch = raw.get("launch")             # dict or None: None = attach to a running machine
        self.poll = dict(raw.get("poll") or {})
        self.rpc_timeout = float(raw.get("rpc_timeout") or 10.0)   # seconds per reply; raise it for valgrind

    @classmethod
    def of(cls, name, raw):
        """A spec from a bare node dict, as a script or rmmctl has it."""
        if not isinstance(raw, dict):
            raise SpecError(f"{name}: a node is an object")
        return cls(name, dict(raw))

    def __repr__(self):
        return f"MachineSpec({self.name!r}, {self.backend}, port={self.port})"
