# SPDX-License-Identifier: MIT
"""Connection stress: N (default 20) in-process reference mocks, each a MockServer on its own socket
standing in for one 86Box monitor port, driven the way a huge project drives them. No Qt, no 86Box,
no cluster: nothing here touches a real VM.

  GroupGui   the GUI's model layer (HostGroup/Node: a connection, console, frame poller and three
             threads per VM) starts all N at once, then group halt/resume/snap/restore, console
             fan-out lines and per-node calls run from several threads together.
  Chaos      some of the mocks vanish mid-run (as a crashed 86Box would): the rest must stay up and
             the group must keep answering.
  Ctl        rmmctl (rmm_86box.ctl) over N registered mocks: ps/pause/resume from several threads, a
             busy port (one client at a time) must cost a bounded probe timeout, not a hang.

Each phase fails on: an unhandled exception in any thread, a call that does not finish inside its
deadline (deadlock), a node that ends up offline when it should not, and leaked threads or fds.

    python3 -m unittest tests.test_many_instances            (from rmm_demo/)
    RMM_MANY=40 python3 -m unittest tests.test_many_instances
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import faulthandler
import gc
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..")
for p in (DEMO, os.path.join(DEMO, "..")):
    sys.path.insert(0, os.path.abspath(p))

from rmmgui.group import GroupSpec  # noqa: E402
from rmmgui.hostgroup import HostGroup  # noqa: E402
from rmm_86box import ctl  # noqa: E402
from rmm_86box.mock import start_mock  # noqa: E402

N = int(os.environ.get("RMM_MANY", "20"))
START_DEADLINE = 30.0       # all N connected and running
CALL_DEADLINE = 20.0        # any one fan-out operation


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def nfds():
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


class Base(unittest.TestCase):
    def setUp(self):
        self.thread_errors = []
        self._old_hook = threading.excepthook
        def hook(a):
            tb = traceback.extract_tb(a.exc_traceback)[-3:]
            where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(tb))
            self.thread_errors.append(f"{a.thread.name if a.thread else '?'}: {a.exc_type.__name__}: "
                                      f"{a.exc_value} @ {where}")
        threading.excepthook = hook
        self.addCleanup(self._restore_hook)
        self.threads0 = threading.active_count()
        self.fds0 = nfds()

    def _restore_hook(self):
        threading.excepthook = self._old_hook

    def parallel(self, fns, deadline=CALL_DEADLINE):
        """Run each fn on its own thread; fail if one raises or is still running at the deadline."""
        errs, ts = [], []

        def wrap(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

        for fn in fns:
            t = threading.Thread(target=wrap, args=(fn,), daemon=True)
            t.start()
            ts.append(t)
        end = time.time() + deadline
        for t in ts:
            t.join(max(0.0, end - time.time()))
        stuck = [t for t in ts if t.is_alive()]
        if stuck:
            faulthandler.dump_traceback(all_threads=True)       # where each one is waiting
        self.assertFalse(stuck, f"{len(stuck)} of {len(ts)} calls still running after {deadline}s (deadlock?)")
        self.assertFalse(errs, errs[:5])

    def assertClean(self, settle=3.0):
        wait_for(lambda: threading.active_count() <= self.threads0 + 2, settle)
        self.assertFalse(self.thread_errors, self.thread_errors[:5])
        self.assertLessEqual(threading.active_count(), self.threads0 + 2,
                             f"threads leaked: {self.threads0} -> {threading.active_count()}: "
                             f"{sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())[:12]}")
        gc.collect()                                            # the mock leaves a file object per server
        if self.fds0 >= 0:
            self.assertLessEqual(nfds(), self.fds0 + 4, f"fds leaked: {self.fds0} -> {nfds()}")


class GroupGui(Base):
    def make(self, names, policy=None, poll=None):
        self.lines = []
        spec = GroupSpec({"schema": "rmm-group/1", "name": "huge", "policy": policy or {},
                          "defaults": {"poll": poll or {"enabled": False}},
                          "nodes": {n: {"mock": True} for n in names}})
        g = HostGroup(spec, tempfile.mkdtemp(), lambda t, tone, n: self.lines.append((n.name if n else None, tone, t)))
        self.addCleanup(g.stop)
        t0 = time.time()
        g.start()
        up = wait_for(lambda: all(n.connected and n.status.get("state") == "running" for n in g.nodes.values()),
                      START_DEADLINE)
        self.start_secs = time.time() - t0
        self.assertTrue(up, f"not all {len(names)} nodes up in {START_DEADLINE}s: "
                            f"{[n.name for n in g.nodes.values() if not n.connected]}")
        self.g = g
        return g

    def test_start_and_hammer(self):
        names = [f"vm{i:02d}" for i in range(N)]
        g = self.make(names)
        print(f"\n  {N} nodes up in {self.start_secs:.1f}s")

        def halt_resume(k):
            def fn():
                for _ in range(k):
                    g.halt(induced=True)
                    g.resume()
            return fn

        def fanout():
            for _ in range(5):
                g.run_line("@* machine.status")

        def per_node(n):
            def fn():
                for _ in range(10):
                    n.pause()
                    n.resume()
                    n.rmm.call("machine.status")
            return fn

        def snaps():
            for i in range(3):
                g.snap(None, f"S{i}")
            for i in range(3):
                g.restore(f"S{i}")

        t0 = time.time()
        self.parallel([halt_resume(4), halt_resume(4), fanout, fanout, snaps] +
                      [per_node(n) for n in list(g.nodes.values())[:8]], deadline=120)
        print(f"  hammer phase {time.time() - t0:.1f}s")
        g.resume()
        self.assertTrue(wait_for(lambda: all(n.status.get("state") == "running" for n in g.nodes.values()), 10),
                        {n.name: n.status.get("state") for n in g.nodes.values() if n.status.get("state") != "running"})
        self.assertEqual([n.name for n in g.nodes.values() if not n.connected], [])
        g.stop()
        self.assertClean()

    def test_polling_load(self):
        """Every node polls frames (20 x 5 fps in one process): the group must stay responsive, not
        just correct. The mocks render in the same process, so this is a worst case for the GIL;
        the number printed is what a halt/resume of the whole group costs under that load."""
        names = [f"vm{i:02d}" for i in range(N)]
        g = self.make(names, poll={"rate": 5, "step": 8})
        t0 = time.time()
        self.parallel([lambda: g.halt(induced=True), lambda: g.resume()], deadline=60)
        took = time.time() - t0
        print(f"\n  {N} nodes polling: start {self.start_secs:.1f}s, halt+resume {took:.1f}s")
        self.assertLess(took, 20.0, f"group halt/resume took {took:.1f}s with {N} nodes polling")
        g.stop()
        self.assertClean()

    def test_nodes_vanish_midrun(self):
        names = [f"vm{i:02d}" for i in range(N)]
        # frame polling on (slowly) so a poller is mid-call when its VM goes away
        g = self.make(names, policy={"halt_all": True, "halt_on": ["disconnect"]}, poll={"rate": 1, "step": 8})
        victims = list(g.nodes.values())[: max(1, N // 5)]
        for v in victims:
            v.mock_srv.stop()                             # the 86Box behind it is gone
        gone = {v.name for v in victims}
        self.assertTrue(wait_for(lambda: all(not v.connected for v in victims), 15), "victims still connected")
        # the group answers, the survivors are intact (halt-all may have paused them), nothing wedged
        self.parallel([lambda: g.run_line("@* machine.status"), lambda: g.halt(induced=True),
                       lambda: g.resume()])
        alive = [n for n in g.nodes.values() if n.name not in gone]
        for n in alive:
            self.assertTrue(n.connected, f"{n.name} lost its connection")
            self.assertIn(n.rmm.call("machine.status")["state"], ("running", "paused"))
        g.stop()
        self.assertClean()


class Ctl(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        os.environ["RMM_86BOX_HOME"] = self.home
        self.addCleanup(os.environ.pop, "RMM_86BOX_HOME", None)
        self.reg = ctl.Registry(self.home)
        self.srvs = []
        for i in range(N):
            s = start_mock()
            self.srvs.append(s)
            self.addCleanup(s.stop)
            self.reg.put(dict(name=f"vm{i:02d}", kind="attached", host="127.0.0.1", port=s.port, pid=None,
                              started=time.time()))

    def run_ctl(self, *argv, timeout=60):
        """rmmctl as its own process, as a user would run it (redirecting stdout in-process would
        garble concurrent callers). A hang is a failure, not a stuck test run."""
        env = dict(os.environ, RMM_86BOX_HOME=self.home, PYTHONPATH=os.path.join(DEMO, ".."))
        try:
            r = subprocess.run([sys.executable, "-m", "rmm_86box.ctl", *argv], capture_output=True, text=True,
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            self.fail(f"rmmctl {' '.join(argv)} still running after {timeout}s (hang)")
        return r.returncode, r.stdout, r.stderr

    def test_ps_pause_resume_from_many_threads(self):
        results = []

        def ps():
            for _ in range(3):
                rc, out, err = self.run_ctl("ps", "--json")
                self.assertEqual(rc, 0, err)
                rows = json.loads(out)
                results.append(len(rows))
                self.assertEqual(rc, 0)

        def pr(verb):
            def fn():
                for _ in range(3):
                    self.run_ctl(verb, "--all")
            return fn

        self.parallel([ps, ps, pr("pause"), pr("resume")], deadline=120)
        self.assertTrue(all(c == N for c in results), results)
        rc, out, err = self.run_ctl("resume", "--all")
        self.assertEqual(rc, 0, err)

    def test_busy_port_costs_a_bounded_timeout(self):
        """One client per port: while a GUI-like client holds a mock, rmmctl ps must still return
        (that instance shows as not answering) rather than hang the whole listing."""
        held = ctl.connect(self.reg.get("vm00"))
        try:
            t0 = time.time()
            rc, out, _ = self.run_ctl("ps", "-a", "--json", timeout=30)
            took = time.time() - t0
            rows = {r["name"]: r for r in json.loads(out)}
            self.assertEqual(len(rows), N)
            self.assertLess(took, getattr(ctl, "PROBE_TIMEOUT", 3) * 3 + 10, f"ps took {took:.1f}s")
            print(f"\n  ps with one busy port: {took:.1f}s, vm00 shows {rows['vm00']['state']}")
        finally:
            held.close()
        for srv in self.srvs:
            srv.stop()
        self.assertClean()


if __name__ == "__main__":
    unittest.main()
