# SPDX-License-Identifier: MIT
"""The guided demos (`demo timetravel | vga | step | vm | life`), each on one node."""

import collections
import random
import struct
import threading
import time

from rmm_86box.rpc import RpcError

NAMES = ("timetravel", "vga", "step", "vm", "life", "gamesnap")


def run(node, name, opts=()):
    if name == "life":
        return life(node, opts) or ["demo life done"]
    if name == "gamesnap":
        return gamesnap(node, opts) or ["demo gamesnap done"]
    fn = {"timetravel": timetravel, "vga": vga, "step": step, "vm": vm, "life": life}.get(name)
    if not fn:
        return ["demo " + " | ".join(NAMES)]
    fn(node)
    return [f"demo {name} done"]


def _type_line(r, text):
    r.call("input.text.type", text=text)
    r.call("input.key.send", combo="enter")


def timetravel(node):
    r = node.rmm
    node.say("timetravel: type a line, snapshot, type another, restore -> the screen rewinds", "accent")
    r.machine.resume()
    _type_line(r, "echo before the snapshot")
    time.sleep(1.5)
    with r.paused():
        r.snapshot.save(tag="timetravel")
    node.say("  saved snapshot 'timetravel'; now typing more...", "muted")
    for word in ("echo", " this", " line", " will", " be", " undone"):
        r.input.text.type(text=word)
        time.sleep(0.4)
    r.input.key.send(combo="enter")
    time.sleep(2.0)
    node.say("  restoring 'timetravel' (the guest is left paused; resuming)", "muted")
    r.snapshot.restore(tag="timetravel")
    r.machine.resume()


def vga(node):
    msg = "  HELLO FROM THE 86BOX REMOTE MONITOR  "
    row, col, attr = 12, (80 - len(msg)) // 2, 0x1F          # white on blue
    data = b"".join(bytes((ord(ch), attr)) for ch in msg)
    node.say(f"vga: writing {len(msg)} characters straight into text memory at B8000 row {row}", "accent")
    node.rmm.write_mem(0xB8000 + (row * 80 + col) * 2, data)


def step(node):
    r = node.rmm
    node.say("step: pause, then single-step five instructions; registers update on each stop", "accent")
    r.machine.pause()
    r.rpc.drain_events()
    for _ in range(5):
        r.debug.step(count=1)
        ev = r.rpc.wait_event("machine.stopped", 2.0)
        regs = r.cpu.regs.get()
        node.say(f"  {regs['cs']:04X}:{regs['eip']:08X}  eax={regs['eax']:08X}  "
                 f"stopped: {ev.get('reason') if ev else '?'}", "text")
        node.regs = regs
    node.say("  paused; F5 resumes", "muted")


def vm(node):
    v = node.rmm.vm
    dom = v.show()
    v.save("demo_base")
    bigger = dict(dom, memory_kib=dom.get("memory_kib", 4096) * 2 if dom.get("memory_kib", 0) <= 8192 else 4096)
    v.store.save("demo_mem", bigger)
    node.say("vm: saved the running VM as 'demo_base', and a copy with "
             f"{bigger['memory_kib']} KiB as 'demo_mem'", "accent")
    for s, k, a, b in v.diff("demo_mem"):
        node.say(f"  [{s}] {k}: {a} -> {b}", "text")
    node.say("  vm load demo_mem: config.load, then a hard reset; watch the BIOS count the RAM", "muted")
    r = v.load("demo_mem")
    node.say(f"  staged {r['load']['keys']} keys, reset applied {r['reset'].get('config_loaded')}", "text")
    node.refresh_vm()
    node.say("  'vm load demo_base' puts it back", "muted")


# -- life: Conway's Game of Life played in the guest's text page ---------------------------

TEXT_BASE = 0xB8000            # physical; 80x25 cells of (char, attribute)
COLS, ROWS = 80, 25
ROW_BYTES = COLS * 2
ALIVE_CH, DEAD_CH = 0xDB, 0x20           # full block / space
DEAD_ATTR, BORN_ATTR = 0x07, 0x09        # a survivor's attribute climbs 0x0A..0x0F with age
DENSITY = 0.30
STALE_GENS = 8                           # a board repeating one of its last 8 is dead (still or short loop)
MAX_GENS = 1500                          # reseed anyway, for variety
WINDOW = 5000                            # samples per op kept for the summary lines

LIFE_USAGE = ("demo life [rows=N] [run_ms=MS] [gens=N] [report=N] [log=FILE] [tsc=0|1] [events=0|1] [wait=S]\n"
              "  rows    rows read+written per pause before the guest is resumed (default 1; 25 = once a generation)\n"
              "  run_ms  how long the guest runs after each batch so 86Box repaints (default 30)\n"
              "  gens    stop after N generations and resume the guest (default 0 = endless)\n"
              "  report  print a timing summary every N generations (default 100)\n"
              "  log     CSV of every RPC and event: t_s,gen,row,op,ms,val   (default none)\n"
              "  tsc     ask machine.status after each pause: op 'run' = ms between resume reply and pause\n"
              "          reply, val = guest tsc ticks advanced in that window (default 0)\n"
              "  events  log machine.stopped / machine.resumed notifications with their arrival time (default 1)\n"
              "  wait    first let the guest run up to S seconds until its text page shows text: life pauses the\n"
              "          guest, and a VM just launched never reaches text mode while paused (default 0)")


def _life_opts(args):
    o = {"rows": 1, "run_ms": 30.0, "gens": 0, "report": 100, "log": "", "tsc": 0, "events": 1, "wait": 0}
    for a in args:
        k, eq, v = a.partition("=")
        if not eq or k not in o:
            raise ValueError(f"bad option {a!r}")
        o[k] = v if k == "log" else (float(v) if k == "run_ms" else int(v))
    if not 1 <= o["rows"] <= ROWS:
        raise ValueError(f"rows must be 1..{ROWS}")
    return o


class _Timing:
    """Per-op RPC latency (wall ms around r.call), a CSV log, and a windowed summary."""

    def __init__(self, r, path):
        self.r = r
        self.t0 = time.perf_counter()
        self.win = collections.defaultdict(lambda: collections.deque(maxlen=WINDOW))
        self.n = collections.Counter()
        self.f = open(path, "a", buffering=1, encoding="utf-8") if path else None
        if self.f:
            self.f.write("t_s,gen,row,op,ms,val\n")
        self.gen = self.row = 0

    def rec(self, op, ms, val=""):
        self.win[op].append(ms)
        self.n[op] += 1
        if self.f:
            self.f.write(f"{time.perf_counter() - self.t0:.6f},{self.gen},{self.row},{op},{ms:.3f},{val}\n")

    def call(self, op, method, **params):
        t = time.perf_counter()
        try:
            return self.r.call(method, **params)
        finally:
            self.rec(op, (time.perf_counter() - t) * 1000)

    def event(self, name):
        if self.f:
            self.f.write(f"{time.perf_counter() - self.t0:.6f},{self.gen},{self.row},ev:{name},0,\n")

    def summary(self):
        out = []
        for op in sorted(self.win):
            v = sorted(self.win[op])
            if op == "run":
                continue
            out.append(f"    {op:<8} n={self.n[op]:<7} mean {sum(v) / len(v):7.2f}  p50 {v[len(v) // 2]:7.2f}  "
                       f"p95 {v[int(len(v) * 0.95)]:7.2f}  max {v[-1]:7.2f} ms")
        return out

    def close(self):
        if self.f:
            self.f.close()


def _wait_for_dos(T, node, seconds):
    """Let the guest run until its text page has visible text (video BIOS in text mode, so B8000
    reads back); False if `seconds` pass first. The guest is paused when this returns True."""
    end = time.monotonic() + seconds
    while node.running and node.connected and time.monotonic() < end:
        try:
            T.call("pause", "machine.pause")
            text = b"".join(_read_row(T, y)[0] for y in range(ROWS))
            if sum(1 for c in text if 33 <= c < 127) >= 8:
                return True
            T.call("resume", "machine.resume")
        except RpcError:
            pass
        time.sleep(1)
    return False


def _read_row(T, y):
    raw = bytes.fromhex(T.call("read", "mem.phys.read", addr=TEXT_BASE + y * ROW_BYTES, len=ROW_BYTES)["data"])
    return raw[0::2], raw[1::2]


def _write_row(T, y, chars, attrs):
    data = bytearray(ROW_BYTES)
    data[0::2] = chars
    data[1::2] = attrs
    T.call("write", "mem.phys.write", addr=TEXT_BASE + y * ROW_BYTES, data=bytes(data).hex())


def _seed(T):
    for y in range(ROWS):
        chars = bytes(ALIVE_CH if random.random() < DENSITY else DEAD_CH for _ in range(COLS))
        _write_row(T, y, chars, bytes(BORN_ATTR if c == ALIVE_CH else DEAD_ATTR for c in chars))


def _next_row(above, here, below):
    """New (chars, attrs) of one row from the old rows around it (chars and attrs of each);
    the board wraps at every edge."""
    ac, hc, bc, ha = above, here[0], below, here[1]
    out_c, out_a = bytearray(COLS), bytearray(COLS)
    for x in range(COLS):
        l, rt = (x - 1) % COLS, (x + 1) % COLS
        n = sum(1 for row in (ac, bc) for i in (l, x, rt) if row[i] == ALIVE_CH)
        n += (hc[l] == ALIVE_CH) + (hc[rt] == ALIVE_CH)
        was = hc[x] == ALIVE_CH
        if n == 3 or (was and n == 2):
            out_c[x] = ALIVE_CH
            out_a[x] = min(max(ha[x], 0x09) + 1, 0x0F) if was else BORN_ATTR
        else:
            out_c[x], out_a[x] = DEAD_CH, DEAD_ATTR
    return bytes(out_c), bytes(out_a)


def life(node, args=()):
    """The board lives in B8000. Every generation reads each row back with mem.phys.read and
    writes it again with mem.phys.write (rows above and below come from a three-row window, so a
    row is read once and written once); memory access needs the guest paused, but 86Box repaints
    only while it runs, so after every `rows` rows the guest is resumed for run_ms and paused
    again. Python keeps no copy of the board between generations. Endless unless gens=N; a dead
    or stuck board is reseeded. Ends when the node does (window closed, VM gone).

    Timing (see LIFE_USAGE): every pause, resume, read and write is timed around its RPC;
    log=FILE gets one CSV line per call plus the machine.stopped/resumed notifications as they
    arrive, and tsc=1 adds the guest's own clock across each run window."""
    try:
        o = _life_opts(args)
    except ValueError as e:
        return [str(e), *LIFE_USAGE.split("\n")]
    r = node.rmm
    T = _Timing(r, o["log"])
    done = []                                   # truthy once this run is over (the callback stays registered)
    if o["events"]:
        r.on_event(lambda name, params: None if done or name not in ("machine.stopped", "machine.resumed")
                   else T.event(name))
    node.say(f"life: B8000, rows={o['rows']} run_ms={o['run_ms']:g} gens={o['gens'] or 'endless'}"
             f"{' log=' + o['log'] if o['log'] else ''}{' tsc' if o['tsc'] else ''}", "accent")

    if o["wait"]:
        node.say(f"life: letting the guest boot (up to {o['wait']}s) until its text page shows text", "muted")
        if not _wait_for_dos(T, node, o["wait"]):
            node.say("life: no text on the page; starting anyway", "warn")

    paused = False

    def pause():
        nonlocal paused
        try:
            T.call("pause", "machine.pause")    # the cached node status can be stale; pausing again is harmless
            paused = True
        except RpcError:
            pass

    gens = 0
    t0 = time.monotonic()
    seen = []
    need_seed = True
    last_tsc = None
    while node.running and node.connected and not (o["gens"] and gens >= o["gens"]):
        tg = time.perf_counter()
        try:
            T.gen, T.row = gens, 0
            if not paused:
                pause()
            if o["tsc"] and last_tsc is None:
                last_tsc = T.call("status", "machine.status")["tsc"]
            if need_seed:
                _seed(T)
                need_seed = False
            first = _read_row(T, 0)
            prev = _read_row(T, ROWS - 1)         # the wrap row above row 0, old generation
            here = first
            alive = 0
            sig = bytearray()
            for y in range(ROWS):
                T.row = y
                below = first if y == ROWS - 1 else _read_row(T, y + 1)
                chars, attrs = _next_row(prev[0], here, below[0])
                _write_row(T, y, chars, attrs)
                if (y + 1) % o["rows"] == 0:
                    T.call("resume", "machine.resume")      # the display only repaints while the guest runs
                    paused = False
                    t_res = time.perf_counter()
                    time.sleep(o["run_ms"] / 1000)
                    pause()
                    if o["tsc"] and paused:
                        tsc = T.call("status", "machine.status")["tsc"]
                        T.rec("run", (time.perf_counter() - t_res) * 1000, tsc - last_tsc)
                        last_tsc = tsc
                alive += chars.count(ALIVE_CH)
                sig += chars
                prev, here = here, below
            gens += 1
            T.rec("gen", (time.perf_counter() - tg) * 1000, alive)
            sig = bytes(sig)
            if alive == 0 or sig in seen or gens % MAX_GENS == 0:
                node.say(f"  gen {gens}: {'empty' if alive == 0 else 'stuck' if sig in seen else 'long run'}, reseeding", "muted")
                seen.clear()
                need_seed = True
                continue
            seen = (seen + [sig])[-STALE_GENS:]
            if o["report"] and gens % o["report"] == 0:
                node.say(f"  gen {gens}  alive {alive}  {gens / (time.monotonic() - t0):.2f} gen/s", "text")
                for line in T.summary():
                    node.say(line, "muted")
        except RpcError as e:
            paused = False                      # most likely "not paused": pause again before the next try
            node.say(f"  life: {e}; pausing and carrying on", "warn")
            time.sleep(0.5)
        except OSError:
            break
    if node.connected:
        try:
            T.call("resume", "machine.resume")
        except (RpcError, OSError):
            pass
    done.append(1)
    node.say(f"life: stopped after {gens} generations, {gens / max(time.monotonic() - t0, 1e-9):.2f} gen/s", "muted")
    for line in T.summary():
        node.say(line, "muted")
    T.close()


# -- gamesnap: the breakpoint / snapshot workflow against MINES.EXE (gamesnap_src/MINES.C) ----------

GS_TAG = b"GSNAPST1"
ICA = 0x4F0                                  # MINES publishes its state's offset (word) and segment (word) here
GS_USAGE = ("demo gamesnap [trigger=watch|kbd] [rounds=N] [drive=0|1] [key_ms=MS] [poke=0|1] [log=FILE] [snap=TAG]\n"
            "  trigger watch: armed through the Monitor page's Add form, as a person would: watch w on the\n"
            "          health byte, when 1 -> 0, on hit restore + resume TAG. The GUI does every revert;\n"
            "          the demo only presses keys (and pokes MOVES) and counts\n"
            "          kbd: breakpoint on the INT 9 handler, stops on every key interrupt; the demo reads\n"
            "          health and restores itself (a check the panel cannot express)\n"
            "  rounds  stop after N snapshot restores (default 0 = endless)\n"
            "  drive   send random w/a/s/d keys ourselves every key_ms (default 1; 0 = a person types)\n"
            "  poke    after each restore write the round number into the guest's MOVES word (default 0)\n"
            "  log     CSV like demo life: t_s,gen,row,op,ms,val  (gen = round)")


def _gs_opts(args):
    o = {"trigger": "watch", "rounds": 0, "drive": 1, "key_ms": 40.0, "poke": 0, "log": "", "snap": "snap1"}
    for a in args:
        k, eq, v = a.partition("=")
        if not eq or k not in o:
            raise ValueError(f"bad option {a!r}")
        o[k] = v if k in ("trigger", "log", "snap") else (float(v) if k == "key_ms" else int(v))
    if o["trigger"] not in ("watch", "kbd"):
        raise ValueError("trigger must be watch or kbd")
    return o


def _gs_find(T):
    """Physical address of MINES' state struct: the pointer in the comm area, else a scan for the tag."""
    off, seg = struct.unpack("<HH", bytes.fromhex(T.call("ptr", "mem.phys.read", addr=ICA, len=4)["data"]))
    cand = seg * 16 + off
    if cand and bytes.fromhex(T.call("ptr", "mem.phys.read", addr=cand, len=8)["data"]) == GS_TAG:
        return cand, "ica"
    step = 4096 - len(GS_TAG)
    for base in range(0x10000, 0xA0000, step):
        data = bytes.fromhex(T.call("scan", "mem.phys.read", addr=base, len=4096)["data"])
        i = data.find(GS_TAG)
        if i >= 0:
            return base + i, "scan"
    return None, ""


def _gs_panel(node, o, T, base, hp, mv):
    """trigger=watch, the app doing the work: fill the Monitor page's Add form (on screen) and press
    Add. The panel saves the snapshot, arms the watch in 86Box (from 1 to 0, so 86Box only stops on
    the death) and keeps the rule; on each hit the GUI restores and resumes. The demo only presses
    keys, optionally pokes the round number into MOVES, and reads the restore count off the rule.
    Clearing the row in the panel ends the demo."""
    r, tag = node.rmm, o["snap"]
    form = {"addr": hp, "kind": "watch w", "len": 1, "from": 1, "to": 0, "on_hit": "restore + resume",
            "arg": tag, "label": f"MINES health byte (state 0x{base:X} +8)"}
    node.say(f"gamesnap: Monitor page, Add form: watch w @0x{hp:X} len 1, when 1 -> 0, "
             f"on hit 'restore + resume' {tag}, label '{form['label']}'", "accent")
    known = set(node.hit_rules)
    node.want_bp_form = form
    key, deadline = None, time.monotonic() + 10
    while key is None and time.monotonic() < deadline and node.running:
        key = next((k for k, v in node.hit_rules.items()
                    if k not in known and v["by"] == "panel" and v["tag"] == tag), None)
        time.sleep(0.1)
    if key is None:
        node.want_bp_form = None
        node.say("gamesnap: the panel did not arm it (is the Monitor page up? see its message line)", "warn")
        return
    rule = node.hit_rules[key]
    node.say(f"  armed {key[0]} #{key[1]}: the row's On hit column is the revert; the GUI runs it on every stop", "muted")
    rounds = keys = 0
    t0 = t_last = time.monotonic()
    try:
        T.call("resume", "machine.resume")
        while node.running and node.connected and node.hit_rules.get(key) is rule \
                and not (o["rounds"] and rounds >= o["rounds"]):
            if o["drive"]:
                T.call("key", "input.key.send", combo=random.choice("wasd"))
                keys += 1
            time.sleep(o["key_ms"] / 1000 if o["drive"] else 0.2)
            if rule["restores"] > rounds:
                rounds = rule["restores"]
                now = time.monotonic()
                T.gen = rounds
                T.rec("round", (now - t_last) * 1000, rounds)
                t_last = now
                if o["poke"]:
                    T.call("write", "mem.phys.write", addr=mv, data=struct.pack("<H", rounds & 0xFFFF).hex())
                if rounds % 10 == 0:
                    node.say(f"  round {rounds}  {node.hits.get(key, 0)} hits  {keys} keys  "
                             f"{rounds / (now - t0):.2f} restores/s", "text")
    except RpcError as e:
        node.say(f"  gamesnap: {e}", "warn")
    except OSError:
        pass
    if node.hit_rules.get(key) is not rule:
        node.say("gamesnap: the watch was cleared in the panel; demo ends", "muted")
    elif node.connected:
        node.drop_hit_rule(*key)
        try:
            r.call("debug.watch.clear", id=key[1])
            r.call("machine.resume")
        except (RpcError, OSError):
            pass
    node.say(f"gamesnap: {rounds} restores by the panel, {keys} keys in {time.monotonic() - t0:.1f}s", "muted")


def gamesnap(node, args=()):
    """MINES.EXE is already running in the guest. Pause, find its health byte, save the snapshot,
    arm the trigger, then loop: resume, (send keys), wait for machine.stopped, read health; when it
    went 1 -> 0 restore the snapshot and go again (optionally poking the round number into MOVES).
    Every step is timed around its RPC (log=FILE for the CSV); `stop` is the wall time from our
    resume reply (or key send) to the stop event's arrival."""
    try:
        o = _gs_opts(args)
    except ValueError as e:
        return [str(e), *GS_USAGE.split("\n")]
    r = node.rmm
    T = _Timing(r, o["log"])
    hit = threading.Event()
    last = {}
    done = []

    def on_event(name, params):
        if done:
            return
        T.event(name)
        if name == "machine.stopped" and params.get("reason") in ("watchpoint", "breakpoint"):
            last.update(t=time.perf_counter(), **params)
            hit.set()

    r.on_event(on_event)
    r.call("machine.pause")
    base, how = _gs_find(T)
    if base is None:
        node.say("gamesnap: MINES state not found (tag GSNAPST1); is MINES.EXE running?", "warn")
        done.append(1)
        T.close()
        return
    hp, mv = base + 8, base + 10
    node.say(f"gamesnap: state at 0x{base:X} ({how}), health 0x{hp:X}, trigger {o['trigger']}", "accent")
    node.want_tab = 2                            # Monitor page: the breakpoint panel shows what gets armed
    for tag in (o["snap"],):
        try:
            r.call("snapshot.delete", tag=tag)
        except RpcError:
            pass
    if o["trigger"] == "watch":
        done.append(1)                           # the panel's rule handles the stops, not on_event
        try:
            _gs_panel(node, o, T, base, hp, mv)
        finally:
            T.close()
        return
    try:
        T.call("snap_save", "snapshot.save", tag=o["snap"])
        ivt = struct.unpack("<HH", bytes.fromhex(T.call("ptr", "mem.phys.read", addr=9 * 4, len=4)["data"]))
        entry = ivt[1] * 16 + ivt[0]
        bid = T.call("arm", "debug.break.set", addr=entry)["id"]
        clear = "debug.break.clear"
        node.say(f"  INT 9 handler at {ivt[1]:04X}:{ivt[0]:04X} (linear 0x{entry:X})", "muted")
        rkind = "break"
        then = f"restore {o['snap']}" + (", poke MOVES=round" if o["poke"] else "") + ", resume"
        label = f"INT 9 handler {ivt[1]:04X}:{ivt[0]:04X} (key press)"
        note = f"read health 0x{hp:X}; 0: {then}; else resume"
        node.set_hit_rule(rkind, bid, label=label, on_hit="restore", tag=o["snap"], resume=True, by="gamesnap", note=note)
        node.say(f"  armed {rkind} #{bid} ({label}): each hit pauses the guest; gamesnap then does: {note}", "muted")
    except (RpcError, OSError) as e:
        node.say(f"gamesnap: setup failed, nothing armed: {e}", "warn")
        done.append(1)
        T.close()
        return

    rounds = stops = keys = 0
    t0 = time.monotonic()
    ok = True
    try:
        while ok and node.running and node.connected and not (o["rounds"] and rounds >= o["rounds"]):
            T.gen, T.row = rounds, stops
            hit.clear()
            T.call("resume", "machine.resume")
            t_go = time.perf_counter()
            while not hit.wait(o["key_ms"] / 1000 if o["drive"] else 0.5):
                if not (node.running and node.connected):
                    ok = False
                    break
                if o["drive"]:
                    t_go = time.perf_counter()           # a stop during the key call counts from its start
                    T.call("key", "input.key.send", combo=random.choice("wasd"))
                    keys += 1
            if not ok:
                break
            T.rec("stop", (last["t"] - t_go) * 1000, last.get("eip", last.get("addr", "")))
            stops += 1
            health = bytes.fromhex(T.call("read", "mem.phys.read", addr=hp, len=1)["data"])[0]
            if health != 0:
                continue                         # a key interrupt / the game's own start: let it run on
            T.call("restore", "snapshot.restore", tag=o["snap"])      # leaves the machine paused
            node.note_restore(rkind, bid)
            rounds += 1
            if o["poke"]:
                T.call("write", "mem.phys.write", addr=mv, data=struct.pack("<H", rounds & 0xFFFF).hex())
            T.rec("round", (time.perf_counter() - t_go) * 1000, rounds)
            if rounds % 10 == 0:
                node.say(f"  round {rounds}  {stops} stops  {keys} keys  {rounds / (time.monotonic() - t0):.2f} restores/s", "text")
                for line in T.summary():
                    node.say(line, "muted")
    except RpcError as e:
        node.say(f"  gamesnap: {e}", "warn")
    except OSError:
        pass
    if node.connected:
        node.drop_hit_rule(rkind, bid)
        for fn, i in ((clear, bid), ("machine.resume", None)):
            try:
                r.call(fn, **({"id": i} if i is not None else {}))
            except (RpcError, OSError):
                pass
    done.append(1)
    node.say(f"gamesnap: {rounds} restores, {stops} stops, {keys} keys in {time.monotonic() - t0:.1f}s", "muted")
    for line in T.summary():
        node.say(line, "muted")
    T.close()
