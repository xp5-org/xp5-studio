"""Host side of the Linux puppet mailbox (sourcedir/86box_remote_monitor/puppet_plan.md, section 2.1).

The guest (sourcedir/linuxpuppet/linux/linuxpuppet.c) keeps a 64 KB anchor at physical 0x80000. The host reads it with
mem.phys.read (works while the machine runs) and writes commands with mem.phys.write (needs a pause today, see
plan 5.4). Offsets mirror sourcedir/linuxpuppet/linux/puppet_abi.h: change both together.

    python3 -m rmm_86box.linuxpuppet --port 56560 wait [--timeout 120]
    python3 -m rmm_86box.linuxpuppet --port 56560 ping [-n 100] [--doorbell stop|poll]
    python3 -m rmm_86box.linuxpuppet --port 56560 echo [--size 4096]
    python3 -m rmm_86box.linuxpuppet --port 56560 xfer [--size 204800]          PUT + GET round trip, CRC checked
    python3 -m rmm_86box.linuxpuppet --port 56560 load DIR                      PUT a built personality (manifest.json)
    python3 -m rmm_86box.linuxpuppet --port 56560 run [--expect-exit N] [--expect-out TEXT] [--kill-after S]
                                                         [--expect-signal N] -- ARGV...
    python3 -m rmm_86box.linuxpuppet --port 56560 kexec --kernel BZIMAGE [--cmdline ...]
    python3 -m rmm_86box.linuxpuppet --port 56560 snap-busy --st /pp/selftest/bin/pp_selftest [--export F.snap]
    python3 -m rmm_86box.linuxpuppet --port 56560 snap-inflight
    python3 -m rmm_86box.linuxpuppet --port 56560 snap-roundtrip --st ... --dir OUT
    python3 -m rmm_86box.linuxpuppet --port 56560 snap-load --file F.snap      (fresh 86Box, no boot wait)
    python3 -m rmm_86box.linuxpuppet --port 56560 bulk

Loader ops (plan 2.3): tests are personalities, static programs PUT into the guest's RAM root and run as children
of the puppet, which keeps answering the mailbox. KEXEC boots another kernel without the BIOS.
"""

import argparse
import contextlib
import json
import os
import struct
import sys
import time
import zlib

from .rpc import RpcClient, RpcError

ANCHOR = 0x80000
MAGIC = b"86PUPPET"
ABI_VERSION = 2
MEM_MAX = 4096                       # RMM_MEM_MAX: bytes per mem.phys.read/write call

CMD_SEQ, CMD_OP, CMD_FLAGS, CMD_LEN, CMD_ARGS = 0x10, 0x14, 0x18, 0x1C, 0x20
RSP_SEQ, RSP_STATUS, RSP_LEN, RSP_CRC, RSP_VALS = 0x40, 0x44, 0x48, 0x4C, 0x50
HEARTBEAT, BULK_PHYS, BULK_SIZE, BOOT_STAGE = 0x70, 0x74, 0x78, 0x7C
ID_GEN = 0xFC
ID_BUILD, ID_KERNEL, ID_CPU, MB_LINEAR, ID_MEM_KB, ID_PCI_N, ID_PCI = 0x80, 0xA0, 0xC0, 0xF8, 0x100, 0x104, 0x108
PAYLOAD = 0x400
PAYLOAD_MAX = 0x10000 - PAYLOAD

STATES = {0: "booting", 1: "ready", 2: "busy", 3: "fault"}

# Text on the guest console that means the puppet will never come up. The wait reads the text screen straight
# from video RAM (no OCR) while it polls the anchor, and stops on the first of these instead of running out
# its timeout. "boot:" is SYSLINUX's prompt: the puppet floppy has PROMPT 0, so the prompt only shows when
# loading the kernel failed and SYSLINUX fell back to it.
CONSOLE_FAIL = (
    "Booting kernel failed", "Could not find kernel", "boot:",
    "Kernel panic", "Call Trace", "Oops:", "BUG:", "general protection fault",
    "Press <F1>", "Press F1", "Invalid configuration information", "No bootable device",
    "Non-System disk", "Boot failed", "DISK BOOT FAILURE",
)
OP_PING, OP_INFO, OP_ECHO = 1, 2, 3
OP_PUT, OP_GET, OP_EXEC, OP_WAIT, OP_KILL, OP_RM, OP_KEXEC = 4, 5, 6, 7, 8, 9, 10
PUT_CREATE, PUT_FINAL = 1, 2
KID_STATES = {0: "running", 1: "exited", 2: "signaled"}
ERRNO = {2: "ENOENT", 3: "ESRCH", 8: "ENOEXEC", 12: "ENOMEM", 13: "EACCES", 16: "EBUSY", 22: "EINVAL", 38: "ENOSYS"}


SNAP_RPC_S = 180.0                   # snapshot.save/restore/export/load move the whole machine: allow minutes


class PuppetError(RuntimeError):
    pass


def work_crcs(rounds, seed=1):
    """The CRC per round that `pp_selftest work ROUNDS SEED` prints (personalities/selftest): xorshift32 over a
    64 KB buffer of u32, CRC32 of the buffer after each round. A snapshot taken mid-run must not change these."""
    import array
    m = 0xFFFFFFFF
    x = seed or 1
    wb = array.array("I", bytes(65536))
    out = []
    for _ in range(rounds):
        for i in range(16384):
            x ^= (x << 13) & m
            x ^= x >> 17
            x ^= (x << 5) & m
            wb[i] ^= x
        out.append(zlib.crc32(wb.tobytes()) & m)
    return out


def save_screenshot(rpc, path):
    """capture.screenshot (rgb24, base64) -> PNG at path, stdlib only. Returns (w, h) or None."""
    import base64
    try:
        r = rpc.call("capture.screenshot", timeout=15)
    except RpcError:
        return None
    w, h, rgb = r["w"], r["h"], base64.b64decode(r["data"])
    raw = b"".join(b"\0" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return w, h


def _cstr(b):
    return b.split(b"\0", 1)[0].decode(errors="replace")


class Puppet:
    def __init__(self, rpc, anchor=ANCHOR):
        self.rpc = rpc
        self.anchor = anchor
        self.seq = None
        self.linear = None
        self.stats = {"pauses": 0, "reads": 0, "writes": 0, "pause_s": 0.0}
        self._depth = 0
        self.last_console = []

    # -- raw memory ---------------------------------------------------------------------
    @contextlib.contextmanager
    def _hold(self):
        """mem.phys.read and .write both need a paused machine (RMM_MF_PAUSED, tree A 2026-11-15). Pause if it
        runs, resume on exit; nested holds and a machine already paused (watchpoint, user) are left alone."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        paused_here = not self._paused()
        t0 = time.time()
        if paused_here:
            self.rpc.call("machine.pause")
            self.stats["pauses"] += 1
        self._depth = 1
        try:
            yield
        finally:
            self._depth = 0
            if paused_here:
                self.rpc.call("machine.resume")
                self.stats["pause_s"] += time.time() - t0

    def read(self, off, n):
        with self._hold():
            return self._read(off, n)

    def _read(self, off, n):
        out = bytearray()
        while n > 0:
            k = min(n, MEM_MAX)
            r = self.rpc.call("mem.phys.read", addr=self.anchor + off, len=k)
            out += bytes.fromhex(r["data"])
            self.stats["reads"] += 1
            off += k
            n -= k
        return bytes(out)

    def u32(self, off):
        return struct.unpack("<I", self.read(off, 4))[0]

    def _write(self, off, data):
        if not self._depth:
            raise PuppetError("internal: write outside a hold")
        for i in range(0, len(data), MEM_MAX):
            self.rpc.call("mem.phys.write", addr=self.anchor + off + i, data=data[i:i + MEM_MAX].hex())
            self.stats["writes"] += 1

    def _paused(self):
        st = self.rpc.call("machine.status")
        return isinstance(st, dict) and st.get("state") == "paused"

    # -- anchor -------------------------------------------------------------------------
    def header(self):
        h = self.read(0, 0x400)
        n = min(struct.unpack_from("<I", h, ID_PCI_N)[0], (0x400 - ID_PCI) // 8)
        pci = []
        for i in range(n):
            bdf, vd = struct.unpack_from("<II", h, ID_PCI + i * 8)
            pci.append({"bus": bdf >> 8, "dev": (bdf >> 3) & 31, "fn": bdf & 7,
                        "vendor": f"{vd >> 16:04x}", "device": f"{vd & 0xffff:04x}"})
        state = struct.unpack_from("<I", h, 0x0C)[0]
        return {
            "magic_ok": h[:8] == MAGIC,
            "version": struct.unpack_from("<I", h, 0x08)[0],
            "state": STATES.get(state, state),
            "boot_stage": struct.unpack_from("<I", h, BOOT_STAGE)[0],
            "heartbeat": struct.unpack_from("<I", h, HEARTBEAT)[0],
            "cmd_seq": struct.unpack_from("<I", h, CMD_SEQ)[0],
            "rsp_seq": struct.unpack_from("<I", h, RSP_SEQ)[0],
            "bulk": (struct.unpack_from("<I", h, BULK_PHYS)[0], struct.unpack_from("<I", h, BULK_SIZE)[0]),
            "linear": struct.unpack_from("<I", h, MB_LINEAR)[0],
            "gen": struct.unpack_from("<I", h, ID_GEN)[0],
            "build": _cstr(h[ID_BUILD:ID_BUILD + 32]),
            "kernel": _cstr(h[ID_KERNEL:ID_KERNEL + 32]),
            "cpu": _cstr(h[ID_CPU:ID_CPU + 56]),
            "mem_kb": struct.unpack_from("<I", h, ID_MEM_KB)[0],
            "pci": pci,
        }

    def _read_phys(self, addr, n):
        out = bytearray()
        while n > 0:
            k = min(n, MEM_MAX)
            out += bytes.fromhex(self.rpc.call("mem.phys.read", addr=addr, len=k)["data"])
            self.stats["reads"] += 1
            addr += k
            n -= k
        return bytes(out)

    def console(self):
        """The guest's text screen as a list of lines, read from video RAM (B800:0, or B000:0 when the BIOS
        data area says mode 7 / MDA). In a graphics mode this returns noise, which matches no phrase."""
        with self._hold():
            bda = self._read_phys(0x449, 0x3C)               # 0x449 mode, 0x44A columns, 0x484 rows-1
            mode, cols, rows = bda[0], bda[1] | bda[2] << 8, bda[0x484 - 0x449] + 1
            cols = cols if cols in (40, 80, 132) else 80
            rows = rows if 25 <= rows <= 60 else 25
            raw = self._read_phys(0xB0000 if mode == 7 else 0xB8000, cols * rows * 2)
        text = bytes(raw[0::2]).decode("cp437", errors="replace")
        return [text[i:i + cols].rstrip() for i in range(0, len(text), cols)]

    def wait_ready(self, timeout=120.0, poll=1.0, fail_phrases=CONSOLE_FAIL, want_gen=None):
        """Wait (reading only, the guest keeps running) until the anchor says ready. want_gen: also wait for that
        boot generation (after KEXEC the old anchor still looks ready until the new puppet rewrites it)."""
        t0 = time.time()
        deadline = t0 + timeout
        last = None
        paused_since = None
        next_report = t0
        seen = set()
        while time.time() < deadline:
            if time.time() >= next_report:
                # one line every 10 s so a run log shows what the wait is seeing, not just silence
                next_report += 10.0
                print(f"wait t={time.time() - t0:5.0f}s last={last}", file=sys.stderr, flush=True)
            try:
                if self._paused():
                    # a person in BIOS setup or a reset can pause briefly; only a pause that lasts is an error
                    paused_since = paused_since or time.time()
                    if time.time() - paused_since >= 20.0 and not self.header()["magic_ok"]:
                        raise PuppetError("the machine has been paused for 20 s and the puppet never came up: if "
                                          "nobody paused it, set rmm_pause_on_connect=0 in the VM's General section "
                                          "(RMM pauses on every client connect by default)")
                    time.sleep(poll)
                    continue
                paused_since = None
                with self._hold():                           # one pause for the anchor and the screen
                    h = self.header()
                    screen = self.console()
                self.last_console = screen
                # skip lines that are mostly non-ASCII: font tables and graphics-mode noise, not text
                new = [ln for ln in screen if ln.strip() and ln not in seen
                       and sum(32 <= ord(c) < 127 for c in ln) >= 0.8 * len(ln)]
                for ln in new:
                    print(f"console: {ln}", file=sys.stderr, flush=True)
                seen.update(new)
                if not h["magic_ok"] or (want_gen is not None and h["gen"] != want_gen):
                    for ln in screen:
                        hit = next((f for f in fail_phrases if f.lower() in ln.lower()), None)
                        if hit:
                            tail = "\n  ".join(x for x in screen if x.strip())
                            raise PuppetError(f"console shows {hit!r} after {time.time() - t0:.0f}s, the puppet "
                                              f"will not come up: {ln.strip()!r}\nscreen:\n  {tail}")
            except RpcError as e:
                last = f"rpc: {e}"
            else:
                if (h["magic_ok"] and h["version"] == ABI_VERSION and h["state"] in ("ready", "busy")
                        and (want_gen is None or h["gen"] == want_gen)):
                    self.seq = h["rsp_seq"]
                    self.linear = h["linear"]
                    return h
                last = h
            time.sleep(poll)
        tail = "\n  ".join(x for x in self.last_console if x.strip())
        raise PuppetError(f"puppet not ready after {timeout:.0f}s; last anchor: {last}\nscreen:\n  {tail}")

    # -- commands -----------------------------------------------------------------------
    def call(self, op, args=(), payload=b"", doorbell="poll", timeout=20.0, in_flight=None):
        """One command. doorbell='poll': read rsp.seq until it moves. 'stop': a write watchpoint on rsp.seq
        stops the machine at the guest's doorbell write; the reply is read while paused, then it resumes.
        in_flight(seq): called while still paused right after cmd.seq is written, before the guest can see it
        (the mailbox-continuity snapshot check, plan 7.2, saves there)."""
        if self.seq is None:
            self.wait_ready()
        if len(payload) > PAYLOAD_MAX:
            raise PuppetError(f"payload {len(payload)} > {PAYLOAD_MAX}")
        seq = (self.seq + 1) & 0xFFFFFFFF
        a = list(args)[:8] + [0] * (8 - len(list(args)[:8]))
        head = struct.pack("<III", op, 0, len(payload)) + struct.pack("<8I", *a)
        t0 = time.time()
        watch = None
        with self._hold():
            if payload:
                self._write(PAYLOAD, payload)
            self._write(CMD_OP, head)
            if doorbell == "stop":
                if not self.linear:
                    raise PuppetError("anchor has no linear address; stop doorbell unavailable")
                watch = self.rpc.call("debug.watch.set", addr=self.linear + RSP_SEQ, len=4, kind="w")
            self._write(CMD_SEQ, struct.pack("<I", seq))   # last: the guest acts on this
            if in_flight is not None:
                in_flight(seq)
        try:
            if doorbell == "stop":
                deadline = time.time() + timeout
                while True:
                    ev = self.rpc.wait_event("machine.stopped", timeout=max(0.1, deadline - time.time()),
                                             where=lambda p: (p or {}).get("reason") == "watchpoint")
                    if ev is None:
                        raise PuppetError(f"seq {seq}: no watchpoint stop within {timeout}s")
                    # stopped right after the doorbell write: the reply is complete; read it before resuming.
                    # A test child can own the same linear address in its own address space: that stop is not
                    # the doorbell, so resume and keep waiting.
                    got = self.u32(RSP_SEQ)
                    if got == seq or time.time() > deadline:
                        break
                    self.rpc.call("machine.resume")
                rsp = self._read(RSP_STATUS, 0x30)
                rlen = struct.unpack_from("<I", rsp, 4)[0]
                data = self._read(PAYLOAD, rlen) if 0 < rlen <= PAYLOAD_MAX else b""
            else:
                got, rsp, data = self._poll_reply(seq, time.time() + timeout)
        finally:
            if watch is not None:
                wid = watch.get("id") if isinstance(watch, dict) else watch
                try:
                    self.rpc.call("debug.watch.clear", id=wid)
                except RpcError:
                    pass
                self.rpc.call("machine.resume")              # the watchpoint stopped it
        return self._result(seq, got, rsp, data, t0, timeout)

    def _poll_reply(self, seq, deadline):
        rsp = data = None
        while True:
            with self._hold():
                got = struct.unpack("<I", self._read(RSP_SEQ, 4))[0]
                if got == seq:
                    rsp = self._read(RSP_STATUS, 0x30)
                    rlen = struct.unpack_from("<I", rsp, 4)[0]
                    data = self._read(PAYLOAD, rlen) if 0 < rlen <= PAYLOAD_MAX else b""
            if got == seq or time.time() > deadline:
                return got, rsp, data
            time.sleep(0.02)                                 # let the guest run between looks

    def collect(self, seq, timeout=20.0):
        """Wait (poll) for the reply to a command already posted, e.g. one that was in flight in a snapshot
        that was just restored."""
        t0 = time.time()
        got, rsp, data = self._poll_reply(seq, t0 + timeout)
        return self._result(seq, got, rsp, data, t0, timeout)

    def _result(self, seq, got, rsp, data, t0, timeout):
        if got != seq:
            raise PuppetError(f"no reply to seq {seq} (rsp.seq {got}) within {timeout}s")
        self.seq = seq
        status, rlen, rcrc = struct.unpack_from("<III", rsp, 0)
        vals = list(struct.unpack_from("<8I", rsp, RSP_VALS - RSP_STATUS))
        if data and (zlib.crc32(data) & 0xFFFFFFFF) != rcrc:
            raise PuppetError(f"seq {seq}: response crc mismatch")
        return {"seq": seq, "status": status, "vals": vals, "data": data, "rtt_s": time.time() - t0}

    def ping(self, cookie=0, **kw):
        return self.call(OP_PING, [cookie], **kw)

    def info(self, **kw):
        r = self.call(OP_INFO, **kw)
        return self.header() if r["status"] == 0 else r

    # -- loader (plan 2.3) ------------------------------------------------------------------
    @staticmethod
    def _ok(r, what):
        if r["status"]:
            raise PuppetError(f"{what}: {ERRNO.get(r['status'], 'errno')} ({r['status']})")
        return r

    def put(self, path, data, mode=0o644, **kw):
        """Copy data to path in the guest's RAM root (parents are created). Checks the guest's whole-file CRC."""
        p = path.encode() + b"\0"
        chunk = PAYLOAD_MAX - len(p)
        off = 0
        t0 = time.time()
        while True:
            part = data[off:off + chunk]
            last = off + len(part) >= len(data)
            fl = (PUT_CREATE if off == 0 else 0) | (PUT_FINAL if last else 0)
            r = self._ok(self.call(OP_PUT, [off, fl, mode], p + part, **kw), f"PUT {path} @{off}")
            if r["vals"][0] != len(part):
                raise PuppetError(f"PUT {path} @{off}: guest wrote {r['vals'][0]} of {len(part)}")
            off += len(part)
            if last:
                break
        want = zlib.crc32(data) & 0xFFFFFFFF
        if r["vals"][1] != len(data) or r["vals"][2] != want:
            raise PuppetError(f"PUT {path}: guest file size {r['vals'][1]} crc {r['vals'][2]:08x}, "
                              f"host {len(data)} {want:08x}")
        return {"path": path, "size": len(data), "crc": want, "s": time.time() - t0}

    def get(self, path, offset=0, limit=None, **kw):
        """Read a guest file (or /proc file) back."""
        out = bytearray()
        p = path.encode() + b"\0"
        while limit is None or len(out) < limit:
            want = PAYLOAD_MAX if limit is None else min(PAYLOAD_MAX, limit - len(out))
            r = self._ok(self.call(OP_GET, [offset + len(out), want], p, **kw), f"GET {path}")
            out += r["data"]
            if len(r["data"]) < want:
                break
        return bytes(out)

    def rm(self, path, **kw):
        return self._ok(self.call(OP_RM, [], path.encode() + b"\0", **kw), f"RM {path}")

    def exec(self, argv, cwd="/", env=None, **kw):
        """Start argv as a child of the puppet; returns its pid. Output goes to /pp/run/<pid>.out."""
        blob = cwd.encode() + b"\0" + b"".join(a.encode() + b"\0" for a in argv) + b"\0"
        if env:
            blob += b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()) + b"\0"
        return self._ok(self.call(OP_EXEC, [], blob, **kw), f"EXEC {argv[0]}")["vals"][0]

    def wait(self, pid, timeout=5.0, out_offset=0, out_max=PAYLOAD_MAX, **kw):
        """Block in the guest up to timeout s for pid. Returns state/code/out_size/run_ms and an output slice."""
        ms = int(timeout * 1000)
        r = self._ok(self.call(OP_WAIT, [pid, ms, out_offset, out_max], timeout=timeout + 20.0, **kw), f"WAIT {pid}")
        v = r["vals"]
        return {"pid": pid, "state": KID_STATES.get(v[0], v[0]), "code": v[1], "out_size": v[2], "run_ms": v[3],
                "out": r["data"]}

    def kill(self, pid, sig=9, **kw):
        return self._ok(self.call(OP_KILL, [pid, sig], **kw), f"KILL {pid}")

    def run(self, argv, timeout=60.0, kill_after=None, cwd="/", env=None, hold=0.0, **kw):
        """EXEC + WAIT until it ends (or kill_after s, then SIGKILL); returns the WAIT result with all output."""
        pid = self.exec(argv, cwd=cwd, env=env, **kw)
        t0 = time.time()
        if hold > 0:                                         # touch nothing for this long: every look pauses the VM (audio!)
            time.sleep(hold)
        limit = kill_after if kill_after is not None else timeout
        while True:
            w = self.wait(pid, timeout=max(0.0, min(5.0, limit - (time.time() - t0))), out_max=0, **kw)
            if w["state"] != "running":
                break
            if time.time() - t0 >= limit:
                if kill_after is None:
                    self.kill(pid)
                    raise PuppetError(f"{argv[0]}: still running after {timeout:.0f}s, killed")
                self.kill(pid)
                w = self.wait(pid, timeout=5.0, out_max=0, **kw)
                break
        w["out"] = self.get(f"/pp/run/{pid}.out", **kw) if w["out_size"] else b""
        self.rm(f"/pp/run/{pid}.out", **kw)
        return w

    def load(self, pdir, **kw):
        """PUT a built personality: pdir/manifest.json lists {src, dst, mode} relative to pdir."""
        with open(os.path.join(pdir, "manifest.json")) as f:
            man = json.load(f)
        man["put"] = [self.put(e["dst"], open(os.path.join(pdir, e["src"]), "rb").read(), e.get("mode", 0o755), **kw)
                      for e in man["files"]]
        return man

    def kexec(self, kernel, initrd="", cmdline="", execute=True, timeout=120.0, **kw):
        """Boot another kernel (a path in the guest) without the BIOS. Empty cmdline = this boot's with puppet.gen+1.
        With execute, waits until that boot's puppet is ready and returns its anchor."""
        gen = self.header()["gen"]
        blob = b"".join(x.encode() + b"\0" for x in (kernel, initrd, cmdline))
        r = self._ok(self.call(OP_KEXEC, [1 if execute else 0], blob, **kw), f"KEXEC {kernel}")
        if not execute:
            return r
        want = gen + 1 if not cmdline else None
        self.seq = None
        if want is None:
            time.sleep(3.0)                                  # explicit cmdline: no gen to wait for
        h = self.wait_ready(timeout=timeout, want_gen=want)
        h["kexec_vals"] = r["vals"]
        return h

    # -- snapshots (plan 2.4, section 7) --------------------------------------------------------
    def snap_save(self, tag):
        """snapshot.save needs a paused machine: pause (unless already), save, resume. Returns seconds."""
        t0 = time.time()
        with self._hold():
            self.rpc.call("snapshot.save", timeout=SNAP_RPC_S, tag=tag)
        return time.time() - t0

    def _after_restore(self, resume):
        """The machine is paused on a restored state: take seq and the anchor from it (the host's own seq
        belongs to the timeline it left)."""
        h = self.header()
        if not h["magic_ok"]:
            raise PuppetError("the restored machine has no puppet anchor at 0x%x" % self.anchor)
        self.seq = h["rsp_seq"]
        self.linear = h["linear"]
        if resume:
            self.rpc.call("machine.resume")
        return h

    def snap_restore(self, tag, resume=True):
        t0 = time.time()
        self.rpc.call("snapshot.restore", timeout=SNAP_RPC_S, tag=tag)   # leaves the machine paused
        h = self._after_restore(resume)
        h["restore_s"] = time.time() - t0
        return h

    def snap_export(self, tag, path):
        t0 = time.time()
        with self._hold():                                   # export needs a paused machine too (error 1)
            self.rpc.call("snapshot.export", timeout=SNAP_RPC_S, tag=tag, path=path)
        return time.time() - t0

    def snap_load(self, path, tag=None, resume=True):
        """snapshot.load a file (any 86Box process of the same VM and build) and restore it."""
        t0 = time.time()
        kw = {"path": path, "restore": True}
        if tag:
            kw["tag"] = tag
        self.rpc.call("snapshot.load", timeout=SNAP_RPC_S, **kw)
        h = self._after_restore(resume)
        h["load_s"] = time.time() - t0
        return h

    def echo(self, data, **kw):
        r = self.call(OP_ECHO, payload=data, **kw)
        want = zlib.crc32(data) & 0xFFFFFFFF
        if r["status"] != 0 or r["vals"][0] != want or r["data"] != data:
            raise PuppetError(f"echo of {len(data)} bytes: status {r['status']} guest crc {r['vals'][0]:08x} "
                              f"host crc {want:08x} data {'same' if r['data'] == data else 'differs'}")
        return r


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rmm_86box.linuxpuppet", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--connect-wait", type=float, default=60.0)
    ap.add_argument("--json", help="also write the result as JSON to this file")
    ap.add_argument("--shot", help="on failure, save an RMM screenshot (PNG) and the first 1 KB of the anchor here (+.bin)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wait")
    w.add_argument("--timeout", type=float, default=120.0)
    p = sub.add_parser("ping")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--doorbell", choices=("poll", "stop"), default="poll")
    p.add_argument("--timeout", type=float, default=120.0)
    e = sub.add_parser("echo")
    e.add_argument("--size", type=int, default=4096)
    e.add_argument("--doorbell", choices=("poll", "stop"), default="poll")
    e.add_argument("--timeout", type=float, default=120.0)
    x = sub.add_parser("xfer", help="PUT random data, GET it back, compare")
    x.add_argument("--size", type=int, default=204800)
    x.add_argument("--path", default="/pp/tmp/xfer.bin")
    x.add_argument("--timeout", type=float, default=120.0)
    ld = sub.add_parser("load", help="PUT a built personality directory")
    ld.add_argument("dir")
    ld.add_argument("--timeout", type=float, default=120.0)
    rn = sub.add_parser("run", help="EXEC ARGV in the guest, wait, print its output")
    rn.add_argument("--expect-exit", type=int)
    rn.add_argument("--expect-signal", type=int)
    rn.add_argument("--expect-out", action="append", default=[], help="text that must be in the output (repeatable)")
    rn.add_argument("--expect-crc-of", help="host file whose CRC32 must appear in the output as crc=%%08x")
    rn.add_argument("--kill-after", type=float, help="SIGKILL it after this many seconds (KILL test)")
    rn.add_argument("--run-timeout", type=float, default=60.0)
    rn.add_argument("--hold", type=float, default=0.0, help="after EXEC, leave the VM alone this many seconds (no pauses) before WAIT")
    rn.add_argument("--timeout", type=float, default=120.0)
    rn.add_argument("argv", nargs="+")
    pt = sub.add_parser("put", help="PUT a host file to a guest path")
    pt.add_argument("local")
    pt.add_argument("remote")
    pt.add_argument("--timeout", type=float, default=300.0)
    gt = sub.add_parser("get", help="GET a guest file to a host file")
    gt.add_argument("remote")
    gt.add_argument("local")
    gt.add_argument("--timeout", type=float, default=300.0)
    kx = sub.add_parser("kexec", help="PUT a bzImage, boot it with KEXEC, wait for the new puppet")
    kx.add_argument("--kernel", required=True, help="host path of the bzImage")
    kx.add_argument("--cmdline", default="")
    kx.add_argument("--timeout", type=float, default=120.0)
    sb = sub.add_parser("snap-busy", help="work child mid-run: save, finish, restore, finish again (plan 7.1)")
    sb.add_argument("--st", required=True, help="guest path of pp_selftest")
    sb.add_argument("--rounds", type=int, default=200)
    sb.add_argument("--ms", type=int, default=50)
    sb.add_argument("--export", help="also export the mid-run snapshot here (+ .json state) for snap-load")
    sb.add_argument("--timeout", type=float, default=120.0)
    si = sub.add_parser("snap-inflight", help="save with a command in flight; the reply comes once per timeline (7.2)")
    si.add_argument("--size", type=int, default=4096)
    si.add_argument("--timeout", type=float, default=120.0)
    sr = sub.add_parser("snap-roundtrip", help="save -> restore -> save while paused; files match per region (7.3)")
    sr.add_argument("--st", required=True)
    sr.add_argument("--dir", required=True, help="host dir for rt_a.snap, rt_b.snap, rt_diff.json")
    sr.add_argument("--rounds", type=int, default=200)
    sr.add_argument("--ms", type=int, default=50)
    sr.add_argument("--timeout", type=float, default=120.0)
    sl = sub.add_parser("snap-load", help="snapshot.load a snap-busy export into this (fresh) 86Box, finish the child")
    sl.add_argument("--file", required=True)
    sl.add_argument("--state", help="default FILE.json")
    sl.add_argument("--timeout", type=float, default=120.0)
    bk = sub.add_parser("bulk", help="ECHO at payload-size edges + 1 MB PUT/GET with bytes/s (plan 5.1)")
    bk.add_argument("--timeout", type=float, default=120.0)
    vm = sub.add_parser("vga-modes", help="standard VGA modes via INT10 (vm86): pattern, VRAM CRC, screenshot, vblank (P3)")
    vm.add_argument("--modes", default="", help="comma list of hex modes (default: 0-7, d, e, 10-13)")
    vm.add_argument("--dir", help="host dir for the screenshots")
    vm.add_argument("--seed", type=lambda x: int(x, 0), default=0x3E5D)
    vm.add_argument("--timeout", type=float, default=120.0)
    vv = sub.add_parser("vga-vbe", help="every VBE mode pp_vga can draw (LFB 8/15/16/24/32 bpp, planar, text) (P3)")
    vv.add_argument("--dir")
    vv.add_argument("--max", type=int, default=0, help="check at most this many modes (0 = all)")
    vv.add_argument("--seed", type=lambda x: int(x, 0), default=0x7BE0)
    vv.add_argument("--timeout", type=float, default=120.0)
    vr = sub.add_parser("vga-regseq", help="mode via BIOS, dump registers, replay them as REGSEQ from text mode (P3)")
    vr.add_argument("--mode", type=lambda x: int(x, 16), default=0x13)
    vr.add_argument("--dir")
    vr.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)

    rpc = RpcClient(a.port, a.host)
    rpc.connect(wait=a.connect_wait)
    pup = Puppet(rpc)
    out = {}
    try:
        if a.cmd == "snap-load":
            # a fresh process is still booting its own puppet: load at once, the file has the machine to use
            from . import puppet_checks
            out["snap_load"] = r = puppet_checks.load(pup, a.file, a.state)
            print("PUPPET SNAP-LOAD OK load={load_s:.1f}s child rounds={work[rounds]} crc={work[crc]} "
                  "ping={ping_ms:.0f}ms".format(**r))
            out["stats"] = pup.stats
            out["verdict"] = "PASS"
            print("PUPPET VERDICT PASS", json.dumps(pup.stats))
            return 0
        h = pup.wait_ready(timeout=a.timeout)
        out["anchor"] = h
        print(f"PUPPET READY build={h['build']} kernel={h['kernel']} cpu={h['cpu']!r} mem={h['mem_kb']}kB "
              f"pci={len(h['pci'])} linear=0x{h['linear']:08x} heartbeat={h['heartbeat']}")
        for d in h["pci"]:
            print(f"  pci {d['bus']:02x}:{d['dev']:02x}.{d['fn']} {d['vendor']}:{d['device']}")
        if a.cmd == "ping":
            rtts = []
            for i in range(a.n):
                r = pup.ping(cookie=0xC0DE0000 + i, doorbell=a.doorbell)
                if r["status"] != 0 or r["vals"][1] != 0xC0DE0000 + i:
                    raise PuppetError(f"ping {i}: {r}")
                rtts.append(r["rtt_s"])
            rtts.sort()
            out["ping"] = {"n": a.n, "doorbell": a.doorbell, "min_ms": rtts[0] * 1e3,
                           "median_ms": rtts[len(rtts) // 2] * 1e3, "max_ms": rtts[-1] * 1e3}
            print("PUPPET PING OK n={n} doorbell={doorbell} min={min_ms:.1f}ms median={median_ms:.1f}ms "
                  "max={max_ms:.1f}ms".format(**out["ping"]))
        elif a.cmd == "echo":
            data = os.urandom(a.size)
            r = pup.echo(data, doorbell=a.doorbell)
            out["echo"] = {"size": a.size, "rtt_ms": r["rtt_s"] * 1e3}
            print(f"PUPPET ECHO OK size={a.size} rtt={r['rtt_s'] * 1e3:.1f}ms")
        elif a.cmd == "xfer":
            data = os.urandom(a.size)
            p = pup.put(a.path, data)
            t0 = time.time()
            back = pup.get(a.path)
            g = time.time() - t0
            pup.rm(a.path)
            if back != data:
                raise PuppetError(f"GET {a.path}: {len(back)} bytes back, differs from the {len(data)} put")
            out["xfer"] = {"size": a.size, "put_s": p["s"], "get_s": g, "put_kBps": a.size / 1024 / p["s"],
                           "get_kBps": a.size / 1024 / g, "crc": f"{p['crc']:08x}"}
            print("PUPPET XFER OK size={size} crc={crc} put={put_s:.2f}s ({put_kBps:.0f} kB/s) "
                  "get={get_s:.2f}s ({get_kBps:.0f} kB/s)".format(**out["xfer"]))
        elif a.cmd == "load":
            man = pup.load(a.dir)
            out["load"] = {"name": man.get("name"), "files": man["put"]}
            for f in man["put"]:
                print(f"PUPPET LOAD {f['path']} size={f['size']} crc={f['crc']:08x} {f['s']:.2f}s")
        elif a.cmd == "run":
            w = pup.run(a.argv, timeout=a.run_timeout, kill_after=a.kill_after, hold=a.hold)
            text = w["out"].decode(errors="replace")
            for ln in text.splitlines():
                print(f"guest: {ln}")
            out["run"] = {k: v for k, v in w.items() if k != "out"}
            out["run"]["out"] = text
            print(f"PUPPET RUN {a.argv[0]} state={w['state']} code={w['code']} run={w['run_ms']}ms out={w['out_size']}B")
            bad = []
            if a.expect_exit is not None and (w["state"], w["code"]) != ("exited", a.expect_exit):
                bad.append(f"wanted exit {a.expect_exit}")
            if a.expect_signal is not None and (w["state"], w["code"]) != ("signaled", a.expect_signal):
                bad.append(f"wanted signal {a.expect_signal}")
            if a.expect_crc_of:
                a.expect_out.append("crc=%08x" % (zlib.crc32(open(a.expect_crc_of, "rb").read()) & 0xFFFFFFFF))
            bad += [f"output lacks {t!r}" for t in a.expect_out if t not in text]
            if bad:
                raise PuppetError(f"run {a.argv}: {w['state']} {w['code']}: " + "; ".join(bad))
        elif a.cmd == "put":
            data = open(a.local, "rb").read()
            p = pup.put(a.remote, data, 0o644)
            out["put"] = {"path": a.remote, "size": p["size"], "crc": f"{p['crc']:08x}", "s": p["s"]}
            print(f"PUPPET PUT {a.remote} size={p['size']} crc={p['crc']:08x} {p['s']:.2f}s")
        elif a.cmd == "get":
            data = pup.get(a.remote)
            open(a.local, "wb").write(data)
            out["get"] = {"path": a.remote, "size": len(data), "crc": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"}
            print(f"PUPPET GET {a.remote} size={len(data)} crc={zlib.crc32(data) & 0xFFFFFFFF:08x}")
        elif a.cmd == "kexec":
            data = open(a.kernel, "rb").read()
            p = pup.put("/pp/boot/bzImage", data)
            print(f"PUPPET KEXEC put bzImage size={p['size']} {p['s']:.1f}s")
            gen0 = pup.header()["gen"]
            t0 = time.time()
            h = pup.kexec("/pp/boot/bzImage", cmdline=a.cmdline, timeout=a.timeout)
            r = pup.ping(cookie=0x6E6E)
            if r["status"] or r["vals"][1] != 0x6E6E:
                raise PuppetError(f"PING after kexec: {r}")
            out["kexec"] = {"gen_before": gen0, "gen_after": h["gen"], "boot_s": time.time() - t0,
                            "segments": h["kexec_vals"][0], "kernel_bytes": h["kexec_vals"][1],
                            "init_size": h["kexec_vals"][3], "kernel": h["kernel"], "build": h["build"]}
            print("PUPPET KEXEC OK gen {gen_before} -> {gen_after} in {boot_s:.1f}s, kernel {kernel} "
                  "({kernel_bytes} bytes, init_size {init_size})".format(**out["kexec"]))
        elif a.cmd.startswith("snap-") or a.cmd == "bulk":
            from . import puppet_checks as pc
            if a.cmd == "snap-busy":
                out["snap_busy"] = r = pc.busy(pup, a.st, a.rounds, ms=a.ms, export=a.export)
                print("PUPPET SNAP-BUSY OK saved at round {lines_at_save}, save={save_s:.1f}s "
                      "restore={restore_s:.1f}s, both timelines crc {a[crc]} ({a[rounds]} rounds)".format(**r))
            elif a.cmd == "snap-inflight":
                out["snap_inflight"] = r = pc.inflight(pup, a.size)
                print("PUPPET SNAP-INFLIGHT OK seq {seq} answered once in each timeline, same {size} bytes, "
                      "save={save_s:.1f}s restore={restore_s:.1f}s".format(**r))
            elif a.cmd == "snap-roundtrip":
                os.makedirs(a.dir, exist_ok=True)
                out["snap_roundtrip"] = r = pc.roundtrip(pup, a.st, a.dir, a.rounds, ms=a.ms)
                for ln in r["ab"]:
                    print(f"snapdiff A/B: {ln}")
                print("PUPPET SNAP-ROUNDTRIP OK B == C exactly; A vs B only {n} re-allocated pointers + allowed {al}; "
                      "paused {paused_s}s, child crc {crc}".format(n=r["ab_ptr_words"], al=r["ab_allowed"] or "none",
                                                                paused_s=r["paused_s"], crc=r["work"]["crc"]))
                for lab, n in r["by_label"].items():
                    print(f"  region {lab}: {n} bytes")
            elif a.cmd == "bulk":
                out["bulk"] = r = pc.bulk(pup)
                for e in r["echo"]:
                    print("PUPPET BULK echo size={size} best={best_ms}ms ({kBps} kB/s both ways)".format(**e))
                print("PUPPET BULK file 1 MB put={put_kBps} kB/s get={get_kBps} kB/s".format(**r["file"]))
        elif a.cmd.startswith("vga-"):
            from . import puppet_vga as pv
            if a.dir:
                os.makedirs(a.dir, exist_ok=True)
            if a.cmd == "vga-modes":
                ml = [int(x, 16) for x in a.modes.split(",") if x] or None
                res = out["vga_modes"] = pv.modes(pup, ml, seed=a.seed, out_dir=a.dir)
            elif a.cmd == "vga-vbe":
                v = out["vga_vbe"] = pv.vbe(pup, seed=a.seed, out_dir=a.dir, max_modes=a.max)
                print(f"PUPPET VGA vbe {v['vbe']}")
                for s_ in v["skipped"]:
                    print(f"PUPPET VGA mode={s_['m']} SKIP {s_['why']}")
                res = v["modes"]
            else:
                rr = out["vga_regseq"] = pv.regseq(pup, a.mode, out_dir=a.dir)
                print(f"PUPPET VGA regseq program {rr['program_lines']} lines, reg diffs {rr['reg_diffs'][:5]}")
                res = [rr["bios"], rr["regseq"]]
                if rr["problems"] and all(x["ok"] for x in res):
                    res.append({"mode": rr["mode"], "ok": False, "problems": rr["problems"]})
            for x in res:
                L = x.get("layout") or {}
                print("PUPPET VGA mode={mode} {st} {kind} {w}x{h} bpp={bpp} crc={crc} hz={hz} screen={scr} "
                      "guest={g}ms {pb}".format(mode=x["mode"], st="OK" if x["ok"] else "FAIL", kind=L.get("kind"),
                                                w=L.get("w"), h=L.get("h"), bpp=L.get("bpp"), crc=x.get("crc"),
                                                hz=x.get("hz"), g=x.get("guest_ms"),
                                                scr="{}/{} off {}x{}".format(x["screen"]["bad"], x["screen"]["samples"],
                                                                             x["shot"]["w"], x["shot"]["h"])
                                                if "screen" in x else "-",
                                                pb="; ".join(x["problems"])))
            bad = [x["mode"] for x in res if not x["ok"]]
            if bad:
                raise PuppetError(f"{len(bad)}/{len(res)} modes failed: {', '.join(bad)}")
            print(f"PUPPET VGA OK {len(res)} modes")
        out["stats"] = pup.stats
        out["verdict"] = "PASS"
        print("PUPPET VERDICT PASS", json.dumps(pup.stats))
        return 0
    except (PuppetError, RpcError) as e:
        out["verdict"] = "FAIL"
        out["error"] = str(e)
        out["console"] = pup.last_console
        print(f"PUPPET VERDICT FAIL {e}")
        if a.shot:
            try:
                st = rpc.call("machine.status")
                print(f"machine.status: {st}")
                with open(a.shot + ".bin", "wb") as f:
                    f.write(pup.read(0, 0x400))
            except RpcError as e2:
                print(f"anchor dump failed: {e2}")
            wh = save_screenshot(rpc, a.shot)
            print(f"screenshot: {a.shot} {wh}" if wh else "screenshot: capture failed")
        return 1
    finally:
        if a.json:
            with open(a.json, "w") as f:
                json.dump(out, f, indent=1)
        rpc.close()


if __name__ == "__main__":
    sys.exit(main())
