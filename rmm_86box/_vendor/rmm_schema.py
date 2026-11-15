# SPDX-License-Identifier: MIT
"""The RMM method schema, as 86Box's src/rmm.c publishes it through rpc.discover.

rmm.c's tables (rmm_methods[], rmm_media_verbs[], rmm_props[], rmm_events[], rmm_errors[]) are
the source; this is their copy for the mock and for clients talking to an 86Box older than the
schema. tests/test_schema_drift.py fails when the two differ, and
`python3 tests/rmm_c_schema.py` prints the literals below from rmm.c.

METHODS  name: (params, returns, errors, doc)
           params   space-separated name:type; '?' optional; '=a|b|c' the accepted values
           returns  the result's top-level fields
           errors   app error codes it can raise; 1 (not paused) means it needs a paused machine
         properties are listed as <name>.get / <name>.set, media verbs as <dev>.N.<verb>
EVENTS   name: (fields, doc), fields in the params syntax
ERRORS   app error code: name (JSON-RPC's own -32xxx codes are not listed)
KEYS     key name (input.key.send, input.events): set 1 scancode, 0x1xx = E0 prefix
Types: int, bool, string, hex (a string of hex byte pairs), object, array.
"""

METHODS = {
    'rpc.hello': ('protocol:int? client:string?', 'server rmm_version protocol min_protocol negotiated capabilities limits', '11', 'version handshake (optional)'),
    'rpc.discover': ('', 'methods events errors', '', 'this schema: methods, events and error codes'),
    'machine.status': ('', 'state sticky_pause tsc cs eip', '', 'run state and time-stamp counter'),
    'machine.pause': ('', 'state', '', 'pause the guest (waits for the CPU thread)'),
    'machine.resume': ('', 'state', '', 'resume the guest'),
    'rmm.sticky_pause.get': ('', 'value', '', 'does a client pause outlive its connection?'),
    'rmm.sticky_pause.set': ('value:bool', 'value', '', 'make a client pause outlive its connection'),
    'cpu.regs.get': ('', 'eax ecx edx ebx esp ebp esi edi eip eflags cs ss ds es fs gs cs_base cr0 cr2 cr3 cr4', '1', 'registers'),
    'cpu.regs.set': ('eax:int? ecx:int? edx:int? ebx:int? esp:int? ebp:int? esi:int? edi:int? eip:int? eflags:int?', 'eax ecx edx ebx esp ebp esi edi eip eflags', '1', 'set general registers, all or nothing'),
    'mem.phys.read': ('addr:int len:int', 'addr len data', '1', 'read physical memory as hex (len up to limits.mem_max)'),
    'mem.lin.read': ('addr:int len:int', 'addr len data', '1 9', 'read linear memory through the page tables'),
    'mem.phys.write': ('addr:int data:hex raw:bool?', 'addr len', '1 5', 'write physical RAM from hex bytes, read back to verify'),
    'input.key.send': ('combo:string', '', '', 'press a key combo, e.g. ctrl+alt+del, enter, f1'),
    'input.text.type': ('text:string', '', '', 'type text'),
    'input.events': ('events:array', 'queued pending', '3 4', 'ordered input, all or nothing: {type:key, scan|key, down}, {type:rel, dx, dy, dz?} (dz in wheel notches, + = away from the user), {type:abs, x, y} (0..1 across the screen; a tablet in absolute mode), {type:btn, buttons} (bit 0 left, 1 right, 2 middle, 3-4 extra); a key waits 2 ms of guest time after the last key, a button change 30 ms after the last change, and whatever follows waits with it; error 4 when the queue (256) has no room'),
    'input.mouse.move': ('dx:int dy:int buttons:int?', 'queued pending', '3 4', 'relative motion, after the button mask (bit 0 left, 1 right, 2 middle) if given; it stays held'),
    'input.release_all': ('', 'keys buttons pending', '', 'drop queued input and release every key and button this client holds (done on disconnect too)'),
    'input.devices': ('', 'keyboard mouse joystick tablet pointer pending', '', 'the input devices the emulator instantiated; pointer: absolute (send abs) or relative (send rel: grab)'),
    'machine.reset': ('hard:bool?', 'state config_applied config_loaded', '', 'hard reset; applies staged config'),
    'machine.cad': ('', '', '', 'send Ctrl+Alt+Del'),
    'machine.cae': ('', '', '', 'send Ctrl+Alt+Esc'),
    'machine.nmi': ('', '', '', 'raise an NMI'),
    'machine.acpi_shutdown': ('', '', '4', 'press the soft power button'),
    'machine.quit': ('', '', '', 'power 86Box off, no confirmation (machine.exit, then the socket closes)'),
    'media.list': ('', 'slots', '', 'removable media slots and their images'),
    'mem.info': ('', 'ram_kb', '', 'RAM size'),
    'io.read': ('port:int size:int?=1|2|4', 'port size value', '1', 'read an I/O port (the device sees a real read)'),
    'io.write': ('port:int value:int size:int?=1|2|4', 'port size value', '1', 'write an I/O port'),
    'config.get': ('section:string key:string?', 'value pending values', '', 'read a cfg key or a section'),
    'config.set': ('section:string key:string value:int', 'applies pending', '', 'set a cfg key (now or at the next hard reset)'),
    'config.describe': ('section:string key:string', 'type applies min max step default value values labels multi', '', 'what a cfg key takes'),
    'config.enums': ('section:string?', 'keys', '', 'every hardware-choice key: value, values, labels for this machine'),
    'config.pending': ('', 'pending load', '', 'what the next hard reset will apply'),
    'config.discard': ('', 'discarded', '', 'drop everything staged'),
    'config.dump': ('section:string?', 'cfg path', '', 'the whole VM cfg'),
    'config.load': ('cfg:object replace:bool? validate_only:bool?', 'staged applies keys unchecked', '10', 'stage a whole cfg for the next hard reset'),
    'capture.screenshot': ('step:int? wait_ms:int?', 'w h src_w src_h step format seq fresh data', '4', 'the guest screen, RGB24 base64; never pauses the guest'),
    'debug.break.set': ('addr:int pre:string? post:string? ret:string?=near|far|near32 on_violation:string?=stop|count', 'id', '4', "execute breakpoint at a linear address; pre/post are function contracts, e.g. pre 'arg0 > 0 && arg1 <= 100', post 'ax >= 0' (stop on broken clause, or count it)"),
    'debug.break.clear': ('id:int', '', '', 'clear a breakpoint'),
    'debug.break.list': ('', 'breakpoints', '', 'breakpoints'),
    'debug.step': ('count:int?', '', '1 4', 'single-step; emits machine.stopped'),
    'debug.watch.set': ('addr:int len:int kind:string=r|w|rw from:int? to:int? action:string?=stop|arm|disarm|rewrite value:int? base_off:int? base_size:int? far:int? child_len:int? len_off:int? len_size:int? child_kind:string?=r|w|rw', 'id', '4', 'watchpoint r|w|rw on a linear range; w of 1/2/4 bytes may need from/to (value before/after); action arm|disarm makes it an in-emulator rule that arms/removes a child watch on a (base,len) read from guest memory next to the watched address; rewrite (kind w, len 1/2/4) replaces the written value with value'),
    'debug.watch.clear': ('id:int', '', '', 'clear a watchpoint'),
    'debug.watch.list': ('', 'watchpoints', '', 'watchpoints'),
    'snapshot.save': ('tag:string', '', '1 8', 'save the machine in memory'),
    'snapshot.restore': ('tag:string', '', '7 8', 'roll the machine back; pauses first and leaves it paused'),
    'snapshot.list': ('', 'snapshots', '', 'snapshots, oldest first'),
    'snapshot.delete': ('tag:string', '', '7', 'drop a snapshot'),
    'snapshot.flush': ('timeout_ms:int?', 'pending errors', '', 'wait for the writer to put every snapshot on disk'),
    'snapshot.export': ('tag:string path:string', '', '1 7 8', 'write a snapshot to one file on the 86Box host'),
    'snapshot.load': ('path:string tag:string? restore:bool?', 'tag', '8', 'read a snapshot file into a slot and restore it (pauses first)'),
    'snapshot.ptrscan': ('', 'hits total', '1 8', 'development aid: pointer words in the snapshot dumps'),
    'snapshot.reloc': ('force:bool', '', '', 'test hook: restore through the pointer table'),
    'snapshot.writer': ('dir:string budget_mb:int? keep:bool? hold:bool?', '', '8', 'start the async writer: snapshots move from memory to dir'),
    'fdd.N.insert': ('path:string wp:bool?', '', '3', 'insert an image (-32602 when the file cannot be opened)'),
    'cdrom.N.insert': ('path:string wp:bool?', '', '3', 'insert an image (-32602 when the file cannot be opened)'),
    'fdd.N.eject': ('', '', '2 3', 'eject the image'),
    'cdrom.N.eject': ('', '', '2 3', 'eject the image'),
    'machine.fast_forward.get': ('', 'value', '', 'fast-forward on/off'),
    'machine.fast_forward.set': ('value:bool', 'value', '4', 'fast-forward on/off'),
    'cpu.force_interpreter.get': ('', 'value', '', 'force the interpreter (no dynarec) on/off'),
    'cpu.force_interpreter.set': ('value:bool', 'value', '4', 'force the interpreter (no dynarec) on/off'),
    'sound.mute.get': ('', 'value', '', 'mute on/off'),
    'sound.mute.set': ('value:bool', 'value', '4', 'mute on/off'),
}

EVENTS = {
    'machine.stopped': ('reason:string=pause|breakpoint|watchpoint|step addr:int? id:int? kind:string?=r|w eip:int? old:int? new:int? rule:int? src:int? violation:string?=pre|post clause:int? lhs:int? rhs:int?', 'the guest stopped: a pause, a breakpoint, a watchpoint or the end of a step'),
    'machine.resumed': ('', 'the guest is running again'),
    'machine.reset': ('hard:bool', 'a hard reset finished'),
    'machine.exit': ('', '86Box is powering off; the socket closes next'),
    'media.changed': ('dev:string slot:int path:string', 'an image was inserted or ejected (path "")'),
}

ERRORS = {
    1: 'not paused',
    2: 'slot empty',
    3: 'no such slot, drive or device',
    4: 'busy, absent or disabled',
    5: 'write dropped',
    7: 'no such snapshot',
    8: 'cannot snapshot',
    9: 'linear address not mapped',
    10: 'config refused',
    11: 'client protocol too old',
}

KEYS = {
    'esc': 0x001,
    '1': 0x002,
    '2': 0x003,
    '3': 0x004,
    '4': 0x005,
    '5': 0x006,
    '6': 0x007,
    '7': 0x008,
    '8': 0x009,
    '9': 0x00A,
    '0': 0x00B,
    '-': 0x00C,
    '=': 0x00D,
    'backspace': 0x00E,
    'tab': 0x00F,
    'q': 0x010,
    'w': 0x011,
    'e': 0x012,
    'r': 0x013,
    't': 0x014,
    'y': 0x015,
    'u': 0x016,
    'i': 0x017,
    'o': 0x018,
    'p': 0x019,
    '[': 0x01A,
    ']': 0x01B,
    'enter': 0x01C,
    'ctrl': 0x01D,
    'a': 0x01E,
    's': 0x01F,
    'd': 0x020,
    'f': 0x021,
    'g': 0x022,
    'h': 0x023,
    'j': 0x024,
    'k': 0x025,
    'l': 0x026,
    ';': 0x027,
    "'": 0x028,
    '`': 0x029,
    'shift': 0x02A,
    '\\': 0x02B,
    'z': 0x02C,
    'x': 0x02D,
    'c': 0x02E,
    'v': 0x02F,
    'b': 0x030,
    'n': 0x031,
    'm': 0x032,
    ',': 0x033,
    '.': 0x034,
    '/': 0x035,
    'rshift': 0x036,
    'kp_multiply': 0x037,
    'alt': 0x038,
    'space': 0x039,
    'capslock': 0x03A,
    'f1': 0x03B,
    'f2': 0x03C,
    'f3': 0x03D,
    'f4': 0x03E,
    'f5': 0x03F,
    'f6': 0x040,
    'f7': 0x041,
    'f8': 0x042,
    'f9': 0x043,
    'f10': 0x044,
    'numlock': 0x045,
    'scrolllock': 0x046,
    'f11': 0x057,
    'f12': 0x058,
    'kpenter': 0x11C,
    'rctrl': 0x11D,
    'kp_divide': 0x135,
    'ralt': 0x138,
    'home': 0x147,
    'up': 0x148,
    'pgup': 0x149,
    'left': 0x14B,
    'right': 0x14D,
    'end': 0x14F,
    'down': 0x150,
    'pgdn': 0x151,
    'ins': 0x152,
    'del': 0x153,
    'lwin': 0x15B,
    'rwin': 0x15C,
    'lctrl': 0x01D,
    'lshift': 0x02A,
    'lalt': 0x038,
}


def parse_spec(spec):
    """'kind:string=r|w size:int?=1|2|4' -> ({"kind": "string", "size": "int?"},
    {"kind": ["r", "w"], "size": [1, 2, 4]})"""
    params, enums = {}, {}
    for tok in (spec or "").split():
        name, _, typ = tok.partition(":")
        typ, eq, vals = typ.partition("=")
        params[name] = typ or "any"
        if eq:
            enums[name] = [int(v) if typ.startswith("int") else v for v in vals.split("|")]
    return params, enums


def discover_entry(name, row):
    """The rpc.discover entry rmm.c builds for one METHODS row."""
    params, returns, errors, doc = row
    ps, enums = parse_spec(params)
    e = {"name": name, "params": ps}
    if enums:
        e["enums"] = enums
    e["returns"] = returns.split()
    codes = [int(c) for c in errors.split()]
    if 1 in codes:
        e["paused"] = True
    if codes:
        e["errors"] = codes
    e["doc"] = doc
    return e


def discover_events():
    out = []
    for name, (fields, doc) in EVENTS.items():
        fs, enums = parse_spec(fields)
        e = {"name": name, "fields": fs}
        if enums:
            e["enums"] = enums
        e["doc"] = doc
        out.append(e)
    return out


def discover_errors():
    return [{"code": c, "name": n} for c, n in sorted(ERRORS.items())]
