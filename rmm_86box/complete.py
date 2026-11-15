# SPDX-License-Identifier: MIT
"""Tab completion for a monitor console line (UI-independent, used by the Qt demo and the
readline REPL alike).

Two kinds of line are understood (see console.py):
    snapshot.save tag=before            method name, then name=value params
    rmm.snapshot.save(tag="before")     a Python expression on the library objects; `_` is the
                                        last result, so `_["` offers the keys it returned

    c = Completer(methods, provider)
    r = c.complete("snap")         -> Completion(start=0, items=["snapshot."], hint=...)
    r.apply("snap")                -> "snapshot."   (the line with the common part filled in)

`provider` answers the live questions (which snapshot tags exist, which cfg keys a section
has, ...); any of its methods may be missing or raise, completion then just offers less.
"""

import glob
import os
import re
import shlex

from .registry import CFG_SECTIONS, HINTS

PY_HEAD = re.compile(r"^\s*(rmm|_)\b")
PY_CHAIN = re.compile(r"^(rmm|_)((?:\.[A-Za-z_]\w*|\[\d+\])*)$")


class Completion:
    def __init__(self, start, items, hint=""):
        self.start = start          # index in the line where the completed token begins
        self.items = items          # full replacements for line[start:]
        self.hint = hint            # a line to show: signature, returns, what is expected

    def common(self):
        if not self.items:
            return ""
        return os.path.commonprefix(self.items)

    def apply(self, line):
        """The line with the longest common completion filled in."""
        c = self.common()
        return line[:self.start] + c if c and len(c) >= len(line) - self.start else line

    def __repr__(self):
        return f"Completion({self.start}, {self.items[:8]}{'...' if len(self.items) > 8 else ''}, {self.hint!r})"


def _tokens(text):
    try:
        return shlex.split(text)
    except ValueError:                          # an open quote: fall back to plain split
        return text.split()


class Completer:
    def __init__(self, methods, provider=None, builtins=None, namespace=None):
        """methods: registry.Methods. builtins: {word: Builtin-ish with .complete(args) and
        .help}. namespace: callable returning {"rmm": obj, "_": last result} for the Python
        form."""
        self.methods = methods
        self.provider = provider
        self.builtins = builtins or {}
        self.namespace = namespace or (lambda: {})

    # -- entry ------------------------------------------------------------------------------

    def complete(self, line):
        if PY_HEAD.match(line):
            return self._complete_py(line)
        head, sep, rest = line.lstrip().partition(" ")
        offset = len(line) - len(line.lstrip())
        if not sep:
            return self._complete_name(head, offset)
        if head in self.builtins:
            return self._complete_builtin(head, line)
        return self._complete_params(head, line)

    # -- method names -----------------------------------------------------------------------

    def _complete_name(self, text, offset):
        names = self.methods.names()
        out = set()
        for n in names:
            if n.startswith(text):
                nxt = n.find(".", len(text))
                out.add(n if nxt < 0 else n[:nxt + 1])       # stop at the next segment
        for b in self.builtins:
            if b.startswith(text):
                out.add(b)
        items = sorted(out)
        hint = ""
        m = self.methods.get(text)
        if m:
            hint = m.signature() + (f"   # {m.doc}" if m.doc else "")
        elif len(items) == 1 and self.methods.get(items[0]):
            m = self.methods.get(items[0])
            hint = m.signature()
        elif items:
            hint = "  ".join(items[:12]) + ("  ..." if len(items) > 12 else "")
        return Completion(offset, items, hint)

    # -- name=value params ----------------------------------------------------------------

    def _complete_params(self, method, line):
        m = self.methods.get(method)
        if not m:
            return Completion(len(line), [], f"unknown method {method!r}")
        word_start = len(line) - len(line.split(" ")[-1]) if not line.endswith(" ") else len(line)
        word = line[word_start:]
        given = {t.split("=", 1)[0] for t in _tokens(line[len(line.lstrip().split(" ")[0]):]) if "=" in t}
        hint = m.signature() + (f"   # {m.doc}" if m.doc else "")
        if "=" not in word:
            items = [f"{p}=" for p in m.params if p not in given and p.startswith(word)]
            return Completion(word_start, items, hint)
        pname, _, prefix = word.partition("=")
        typ = m.params.get(pname, "")
        values = self._values(method, pname, typ, line)
        items = [f"{pname}={self._quote(v)}" for v in values if str(v).startswith(prefix.strip('"'))]
        return Completion(word_start, items, f"{pname}: {typ or '?'}   " + hint)

    def _quote(self, v):
        v = str(v)
        return f'"{v}"' if (" " in v or not v) else v

    def _ask(self, name, *a):
        fn = getattr(self.provider, name, None)
        if not fn:
            return []
        try:
            return list(fn(*a) or [])
        except Exception:  # noqa: BLE001 - completion must never break the console
            return []

    def _given(self, line, pname):
        for t in _tokens(line):
            if t.startswith(pname + "="):
                return t.split("=", 1)[1]
        return None

    def _values(self, method, pname, typ, line):
        base = typ.rstrip("?")
        if base == "bool":
            return ["true", "false"]
        m = self.methods.get(method)
        if m and pname in m.enums:
            return [str(v) for v in m.enums[pname]]
        if (method, pname) in HINTS:
            return HINTS[(method, pname)]
        if pname == "tag":
            return self._ask("snapshot_tags")
        if pname == "path":
            return self._paths(self._given(line, "path") or "")
        if pname == "section":
            return self._ask("sections") or CFG_SECTIONS
        if pname == "key" and method.startswith("config."):
            sec = self._given(line, "section")
            return self._ask("cfg_keys", sec.strip('"')) if sec else []
        if pname == "value" and method == "config.set":
            sec, key = self._given(line, "section"), self._given(line, "key")
            return self._ask("cfg_values", (sec or "").strip('"'), (key or "").strip('"'))
        if pname == "id" and method.startswith("debug.break"):
            return [str(i) for i in self._ask("break_ids")]
        if pname == "id" and method.startswith("debug.watch"):
            return [str(i) for i in self._ask("watch_ids")]
        if pname == "addr":
            return ["0xB8000", "0x400", "0x7C00"]
        if pname in ("count",):
            return ["1", "10", "100"]
        return []

    def _paths(self, prefix):
        prefix = prefix.strip('"')
        pat = os.path.expanduser(prefix) + "*"
        out = []
        for p in sorted(glob.glob(pat))[:200]:
            out.append(p + "/" if os.path.isdir(p) else p)
        return out

    # -- builtins -------------------------------------------------------------------------

    def _complete_builtin(self, word, line):
        b = self.builtins[word]
        args = line.lstrip()[len(word) + 1:]
        toks = args.split(" ")
        cur = toks[-1]
        start = len(line) - len(cur)
        try:
            cands = b.complete(toks[:-1], cur, self) or []
        except Exception:  # noqa: BLE001
            cands = []
        return Completion(start, [c for c in cands if c.startswith(cur)], getattr(b, "help", ""))

    # -- Python form ----------------------------------------------------------------------

    def _complete_py(self, line):
        m = re.search(r"(rmm|_)((?:\.[A-Za-z_]\w*|\[\d+\])*?)(\.([A-Za-z_]\w*)?|\[[\"']?([^\]\"']*))?$", line)
        if not m:
            return Completion(len(line), [], "")
        start = m.start()
        base = m.group(1) + (m.group(2) or "")
        tail = m.group(3) or ""
        ns = self.namespace()
        try:
            obj = self._resolve(base, ns)
        except Exception:  # noqa: BLE001
            return Completion(len(line), [], "")
        hint = repr(obj)[:200]
        if tail.startswith("["):
            if isinstance(obj, dict):
                pre = m.group(5) or ""
                return Completion(start, [f'{base}["{k}"]' for k in obj if str(k).startswith(pre)],
                                  "keys: " + ", ".join(map(str, obj))[:200])
            if isinstance(obj, (list, tuple)):
                return Completion(start, [f"{base}[{i}]" for i in range(min(len(obj), 50))], hint)
            return Completion(len(line), [], hint)
        attr = m.group(4) or ""
        if not tail and isinstance(obj, dict):          # `_` alone: show what it holds
            return Completion(start, [f'{base}["{k}"]' for k in obj], "keys: " + ", ".join(map(str, obj))[:200])
        if not tail:
            return Completion(start, [base + "."], hint)
        names = [n for n in dir(obj) if not n.startswith("_") and n.startswith(attr)]
        if len(names) == 1:
            try:
                hint = repr(getattr(obj, names[0]))[:200]
            except Exception:  # noqa: BLE001
                pass
        return Completion(start, [f"{base}.{n}" for n in names], hint)

    def _resolve(self, chain, ns):
        """Walk a pure attribute/index chain (no calls), e.g. rmm.fdd[0] or _ ."""
        if not PY_CHAIN.match(chain):
            raise ValueError(chain)
        head = re.match(r"^(rmm|_)", chain).group(1)
        obj = ns[head]
        for part in re.findall(r"\.([A-Za-z_]\w*)|\[(\d+)\]", chain[len(head):]):
            obj = getattr(obj, part[0]) if part[0] else obj[int(part[1])]
        return obj
