# SPDX-License-Identifier: MIT
"""What the monitor offers: method names, parameters and their accepted values, the result
fields they return, the error codes they raise, the events the server pushes. Used for tab
completion and help.

86Box's rmm.c holds the schema and publishes all of it through rpc.discover;
Methods.merge_discover() takes it from there. STATIC (from _vendor/rmm_schema.py, a copy of the
C table that tests/test_schema_drift.py holds to it) is the fallback for an 86Box from before
the schema, whose discover carries names and param types only.
"""

from ._vendor import rmm_schema

# name: (params "name:type[?][=a|b]" space separated, result fields, error codes, one-line help)
STATIC = rmm_schema.METHODS

KEYS = ("enter esc tab space backspace del ins home end pgup pgdn up down left right "
        "ctrl alt shift f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12").split()
REGS = "eax ecx edx ebx esp ebp esi edi eip eflags".split()
CFG_SECTIONS = ["Machine", "Video", "Sound", "Network", "Input devices", "Storage controllers",
                "Hard disks", "Floppy and CD-ROM drives", "Other removable devices",
                "Other peripherals", "Ports (COM & LPT)", "General"]

# value hints the schema does not carry (it lists only values the server enforces):
# (method, param) -> values; ("*", param) applies to every method
HINTS = {
    ("input.key.send", "combo"): KEYS + ["ctrl+alt+del", "alt+f4", "ctrl+c"],
    ("capture.screenshot", "step"): ["1", "2", "4"],
    ("*", "section"): CFG_SECTIONS,
}


def parse_params(spec):
    """'addr:int len:int raw:bool?' -> {"addr": "int", "len": "int", "raw": "bool?"}"""
    return rmm_schema.parse_spec(spec)[0]


class Method:
    def __init__(self, name, params=None, returns=(), doc="", indices=None, enums=None, errors=()):
        self.name = name
        self.params = dict(params or {})
        self.returns = list(returns)
        self.doc = doc
        self.indices = indices            # slot numbers for <dev>.N.<verb>, None otherwise
        self.enums = dict(enums or {})    # param -> the values the server accepts
        self.errors = list(errors)        # app error codes it can raise

    @property
    def paused(self):
        """Needs a paused machine (error 1 otherwise)."""
        return 1 in self.errors

    def signature(self):
        ps = " ".join(f"{k}={'<' + t.rstrip('?') + '>'}" + ("?" if t.endswith("?") else "")
                      for k, t in self.params.items())
        ret = (" -> {" + ", ".join(self.returns) + "}") if self.returns else ""
        return f"{self.name} {ps}".rstrip() + ret


class Methods:
    """The method table: STATIC, then whatever rpc.discover says on top."""

    def __init__(self):
        self.by_name = {}
        for name, (ps, ret, errors, doc) in STATIC.items():
            params, enums = rmm_schema.parse_spec(ps)
            self.by_name[name] = Method(name, params, ret.split(), doc, enums=enums,
                                        errors=[int(c) for c in errors.split()])
        self.events = {n: rmm_schema.parse_spec(fs)[0] for n, (fs, _doc) in rmm_schema.EVENTS.items()}
        self.errors = dict(rmm_schema.ERRORS)
        self.from_server = False

    def merge_discover(self, discover):
        """Lay an rpc.discover result over the table. Properties come back as one entry with
        verbs [get, set]; they are expanded to <name>.get / <name>.set (get takes no params
        and cannot fail). A field the server leaves out (an 86Box from before the schema sends
        only name and params) keeps the static value."""
        for m in discover.get("methods", []):
            name = m["name"]
            names = [f"{name}.{v}" for v in m["verbs"]] if m.get("verbs") else [name]
            for n in names:
                old = self.by_name.get(n) or Method(n)
                # the server's params win; a server that lists none keeps the static ones
                params = m.get("params") or old.params
                enums = m.get("enums", old.enums)
                errors = m.get("errors", old.errors)
                if n.endswith(".get") and m.get("verbs"):
                    params, enums, errors = {}, {}, []
                self.by_name[n] = Method(n, params, m.get("returns", old.returns), m.get("doc", old.doc),
                                         m.get("indices"), enums, errors)
        if "events" in discover:
            self.events = {e["name"]: e.get("fields", {}) for e in discover["events"]}
        if "errors" in discover:
            self.errors = {e["code"]: e["name"] for e in discover["errors"]}
        self.from_server = True

    def names(self):
        """Callable names: <dev>.N.<verb> expanded to each slot index the server reported
        (0 and 1 when it reported none)."""
        out = []
        for n, m in self.by_name.items():
            if ".N." in n:
                dev, _, verb = n.partition(".N.")
                for i in (m.indices if m.indices is not None else [0, 1]):
                    out.append(f"{dev}.{i}.{verb}")
            else:
                out.append(n)
        return sorted(out)

    def get(self, name):
        """The Method for a callable name (fdd.1.insert finds fdd.N.insert)."""
        if name in self.by_name:
            return self.by_name[name]
        parts = name.split(".")
        if len(parts) == 3 and parts[1].isdigit():
            return self.by_name.get(f"{parts[0]}.N.{parts[2]}")
        return None
