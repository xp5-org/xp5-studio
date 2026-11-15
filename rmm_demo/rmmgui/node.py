# SPDX-License-Identifier: MIT
"""One VM in the GUI: its connection, console, frame poller, cached status, and (when the GUI
started it) its emulator process. No Qt here.

What starts and drives the emulator is the library's backend (rmm_86box.backends: 86Box, QEMU,
VICE, SIMH); a Node wraps the Machine it returns with the GUI's threads and caches.

Each node has its own worker thread, so a slow line on one VM never holds up another, and its
own connection: 86Box serves one RMM client per port, so the GUI cannot share a VM with the
test runner's Box86Monitor while both are attached.
"""

import collections
import queue
import threading
import time

from rmm_86box import Console, FramePoller, Rmm, RpcError, backends, kvm

from .symbols import Symbols



class Node:
    def __init__(self, spec, vm_store, say, vm_config_dir=None):
        """say(text, tone, node): tone is one of text/ok/warn/err/accent/muted."""
        self.spec = spec
        self.name = spec.name
        self.vm_store = spec.raw.get("vm_store") or vm_store
        self.vm_config_dir = vm_config_dir
        self._say = say
        self.listeners = []                      # fn(node, reason, params) for stops/exits
        self.snap_listeners = []                 # fn(node, tags, t) after each snapshot.list
        self.line_hooks = []                     # fn(node, line, output, t0) after a console line ran
        self.mock_srv = None
        self.machine = None                      # the backend's Machine, None for a mock
        self.proc = None                         # its Proc when this GUI launched the emulator
        self.port = spec.port if isinstance(spec.port, int) else None
        self.rmm = None
        self.console = None
        self.poller = None
        self.status = {}
        self.tsc_rate = 0.0
        self.regs = None
        self.regs_queued = False
        self.snaps = []
        self.dom_lines = []
        self.events = collections.deque(maxlen=50)
        self.conn_msg = "not started"
        self.busy = ""
        self.demo = ""                           # the console line of the demo running on its own thread
        self.work = queue.Queue()
        self.running = False
        self.exit_seen = False
        self.ever_connected = False
        self.linked = False                      # connect() returned: discover done, the attach pause resumed
        self.induced_until = 0.0                 # pause events before this are the group's own
        self.turbo = None                        # machine.fast_forward as last read; None = unknown
        self.stop_note = ""                      # why it last stopped, e.g. "breakpoint @0x810F"
        self.want_tab = None                     # a demo asks the shell to show this tab (GUI thread consumes it)
        self.ended = None                        # (kind, text) once a launched emulator is gone
        # What a breakpoint / watchpoint is FOR, which 86Box does not hold: ("break"|"watch", id) ->
        # {label, on_hit: "stop"|"restore"|"script", tag, resume, by: "panel"|"console"|<demo>, note,
        # restores, seen, hook}. by "panel": this node runs the restore on the stop event; on_hit
        # "script": this node runs rule["hook"] (rmmgui.hooks.Hook, a Python file's on_hit) on the
        # stop event, whoever attached it; any other by (a demo) runs its own loop and only
        # describes it here, so the Monitor panel can show what a hit leads to.
        self.hit_rules = {}
        self.hits = collections.Counter()        # stops seen per ("break"|"watch", id), whoever armed it
        self.last_hit = {}                       # ("break"|"watch", id) -> the stop event's params
        self.want_bp_form = None                 # a demo's Add-form values; the Monitor page fills the form and adds
        self.symbols = Symbols()                 # `sym load` tables: names for addresses (rmmgui/symbols.py)

    # -- lifecycle --------------------------------------------------------------------------

    @property
    def full(self):
        """The adapter is the whole 86Box Rmm (console, registers, breakpoints, media)."""
        return self.machine is None or self.machine.full_rmm

    def start(self, builtins=None):
        """Start the VM (mock / launch) or just begin attaching. Raises on a launch error."""
        s = self.spec
        if s.mock:
            from rmm_86box.mock import start_mock
            self.mock_srv = start_mock()
            self.port = self.mock_srv.port
            self.rmm = Rmm(self.port, s.host, timeout=s.rpc_timeout, vm_store=self.vm_store)
        else:
            m = self.machine = backends.get(s.backend).start(
                s, vm_store=self.vm_store, vm_config_dir=self.vm_config_dir, say=self.say)
            self.rmm, self.proc = m.rmm, m.proc
            if m.port is not None:
                self.port = m.port
        self.rmm.on_event(self._on_event)
        poll = s.poll
        if self.full:
            self.console = Console(self.rmm, width=110, autoresume=True)
            if builtins:
                self.console.builtins.update(builtins(self))
        if self.full or self.machine.frames:
            rate = self.machine.frame_rate if self.machine else 10.0
            self.poller = FramePoller(self.rmm, rate=poll.get("rate", rate), step=poll.get("step", 1),
                                      enabled=poll.get("enabled", True))
            self.poll_rate = self.poller.rate      # what the user asked for; the view may slow a hidden node
        self.running = True
        for fn in (self._connect_loop, self._status_loop, self._worker):
            threading.Thread(target=fn, name=f"node-{self.name}-{fn.__name__}", daemon=True).start()
        if self.poller:
            self.poller.start()
        if self.full:
            for line in s.run:
                self.put_line(line)

    def stop(self):
        self.running = False
        self.work.put(("stop", None))            # ends _worker, which sits in work.get()
        if self.poller:
            self.poller.stop()
        if self.machine:
            self.machine.stop()
        elif self.rmm:
            self.rmm.close()
        if self.mock_srv:
            self.mock_srv.stop()

    @property
    def connected(self):
        return bool(self.rmm and self.rmm.connected)

    @property
    def address(self):
        """Where its monitor is: host:port, or a QEMU node's QMP socket."""
        if self.spec.mock:
            return "in-process mock"
        if self.machine is not None and getattr(self.machine, "address", None):
            return self.machine.address
        return f"{self.spec.host}:{self.port or self.spec.port}"

    @property
    def state(self):
        if self.ended:
            return self.ended[0]
        if not self.connected:
            return "offline"
        return self.status.get("state", "?")

    def say(self, text, tone="text"):
        self._say(text, tone, self)

    # -- background ------------------------------------------------------------------------

    def _connect_loop(self):
        while self.running:
            if self.connected or self.ended:
                time.sleep(0.5)
                continue
            try:
                self.linked = False
                banner = self.rmm.connect(wait=0)
                self.linked = True
                self.conn_msg = banner
                self.exit_seen = False
                first = not self.ever_connected
                self.ever_connected = True
                self.say(f"connected: {banner}", "ok")
                if not first and self.full:
                    self.say("reconnected: 86Box cleared every breakpoint and watchpoint at the disconnect", "warn")
                self.work.put(("vm", None))
            except (OSError, RpcError) as e:
                self.conn_msg = f"connecting to {self.address}... ({e})"
                if self.rmm.connected:           # socket up but discover/resume failed: start over
                    self.rmm.close()
                if self.proc:
                    end = self.proc.ended()
                    if end:
                        self._process_ended(end)
                    elif self.proc.bind_failed():
                        self.conn_msg = f"{self.proc.label} could not bind {self.spec.host}:{self.port} (see {self.proc.log})"
                time.sleep(1.0)

    def _status_loop(self):
        last, n = None, 0
        while self.running:
            if self.connected:
                try:
                    st = self.rmm.call("machine.status")
                    now = time.time()
                    if last and st.get("tsc") is not None and now > last[0]:
                        self.tsc_rate = max(0.0, (st["tsc"] - last[1]) / (now - last[0]))
                    last = (now, st.get("tsc", 0))
                    self.status = st
                    self.turbo = bool(self.rmm.call("machine.fast_forward.get")["value"])
                    if n % 4 == 0:
                        self.refresh_snaps()
                except (RpcError, OSError):
                    pass
                n += 1
            elif self.proc and not self.ended:
                end = self.proc.ended()
                if end:
                    self._process_ended(end)
            time.sleep(0.5)

    def _worker(self):
        while self.running:
            kind, arg = self.work.get()
            if kind == "stop":
                return
            if kind == "line":
                self.busy = arg[0]                   # counts as busy while it waits, too
            # queued lines wait for connect() to finish: it resumes the attach pause, which would
            # undo a pause the line made in the meantime (demo gamesnap)
            while self.running and not (self.connected and self.linked) and not self.ended:
                time.sleep(0.2)
            if not self.connected:
                if kind == "line":
                    arg[1](["error: not connected"])
                self.busy = self.demo
                continue
            try:
                if kind == "line" and arg[0].split(" ")[0] == "demo" and self.demo:
                    arg[1]([f"error: '{self.demo}' is still running"])
                elif kind == "line" and arg[0].split(" ")[0] == "demo":
                    # a demo loops for minutes: give it its own thread, so the pages' calls
                    # (breakpoint list, hex, regs) still go through this worker meanwhile
                    self.demo = arg[0]
                    threading.Thread(target=self._run_demo, args=arg, name=f"node-{self.name}-demo",
                                     daemon=True).start()
                elif kind == "line":
                    self._run_line(*arg)
                elif kind == "call":
                    arg()
                elif kind == "regs":
                    self.refresh_regs()
                elif kind == "vm":
                    self.refresh_vm()
            except Exception as e:  # noqa: BLE001 - keep the worker alive, say what broke
                self.say(f"error: {e}", "err")
            finally:
                self.busy = self.demo

    def _run_line(self, line, done):
        self.busy = line
        t0 = time.time()
        out = self.console.run(line)
        done(out)
        for fn in list(self.line_hooks):
            fn(self, line, out, t0)
        if line.split(" ")[0] in ("machine.reset", "config.load") or line.startswith("vm load"):
            self.refresh_vm()

    def _run_demo(self, line, done):
        try:
            self._run_line(line, done)
        except Exception as e:  # noqa: BLE001 - same as the worker: say what broke
            self.say(f"error: {e}", "err")
        finally:
            self.demo = ""
            self.busy = ""

    def put_line(self, line, done=None):
        """Queue a console line; done(lines) gets the output (default: print it)."""
        def show(lines):
            for t in lines:
                self.say(t, "err" if t.startswith("error") else "text")
        self.work.put(("line", (line, done or show)))

    def put_call(self, fn):
        self.work.put(("call", fn))

    # -- events ------------------------------------------------------------------------------

    def _on_event(self, name, params):
        detail = " ".join(f"{k}={hex(v) if k == 'addr' and isinstance(v, int) else v}" for k, v in params.items())
        self.events.appendleft(f"{time.strftime('%H:%M:%S')} {name} {detail}".rstrip())
        reason = None
        if name == "machine.stopped":
            reason = params.get("reason", "pause")
            if not self.regs_queued:
                self.regs_queued = True
                self.work.put(("regs", None))
            if reason == "pause" and time.time() < self.induced_until:
                self.stop_note = "paused by the group"
                return                           # the group paused us: not a cause
            addr = params.get("addr")
            self.stop_note = reason + (f" @0x{addr:X}" if isinstance(addr, int) else "")
            self._count_hit(reason, params)
        elif name == "machine.resumed":
            self.stop_note = ""
        elif name == "machine.reset":
            self.work.put(("vm", None))
        elif name == "machine.exit":
            self.exit_seen = True
            reason = "exit"
            self.conn_msg = f"{self.proc.label if self.proc else '86Box'} exited"
        elif name == "rpc.disconnected":
            if not self.running:
                return                           # we are closing
            self.status = {}
            self.turbo = None
            if self.exit_seen:
                return                           # already reported as exit
            reason, params = self._why_disconnected()
        if reason:
            for fn in list(self.listeners):
                fn(self, reason, params)

    # -- hit rules (what a breakpoint / watchpoint leads to) ----------------------------------

    def set_hit_rule(self, kind, ident, label="", on_hit="stop", tag="", resume=False, by="panel", note=""):
        """Describe (and for by="panel", run) what a hit on break/watch #ident does."""
        self.hit_rules[(kind, int(ident))] = {"label": label, "on_hit": on_hit, "tag": tag, "resume": resume,
                                              "by": by, "note": note, "restores": 0, "seen": False,
                                              "t": time.time()}

    def drop_hit_rule(self, kind, ident):
        self._retire(self.hit_rules.pop((kind, int(ident)), None))

    def _retire(self, rule):
        """A rule going away: a hook script's on_disarm runs on the worker."""
        if rule and rule.get("hook"):
            self.work.put(("call", rule["hook"].disarm))

    def note_restore(self, kind, ident):
        """A script that runs its own restore loop counts it here, for the panel's Hits column."""
        rule = self.hit_rules.get((kind, int(ident)))
        if rule:
            rule["restores"] += 1

    def prune_hit_rules(self, present):
        """present: the (kind, id) keys 86Box just listed. A rule goes once its id was listed and is
        gone (cleared, or 86Box dropped everything at a reconnect); one never listed goes after 15 s."""
        now = time.time()
        for key, rule in list(self.hit_rules.items()):
            if key in present:
                rule["seen"] = True
            elif rule["seen"] or now - rule["t"] > 15:
                self._retire(self.hit_rules.pop(key))
                self.hits.pop(key, None)
                self.last_hit.pop(key, None)

    def _count_hit(self, reason, params):
        kind = {"breakpoint": "break", "watchpoint": "watch"}.get(reason)
        if kind is None or "id" not in params:
            return
        key = (kind, int(params["id"]))
        self.hits[key] += 1
        self.last_hit[key] = dict(params)
        rule = self.hit_rules.get(key)
        if rule and rule["by"] == "panel" and rule["on_hit"] == "restore":
            self.work.put(("call", lambda: self._run_restore(key, rule)))
        elif rule and rule["on_hit"] == "script" and rule.get("hook"):
            self.work.put(("call", lambda: self._run_hook(key, rule, dict(params))))

    def _run_hook(self, key, rule, params):
        """A hook script's on_hit (rmmgui.hooks), on the worker like a console line."""
        if self.hit_rules.get(key) is rule:          # not cleared / replaced between the stop and now
            rule["hook"].run(params)

    def _run_restore(self, key, rule):
        """The panel's own on-hit restore, on the worker (never on the event thread)."""
        if self.hit_rules.get(key) is not rule:
            return                               # cleared between the stop and now
        what = f"{key[0]} #{key[1]}"
        try:
            self.rmm.call("snapshot.restore", tag=rule["tag"])       # leaves the machine paused
            rule["restores"] += 1
            if rule["resume"]:
                self.rmm.call("machine.resume")
            if rule["restores"] <= 3 or rule["restores"] % 25 == 0:       # a restore loop runs many per second
                self.say(f"{what} hit #{self.hits[key]}: restored {rule['tag']}" + (", resumed" if rule["resume"] else
                                                                                   ", paused")
                         + f" ({rule['restores']} restores)", "muted")
        except (RpcError, OSError) as e:
            self.say(f"{what} hit: restore {rule['tag']} failed: {e}", "warn")

    def _why_disconnected(self):
        """A dropped connection with no machine.exit: a crash if our 86Box died by a signal
        (or a Windows exception), else just a disconnect (we cannot tell for an attached VM)."""
        if self.proc:
            end = self.proc.ended(wait=1.5)
            if end:
                self._process_ended(end, notify=False)
                return end[0], {"detail": end[1]}
        self.conn_msg = "disconnected (no machine.exit)"
        self.say("connection lost without machine.exit", "warn")
        return "disconnect", {}

    def _process_ended(self, end, notify=True):
        if self.ended:
            return
        self.ended = end
        kind, text = end
        self.stop_note = f"{kind}: {text}"
        self.conn_msg = f"{self.proc.label} ended: {text}"
        self.say(f"{self.proc.label} pid {self.proc.pid} ended: {text}", "err" if kind == "crash" else "muted")
        for ln in self.proc.log_tail(3):
            self.say(f"  log: {ln}", "muted")
        if notify and not self.exit_seen:
            for fn in list(self.listeners):
                fn(self, kind, {"detail": text})

    # -- refreshes ---------------------------------------------------------------------------

    def refresh_vm(self):
        if self.machine:
            self.dom_lines = self.machine.title_lines()
            return
        try:
            self.dom_lines = kvm.describe(self.rmm.vm.show())
        except (RpcError, OSError, KeyError) as e:
            self.dom_lines = [f"(config.dump: {e})"]

    def tty(self, tail=None):
        """Console text of a node whose console is a terminal (SIMH), else None."""
        return self.machine.tty(tail) if self.machine else None

    def type_line(self, text):
        """One line to the node's terminal console."""
        self.machine.type_line(text)

    def refresh_snaps(self):
        t = time.time()                          # asked at t: lists older than a group op are stale
        self.snaps = [s["tag"] for s in self.rmm.call("snapshot.list")["snapshots"]]
        for fn in list(self.snap_listeners):
            fn(self, self.snaps, t)

    def refresh_regs(self):
        """After a stop. No pause of our own: that would stop the machine again."""
        self.regs_queued = False
        if not self.full:
            return                                   # no register file behind this adapter
        try:
            self.regs = self.rmm.call("cpu.regs.get")
        except RpcError as e:
            if e.code == -32601:
                self.regs = None                 # a backend with no registers (QEMU until gdb.py)
            elif e.code != 1:
                self.regs = {"error": str(e)}
        except OSError as e:
            self.regs = {"error": str(e)}

    def pause(self, induced=False):
        """machine.pause; induced=True marks it as the group's, so its event is no cause."""
        if not self.connected:
            return False
        try:
            if self.rmm.state() != "running":    # asked now: the cached status can be 0.5 s old
                return False
            if induced:
                self.induced_until = time.time() + 2.0
            self.rmm.call("machine.pause")
            return True
        except (RpcError, OSError):
            return False

    def resume(self):
        if not self.connected:
            return False
        try:
            self.rmm.call("machine.resume")
            return True
        except (RpcError, OSError):
            return False
