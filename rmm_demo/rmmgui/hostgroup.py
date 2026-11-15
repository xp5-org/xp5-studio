# SPDX-License-Identifier: MIT
"""The running group: its nodes, the halt policy, and console targeting. No Qt here.

Targets, at the start of a console line:
    @b machine.status            node b
    @a,b machine.pause           a and b
    @* machine.status            every node
    @GROUPNAME machine.status    every node (the group's own name)
    machine.status               the current node (`use NAME` changes it)

Group snapshots (snap / restore, see snaps.py): every targeted node is paused, saved under one
tag, and resumed if it was running; a restore pauses them, rolls each back, and resumes them
together. The pauses go out in parallel, so the nodes' snapshots are taken within a round trip
or two of each other (tens to hundreds of ms of guest time apart), not at one instant.

Halt-all: when a node stops for a reason in policy.halt_on (breakpoint, watchpoint, crash, ...)
and policy.halt_all is on, every other running node is sent machine.pause at once, from one
thread per node. The others stop a little after the first: each pause is a round trip and
86Box applies it on its UI thread, so expect tens to a few hundred milliseconds of skew, not
lockstep. Their own pause events are recognised and do not cascade.

Lock / unlock (the Snapshot timeline dock): every node starts locked, i.e. it follows the
group's snap, restore, halt and resume. `unlock b` takes b out: those group commands skip it
(an explicit @b still reaches it), so b can be rewound to an older snapshot, run, snapped and
paused on its own while the others go on. Halt-all leaves it alone too: its own stop pauses
nobody else, and another node's stop does not pause it (policy halt_unlocked changes both).
`lock b` takes it back as it is now: the next group snapshot includes it again, and what it
saved meanwhile stays in the tree as a side branch. `rollback TAG` restores every node, locked
or not, to a snapshot all of them hold, deletes what the unlocked ones saved since they were
unlocked, and locks them again; it checks every node first and changes nothing if one cannot.
"""

import contextlib
import re
import threading
import time

from rmm_86box import RpcError

from .group import GroupError, GroupSpec, norm_reasons
from .node import Node
from .snaps import SnapError, SnapTree

SNAP_LINE = re.compile(r"^snapshot\.(save|restore|delete)\s+(?:tag=)?(\S+)")


class TargetError(ValueError):
    pass


class HostGroup:
    def __init__(self, spec, vm_store, say, builtins=None):
        self.spec = spec
        self.vm_store = vm_store
        self._say = say                          # say(text, tone, node_or_None)
        self.builtins = builtins                 # fn(node) -> {name: Builtin}, per node console
        self.policy = spec.policy
        self.nodes = {}
        self.current = None
        self.halted = None                       # {"by", "reason", "detail", "t", "paused": [...]}
        self.history = []                        # the last halts, newest first
        self.t0 = time.time()                    # when this group started (the timeline's left edge)
        vm_config_dir = spec.vm_config_dir
        for ns in spec.nodes:
            n = Node(ns, vm_store, say, vm_config_dir)
            n.listeners.append(self._on_stop)
            n.snap_listeners.append(self._on_snap_list)
            n.line_hooks.append(self._on_line)
            self.nodes[ns.name] = n
        self.current = next(iter(self.nodes))
        self.tree = SnapTree(self.nodes)
        self.locked = frozenset(self.nodes)      # replaced, never changed in place (other threads read it)
        self.forks = {}                          # unlocked node -> {"from", "seq", "t", "base", "since"}
        self._snap_ops = 0                       # group snap/restore/delete in flight
        self._snap_done = 0.0                    # when the last one ended
        self._snap_lock = threading.Lock()       # one group snapshot op at a time

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self):
        """Start nodes in order (depends_on already applied). A node that fails to start is
        reported and left offline; the rest still start."""
        for n in self.nodes.values():
            try:
                n.start(self.builtins)
                wait = (n.spec.launch or n.spec.qemu or {}).get("wait")
                if wait and n.spec.mode in ("launch", "qemu"):
                    self._wait_connected(n, float(wait))
            except Exception as e:  # noqa: BLE001 - one bad node must not stop the group
                n.ended = ("error", str(e))
                n.conn_msg = f"not started: {e}"
                self.say(f"[{n.name}] not started: {e}", "err")
        for line in self.spec.run:
            self.run_line(line)

    def _wait_connected(self, n, secs):
        deadline = time.time() + secs
        while time.time() < deadline and not n.connected and not n.ended:
            time.sleep(0.2)

    def add_node(self, name, body):
        """Add a node to the running group: attach to an 86Box ({"host", "port"}) or launch one
        ({"launch": {...}, ...}). The group file is checked with it first (name, port clashes),
        so a bad body changes nothing. Returns the started Node."""
        if name in self.nodes:
            raise GroupError(f"there is already a node {name!r}")
        raw = dict(self.spec.raw, nodes=dict(self.spec.raw.get("nodes") or {}, **{name: body}),
                   policy=self.policy.to_spec())
        spec = GroupSpec(raw, self.spec.path)
        spec.policy = self.policy
        ns = spec.node(name)
        n = Node(ns, self.vm_store, self._say, spec.vm_config_dir)
        n.listeners.append(self._on_stop)
        n.snap_listeners.append(self._on_snap_list)
        n.line_hooks.append(self._on_line)
        self.spec = spec
        with self.tree.lock:
            self.tree.node_names.append(name)
            self.tree.heads[name] = None
        self.nodes = dict(self.nodes, **{name: n})       # a new dict: other threads may be iterating
        self.locked = self.locked | {name}
        try:
            n.start(self.builtins)
        except Exception as e:  # noqa: BLE001 - keep it listed, offline, with the reason
            n.ended = ("error", str(e))
            n.conn_msg = f"not started: {e}"
            raise
        return n

    def stop(self):
        threads = [threading.Thread(target=n.stop, daemon=True) for n in self.nodes.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

    def say(self, text, tone="text"):
        self._say(text, tone, None)

    # -- targeting ---------------------------------------------------------------------------

    def node(self, name=None):
        return self.nodes[name or self.current]

    def use(self, name):
        if name not in self.nodes:
            raise TargetError(f"no node {name!r} (nodes: {', '.join(self.nodes)})")
        self.current = name

    def split_target(self, line):
        """('@a,b rest') -> ([nodes], 'rest'); no @ -> ([current], line)."""
        line = line.strip()
        if not line.startswith("@"):
            return [self.node()], line
        head, _, rest = line.partition(" ")
        names = head[1:]
        if names in ("*", "all") or (names == self.spec.name and names not in self.nodes):
            return list(self.nodes.values()), rest.strip()
        out = []
        for nm in filter(None, names.split(",")):
            if nm not in self.nodes:
                raise TargetError(f"@{nm}: no such node (nodes: {', '.join(self.nodes)})")
            out.append(self.nodes[nm])
        if not out:
            raise TargetError("@: name a node, a,b or *")
        return out, rest.strip()

    def run_line(self, line):
        """Queue a console line on its target node(s). With several targets each output line
        is prefixed with the node's name (the say() callback adds it)."""
        try:
            targets, rest = self.split_target(line)
        except TargetError as e:
            self.say(f"error: {e}", "err")
            return
        if not rest:
            if len(targets) == 1:          # "@b" alone: switch to it
                self.use(targets[0].name)
                self.say(f"using {targets[0].name}", "muted")
            return
        for n in targets:
            n.put_line(rest)

    # -- halt-all ----------------------------------------------------------------------------

    def _on_stop(self, node, reason, params):
        """A node's listener: something stopped it. Runs on that node's reader thread."""
        detail = params.get("detail") or node.stop_note
        if reason in ("crash", "exit"):
            self.tree.forget_node_state(node.name)     # its snapshots died with the process
        if not self.policy.triggers(reason):
            if reason in ("crash", "disconnect", "exit"):
                self.say(f"[{node.name}] {reason}: {detail}", "err" if reason == "crash" else "warn")
            return
        reach = self.policy.halt_unlocked
        if node.name not in self.locked and not reach:
            self.say(f"[{node.name}] stopped ({detail or reason}); it is unlocked, so the others keep running", "warn")
            return
        others = [n for n in self.nodes.values() if n is not node and n.connected
                  and (reach or n.name in self.locked)]
        paused = []                                  # not self.halted["paused"]: a resume may clear halted mid-fanout
        self.halted = {"by": node.name, "reason": reason, "detail": detail, "t": time.time(), "paused": paused}
        self.history.insert(0, self.halted)
        del self.history[20:]
        self.say(f"HALT ALL: {node.name} stopped ({detail or reason}); pausing "
                 f"{', '.join(n.name for n in others) or 'nothing else'}", "warn")
        self._fanout(others, lambda n: n.pause(induced=True) and paused.append(n.name))

    def halt(self, nodes=None, induced=False):
        """Pause these nodes (default the locked ones) together."""
        nodes = self.locked_nodes() if nodes is None else nodes
        self._fanout(nodes, lambda n: n.pause(induced=induced))

    def resume(self, nodes=None):
        """Resume these nodes (default the locked ones) together; clears the halt banner when
        every locked node goes."""
        nodes = self.locked_nodes() if nodes is None else nodes
        self._fanout(nodes, lambda n: n.resume())
        if {n.name for n in nodes} >= self.locked:
            self.halted = None

    @staticmethod
    def _fanout(nodes, fn):
        ts = [threading.Thread(target=fn, args=(n,), daemon=True) for n in nodes]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)

    def set_policy(self, halt_all=None, halt_on=None):
        if halt_all is not None:
            self.policy.halt_all = bool(halt_all)
        if halt_on is not None:
            self.policy.halt_on = norm_reasons(halt_on)

    # -- lock / unlock -----------------------------------------------------------------------

    def locked_nodes(self):
        locked = self.locked
        return [n for nm, n in self.nodes.items() if nm in locked]

    def unlocked(self):
        locked = self.locked
        return [nm for nm in self.nodes if nm not in locked]

    def _name(self, name):
        if name not in self.nodes:
            raise TargetError(f"no node {name!r} (nodes: {', '.join(self.nodes)})")
        return name

    def unlock(self, name):
        """Take a node out of the group's snap / restore / halt / resume."""
        self._name(name)
        if name not in self.locked:
            raise GroupError(f"{name} is already unlocked")
        now = time.time()
        with self.tree.lock:
            head = self.tree.heads.get(name)
            self.forks = dict(self.forks, **{name: {"from": head, "seq": self.tree.seq, "t": now,
                                                    "base": head, "since": now}})
        self.locked = self.locked - {name}

    def relock(self, name):
        """Put a node back in step with the group, as it is now."""
        self._name(name)
        if name in self.locked:
            raise GroupError(f"{name} is already locked")
        self.forks = {k: v for k, v in self.forks.items() if k != name}
        self.locked = self.locked | {name}

    def own_records(self, name):
        """What an unlocked node saved on its own since it was unlocked (oldest first)."""
        fork = self.forks.get(name)
        if fork is None:
            return []
        with self.tree.lock:
            return [r for r in self.tree.recs.values()
                    if r.id > fork["seq"] and name in r.nodes and r.scope != "group"]

    def branch_point(self, name):
        """The group snapshot a node's current state descends from (None: the start): walk its
        head back through its own parents until a group record."""
        tree = self.tree
        with tree.lock:
            h = tree.heads.get(name)
            seen = set()
            while h is not None and h in tree.recs and tree.recs[h].scope != "group" and h not in seen:
                seen.add(h)
                h = tree.recs[h].parents.get(name)
            return h if h in tree.recs else None

    def lock_state(self):
        return {"locked": [n for n in self.nodes if n in self.locked], "forks": dict(self.forks)}

    def set_lock_state(self, data):
        """From a project file: nodes it does not name stay locked."""
        if not data:
            return
        forks = {n: dict(f) for n, f in (data.get("forks") or {}).items() if n in self.nodes}
        self.forks = forks
        self.locked = frozenset(n for n in self.nodes if n not in forks)

    def rollback_plan(self, what):
        """(record, {unlocked node: [its own records still live]}) for rollback_group, or a
        SnapError when the record is not on every node."""
        rec = self.tree.find(what)
        missing = [x for x in self.nodes if x not in rec.nodes or not rec.live(x)]
        if missing:
            raise SnapError(f"{rec.ref} {rec.tag} is not on {', '.join(missing)}: roll back to a snapshot "
                            "every node holds")
        return rec, {u: [r for r in self.own_records(u) if r.live(u)] for u in self.forks}

    def rollback_group(self, what, run=True):
        """Every node, locked or not, back to one snapshot together; the unlocked nodes' own
        snapshots since they were unlocked are deleted and they are locked again. Changes
        nothing if any node cannot restore it. Returns (record, restored, failed, {node: [tags
        deleted]})."""
        rec, discard = self.rollback_plan(what)
        rec, done, failed = self.restore(rec.ref, list(self.nodes.values()), run=run)
        dropped = {}
        for u, recs in discard.items():
            if u not in done:
                continue
            dropped[u] = []
            for r in recs:
                _, gone, _ = self.delete_snap(r.ref, [self.nodes[u]])
                if u in gone:
                    dropped[u].append(r.tag)
            self.relock(u)
        return rec, done, failed, dropped

    # -- snapshots ---------------------------------------------------------------------------

    @contextlib.contextmanager
    def _snap_op(self):
        with self._snap_lock:
            self._snap_ops += 1
            try:
                yield
            finally:
                self._snap_ops -= 1
                self._snap_done = time.time()

    def _pause_running(self, nodes):
        """Pause those of nodes that are running (as the group's own pause); returns them."""
        was = []

        def one(n):
            try:
                if n.rmm.call("machine.status").get("state") == "running":
                    n.induced_until = time.time() + 3.0
                    n.rmm.call("machine.pause")
                    was.append(n)
            except (RpcError, OSError):
                pass
        self._fanout(nodes, one)
        return was

    def snap(self, nodes=None, tag=None):
        """Save `tag` (default s<N>) on these nodes (default every locked node) together. Returns
        the record and {node: why} for nodes it could not save on."""
        whole = nodes is None
        nodes = self.locked_nodes() if whole else list(nodes)
        if not nodes:
            raise SnapError("every node is unlocked: lock one again, or name nodes (@b snap)")
        failed = {n.name: f"not connected ({n.state})" for n in nodes if not n.connected}
        live = [n for n in nodes if n.connected]
        if not live:
            raise SnapError("no connected node to snapshot: " + ", ".join(f"{k} {v}" for k, v in failed.items()))
        tag = tag or self.tree.auto_tag({t for n in live for t in n.snaps})
        self.tree.check_tag(tag)
        saved, tsc = [], {}
        with self._snap_op():
            was = self._pause_running(live)

            def save(n):
                try:
                    try:
                        n.rmm.call("snapshot.save", tag=tag)
                    except RpcError as e:
                        if e.code != 1:              # 1: not paused; something resumed it since
                            raise
                        n.induced_until = time.time() + 3.0
                        n.rmm.call("machine.pause")
                        was.append(n)
                        n.rmm.call("snapshot.save", tag=tag)
                    tsc[n.name] = n.rmm.call("machine.status").get("tsc")
                    saved.append(n.name)
                except (RpcError, OSError) as e:
                    failed[n.name] = str(e)
            self._fanout(live, save)
            self._fanout(was, lambda n: n.resume())
            if not saved:
                raise SnapError(f"snap {tag} failed on every node: " +
                                "; ".join(f"{k}: {v}" for k, v in failed.items()))
            order = [n for n in self.nodes if n in saved]
            names = {n.name for n in nodes}
            whole = whole or names == set(self.nodes) or names == set(self.locked)
            rec = self.tree.add(tag, order, "group" if whole else "nodes", failed, tsc)
            for n in live:
                n.snaps = [t for t in n.snaps if t != tag] + ([tag] if n.name in saved else [])
        return rec, failed

    def restore(self, what, nodes=None, run=True):
        """Roll the nodes of snapshot `what` ('#id' or a tag) back to it, all together; with
        `nodes`, only those (they must hold it). Without `nodes`, unlocked nodes are skipped.
        Checks every node first and changes nothing if one cannot. run=False leaves them paused."""
        rec = self.tree.find(what, [n.name for n in nodes] if nodes else self.locked or None)
        if nodes is None:
            names = [x for x in rec.nodes if x in self.locked]
            if not names:
                raise SnapError(f"{rec.ref} {rec.tag} is only on unlocked nodes ({', '.join(rec.nodes)}): "
                                f"@{rec.nodes[0]} restore {rec.ref} restores it there")
        else:
            names = [n.name for n in nodes]
        outside = [x for x in names if x not in rec.nodes]
        if outside:
            raise SnapError(f"{rec.ref} {rec.tag} holds {', '.join(rec.nodes)}, not {', '.join(outside)}")
        targets = [self.nodes[x] for x in names]
        with self._snap_op():
            missing = {}
            for n in targets:
                if not n.connected:
                    missing[n.name] = n.state
                    continue
                try:
                    tags = [s["tag"] for s in n.rmm.call("snapshot.list")["snapshots"]]
                except (RpcError, OSError) as e:
                    missing[n.name] = str(e)
                    continue
                if rec.tag not in tags or not rec.live(n.name):
                    missing[n.name] = "no longer holds it" if rec.tag not in tags else rec.status.get(n.name)
            if missing:
                raise SnapError(f"cannot restore {rec.ref} {rec.tag} (nothing was changed): " +
                                "; ".join(f"{k} {v}" for k, v in missing.items()))
            self._pause_running(targets)
            done, failed = [], {}

            def one(n):
                n.induced_until = time.time() + 3.0
                try:
                    n.rmm.call("snapshot.restore", tag=rec.tag)
                    done.append(n)
                except (RpcError, OSError) as e:
                    failed[n.name] = str(e)
            self._fanout(targets, one)
            self.tree.restored(rec, [n.name for n in done])
            now = time.time()
            for n in done:
                if n.name in self.forks:                 # an unlocked node's own run starts again here
                    self.forks = dict(self.forks, **{n.name: dict(self.forks[n.name], base=rec.id, since=now)})
            for n in done:
                n.regs_queued = True
                n.work.put(("regs", None))
            if run:
                self._fanout(done, lambda n: n.resume())
        return rec, [n.name for n in done], failed

    def delete_snap(self, what, nodes=None):
        rec = self.tree.find(what, [n.name for n in nodes] if nodes else None)
        names = [x for x in (rec.nodes if nodes is None else [n.name for n in nodes]) if rec.live(x)]
        gone, failed = [], {}
        with self._snap_op():
            def one(n):
                try:
                    n.rmm.call("snapshot.delete", tag=rec.tag)
                    gone.append(n.name)
                except (RpcError, OSError) as e:
                    failed[n.name] = str(e)
            self._fanout([self.nodes[x] for x in names if self.nodes[x].connected], one)
            self.tree.deleted(rec, gone)
            for x in gone:
                self.nodes[x].snaps = [t for t in self.nodes[x].snaps if t != rec.tag]
        return rec, gone, failed

    def _on_snap_list(self, node, tags, t):
        """A node's status loop listed its snapshots. Lists asked for during or before a group
        op may not show it yet: skip those."""
        if self._snap_ops or t <= self._snap_done:
            return
        for r in self.tree.reconcile(node.name, tags):
            if r.scope == "console":
                self.say(f"[{node.name}] snapshot {r.tag} taken outside snap: added to the tree as {r.ref}", "muted")

    def _on_line(self, node, line, out, t0):
        """snapshot.save / restore / delete typed on a node's own console."""
        m = SNAP_LINE.match(line.strip())
        if not m or any(o.startswith("error") for o in out):
            return
        verb, tag = m.group(1), m.group(2)
        tree = self.tree
        with self._snap_op():
            if verb == "save":
                if not any(r.tag == tag and r.t >= t0 and r.live(node.name) for r in tree.recs.values()):
                    tree.add(tag, [node.name], "console").note = "snapshot.save on the node's console"
            else:
                try:
                    rec = tree.find(tag, [node.name])
                except SnapError:
                    return
                (tree.restored if verb == "restore" else tree.deleted)(rec, [node.name])

    # -- files -------------------------------------------------------------------------------

    def save(self, path, resolved=False):
        return self.spec.save(path, resolved)

    def export(self, path):
        """A group file from what is running now: each connected node's live config as an
        inline domain plus its port, policy as set. Attach-only (no launch): add launch
        blocks by hand to have the GUI start them."""
        nodes = {}
        for n in self.nodes.values():
            if n.spec.qemu is not None:          # attach to its QMP socket; QEMU has no cfg domain
                qmp = n.proc.qp.qmp_path if n.proc else n.spec.qemu["qmp"]
                nodes[n.name] = {"qemu": {"profile": n.spec.qemu["profile"], "qmp": qmp}}
                continue
            body = {"port": n.port} if not n.spec.mock else {"mock": True}
            if n.connected and not n.spec.mock:
                try:
                    body["domain"] = n.rmm.vm.show()
                except Exception as e:  # noqa: BLE001
                    body["x-export-error"] = str(e)
            nodes[n.name] = body
        spec = GroupSpec({"schema": "rmm-group/1", "name": self.spec.name, "policy": self.policy.to_spec(),
                          "nodes": nodes})
        return spec.save(path)


__all__ = ["HostGroup", "TargetError", "GroupError"]
