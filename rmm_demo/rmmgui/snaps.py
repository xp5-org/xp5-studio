# SPDX-License-Identifier: MIT
"""The snapshot family tree: which snapshots the group took, which VMs each one holds, and what
each one was taken from. No Qt here; hostgroup.py does the RMM calls, view.py draws it.

A record is one `snap` line: a tag saved on one or more nodes at (nearly) the same moment.
Each node has a head: the record it was last saved into or restored from. A new record's
parent, per node, is that node's head, so restoring an older record and snapping again starts
a branch, as in QEMU's snapshot tree or a git history.

The snapshots themselves live inside each 86Box process (at most 32 per VM, lost when it exits);
the tree only names them. A tag one node no longer lists (deleted from its console, replaced by
a later save of the same tag, or its 86Box restarted) is marked on that record and greyed out.
"""

import collections
import json
import re
import threading
import time

TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
OK, GONE, REPLACED = "ok", "gone", "replaced"


class SnapError(ValueError):
    pass


class Snap:
    def __init__(self, sid, tag, nodes, parents, scope, t=None):
        self.id = sid
        self.tag = tag
        self.nodes = list(nodes)                 # the nodes holding it, in group order
        self.parents = dict(parents)             # node -> record id it was taken from (None: start)
        self.scope = scope                       # "group" (every node) | "nodes" | "console" (seen, not ours)
        self.t = t or time.time()
        self.tsc = {}                            # node -> guest TSC at the save
        self.status = {n: OK for n in nodes}     # node -> ok | gone | replaced
        self.failed = {}                         # node -> why the save failed there
        self.note = ""

    @property
    def ref(self):
        return f"#{self.id}"

    def live(self, node=None):
        if node is not None:
            return self.status.get(node) == OK
        return any(v == OK for v in self.status.values())

    def parent(self):
        """The parent drawn as the tree edge: the one most members came from (newest on a tie)."""
        if not self.parents:
            return None
        c = collections.Counter(self.parents.values())
        best = max(c.values())
        return max((p for p, k in c.items() if k == best), key=lambda p: -1 if p is None else p)

    def other_parents(self):
        p = self.parent()
        return sorted({q for q in self.parents.values() if q != p}, key=lambda q: -1 if q is None else q)

    def members_text(self, all_nodes):
        if self.scope == "group" and set(self.nodes) == set(all_nodes) and len(all_nodes) > 1:
            return "all"
        return ",".join(self.nodes)

    def to_json(self):
        return {"id": self.id, "tag": self.tag, "nodes": self.nodes, "parents": self.parents,
                "scope": self.scope, "t": round(self.t, 3), "tsc": self.tsc, "status": self.status,
                "failed": self.failed, "note": self.note}


class SnapTree:
    def __init__(self, node_names):
        self.node_names = list(node_names)
        self.recs = {}                           # id -> Snap, in creation order
        self.heads = {n: None for n in node_names}
        self.seq = 0
        self.selected = None                     # record id picked in the view
        self.lock = threading.RLock()

    # -- naming --------------------------------------------------------------------------

    def auto_tag(self, taken=()):
        """s1, s2, ... not used by any record or any node's own list."""
        used = {r.tag for r in self.recs.values()} | set(taken)
        i = len(self.recs) + 1
        while f"s{i}" in used:
            i += 1
        return f"s{i}"

    @staticmethod
    def check_tag(tag):
        if not TAG_RE.match(tag or ""):
            raise SnapError(f"tag {tag!r}: letters, digits, _ . - (up to 48)")
        if tag.startswith("#"):
            raise SnapError("a tag cannot start with #")

    def find(self, what, nodes=None):
        """A record by '#id' or tag. A tag several records used means the newest one still
        live on the given nodes (else the newest of all)."""
        with self.lock:
            if what.startswith("#"):
                try:
                    return self.recs[int(what[1:])]
                except (ValueError, KeyError):
                    raise SnapError(f"no snapshot {what}") from None
            cands = [r for r in self.recs.values() if r.tag == what]
            if not cands:
                raise SnapError(f"no snapshot {what!r} (snaps lists them)")
            if nodes:
                live = [r for r in cands if all(r.live(n) for n in nodes if n in r.nodes)
                        and any(n in r.nodes for n in nodes)]
                if live:
                    return live[-1]
            return cands[-1]

    # -- changes ------------------------------------------------------------------------

    def add(self, tag, saved, scope, failed=None, tsc=None):
        """A save of `tag` that worked on the nodes in `saved`. Earlier records holding the same
        tag on those nodes lose it there (86Box replaces a tag on save)."""
        with self.lock:
            for r in self.recs.values():
                if r.tag == tag:
                    for n in saved:
                        if r.status.get(n) == OK:
                            r.status[n] = REPLACED
            self.seq += 1
            rec = Snap(self.seq, tag, saved, {n: self.heads.get(n) for n in saved}, scope)
            rec.failed = dict(failed or {})
            rec.tsc = dict(tsc or {})
            self.recs[rec.id] = rec
            for n in saved:
                self.heads[n] = rec.id
            return rec

    def restored(self, rec, nodes):
        with self.lock:
            for n in nodes:
                self.heads[n] = rec.id

    def deleted(self, rec, nodes):
        with self.lock:
            for n in nodes:
                if rec.status.get(n) == OK:
                    rec.status[n] = GONE

    def reconcile(self, node, tags):
        """`tags`: what node's 86Box lists now. Records whose tag it no longer lists are marked
        gone there; a tag it lists that no record has (a `snapshot.save` typed on its console)
        becomes a record of its own. Returns the records it changed."""
        changed = []
        tags = list(tags)
        with self.lock:
            known = set()
            for r in self.recs.values():
                if r.status.get(node) == OK:
                    if r.tag in tags:
                        known.add(r.tag)
                    else:
                        r.status[node] = GONE
                        changed.append(r)
            for tag in tags:
                if tag not in known:
                    rec = self.add(tag, [node], "console")
                    rec.note = "saved outside the group commands"
                    changed.append(rec)
        return changed

    def forget_node_state(self, node):
        """Its 86Box is gone: every snapshot it held went with it."""
        with self.lock:
            for r in self.recs.values():
                if r.status.get(node) == OK:
                    r.status[node] = GONE

    # -- reading ------------------------------------------------------------------------

    def children(self):
        """{parent id or None: [child records]} along the drawn edges."""
        out = collections.defaultdict(list)
        with self.lock:
            for r in self.recs.values():
                out[r.parent()].append(r)
        return out

    def lines(self, all_nodes=None):
        """The tree as indented text, for the console."""
        all_nodes = all_nodes or self.node_names
        kids = self.children()
        heads = collections.defaultdict(list)
        for n, h in self.heads.items():
            heads[h].append(n)
        out = ["start" + (f"   <- {','.join(heads[None])}" if heads[None] else "")]

        def walk(pid, pre):
            ch = kids.get(pid, [])
            for i, r in enumerate(ch):
                last = i == len(ch) - 1
                out.append(pre + ("`-- " if last else "|-- ") + self.describe(r, all_nodes, heads.get(r.id)))
                walk(r.id, pre + ("    " if last else "|   "))
        walk(None, "")
        if len(out) == 1:
            out.append("  (no snapshots yet: snap [TAG] saves every node, @a snap only a)")
        return out

    def describe(self, r, all_nodes, here=None):
        bits = [f"{r.ref} {r.tag}", f"[{r.members_text(all_nodes)}]", time.strftime("%H:%M:%S", time.localtime(r.t))]
        dead = [n for n, s in r.status.items() if s != OK]
        if dead:
            bits.append("lost on " + ",".join(f"{n}({r.status[n]})" for n in dead))
        if r.failed:
            bits.append("failed on " + ",".join(r.failed))
        if r.other_parents():
            bits.append("also from " + ",".join("start" if p is None else f"#{p}" for p in r.other_parents()))
        if here:
            bits.append("<- " + ",".join(here))
        return "  ".join(bits)

    def to_json(self):
        with self.lock:
            return {"heads": self.heads, "snapshots": [r.to_json() for r in self.recs.values()]}

    @classmethod
    def from_json(cls, data, node_names):
        """A tree saved by to_json(), for these nodes. Whether each 86Box still holds a
        snapshot is not known until its next snapshot.list (reconcile marks the lost ones)."""
        t = cls(node_names)
        for d in data.get("snapshots", []):
            r = Snap(d["id"], d["tag"], d["nodes"], {}, d["scope"], d.get("t"))
            r.parents = {n: p for n, p in d.get("parents", {}).items()}
            r.tsc = dict(d.get("tsc", {}))
            r.status = dict(d.get("status", r.status))
            r.failed = dict(d.get("failed", {}))
            r.note = d.get("note", "")
            t.recs[r.id] = r
            t.seq = max(t.seq, r.id)
        for n, h in data.get("heads", {}).items():
            if n in t.heads:
                t.heads[n] = h if h is None or h in t.recs else None
        return t

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2)
            f.write("\n")
        return path


def layout(tree, box_w, box_h, gap_x, gap_y, root_h=None):
    """Family-tree placement: {id or None: (x, y)} box top-left corners (None is "start", root_h
    tall), leaves spread left to right, each parent centred over its children, one row per
    generation. Returns (positions, width, height)."""
    root_h = box_h if root_h is None else root_h
    kids = tree.children()
    pos = {}
    nxt = [0.0]

    def place(pid, depth):
        ch = kids.get(pid, [])
        if not ch:
            x = nxt[0]
            nxt[0] += box_w + gap_x
        else:
            xs = [place(r.id, depth + 1) for r in ch]
            x = (xs[0] + xs[-1]) / 2
        pos[pid] = (x, 0 if depth == 0 else root_h + gap_y + (depth - 1) * (box_h + gap_y))
        return x
    place(None, 0)
    w = max(x for x, _ in pos.values()) + box_w
    h = max(y for _, y in pos.values()) + (box_h if len(pos) > 1 else root_h)
    return pos, w, h
