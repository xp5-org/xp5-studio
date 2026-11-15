# SPDX-License-Identifier: MIT
"""The method schema has one source, 86Box's src/rmm.c; these hold every copy to it:
_vendor/rmm_schema.py (the mock's and old servers' schema), _vendor/rmm_registry.py's "done"
rows, and rmm.c's own handlers and rmm_event() calls.

rmm.c is looked for in the A build slot beside this repo (GIT_SRC_ROOT/A/86Box/src), or at
$RMM_C; without it the C checks are skipped. To bring rmm_schema.py back in step:

    python3 tests/rmm_c_schema.py > /tmp/lits.py      then paste METHODS/EVENTS/ERRORS/KEYS over
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import rmm_c_schema  # noqa: E402
from rmm_86box._vendor import rmm_registry, rmm_schema  # noqa: E402

# params a handler reads that its row does not declare, or the reverse, on purpose
HANDLER_EXCEPTIONS = {
    "io.read": {"value"},          # rpc_io_args is shared with io.write and reads value only there
    "cpu.regs.set": {"eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip"},   # read in a loop
    "rpc.hello": {"client"},       # informational; never read
}


def _diff(want, got):
    """Lines naming each key that is missing, extra or different."""
    out = []
    for k in sorted(set(want) | set(got), key=str):
        if k not in got:
            out.append(f"  missing {k!r}: rmm.c has {want[k]!r}")
        elif k not in want:
            out.append(f"  extra   {k!r}: rmm.c has no such entry")
        elif want[k] != got[k]:
            out.append(f"  differs {k!r}:\n    rmm.c  {want[k]!r}\n    python {got[k]!r}")
    return out


class SchemaOnly(unittest.TestCase):
    """Checks that need no rmm.c."""

    def test_error_codes_are_listed(self):
        for name, (_ps, _ret, errors, _doc) in rmm_schema.METHODS.items():
            for c in errors.split():
                self.assertIn(int(c), rmm_schema.ERRORS, f"{name} raises {c}, which ERRORS does not name")

    def test_specs_parse(self):
        for name, (ps, _ret, _errs, _doc) in rmm_schema.METHODS.items():
            params, enums = rmm_schema.parse_spec(ps)
            for p, t in params.items():
                self.assertIn(t.rstrip("?"), ("int", "bool", "string", "hex", "object", "array"), f"{name}.{p}")
            self.assertLessEqual(set(enums), set(params), name)

    def test_discover_entry_shape(self):
        e = rmm_schema.discover_entry("debug.watch.set", rmm_schema.METHODS["debug.watch.set"])
        self.assertEqual(e["enums"]["kind"], ["r", "w", "rw"])
        self.assertIn("child_kind", e["params"])           # past the old 128-byte cut-off
        e = rmm_schema.discover_entry("io.read", rmm_schema.METHODS["io.read"])
        self.assertEqual((e["enums"], e["paused"], e["errors"]), ({"size": [1, 2, 4]}, True, [1]))


class AgainstRmmC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = rmm_c_schema.rmm_c_path()
        if not os.path.exists(path):
            raise unittest.SkipTest(f"no rmm.c at {path} (set RMM_C)")
        cls.path = path
        cls.src = rmm_c_schema.source(path)
        cls.c = rmm_c_schema.read(path)

    def test_methods(self):
        d = _diff(self.c["methods"], rmm_schema.METHODS)
        self.assertFalse(d, "rmm_schema.METHODS is out of step with rmm.c:\n" + "\n".join(d))

    def test_events(self):
        d = _diff(self.c["events"], rmm_schema.EVENTS)
        self.assertFalse(d, "rmm_schema.EVENTS is out of step with rmm.c:\n" + "\n".join(d))

    def test_errors(self):
        d = _diff(self.c["errors"], rmm_schema.ERRORS)
        self.assertFalse(d, "rmm_schema.ERRORS is out of step with rmm.c:\n" + "\n".join(d))

    def test_keys(self):
        d = _diff(self.c["keys"], rmm_schema.KEYS)
        self.assertFalse(d, "rmm_schema.KEYS is out of step with rmm.c:\n" + "\n".join(d))

    def test_registry_done_rows_are_what_c_implements(self):
        c_names = set()
        for n in self.c["methods"]:
            base, _, verb = n.rpartition(".")
            # properties are one registry row (note "get/set"); rmm.sticky_pause has two
            c_names.add(base if verb in ("get", "set") and base + ".get" in self.c["methods"]
                        and base + ".get" not in {r["method"] for r in rmm_registry.R} else n)
        done = {r["method"] for r in rmm_registry.R if r["status"] == "done"}
        self.assertEqual(sorted(c_names - done), [], "in rmm.c, not 'done' in rmm_registry")
        self.assertEqual(sorted(done - c_names), [], "'done' in rmm_registry, not in rmm.c")

    def test_handlers_read_the_declared_params(self):
        for name, read in rmm_c_schema.handler_params(self.path).items():
            declared = set(rmm_schema.parse_spec(self.c["methods"][name][0])[0])
            odd = (read ^ declared) - HANDLER_EXCEPTIONS.get(name, set())
            self.assertFalse(odd, f"{name}: handler reads {sorted(read - declared)} undeclared, "
                                  f"declares {sorted(declared - read)} unread")

    def test_every_emitted_event_is_declared(self):
        emitted = set(re.findall(r'rmm_event\("([\w.]+)"', self.src))
        self.assertEqual(emitted, set(self.c["events"]))

    def test_protocol_matches(self):
        from rmm_86box import client
        sys.path.insert(0, os.path.join(HERE, "..", "rmm_86box", "_vendor"))
        import rmm_mock
        c = int(re.search(r"#define\s+RMM_PROTOCOL\s+(\d+)", self.src).group(1))
        self.assertEqual((client.PROTOCOL, rmm_mock.PROTOCOL), (c, c))


if __name__ == "__main__":
    unittest.main()
