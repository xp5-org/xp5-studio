# SPDX-License-Identifier: MIT
"""A SIMH (dgnova, the Data General Nova simulator) machine. Stdlib only, POSIX (the console is a pty).

SIMH has no remote monitor: the console is a pty. Dgnova owns the simulator process and that pty
(its buffer is the console text). Pause is SIGINT to the sim> prompt, resume is `cont`, and a
snapshot is SAVE plus a copy of every writable attached unit, kept in a temp dir of the instance.
SimhRmm answers the RMM calls a manager uses on top of that, so group snap / restore / delete /
pause / resume treat it like an 86Box.

Because the pty dies with whoever holds it, a SIMH machine that must outlive its caller (rmmctl)
runs under backends/serve.py, which serves SimhRmm as a TCP JSON-RPC port; RemoteRmm is the client
for that port, and `"simh": {"attach": true, "port": N}` attaches a node to it.

Node keys: "simh": {"disk_image" | "script_path", "memory", "binary", "cwd", "rdos_boot"}, or
{"attach": true, "port": N}. SimhRmm also answers console.tty (text) and console.type (one line).
"""

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from ..rpc import RpcClient, RpcError
from . import Machine
from .proc import Proc

# pty is POSIX-only; the module must still import on Windows
if not sys.platform.startswith("win"):
    import pty

DEFAULT_BOOT_SCRIPT = """\
{memory_line}attach dkp0 {disk_image}
set tti dasher
boot dkp0
"""


def default_binary():
    """dgnova: $DGNOVA, the test tree's vendored build, else PATH."""
    for cand in (os.environ.get("DGNOVA"),
                 os.path.join(os.environ.get("TESTSRC_ROOT") or "/testsrc", "disk_artifacts", "bin", "dgnova")):
        if cand and os.path.exists(cand):
            return cand
    return shutil.which("dgnova") or "dgnova"


class Dgnova:
    """One dgnova process on a pty."""

    SNAPSHOT_DEVICES = ("dkp", "dsk", "mta")
    _ATTACHED_RE = re.compile(r"^\s*([A-Za-z]+\d*)\b[^\r\n]*?attached to ([^,\r\n]+)", re.M)
    _SCP_ERR = ("invalid", "non-existent", "command not completed", "cannot", "error", "unknown", "not found")

    def __init__(self, name, disk_image=None, script_path=None, memory=None, binary=None, cwd=None):
        self.name = name
        self.disk_image = disk_image
        self.script_path = script_path
        self.memory = memory                     # e.g. "60k" (dgnova max; default 32KW)
        self.binary = binary or default_binary()
        self.cwd = cwd                           # relative script paths resolve against it
        self.process = None
        self.pid = None
        self.master_fd = None
        self.buf = ""
        self._stop_reader = False
        self._generated_script = None
        self._snap_root = None
        self._snaps = {}

    # -- process ---------------------------------------------------------------------------

    def _script(self):
        if self.script_path:
            return self.script_path
        memory_line = f"set cpu {self.memory}\n" if self.memory else ""
        fd, path = tempfile.mkstemp(prefix=f"simh_{self.name}_", suffix=".ini")
        with os.fdopen(fd, "w") as f:
            f.write(DEFAULT_BOOT_SCRIPT.format(memory_line=memory_line, disk_image=self.disk_image))
        self._generated_script = path
        return path

    def start(self):
        if sys.platform.startswith("win"):
            self.buf += "SIMH nodes need a POSIX pty"
            return False
        try:
            master_fd, slave_fd = pty.openpty()
            self.process = subprocess.Popen([self.binary, self._script()], stdin=slave_fd, stdout=slave_fd,
                                            stderr=slave_fd, close_fds=True, cwd=self.cwd)
            os.close(slave_fd)
            self.master_fd = master_fd
            self.pid = self.process.pid
            threading.Thread(target=self._read_loop, name=f"dgnova-{self.name}", daemon=True).start()
            return True
        except Exception as e:  # noqa: BLE001 - the caller shows buf
            self.buf += str(e)
            return False

    def _read_loop(self):
        while not self._stop_reader:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self.buf += data.decode(errors="replace")

    def wait_for(self, phrase, timeout=15):
        start = time.time()
        while time.time() - start < timeout:
            if phrase in self.buf:
                return True
            if self.process.poll() is not None:
                return phrase in self.buf
            time.sleep(0.2)
        return False

    def send_command(self, cmd_text):
        if self.master_fd is not None:
            os.write(self.master_fd, (cmd_text + "\r").encode())

    def break_to_scp(self):
        if self.master_fd is not None:           # Ctrl-E (WRU): interrupt to the sim> prompt
            os.write(self.master_fd, b"\x05")
            time.sleep(0.5)

    def boot_to_rdos(self, date=None, time_str=None, timeout=30):
        """Drive dgnova/RDOS cold boot prompts: Filename? -> [optional "Partition in use"] -> Date -> Time -> R"""
        date = date or time.strftime("%m/%d/%Y")
        time_str = time_str or time.strftime("%H:%M:%S")
        if not self.wait_for("Filename?", timeout):
            return False
        self.send_command("")
        if self.wait_for("Type C to continue", 5):
            self.send_command("C")
        if not self.wait_for("Date", timeout):
            return False
        self.send_command(date)
        if not self.wait_for("Time", timeout):
            return False
        self.send_command(time_str)
        return self.wait_for("\nR", timeout)

    # -- simulator control prompt ------------------------------------------------------------

    def _scp(self, cmd, timeout=30):
        """Run one SCP command and return its output (up to the next prompt)."""
        mark = len(self.buf)
        self.send_command(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "sim>" in self.buf[mark:] or self.process.poll() is not None:
                break
            time.sleep(0.05)
        return self.buf[mark:]

    def _enter_scp(self, halted=False):
        """Stop the simulation at the sim> prompt -> True if it was running (caller `cont`s), False if
        already halted, None if no prompt appeared. halted=True: the caller knows it is at sim>
        already (a SIGINT there leaves the console deaf)."""
        if halted:
            return False
        mark = len(self.buf)
        os.kill(self.pid, signal.SIGINT)         # with `set tti dasher` ^E goes to the guest
        deadline = time.time() + 5
        while time.time() < deadline:
            out = self.buf[mark:]
            if "sim>" in out:
                return "Simulation stopped" in out
            time.sleep(0.05)
        if "sim>" in self._scp("", timeout=3):   # already at the prompt: SIGINT did nothing
            return False
        return None

    def _scp_error(self, out):
        for line in (out or "").replace("\r", "").splitlines():
            if line.strip().lower().startswith(self._SCP_ERR) or "not completed" in line.lower():
                return line.strip()
        return ""

    # -- snapshots: .sav plus a copy of each writable attached unit; errors reply "Error: ..." ----

    def _attached_units(self):
        """[(unit, host_path)] for every writable attached disk/tape unit."""
        units = []
        for dev in self.SNAPSHOT_DEVICES:
            out = self._scp(f"show {dev}", timeout=5)
            for line in out.replace("\r", "").splitlines():
                m = self._ATTACHED_RE.match(line)
                if m and "read only" not in line.lower():
                    units.append((m.group(1), m.group(2).strip()))
        return units

    def _snap_dir(self):
        if not self._snap_root:
            self._snap_root = tempfile.mkdtemp(prefix=f"simhsnap_{self.name}_")
        return self._snap_root

    def save_snapshot(self, tag="quicksave", halted=False):
        """SAVE + copy of every writable attached unit; pauses and resumes the simulation -> reply text,
        "Error: ..." on failure."""
        if self.master_fd is None or self.process is None or self.process.poll() is not None:
            return "Error: simulator not running"
        root = self._snap_dir()
        console = self.buf
        running = self._enter_scp(halted)
        if running is None:
            return "Error: no sim> prompt after SIGINT (console busy?)"
        sav = os.path.join(root, f"{tag}.sav")
        disks, log, err = {}, [], ""
        try:
            out = self._scp(f"save {sav}")
            err = self._scp_error(out)
            log.append(out.strip())
            if not err:
                for unit, path in self._attached_units():
                    self._scp(f"detach {unit}", timeout=10)
                    try:
                        copy = os.path.join(root, f"{tag}.{unit}.img")
                        shutil.copyfile(path, copy)
                        disks[unit] = (path, copy)
                    except OSError as e:
                        err = f"could not copy {path}: {e}"
                    out = self._scp(f"attach {unit} {path}", timeout=10)
                    err = err or self._scp_error(out)
        finally:
            if running:
                self._scp("cont", timeout=2)
        if err:
            for f in [sav] + [c for _, c in disks.values()]:
                if os.path.exists(f):
                    os.remove(f)
            return f"Error: {err}"
        self._snaps[tag] = {"sav": sav, "disks": disks, "running": running, "console": console}
        return f"saved {tag}: {os.path.getsize(sav)} bytes state, {len(disks)} unit(s) copied\n" + "\n".join(log)

    def load_snapshot(self, tag="quicksave", halted=False):
        """Restore unit copies, RESTORE, resume if live when saved; the console buffer is restored too."""
        meta = self._snaps.get(tag)
        if meta is None:
            return f"Error: no snapshot {tag!r} on {self.name}"
        if self.master_fd is None or self.process.poll() is not None:
            return "Error: simulator not running"
        if self._enter_scp(halted) is None:
            return "Error: no sim> prompt after SIGINT (console busy?)"
        err = ""
        for unit, (path, copy) in meta["disks"].items():
            self._scp(f"detach {unit}", timeout=10)
            try:
                shutil.copyfile(copy, path)
            except OSError as e:
                err = f"could not restore {path}: {e}"
        out = self._scp(f"restore {meta['sav']}")
        err = err or self._scp_error(out)
        if err:
            return f"Error: {err}"
        self.buf = meta["console"]
        if meta["running"]:
            self.send_command("cont")
        return f"restored {tag}\n{out.strip()}"

    def delete_snapshot(self, tag):
        meta = self._snaps.pop(tag, None)
        if meta:
            for f in [meta["sav"]] + [c for _, c in meta["disks"].values()]:
                if os.path.exists(f):
                    os.remove(f)
        return f"deleted {tag}"

    def stop(self):
        self._stop_reader = True
        if self.process and self.process.poll() is None:
            try:
                self.break_to_scp()
                self.send_command("quit")
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - fall back to signals
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self.process.kill()
                    self.process.wait()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self._generated_script and os.path.exists(self._generated_script):
            os.remove(self._generated_script)
        if self._snap_root:
            shutil.rmtree(self._snap_root, ignore_errors=True)
            self._snap_root = None


def _failed(reply):
    text = (reply or "").strip()
    return text if text.startswith("Error") else None


class _Vm:
    def show(self):
        raise RpcError("no 86Box domain on a SIMH node", code=None)


class SimhRmm:
    """The RMM subset over a Dgnova (or anything with its save_snapshot / _enter_scp / send_command shape)."""

    def __init__(self, inst):
        self.inst = inst
        self.vm = _Vm()
        self.port = 0
        self.banner = f"SIMH {os.path.basename(inst.binary)}"
        self._listeners = []
        self._paused = False
        self._exit_sent = False
        self._lock = threading.Lock()            # one SCP exchange at a time on the one pty

    def on_event(self, fn):
        self._listeners.append(fn)

    def _emit(self, name, params):
        for fn in list(self._listeners):
            fn(name, params)

    @property
    def connected(self):
        p = self.inst.process
        return p is not None and p.poll() is None

    def connect(self, wait=0):
        if not self.connected:
            raise OSError("simulator is not running")
        return self.banner

    def close(self):
        pass                                     # the Machine's Proc owns the process

    def state(self):
        if not self.connected:
            if not self._exit_sent:
                self._exit_sent = True
                self._emit("machine.exit", {})
            raise OSError("simulator exited")
        return "paused" if self._paused else "running"

    def pause(self):
        with self._lock:
            if self._paused:
                return
            if self.inst._enter_scp() is None:
                raise RpcError("no sim> prompt after SIGINT (console busy?)", code=1)
            self._paused = True
        self._emit("machine.stopped", {"reason": "pause"})

    def resume(self):
        with self._lock:
            if not self._paused:
                return
            self.inst.send_command("cont")
            self._paused = False
        self._emit("machine.resumed", {})

    def call(self, method, _timeout=None, **p):
        if method == "machine.status":
            return {"state": self.state(), "tsc": None}
        if method == "machine.pause":
            self.pause()
            return {}
        if method == "machine.resume":
            self.resume()
            return {}
        if method == "machine.quit":
            self.inst.stop()
            return {}
        if method == "machine.fast_forward.get":
            return {"value": False}
        if method == "console.tty":
            tail = p.get("tail")
            return {"text": self.inst.buf[-int(tail):] if tail else self.inst.buf}
        if method == "console.type":
            self.inst.send_command(p["text"])
            return {}
        if method == "snapshot.list":
            return {"snapshots": [{"tag": t} for t in self.inst._snaps]}
        if method in ("snapshot.save", "snapshot.restore", "snapshot.delete"):
            tag = p["tag"]
            if method == "snapshot.save" and not self._paused:
                raise RpcError("not paused", code=1)
            with self._lock:
                if method == "snapshot.save":
                    reply = self.inst.save_snapshot(tag, halted=self._paused)
                elif method == "snapshot.restore":
                    reply = self.inst.load_snapshot(tag, halted=self._paused)
                else:
                    reply = self.inst.delete_snapshot(tag)
            bad = _failed(reply)
            if bad:
                raise RpcError(f"{method} {tag}: {bad}", code=8 if method == "snapshot.save" else 7)
            return {}
        raise RpcError(f"{method}: not available on SIMH", code=-32601)


class RemoteRmm(RpcClient):
    """A SIMH machine served by backends/serve.py, from another process: same call() as the others."""

    def call(self, method, _timeout=None, **p):
        return super().call(method, p, timeout=_timeout)

    def state(self):
        return self.call("machine.status")["state"]


# -- the backend ----------------------------------------------------------------------------------

def validate(spec):
    if spec.launch is not None:
        return "a simh node is described by its \"simh\" object, not \"launch\""
    cfg = spec.raw.get("simh")
    if isinstance(cfg, dict) and cfg.get("attach"):
        if not isinstance(cfg.get("port"), int):
            return "an attached simh node needs \"simh\": {\"attach\": true, \"port\": <serve port>}"
        return None
    if not isinstance(cfg, dict) or not (cfg.get("disk_image") or cfg.get("script_path")):
        return "a simh node needs \"simh\": {\"disk_image\": ...} or {\"script_path\": ...} (or \"attach\": true with a port)"
    return None


def attaches(spec):
    return bool(spec.raw["simh"].get("attach"))


def attach_port(spec):
    return int(spec.raw["simh"]["port"])


class SimhMachine(Machine):
    def __init__(self, *a, sim=None, **kw):
        super().__init__(*a, **kw)
        self.sim = sim                           # the Dgnova, None when attached to a served one

    def title_lines(self):
        return [self.rmm.banner] + ([f"pid {self.sim.pid}"] if self.sim else [])

    def tty(self, tail=None):
        if self.sim is not None:
            return self.sim.buf[-tail:] if tail else self.sim.buf
        return self.rmm.call("console.tty", tail=tail)["text"]

    def type_line(self, text):
        if self.sim is not None:
            self.sim.send_command(text)
        else:
            self.rmm.call("console.type", text=text)


def start(spec, say=None, **_):
    say = say or (lambda text, tone="text": None)
    cfg = spec.raw["simh"]
    if cfg.get("attach"):
        return SimhMachine("simh", RemoteRmm(cfg["port"], spec.host, timeout=spec.rpc_timeout), None,
                           spec.host, int(cfg["port"]))
    sim = Dgnova(spec.name, disk_image=cfg.get("disk_image"), script_path=cfg.get("script_path"),
                 memory=cfg.get("memory"), binary=cfg.get("binary"), cwd=cfg.get("cwd"))
    if not sim.start():
        raise RuntimeError(f"{spec.name}: dgnova did not start: {sim.buf.strip()}")
    say(f"started SIMH pid {sim.pid}", "muted")
    sim.wait_for("simulator", 10)                # SIGINT before the banner would kill it
    time.sleep(0.5)
    if cfg.get("rdos_boot") and not sim.boot_to_rdos():
        say("RDOS boot prompts not seen", "warn")
    return SimhMachine("simh", SimhRmm(sim), Proc(sim.process, "SIMH"), spec.host, None, sim=sim)


def adapter(host, port, timeout=10.0, **_):
    return RemoteRmm(port, host, timeout=timeout)
