# SPDX-License-Identifier: MIT
"""Console builtins the GUI adds on top of rmm_86box's (help, vm, autopause, autoresume).

Group-level words run once, in the app, whatever @targets the line names (the targets become
their default argument); node-level words run on each targeted node's own worker.

    use NAME                       make NAME the target of lines with no @
    nodes                          every node: state, port, how it is run, why it stopped
    halt [a,b|*]                   pause nodes together (default: the @targets, else the locked ones)
    resume [a,b|*]                 resume them together; resuming all clears the halt banner
    unlock NODE | lock NODE        take a node out of group snap/restore/halt/resume, put it back
    lock                           which nodes are locked, and where the unlocked ones branched
    rollback TAG|#ID [--paused]    every node back to it; unlocked nodes' own snapshots deleted, relocked
    group show | policy | save FILE [--resolved] | export FILE | load FILE | bundle export|import
    group halt-all on|off          pause every node when one stops
    group halt-on breakpoint,watchpoint,crash,...
    copy screen | regs | out | last   to the clipboard (screen/regs of the target nodes)
    snap [TAG]                     snapshot every locked node together (@a snap: only a; @a,b: those)
    restore TAG|#ID [--paused]     roll the snapshot's locked nodes back together (@a restore: only a)
    snaps [tree]                   the snapshot family tree as text
    snaps view                     the Snapshots tab (F12)
    snaps pick TAG|#ID             select it in the tab (its Restore / Delete buttons)
    snaps delete TAG|#ID           drop it on its nodes (@a: only on a)
    snaps save FILE                the tree as JSON
    clear | quit
  per node:
    poll on | off | rate=FPS | step=N
    paste                          type the clipboard into the guest
    hook [w3|b2 FILE.py [k=v..] | new FILE.py [from=EX] | examples | reload w3 | off w3]
                                   Python on a stop: FILE's on_hit(hit) runs in the GUI (rmmgui/hooks.py)
    sym [load FILE | base | find | verify | list | break NAME | watch NAME | NAME|ADDR | ghidra-script DIR]
                                   symbols from a linker map, nm, or a Ghidra export (rmmgui/symbols.py)
    demo timetravel | vga | step | vm | life [rows=N run_ms=MS gens=N report=N log=FILE tsc=1] | gamesnap [trigger=watch|kbd rounds=N drive=0|1 log=FILE]
"""

import json

from rmm_86box.console import Builtin

from . import demos, hooks, symbols
from .group import STOP_REASONS, GroupError
from .snaps import SnapError

GROUP_WORDS = ("use", "nodes", "halt", "resume", "group", "copy", "snap", "restore", "snaps", "lock", "unlock",
               "rollback", "clear", "quit")
SNAPS_SUBS = ("tree", "view", "pick", "delete", "save")
GROUP_SUBS = ("show", "policy", "save", "export", "load", "bundle", "halt-all", "halt-on")
COPY_WHAT = ("screen", "regs", "out", "last")


class Commands:
    def __init__(self, app):
        self.app = app

    # -- wiring into each node's Console --------------------------------------------------

    def for_node(self, node):
        """{word: Builtin} for one node's console: completion everywhere, and run() for lines
        that reach the node directly (a node's "run" list, --run)."""
        names = lambda d, c, comp: self._node_args(d)  # noqa: E731
        g = lambda word, help, comp=None: Builtin(  # noqa: E731
            help, lambda args, w=word: self.run_group(w, args, None), comp)
        return {
            "use": g("use", "use NAME  - the node lines without @ go to", names),
            "nodes": g("nodes", "nodes  - list the group's nodes"),
            "halt": g("halt", "halt [a,b|*]  - pause nodes together (default all)", names),
            "resume": g("resume", "resume [a,b|*]  - resume nodes together (default all)", names),
            "group": g("group", "group show | policy | save FILE [--resolved] | export FILE | load FILE | "
                                "halt-all on|off | halt-on R,R..", self._group_complete),
            "copy": g("copy", "copy screen | regs | out | last  - to the clipboard",
                      lambda d, c, comp: list(COPY_WHAT) if not d else []),
            "snap": g("snap", "snap [TAG]  - snapshot every locked node together (@a snap: only a)"),
            "unlock": g("unlock", "unlock NODE  - group snap/restore/halt/resume skip it until `lock NODE`", names),
            "lock": g("lock", "lock [NODE]  - back in step with the group as it is now; no NODE: the lock state",
                      names),
            "rollback": g("rollback", "rollback TAG|#ID [--paused]  - every node back to it, unlocked nodes' own "
                                      "snapshots deleted, all locked again", self._restore_complete),
            "restore": g("restore", "restore TAG|#ID [--paused]  - roll its nodes back together (@a: only a)",
                         self._restore_complete),
            "snaps": g("snaps", "snaps [tree] | view | pick X | delete X | save FILE  - the snapshot tree",
                       self._snaps_complete),
            "clear": g("clear", "clear  - empty the console"),
            "quit": g("quit", "quit  - close the demo (and 86Box instances it started)"),
            "poll": Builtin("poll on | off | rate=FPS | step=N  - guest frame polling",
                            lambda args: self.poll(node, args),
                            lambda d, c, comp: ["on", "off", "rate=", "step="]),
            "paste": Builtin("paste  - type the clipboard into the guest", lambda args: self.paste(node)),
            "hook": Builtin("hook w3|b2 FILE.py [k=v ..] | new FILE.py | reload w3 | off w3  - run a Python "
                            "script's on_hit(hit) at every stop (no args: the hooks)",
                            lambda args: hooks.console(node, args), self._hook_complete),
            "sym": Builtin("sym load FILE | base | find | verify | list | break NAME | watch NAME | NAME|ADDR  - "
                           "symbols from a linker map, nm or a Ghidra export (no args: the tables)",
                           lambda args: symbols.console(node, args), lambda d, c, comp: self._sym_complete(node, d)),
            "demo": Builtin("demo timetravel | vga | step | vm | life | gamesnap  - guided highlights",
                            lambda args: demos.run(node, args[0] if args else "", args[1:]),
                            lambda d, c, comp: list(demos.NAMES) if not d else []),
        }

    def _hook_complete(self, done, cur, comp):
        if not done:
            try:
                ids = [f"w{i}" for i in comp.provider.watch_ids()] + [f"b{i}" for i in comp.provider.break_ids()]
            except Exception:  # noqa: BLE001 - offline / no provider: just the words
                ids = []
            return ["new", "examples", "reload", "off", "help"] + ids
        if done[0] in ("reload", "off") and len(done) == 1:
            return [hooks.ref_of(k) for k, r in self._cur_hooks()]
        return []

    def _sym_complete(self, node, done):
        S = node.symbols
        if not done:
            return list(symbols.SUBWORDS) + S.names()
        if done[0] in ("base", "find", "verify", "drop") and len(done) == 1:
            return list(S.tables)
        if done[0] in ("break", "watch") and len(done) == 1:
            return S.names()
        if done[0] == "watch" and len(done) >= 2:
            return ["1", "2", "4", "r", "w", "rw"]
        if done[0] == "load" and len(done) >= 2:
            return ["as=", "exe=", "seg=", "psp=", "base=", "sec1=", "origin="]
        return []

    def _cur_hooks(self):
        n = self.app.group.node() if self.app.group else None
        return [(k, r) for k, r in (n.hit_rules.items() if n else []) if r.get("hook")]

    def _node_args(self, done):
        if done:
            return []
        return list(self.app.group.nodes) + ["*"]

    def _group_complete(self, done, cur, comp):
        if not done:
            return list(GROUP_SUBS)
        if len(done) == 1 and done[0] == "halt-all":
            return ["on", "off"]
        if len(done) == 1 and done[0] == "halt-on":
            return [",".join(self.app.group.policy.halt_on)] + list(STOP_REASONS)
        if len(done) == 2 and done[0] == "save":
            return ["--resolved"]
        return []

    def _snap_refs(self):
        tree = self.app.group.tree
        tags, refs = [], []
        for r in tree.recs.values():
            if r.live():
                refs.append(r.ref)
                if r.tag not in tags:
                    tags.append(r.tag)
        return tags + refs

    def _restore_complete(self, done, cur, comp):
        if not done:
            return self._snap_refs()
        return ["--paused"] if len(done) == 1 else []

    def _snaps_complete(self, done, cur, comp):
        if not done:
            return list(SNAPS_SUBS)
        if len(done) == 1 and done[0] in ("delete", "pick"):
            return self._snap_refs()
        return []

    # -- group-level ----------------------------------------------------------------------

    def run_group(self, word, args, targets=None):
        """targets: the nodes a line's @ named, or None when it named none."""
        fn = getattr(self, "g_" + word)
        return fn(args, targets)

    def _targets(self, targets):
        return targets or [self.app.group.node()]

    def _pick(self, args, targets):
        """Nodes named by args ("a,b", "a b", "*"), else the @targets."""
        grp = self.app.group
        if not args:
            return targets
        names = [x for a in args for x in a.split(",") if x]
        if "*" in names or "all" in names:
            return list(grp.nodes.values())
        missing = [n for n in names if n not in grp.nodes]
        if missing:
            raise GroupError(f"no node {', '.join(missing)} (nodes: {', '.join(grp.nodes)})")
        return [grp.nodes[n] for n in names]

    def g_use(self, args, targets):
        if not args:
            return [f"using {self.app.group.current}"]
        self.app.group.use(args[0])
        return [f"using {args[0]}"]

    def g_nodes(self, args, targets):
        grp = self.app.group
        out = []
        for n in grp.nodes.values():
            mark = "*" if n.name == grp.current else " "
            how = n.spec.mode + (f" pid {n.proc.pid}" if n.proc else "")
            lk = "locked" if n.name in grp.locked else "UNLOCKED"
            out.append(f"{mark} {n.name:12} {n.state:9} {lk:8} {n.address:<16} {how:18} {n.stop_note}")
        out.append(f"policy: {grp.policy}")
        return out

    def g_halt(self, args, targets):
        nodes = self._pick(args, targets or self.app.group.locked_nodes())
        self.app.group.halt(nodes)
        return [f"paused {', '.join(n.name for n in nodes) or 'nothing (every node is unlocked)'}"]

    def g_resume(self, args, targets):
        nodes = self._pick(args, targets or self.app.group.locked_nodes())
        self.app.group.resume(nodes)
        return [f"resumed {', '.join(n.name for n in nodes) or 'nothing (every node is unlocked)'}"]

    def _lock_names(self, args, targets):
        nodes = self._pick(args, targets)
        if not nodes:
            raise GroupError("name a node: unlock b, or @b unlock")
        return [n.name for n in nodes]

    def g_unlock(self, args, targets):
        grp = self.app.group
        if not args and not targets:
            return self._lock_lines()
        out = []
        for nm in self._lock_names(args, targets):
            grp.unlock(nm)
            out.append(f"{nm} unlocked: group snap / restore / halt / resume skip it until `lock {nm}`")
        return out

    def g_lock(self, args, targets):
        grp = self.app.group
        if not args and not targets:
            return self._lock_lines()
        out = []
        for nm in self._lock_names(args, targets):
            grp.relock(nm)
            out.append(f"{nm} locked again as it is now; its own snapshots stay in the tree as a side branch")
        return out

    def _lock_lines(self):
        grp = self.app.group
        tree = grp.tree
        out = [f"locked: {', '.join(n for n in grp.nodes if n in grp.locked) or '-'}"]
        for u, f in grp.forks.items():
            bp = grp.branch_point(u)
            own = grp.own_records(u)
            out.append(f"unlocked: {u}  from " + ("start" if bp is None else f"#{bp} {tree.recs[bp].tag}")
                       + (f"; own snapshots: {', '.join(r.tag for r in own)}" if own else ""))
        if not grp.forks:
            out.append("every node is locked (unlock NODE takes one out)")
        return out

    def g_rollback(self, args, targets):
        if not args:
            return ["rollback TAG|#ID [--paused]   (a snapshot every node holds)"]
        run = "--paused" not in args
        rec, done, failed, dropped = self.app.group.rollback_group(args[0], run=run)
        out = [f"rolled back to {rec.ref} {rec.tag} on {', '.join(done) or 'nothing'}"]
        for u, tags in dropped.items():
            out.append(f"{u}: locked again" + (f", deleted {', '.join(tags)}" if tags else ""))
        out += [f"error: {n}: {why}" for n, why in failed.items()]
        return out

    def g_group(self, args, targets):
        grp = self.app.group
        sub = args[0] if args else "show"
        if sub == "show":
            out = [f"group {grp.spec.name}" + (f"  ({grp.spec.path})" if grp.spec.path else "  (not from a file)"),
                   f"policy: {grp.policy}"]
            if grp.forks:
                out.append(f"unlocked: {', '.join(grp.forks)}")
            if grp.halted:
                h = grp.halted
                out.append(f"halted by {h['by']}: {h['detail'] or h['reason']}; paused {', '.join(h['paused']) or '-'}")
            out += self.g_nodes([], targets)[:-1]
            return out
        if sub == "policy":
            return [json.dumps(grp.policy.to_spec())]
        if sub == "halt-all":
            if len(args) > 1:
                grp.set_policy(halt_all=args[1] in ("on", "1", "true"))
            return [f"policy: {grp.policy}"]
        if sub == "halt-on":
            if len(args) > 1:
                grp.set_policy(halt_on=[x for a in args[1:] for x in a.split(",") if x])
            return [f"policy: {grp.policy}"]
        if sub == "bundle":
            return self.g_bundle(args[1:])
        if len(args) < 2:
            return [f"group {sub} FILE"]
        path = args[1]
        if sub == "save":
            return [f"saved {grp.save(path, resolved='--resolved' in args)}"]
        if sub == "export":
            return [f"exported {grp.export(path)} (live configs as inline domains; add launch blocks to start them)"]
        if sub == "load":
            self.app.load_group_path(path)
            return [f"loading {path}"]
        return [f"unknown: group {sub}"]

    def g_bundle(self, args):
        from . import bundle
        usage = ["group bundle export FILE.zip [full|latest]  |  group bundle import FILE.zip DIR"]
        if len(args) < 2 or args[0] not in ("export", "import"):
            return usage
        say = self.app.say
        try:
            if args[0] == "export":
                m = bundle.export_bundle(self.app, args[1], args[2] if len(args) > 2 else "full", say)
                return [f"bundle {args[1]}: {len(m['nodes'])} node(s), {m['mode']}"]
            if len(args) < 3:
                return usage
            bundle.import_bundle(self.app, args[1], args[2], say)
            return [f"importing {args[1]} into {args[2]}"]
        except bundle.BundleError as e:
            b = self.app.bundle
            if b is not None and b.state == "running":
                b.end("failed", str(e))
            return [f"error: {e}"]

    def g_copy(self, args, targets):
        what = args[0] if args else "last"
        targets = self._targets(targets)
        multi = len(targets) > 1
        if what == "screen":
            text = []
            for n in targets:
                if multi:
                    text.append(f"--- {n.name} ---")
                text += [ln.rstrip() for ln in n.rmm.text_screen()]
            body = "\n".join(text)
        elif what == "regs":
            text = []
            for n in targets:
                r = n.rmm.regs()
                line = "  ".join(f"{k}={v:08X}" if isinstance(v, int) else f"{k}={v}" for k, v in r.items())
                text.append(f"{n.name}: {line}" if multi else line)
            body = "\n".join(text)
        elif what == "out":
            body = "\n".join(t for t, _, _ in self.app.out)
        elif what == "last":
            body = "\n".join(self.app.last_block())
        else:
            return ["copy " + " | ".join(COPY_WHAT)]
        how = self.app.clip.put(body)
        return [f"copied {what}: {len(body.splitlines())} lines, {len(body)} chars ({how})"]

    def g_snap(self, args, targets):
        rec, failed = self.app.group.snap(targets, args[0] if args else None)
        out = [f"snap {rec.ref} {rec.tag}: saved on {', '.join(rec.nodes)}"]
        out += [f"error: {n}: {why}" for n, why in failed.items()]
        return out

    def g_restore(self, args, targets):
        if not args:
            return ["restore TAG|#ID [--paused]   (snaps lists them)"]
        run = "--paused" not in args
        rec, done, failed = self.app.group.restore(args[0], targets, run=run)
        out = [f"restored {rec.ref} {rec.tag} on {', '.join(done) or 'nothing'}"
               + ("" if run else "; left paused (resume continues them together)")]
        out += [f"error: {n}: {why}" for n, why in failed.items()]
        return out

    def g_snaps(self, args, targets):
        grp = self.app.group
        sub = args[0] if args else "tree"
        if sub == "tree":
            return grp.tree.lines(list(grp.nodes))
        if sub == "view":
            self.app.show_snaps = not self.app.show_snaps
            return [f"Snapshots tab {'shown' if self.app.show_snaps else 'hidden'} (F12)"]
        if sub == "delete" and len(args) > 1:
            rec, gone, failed = grp.delete_snap(args[1], targets)
            return ([f"deleted {rec.ref} {rec.tag} on {', '.join(gone) or 'nothing'}"]
                    + [f"error: {n}: {why}" for n, why in failed.items()])
        if sub == "pick" and len(args) > 1:
            rec = grp.tree.find(args[1], [n.name for n in targets] if targets else None)
            grp.tree.selected = rec.id
            self.app.show_snaps = True
            return [grp.tree.describe(rec, list(grp.nodes))]
        if sub == "save" and len(args) > 1:
            return [f"saved {grp.tree.save(args[1])}"]
        return ["snaps [tree] | view | pick TAG|#ID | delete TAG|#ID | save FILE"]

    def g_clear(self, args, targets):
        self.app.out.clear()
        return []

    def g_quit(self, args, targets):
        self.app.running = False
        return []

    # -- node-level -----------------------------------------------------------------------

    def poll(self, node, args):
        p = node.poller
        for a in args:
            if a in ("on", "off"):
                p.enabled = a == "on"
            elif a.startswith("rate="):
                p.rate = float(a[5:])
                node.poll_rate = p.rate
            elif a.startswith("step="):
                p.step = max(1, min(8, int(a[5:])))
        return [f"poll {'on' if p.enabled else 'off'}, rate {p.rate:g} fps, step {p.step}"]

    def paste(self, node):
        text = self.app.clip.get()
        if not text:
            return ["clipboard is empty"]
        lines = text.replace("\r\n", "\n").split("\n")
        for i, ln in enumerate(lines):
            if ln:
                node.rmm.call("input.text.type", text=ln)
            if i < len(lines) - 1:
                node.rmm.call("input.key.send", combo="enter")
        return [f"typed {len(text)} chars into {node.name}"]
