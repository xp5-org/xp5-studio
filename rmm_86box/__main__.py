# SPDX-License-Identifier: MIT
"""python -m rmm_86box PORT [LINE ...]   run console lines and exit
   python -m rmm_86box PORT               interactive console, Tab completes
   python -m rmm_86box --mock             the same against the reference mock (no VM)
"""

import argparse
import sys

from .client import Rmm
from .console import Console


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m rmm_86box", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("args", nargs="*", metavar="PORT [LINE]",
                    help="the RMM port (left out with --mock), then console lines to run, e.g. 'machine.status'")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--wait", type=float, default=0, help="seconds to retry the connection")
    ap.add_argument("--keep-paused", action="store_true", help="do not resume on connect")
    ap.add_argument("--mock", action="store_true", help="start the reference mock and use it")
    ap.add_argument("--vm-store", help="directory for vm save/load domains")
    a = ap.parse_args(argv)

    srv = None
    if a.mock:
        from .mock import start_mock
        srv = start_mock()
        a.port, a.lines = srv.port, a.args
    elif a.args and a.args[0].isdigit():
        a.port, a.lines = int(a.args[0]), a.args[1:]
    else:
        ap.error("PORT (or --mock) is required")
    rmm = Rmm(a.port, a.host, resume_on_connect=not a.keep_paused, vm_store=a.vm_store)
    print(rmm.connect(wait=a.wait))
    con = Console(rmm)
    try:
        if a.lines:
            for line in a.lines:
                for out in con.run(line):
                    print(out)
            return 0
        return repl(con)
    finally:
        rmm.close()
        if srv:
            srv.stop()


def repl(con):
    try:
        import readline
    except ImportError:          # no readline (Windows): no completion, still a console
        readline = None
    if readline:
        state = {}

        def complete(text, n):
            if n == 0:
                line = readline.get_line_buffer()[:readline.get_endidx()]
                c = con.complete(line)
                # readline replaces from its own word start; hand back the text from there
                cut = len(line) - len(text)
                state["items"] = [it[cut - c.start:] if cut >= c.start else it for it in c.items]
            items = state.get("items", [])
            return items[n] if n < len(items) else None

        readline.set_completer_delims(" ")
        readline.set_completer(complete)
        readline.parse_and_bind("tab: complete")
    while True:
        try:
            line = input("rmm> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in ("quit", "exit"):
            return 0
        for out in con.run(line):
            print(out)


if __name__ == "__main__":
    sys.exit(main())
