# SPDX-License-Identifier: MIT
"""Read the method schema out of 86Box's src/rmm.c (rmm_methods[], rmm_media_verbs[],
rmm_props[], rmm_events[], rmm_errors[], rmm_keys[]) in the shape of _vendor/rmm_schema.py, so the
tests can hold the Python copy to the C table.

    python3 tests/rmm_c_schema.py [path/to/rmm.c]    print it as rmm_schema.py literals
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# the A build slot sits beside this repo: GIT_SRC_ROOT/{86box-rmm,A/86Box}
DEFAULT_RMM_C = os.path.join(HERE, "..", "..", "A", "86Box", "src", "rmm.c")

_TOKEN = re.compile(r'\s+|/\*.*?\*/|//[^\n]*|"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*|-?\d+)|([{},|&()\[\].=])', re.S)

# rmm_method_t field order; a designated row (.name = ...) fills these, missing ones are "" / 0
_METHOD_FIELDS = ("name", "fn", "params", "returns", "flags", "errors", "doc")


def rmm_c_path():
    return os.environ.get("RMM_C") or DEFAULT_RMM_C


def source(path=None):
    with open(path or rmm_c_path(), encoding="utf-8", errors="replace") as f:
        return f.read()


def _rows(src, table):
    """The rows of a `static const ... table[] = { {...}, ... };` initialiser, each a list of
    fields; adjacent string literals are joined, as the C compiler does."""
    defs = _defines(src)
    m = re.search(r"\b%s\[\]\s*=\s*\{" % re.escape(table), src)
    if not m:
        raise ValueError(f"no {table}[] in rmm.c")
    rows, row, depth, pos = [], None, 1, m.end()
    field, last_str = None, False
    desig, want_name, in_err = None, False, False   # .name = value rows; RMM_ERR(X) is X's number as a string
    while depth:
        t = _TOKEN.match(src, pos)
        if not t:
            raise ValueError(f"cannot read {table}[] at offset {pos}")
        pos = t.end()
        s, word, punct = t.group(1), t.group(2), t.group(3)
        if s is None and word is None and punct is None:
            continue                              # whitespace or a comment
        if s is not None:
            s = s.encode().decode("unicode_escape")
            field = (field + s) if last_str else s
            last_str = True
            continue
        if word is not None:
            if want_name:
                desig, want_name = word, False
                continue
            if word == "RMM_ERR":
                in_err = True
                continue
            if in_err:
                s = str(defs["RMM_E_" + word])
                field = (field + s) if last_str else s
                last_str = True
                continue
            field = word if field is None else f"{field} {word}"
        elif punct in "().=" and punct != ".":
            if punct == ")":
                in_err = False
            continue
        elif punct == ".":
            want_name = True
            continue
        elif punct == "{":
            depth += 1
            row, field, desig = [], None, None
        elif punct in ",}":
            if row is not None and (field is not None or punct == ","):
                if desig is not None:
                    row.append((desig, field))
                    desig = None
                else:
                    row.append(field)
            field = None
            if punct == "}":
                depth -= 1
                if row is not None:
                    if row and isinstance(row[0], tuple):
                        d = dict(row)
                        row = [d.get(f, 0 if f == "flags" else "" if f != "fn" else None) for f in _METHOD_FIELDS]
                    rows.append(row)
                row = None
        elif punct == "|":
            field = f"{field} |"
        last_str = False
    return rows


def _defines(src):
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"#define\s+(\w+)\s+(\d+)\b", src)}


def read(path=None):
    """-> dict(methods={name: (params, returns, errors, doc)}, events={name: (fields, doc)},
    errors={code: name}, keys={key name: set 1 scancode}). errors in methods is a space-separated code list, "1" first for a
    method flagged RMM_MF_PAUSED; properties come out as <name>.get / <name>.set and media
    verbs as <dev>.N.<verb>, as rpc.discover's client expands them."""
    src = source(path)
    defs = _defines(src)
    methods = {}
    for r in _rows(src, "rmm_methods"):
        if r[0] == "NULL":
            continue
        name, _fn, params, returns, flags, errors, doc = r
        paused = "RMM_MF_PAUSED" in (flags or "")
        methods[name] = (params, returns, " ".join((["1"] if paused else []) + errors.split()), doc)
    devs = re.search(r"rmm_media_devs\[\]\s*=\s*\{([^}]*)\}", src).group(1)
    devs = re.findall(r'"(\w+)"', devs)
    for verb, params, errors, doc in (r for r in _rows(src, "rmm_media_verbs") if r[0] != "NULL"):
        for dev in devs:
            methods[f"{dev}.N.{verb}"] = (params, "", errors, doc)
    for r in _rows(src, "rmm_props"):
        if r[0] == "NULL":
            continue
        name, doc = r[0], r[3]
        methods[f"{name}.get"] = ("", "value", "", doc)
        methods[f"{name}.set"] = ("value:bool", "value", "4", doc)
    events = {r[0]: (r[1], r[2]) for r in _rows(src, "rmm_events") if r[0] != "NULL"}
    errors = {}
    for code, name in (r for r in _rows(src, "rmm_errors") if r[1] != "NULL"):
        errors[int(code) if code.lstrip("-").isdigit() else defs[code]] = name
    keys = {r[0]: int(r[1].replace(" ", ""), 0) for r in _rows(src, "rmm_keys") if r[0] != "NULL"}
    return dict(methods=methods, events=events, errors=errors, keys=keys)


def handler_params(path=None):
    """{method: set of param names its handler reads}: rpc_uint/rpc_int/rpc_string or a
    params lookup by literal name, followed one level into rpc_* helpers given params."""
    src = source(path)
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"\n(\w+)\([^;{]*\)\n\{", src)]
    bodies = {n: src[s:(starts[i + 1][0] if i + 1 < len(starts) else len(src))]
              for i, (s, n) in enumerate(starts)}
    read_pat = re.compile(r'(?:rpc_uint|rpc_int|rpc_string|rpc_opt_string|rpc_break_spec|GetObjectItemCaseSensitive)\(\s*params,\s*"(\w+)"')

    def used(fn, seen):
        if fn in seen or fn not in bodies:
            return set()
        seen.add(fn)
        out = set(read_pat.findall(bodies[fn]))
        for helper in set(re.findall(r"\b(rpc_\w+)\(\s*params", bodies[fn])) - {fn}:
            out |= used(helper, seen)
        return out

    return {r[0]: used(r[1], set()) for r in _rows(src, "rmm_methods") if r[0] != "NULL"}


if __name__ == "__main__":
    s = read(sys.argv[1] if len(sys.argv) > 1 else None)
    print("METHODS = {")
    for n, v in s["methods"].items():
        print(f"    {n!r}: {v!r},")
    print("}\n\nEVENTS = {")
    for n, v in s["events"].items():
        print(f"    {n!r}: {v!r},")
    print("}\n\nERRORS = {")
    for c, n in sorted(s["errors"].items()):
        print(f"    {c}: {n!r},")
    print("}\n\nKEYS = {")
    for n, v in s["keys"].items():
        print(f"    {n!r}: 0x{v:03X},")
    print("}")
