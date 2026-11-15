# SPDX-License-Identifier: MIT
"""Python on a breakpoint / watchpoint hit: the console's `hook` word and the panel's "python script".

A hook script is an ordinary .py file. It runs inside this GUI (so `import rmm_86box` and the
rest of the stdlib work), on the node's worker thread, in order with console lines, while 86Box
holds the guest paused at the stop:

    import rmm_86box

    def on_arm(hit):            # optional: once, when the hook is attached (hit.event is {})
        hit.state["n"] = 0

    def on_hit(hit):            # required: every stop of that breakpoint / watchpoint
        hit.state["n"] += 1
        regs = hit.regs()                       # rmm_86box.Rmm.regs()
        b = hit.read(hit.addr, 2)               # bytes
        hit.say(f"#{hit.count} eip={hit.eip:#x} {b.hex()}")
        if hit.new == 0:
            hit.restore("snap1")                # leaves the guest paused...
        return "resume"                         # ...this resumes it; None / "stay" = stay paused

    def on_disarm(hit):         # optional: the hook was taken off, or the breakpoint cleared
        hit.say(f"saw {hit.state['n']} hits")

`hit.rmm` is the node's own rmm_86box.Rmm (call(), read_mem(), snapshot.save(...), ...); hit.state
is a dict kept across hits and reloads. With symbols loaded (the `sym` word, rmmgui/symbols.py),
hit.sym(addr) names an address ("hurt_+0x12"), hit.addr_of("hero.health") goes the other way,
hit.file_addr(addr) is the address as the map / Ghidra writes it, and hit.pc() is cs_base + eip.
Worked examples ship in rmmgui/hook_examples/ (hook examples; hook new FILE.py w3 from=NAME).
The script is reloaded when its file changes, so it can be
edited while armed. An exception prints its traceback in the console and leaves the guest paused.
"""

import importlib.util
import os
import pkgutil
import time
import traceback

from rmm_86box import RpcError

RESUME = ("resume", "go", "continue")
STAY = ("stay", "stop", "pause")

TEMPLATE = '''\
"""Hook for {ref} on {node}: on_hit(hit) runs at every stop (guest paused).
hit: .rmm (rmm_86box.Rmm) .node .kind .id .count .addr .eip .old .new .event .state .args
     .say(text) .regs() .read(addr, n) .write(addr, data) .snap(tag) .restore(tag) .clear()
     .pc() .sym(addr) .addr_of("name") .file_addr(addr) .symbols   (with `sym load` tables)
Return "resume" to let the guest run on; None / "stay" leaves it paused."""

import rmm_86box  # noqa: F401  (the same package the console uses: hit.rmm is an rmm_86box.Rmm)


def on_arm(hit):
    hit.state.setdefault("hits", 0)
    hit.say(f"armed on {{hit.kind}} #{{hit.id}}")


def on_hit(hit):
    hit.state["hits"] += 1
    r = hit.regs()
    hit.say(f"hit {{hit.count}}: eip={{r.get('eip', 0):#x}} old={{hit.old}} new={{hit.new}}")
    return "resume"


def on_disarm(hit):
    hit.say(f"disarmed after {{hit.state['hits']}} hits")
'''


class HookError(Exception):
    pass


def parse_ref(ref):
    """'w3' / 'watch3' / 'b2' / 'break2' -> ("watch", 3) / ("break", 2)."""
    r = ref.lower().lstrip("#")
    for pre, kind in (("watch", "watch"), ("break", "break"), ("bp", "break"), ("w", "watch"), ("b", "break")):
        if r.startswith(pre) and r[len(pre):].lstrip("#").isdigit():
            return kind, int(r[len(pre):].lstrip("#"))
    raise HookError(f"{ref!r}: name it w<ID> (watchpoint) or b<ID> (breakpoint), as debug.*.list shows")


def ref_of(key):
    return f"{key[0][0]}{key[1]}"


def parse_args(words):
    """k=v words -> dict (ints where they parse as int, hex with 0x)."""
    out = {}
    for w in words:
        k, sep, v = w.partition("=")
        if not sep:
            raise HookError(f"{w!r}: script arguments are key=value")
        try:
            out[k] = int(v, 0)
        except ValueError:
            out[k] = v
    return out


class Hit:
    """What on_arm / on_hit / on_disarm get. Built fresh per call; .state is the hook's own."""

    def __init__(self, hook, event=None):
        self._hook = hook
        node = hook.node
        self.node = node                       # the GUI's Node (status, regs cache, name, ...)
        self.name = node.name
        self.rmm = node.rmm                    # rmm_86box.Rmm
        self.kind, self.id = hook.key
        self.event = dict(event or {})          # the machine.stopped params as 86Box sent them
        self.reason = self.event.get("reason", "")
        self.addr = self.event.get("addr")
        self.eip = self.event.get("eip")
        self.old = self.event.get("old")
        self.new = self.event.get("new")
        self.count = node.hits.get(hook.key, 0)
        self.state = hook.state
        self.args = hook.args
        self.script = hook.path
        self._regs = None

    def say(self, text, tone="text"):
        self.node.say(f"[{os.path.basename(self.script)}] {text}", tone)

    def regs(self):
        if self._regs is None:
            self._regs = self.rmm.call("cpu.regs.get")     # paused at the stop: no pause dance needed
        return dict(self._regs)

    # symbols (the node's `sym` tables)
    @property
    def symbols(self):
        return self.node.symbols

    def pc(self):
        """Linear address of the next instruction: cs_base + eip."""
        r = self.regs()
        return r.get("cs_base", r.get("cs", 0) * 16) + r.get("eip", 0)

    def sym(self, addr=None):
        """'hurt_+0x12' for addr (default: pc), or its hex when no table covers it."""
        a = self.pc() if addr is None else addr
        return self.node.symbols.name_of(a) or f"{a:#x}"

    def addr_of(self, name):
        """'hero.health', 'game!main+4' -> linear address (rmmgui.symbols.SymbolError if unknown)."""
        return self.node.symbols.addr(name)

    def file_addr(self, addr=None):
        """addr as the map file / Ghidra writes it (1000:0234, 00401234), or None."""
        return self.node.symbols.file_addr(self.pc() if addr is None else addr)

    def read(self, addr, length, linear=False):
        return self.rmm.read_mem(addr, length, linear=linear)

    def write(self, addr, data):
        self.rmm.write_mem(addr, bytes(data))

    def snap(self, tag):
        return self.rmm.call("snapshot.save", tag=tag)

    def restore(self, tag):
        """Back to snapshot tag; the guest stays paused (return "resume" to run on)."""
        self._hook.restores += 1
        self._regs = None
        return self.rmm.call("snapshot.restore", tag=tag)

    def resume(self):
        self.rmm.call("machine.resume")

    def clear(self):
        """Clear this breakpoint / watchpoint in 86Box (the hook goes with it)."""
        meth = "debug.break.clear" if self.kind == "break" else "debug.watch.clear"
        self.rmm.call(meth, id=self.id)
        self._hook.cleared = True
        self.node.drop_hit_rule(self.kind, self.id)     # on_disarm runs after this on_hit returns


class Hook:
    """One script attached to one (kind, id) on one node."""

    def __init__(self, node, key, path, args=None):
        self.node = node
        self.key = key
        self.path = os.path.abspath(os.path.expanduser(path))
        self.args = dict(args or {})
        self.state = {}
        self.mod = None
        self.mtime = None
        self.runs = 0
        self.errors = 0
        self.restores = 0
        self.last = ""                          # what the last run did, for the panel
        self.last_error = ""
        self.cleared = False
        self.load()

    def load(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError as e:
            raise HookError(f"{self.path}: {e.strerror or e}") from e
        name = f"rmm_hook_{self.node.name}_{self.key[0]}{self.key[1]}"
        spec = importlib.util.spec_from_file_location(name, self.path)
        if spec is None:
            raise HookError(f"{self.path}: not a Python file")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 - a script's own import error, shown as such
            raise HookError(f"{os.path.basename(self.path)} did not load: {type(e).__name__}: {e}\n"
                            + _tb_tail(e)) from e
        if not callable(getattr(mod, "on_hit", None)):
            raise HookError(f"{os.path.basename(self.path)} has no on_hit(hit)")
        self.mod, self.mtime = mod, mtime

    @property
    def funcs(self):
        return [f for f in ("on_arm", "on_hit", "on_disarm") if callable(getattr(self.mod, f, None))]

    def _reload_if_changed(self):
        try:
            if os.path.getmtime(self.path) != self.mtime:
                self.load()
                self.node.say(f"{ref_of(self.key)}: reloaded {os.path.basename(self.path)} (state kept)", "muted")
        except HookError as e:
            self.node.say(f"{ref_of(self.key)}: {e} (keeping the version loaded before)", "warn")
            self.mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else self.mtime

    def _call(self, fname, event):
        fn = getattr(self.mod, fname, None)
        if not callable(fn):
            return None
        try:
            return fn(Hit(self, event))
        except Exception as e:  # noqa: BLE001 - the script's bug: say it, keep the GUI up
            self.errors += 1
            self.last_error = f"{fname}: {type(e).__name__}: {e}"
            self.node.say(f"{ref_of(self.key)} {os.path.basename(self.path)} {fname} raised "
                          f"{type(e).__name__}: {e}\n{_tb_tail(e)}", "err")
            raise _ScriptFailed from e

    def arm(self):
        try:
            self._call("on_arm", {})
        except _ScriptFailed:
            pass

    def disarm(self):
        try:
            self._call("on_disarm", {})
        except _ScriptFailed:
            pass

    def run(self, event):
        """One stop: call on_hit, then resume or stay as it returned. On the node's worker."""
        self._reload_if_changed()
        t0 = time.time()
        try:
            r = self._call("on_hit", event)
        except _ScriptFailed:
            self.last = "raised: guest left paused"
            return
        self.runs += 1
        ms = (time.time() - t0) * 1000
        go = r is True or (isinstance(r, str) and r.lower() in RESUME)
        if r is not None and not isinstance(r, bool) and not (isinstance(r, str) and r.lower() in RESUME + STAY):
            self.node.say(f"{ref_of(self.key)}: on_hit returned {r!r}; 'resume' runs on, None/'stay' stays paused",
                          "warn")
        if go:
            try:
                self.node.rmm.call("machine.resume")
            except (RpcError, OSError) as e:
                self.node.say(f"{ref_of(self.key)}: resume after the hook failed: {e}", "warn")
        self.last = ("cleared it, " if self.cleared else "") + ("resumed" if go else "stayed paused") + f" ({ms:.0f} ms)"

    def describe(self):
        return f"python {os.path.basename(self.path)}" + (f" {fmt_args(self.args)}" if self.args else "")


class _ScriptFailed(Exception):
    pass


def fmt_args(args):
    return " ".join(f"{k}={v}" for k, v in args.items())


def _tb_tail(e, n=6):
    lines = traceback.format_exception(type(e), e, e.__traceback__)
    return "".join(lines[-n:]).rstrip()


# -- the console word -------------------------------------------------------------------------

USAGE = """\
hook                              hooks on this node: script, runs, errors, what the last hit did
hook w3|b2 FILE.py [k=v ...]      run FILE's on_hit(hit) at every stop of watch #3 / break #2
hook new FILE.py [w3|b2]          write a starter script (rmm_86box imported, every callback stubbed)
hook new FILE.py w3 from=NAME     start from a shipped example instead (hook examples lists them)
hook examples                     the shipped examples: who_writes, crash_backtrace
hook reload w3                    load the file again now (it also reloads by itself when saved)
hook off w3                       detach (on_disarm runs); the watchpoint itself stays
  The script runs in this GUI with the guest paused at the stop; return "resume" to run on.
  It sees hit.rmm (rmm_86box.Rmm), hit.state (kept across hits), hit.args (the k=v words)."""


def examples():
    """(name, first docstring line) for each shipped example in rmmgui/hook_examples/."""
    out = []
    for name in ("who_writes", "crash_backtrace"):
        text = example_text(name)
        doc = text.split('"""', 2)[1].strip().splitlines()[0] if '"""' in text else ""
        out.append((name, doc))
    return out


def example_text(name):
    """A shipped example's source; works from a checkout and from inside the .pyz."""
    name = name[:-3] if name.endswith(".py") else name
    try:
        data = pkgutil.get_data(__package__, f"hook_examples/{name}.py")
    except (OSError, FileNotFoundError):
        data = None
    if not data:
        raise HookError(f"no example {name!r} (hook examples lists them)")
    return data.decode()


def attach(node, key, path, args=None, by="console", label=""):
    """Load path and make it key's hit rule (replacing any rule there). Runs on_arm. Worker thread."""
    hook = Hook(node, key, path, args)
    old = node.hit_rules.get(key)
    if old and old.get("hook"):
        old["hook"].disarm()
    node.set_hit_rule(key[0], key[1], label=label or (old or {}).get("label", ""), on_hit="script", by=by,
                      note=hook.describe())
    node.hit_rules[key]["hook"] = hook
    hook.arm()
    return hook


def console(node, args):
    """The per-node `hook` builtin. Runs on the node's worker thread."""
    if not args:
        rows = [(k, r) for k, r in sorted(node.hit_rules.items()) if r.get("hook")]
        if not rows:
            return ["no hooks on this node", *USAGE.splitlines()]
        out = []
        for k, r in rows:
            h = r["hook"]
            out.append(f"{ref_of(k):<5} {h.path}" + (f"  {fmt_args(h.args)}" if h.args else ""))
            out.append(f"      {', '.join(h.funcs)} | hits {node.hits.get(k, 0)} runs {h.runs} errors {h.errors}"
                       + (f" restores {h.restores}" if h.restores else "") + (f" | last: {h.last}" if h.last else ""))
            if h.last_error:
                out.append(f"      last error: {h.last_error}")
        return out
    word = args[0]
    try:
        if word in ("help", "-h", "--help"):
            return USAGE.splitlines()
        if word == "examples":
            return [f"{n:<16} {first}" for n, first in examples()] + ["copy one: hook new FILE.py w3 from=NAME"]
        if word == "new":
            src = next((a.split("=", 1)[1] for a in args[2:] if a.startswith("from=")), None)
            rest = [a for a in args[2:] if not a.startswith("from=")]
            if len(args) < 2:
                raise HookError("hook new FILE.py [w3|b2] [from=EXAMPLE]")
            path = os.path.abspath(os.path.expanduser(args[1]))
            if os.path.exists(path):
                raise HookError(f"{path} exists; edit it, or pick another name")
            ref = rest[0] if rest else "w1"
            parse_ref(ref)
            body = example_text(src) if src else TEMPLATE.format(ref=ref, node=node.name)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
            return [f"wrote {path}" + (f" (from {src})" if src else ""), f"attach it: hook {ref} {path}"]
        if word in ("off", "reload"):
            if len(args) < 2:
                raise HookError(f"hook {word} w3|b2")
            key = parse_ref(args[1])
            rule = node.hit_rules.get(key)
            if not rule or not rule.get("hook"):
                raise HookError(f"no hook on {args[1]}")
            h = rule["hook"]
            if word == "off":
                node.drop_hit_rule(*key)                 # queues on_disarm behind this line
                return [f"{args[1]}: hook off; 86Box still stops there (debug.*.clear to remove it)"]
            h.load()
            return [f"{args[1]}: reloaded {h.path} ({', '.join(h.funcs)}), state kept"]
        key = parse_ref(word)
        if len(args) < 2:
            raise HookError(f"hook {word} FILE.py [k=v ...]")
        listed = node.rmm.call("debug.watch.list" if key[0] == "watch" else "debug.break.list")
        ids = [int(d["id"]) for d in listed.get("watchpoints" if key[0] == "watch" else "breakpoints", [])]
        if key[1] not in ids:
            raise HookError(f"86Box has no {key[0]} #{key[1]} (it has {', '.join(map(str, ids)) or 'none'}); "
                            f"set it first: debug.{key[0]}.set addr=...")
        h = attach(node, key, args[1], parse_args(args[2:]))
        return [f"{word}: {h.path} armed ({', '.join(h.funcs)}); each stop runs on_hit, "
                "'resume' from it runs the guest on"]
    except (HookError, RpcError) as e:
        return [f"error: {e}"]
