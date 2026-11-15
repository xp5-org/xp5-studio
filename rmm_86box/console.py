# SPDX-License-Identifier: MIT
"""A monitor console: parse a line, run it, format the answer as text lines. No UI here; the
Qt demo and the readline REPL (python -m rmm_86box) both drive this.

Line forms:
    machine.status                          a method, no params
    mem.phys.read addr=0xB8000 len=64       name=value params (ints may be hex; true/false/null;
                                            "quoted strings"; JSON for objects and lists)
    input.text.type dir a:                  one-param methods take the rest of the line as it
    config.load {"cfg": {...}}              a JSON object as the whole params
    rmm.snapshot.list()                     a Python expression on the library (rmm, _ = last result)
    help [method] | vm ... | clear          builtins; a UI adds its own (Console.builtins)
"""

import json
import shlex
import textwrap

from . import kvm
from .complete import PY_HEAD, Completer
from .rpc import RpcError


def parse_value(s):
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    try:
        return int(s, 0)
    except ValueError:
        pass
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            pass
    return s


def parse_call(line, methods=None):
    """'mem.phys.read addr=0x10 len=4' -> ("mem.phys.read", {"addr": 16, "len": 4})"""
    line = line.strip()
    method, _, rest = line.partition(" ")
    rest = rest.strip()
    if not rest:
        return method, {}
    if rest.startswith("{"):
        return method, json.loads(rest)
    try:
        toks = shlex.split(rest)
    except ValueError as e:
        raise ValueError(f"unbalanced quotes: {e}") from None
    if not any("=" in t for t in toks):
        m = methods.get(method) if methods else None
        if m and m.params:
            first = next(iter(m.params))
            typ = m.params[first].rstrip("?")
            text = rest[1:-1] if rest[:1] == rest[-1:] == '"' and len(rest) > 1 else rest
            return method, {first: text if typ == "string" else parse_value(text)}
        raise ValueError("params are name=value (try Tab)")
    params = {}
    for t in toks:
        k, eq, v = t.partition("=")
        if not eq:
            raise ValueError(f"{t!r}: params are name=value")
        params[k] = parse_value(v)
    return method, params


# -- formatting ---------------------------------------------------------------------------------

def hexdump(addr, data, width=16):
    out = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{addr + off:08X}  {hx:<{width * 3}} {asc}")
    return out


def format_result(method, r, width=100):
    if r is None or r == {}:
        return ["ok"]
    if method in ("mem.phys.read", "mem.lin.read") and isinstance(r, dict) and "data" in r:
        return hexdump(r["addr"], bytes.fromhex(r["data"]))
    if method == "cpu.regs.get" and isinstance(r, dict):
        gp = [f"{k}={r[k]:08X}" for k in ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp") if k in r]
        sg = [f"{k}={r[k]:04X}" for k in ("cs", "ds", "es", "ss", "fs", "gs") if k in r]
        tail = [f"eip={r.get('eip', 0):08X}", f"eflags={r.get('eflags', 0):08X}", f"cr0={r.get('cr0', 0):08X}"]
        return ["  ".join(gp[:4]), "  ".join(gp[4:]), "  ".join(sg), "  ".join(tail)]
    if method == "capture.screenshot" and isinstance(r, dict):
        return [f"frame {r['w']}x{r['h']} (of {r.get('src_w')}x{r.get('src_h')}) seq={r.get('seq')} "
                f"fresh={r.get('fresh')} {len(r.get('data', '')) * 3 // 4} bytes"]
    if method == "config.dump" and isinstance(r, dict):
        out = []
        for sec, kv in r["cfg"].items():
            out.append(f"[{sec}]")
            out += [f"  {k} = {v}" for k, v in kv.items()]
        return out
    if method == "rpc.discover":
        return textwrap.wrap("  ".join(m["name"] for m in r["methods"]), width)
    text = json.dumps(r, default=str)
    if len(text) > 4000:
        text = text[:4000] + " ..."
    return textwrap.wrap(text, width) or [text]


# -- builtins -----------------------------------------------------------------------------------

# With autopause on, the console pauses around a method the schema marks paused-only and
# resumes after, as a person would; these are left out on purpose: they are meant to leave
# the machine paused (autoresume covers snapshot.restore, which pauses by itself).
LEAVES_PAUSED = {"debug.step"}


class Builtin:
    def __init__(self, help, run, complete=None):
        self.help = help
        self._run = run
        self._complete = complete

    def run(self, args):
        return self._run(args)

    def complete(self, done, cur, completer):
        return self._complete(done, cur, completer) if self._complete else []


class Console:
    def __init__(self, rmm, width=100, autopause=True, autoresume=False):
        self.rmm = rmm
        self.width = width
        self.autopause = autopause
        self.autoresume = autoresume
        self.last = None
        self.history = []
        self.builtins = {
            "help": Builtin("help [method]  - list methods, or one method's params and result",
                            self._help, lambda d, c, comp: comp.methods.names() if not d else []),
            "autopause": Builtin("autopause on|off  - pause around calls that need a paused guest",
                                 self._autopause, lambda d, c, comp: ["on", "off"] if not d else []),
            "autoresume": Builtin("autoresume on|off  - resume the guest after snapshot.restore (it is left paused)",
                                  self._autoresume, lambda d, c, comp: ["on", "off"] if not d else []),
            "vm": Builtin("vm show | list | save NAME | load NAME | diff NAME | check NAME  - VM config, KVM style",
                          self._vm, self._vm_complete),
        }
        self.completer = Completer(rmm.methods, provider=Provider(rmm), builtins=self.builtins,
                                   namespace=lambda: {"rmm": self.rmm, "_": self.last})

    def complete(self, line):
        return self.completer.complete(line)

    def run(self, line):
        """Run one line; returns the output lines. Errors come back as lines too."""
        line = line.strip()
        if not line:
            return []
        self.history.append(line)
        word, _, rest = line.partition(" ")
        try:
            if word in self.builtins:
                return self.builtins[word].run(rest.split())
            if PY_HEAD.match(line):
                return self._py(line)
            method, params = parse_call(line, self.rmm.methods)
            if self.rmm.methods.get(method) is None and "." not in method:
                return [f"unknown: {word!r}  (Tab completes; 'help' lists methods)"]
            note = []
            m = self.rmm.methods.get(method)
            if (self.autopause and m and m.paused and method not in LEAVES_PAUSED
                    and self.rmm.state() == "running"):
                with self.rmm.paused():
                    r = self.rmm.call(method, **params)
                note = ["(paused for the call, resumed)"]
            else:
                r = self.rmm.call(method, _timeout=30 if method == "machine.reset" else None, **params)
            if self.autoresume and method == "snapshot.restore" and self.rmm.state() == "paused":
                self.rmm.call("machine.resume")
                note = ["(resumed after restore)"]
            self.last = r
            return format_result(method, r, self.width) + note
        except RpcError as e:
            out = [f"error {e}"]
            if e.data:
                for x in (e.data.get("errors") if isinstance(e.data, dict) else None) or []:
                    out.append(f"  [{x.get('section')}] {x.get('key')} = {x.get('value')!r}: {x.get('why')}")
                if not isinstance(e.data, dict) or "errors" not in e.data:
                    out.append(f"  {json.dumps(e.data)[:300]}")
            return out
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return [f"error: {e}"]

    def _py(self, expr):
        ns = {"rmm": self.rmm, "_": self.last, "hex": hex, "len": len, "bytes": bytes, "kvm": kvm}
        r = eval(expr, {"__builtins__": {}}, ns)      # a local console on your own VM
        self.last = r
        if isinstance(r, (bytes, bytearray)):
            return hexdump(0, r[:4096])
        if isinstance(r, list) and all(isinstance(x, str) for x in r):
            return r or ["[]"]
        return textwrap.wrap(repr(r), self.width) or [repr(r)]

    def _autopause(self, args):
        if args:
            self.autopause = args[0] in ("on", "1", "true")
        return [f"autopause {'on' if self.autopause else 'off'}"]

    def _autoresume(self, args):
        if args:
            self.autoresume = args[0] in ("on", "1", "true")
        return [f"autoresume {'on' if self.autoresume else 'off'}"]

    def _help(self, args):
        if args:
            m = self.rmm.methods.get(args[0])
            if not m:
                return [f"no method {args[0]!r}"]
            out = [m.signature(), f"  {m.doc}" if m.doc else ""]
            out += [f"  {p}: {' | '.join(map(str, vs))}" for p, vs in m.enums.items()]
            errs = self.rmm.methods.errors
            if m.errors:
                out.append("  errors: " + ", ".join(f"{c} {errs.get(c, '')}".rstrip() for c in m.errors))
            return out
        out = ["methods (Tab completes, `help NAME` for one):"]
        out += textwrap.wrap("  ".join(self.rmm.methods.names()), self.width)
        out.append("builtins: " + "  ".join(sorted(self.builtins)))
        out.append("python:   rmm.machine.status()   rmm.screenshot()   _  (the last result)")
        return out

    # vm -----------------------------------------------------------------------------------

    def _vm(self, args):
        vm = self.rmm.vm
        sub = args[0] if args else "show"
        if sub == "show":
            return kvm.describe(vm.show())
        if sub == "list":
            return vm.list() or [f"no domains in {vm.store.path}"]
        if len(args) < 2:
            return [f"vm {sub} NAME"]
        name = args[1]
        if sub == "save":
            return [f"saved {vm.save(name)}"]
        if sub == "diff":
            d = vm.diff(name)
            return [f"[{s}] {k}: {a!r} -> {b!r}" for s, k, a, b in d] or ["no differences"]
        if sub == "check":
            r = vm.load(name, validate_only=True)["load"]
            return [f"{name}: valid, {r['keys']} keys ({len(r['unchecked'])} unchecked)"]
        if sub == "load":
            r = vm.load(name, reset=True)
            out = [f"loaded {name}: {r['load']['keys']} keys staged, hard reset done "
                   f"(config_loaded={r['reset'].get('config_loaded') if r['reset'] else None})"]
            return out + kvm.describe(vm.show())
        return [f"unknown: vm {sub}"]

    def _vm_complete(self, done, cur, completer):
        if not done:
            return ["show", "list", "save", "load", "diff", "check"]
        if len(done) == 1 and done[0] in ("load", "diff", "check", "save"):
            return self.rmm.vm.list()
        return []


class Provider:
    """Live answers for completion, cached for a couple of seconds so Tab stays snappy."""

    def __init__(self, rmm, ttl=2.0):
        self.rmm = rmm
        self.ttl = ttl
        self._cache = {}

    def _cached(self, key, fn):
        import time
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        v = fn()
        self._cache[key] = (time.time(), v)
        return v

    def snapshot_tags(self):
        return self._cached("tags", lambda: [s["tag"] for s in self.rmm.call("snapshot.list")["snapshots"]])

    def _cfg(self):
        return self._cached("cfg", lambda: self.rmm.call("config.dump")["cfg"])

    def sections(self):
        return list(self._cfg())

    def cfg_keys(self, section):
        return list(self._cfg().get(section, {}))

    def cfg_values(self, section, key):
        d = self._cached(("describe", section, key),
                         lambda: self.rmm.call("config.describe", section=section, key=key))
        if d.get("type") == "enum":
            return d["values"]
        if d.get("type") == "int":
            lo, hi, st = d["min"], d["max"], d.get("step", 1)
            vals = list(range(lo, hi + 1, st))
            return [str(v) for v in (vals if len(vals) <= 64 else vals[:: max(1, len(vals) // 32)])]
        return []

    def break_ids(self):
        return [b["id"] for b in self.rmm.call("debug.break.list")["breakpoints"]]

    def watch_ids(self):
        return [w["id"] for w in self.rmm.call("debug.watch.list")["watchpoints"]]
