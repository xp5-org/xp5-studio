# SPDX-License-Identifier: MIT
"""The RMM namespace: one table, the single source of truth for what the
monitor exposes. ui_coverage.py diffs it against the Qt UI so nothing a person
can click is left unaddressable.

Naming:  <object>[.<index>].<verb>      e.g.  cdrom.0.insert   fdd.1.eject
         objects are singular nouns; indexes are 0-based slot numbers; verbs are
         eject/insert/get/set/read/write/... Properties are get/set pairs.

status:  done     implemented in src/rmm.c today (under its legacy command name)
         planned  decided, not built
         gui      GUI-only (dialog, link, window geometry); no method, reachable
                  through config.* where it persists anything
"""

# method, status, legacy name (done only), covers: tokens ui_coverage.py checks.
#   covers entries: "action:<ui action name>", "media:<type>:<verb>",
#   "settings:<page>"
R = []


def add(method, status, covers=(), legacy=None, note=""):
    R.append(dict(method=method, status=status, covers=list(covers), legacy=legacy, note=note))


# -- machine -------------------------------------------------------------------
add("machine.pause", "done", ["action:Pause"], "pause", "waits for CPU thread ack")
add("machine.resume", "done", [], "continue")
add("machine.status", "done", [], "status", "state, tsc, cs, eip, sticky_pause")
add("rmm.sticky_pause.get", "done", [], note="a pause the client asked for outlives its connection ([General] rmm_sticky_pause, --monitor-sticky-pause); pause-on-connect pauses are always undone on disconnect")
add("rmm.sticky_pause.set", "done", [], note="{value:bool}: runtime switch; [General] rmm_sticky_pause is the saved default")
add("machine.reset", "done", ["action:Hard_Reset"], note="{hard=true}; no confirm dialog; waits for pc_reset_hard_init to finish; pause state kept; soft reset not implemented")
add("machine.cad", "done", ["action:Ctrl_Alt_Del"], note="pc_send_cad")
add("machine.cae", "done", ["action:Ctrl_Alt_Esc"], note="pc_send_cae")
add("machine.nmi", "done", ["action:Non_maskable_interrupt"], note="nmi_raise; spec proves delivery to a guest handler")
add("machine.acpi_shutdown", "done", ["action:ACPI_Shutdown"], note="ACPI power button or device power button; error 4 when neither (the menu would exit 86Box instead)")
add("machine.quit", "done", ["action:Exit"], note="plat_power_off, no confirmation; machine.exit event, socket closes")
add("machine.fast_forward", "done", ["action:Fast_forward"], note="get/set; triggers actionFast_forward")
add("cpu.force_interpreter", "done", ["action:Force_interpretation"], note="get/set; triggers actionForce_interpretation (error 4 when disabled: no dynarec)")
add("cpu.regs.get", "done", [], "regs")
add("cpu.regs.set", "done", [], note="paused only; eax..edi, eip, eflags (not segments/cr*, not eflags.VM); all-or-nothing; lazy flags rebuilt first")
add("trace.begin", "planned", ["action:Begin_trace"])
add("trace.end", "planned", ["action:End_trace"])

# -- memory --------------------------------------------------------------------
add("mem.phys.read", "done", [], "mem", "mem_readb_phys; device ranges have read side effects")
add("mem.phys.write", "done", [], "memw", "RAM; paused only; bytewise write, mem_invalidate_range, readback verify (ERR 5 if dropped); `raw` skips invalidation (diagnostic)")
add("mem.lin.read", "done", [], note="paused only; mmutranslate_noabrt per 4K page at CPL 0, tables read only; error 9 {first_bad} if not present")
add("io.read", "done", [], note="{port, size?=1|2|4} -> {port, size, value}; paused only; inb/inw/inl, so a read has the device's side effects (a UART RBR read pops its FIFO)")
add("io.write", "done", [], note="{port, value, size?=1|2|4} -> {port, size, value}; paused only; outb/outw/outl (a word to an 8-bit device is two byte writes, port then port+1)")
add("mem.info", "done", [], note="{ram_kb} today (mem_size after the last reset); mapping/shadow layout still to come")

# -- input ---------------------------------------------------------------------
add("input.key.send", "done", [], "sendkey")
add("input.text.type", "done", [], "type")
add("input.mouse.mode", "planned",
    ["action:Mouse", "action:Tablet", "action:Tablet_Crosshair", "action:Pen", "action:Cursor_Puck"],
    note="get/set: mouse|tablet|tablet_crosshair|pen|puck")
add("input.events", "done", [], note="{events:[...]} one ordered queue, all or nothing: {type:key, scan|key, down} (scan: set 1, 0x1xx = E0; key: an input.key.send name), {type:rel, dx, dy, dz?} (dz in wheel notches), {type:abs, x, y} (0..1; a tablet in absolute mode: error 3 without one, 4 in relative mode), {type:btn, buttons} (mask as input.mouse.move) -> {queued, pending}. Motion goes at once; a key waits 2 ms of guest time after the last key and a button change 30 ms after the last change, and what follows waits behind it, so paused input waits for a resume. Error 4 when the 256-event queue has no room; rel/btn error 3 with no mouse and no tablet")
add("input.release_all", "done", [], note="-> {keys:[scan...], buttons, pending}: drops queued input, then queues a release for every key and button this client holds; 86Box does the same, at once, when the client disconnects")
add("input.mouse.move", "done", [], note="{dx, dy, buttons?} relative motion, after the button mask (bit 0 left, 1 right, 2 middle, 3-4 extra) when given, held from then on (omitted: unchanged); through input.events' queue, -> {queued, pending}; the attached mouse reports it as the guest would see a real one's (a serial mouse: its packet on the UART); error 3 when mouse_type is none; -32602 for a non-integer or out of range (-32768..32767) delta. Specified by rmm_spec.py's `input` group")
add("input.devices", "done", [], note="-> {keyboard:{type}, mouse:{type, port?, buttons}|null, joystick:{type, axes, buttons, gameport}|null, tablet:{type}|null, pointer:absolute|relative, pending}; what the emulator instantiated, not what the cfg names (port: serial port index of a serial mouse, 0 = COM1; gameport: the I/O address of the port that answers, null when none; pointer: absolute = send abs, relative = send rel, i.e. grab the host pointer). Specified by rmm_spec.py's `input` group")
add("input.joystick.set", "planned", [], note="{axes?:[int...], buttons?:[bool...]}: the host-side state of the first joystick, as 86Box keeps it (axis -32768..32767, centre 0); a list shorter than the joystick's axes/buttons leaves the rest as they were; error 3 when no joystick is configured; -32602 for a list longer than the joystick has or an axis out of range. Guest-visible at the game port (button N pressed = bit 4+N of the port reads 0; a write to the port starts the axis timers, one bit per present axis)")
add("input.keyboard.require_capture", "planned", ["action:Keyboard_requires_capture"], note="get/set")
add("input.keyboard.right_ctrl_is_alt", "planned", ["action:Right_CTRL_is_left_ALT"], note="get/set")
add("input.mouse.sample_every_cpu_frame", "planned", ["action:Update_mouse_every_CPU_frame"], note="get/set")

# -- display / capture / sound -------------------------------------------------
_scales = ["action:0_5x", "action:1x", "action:1_5x", "action:2x", "action:3x", "action:4x",
           "action:5x", "action:6x", "action:7x", "action:8x",
           "action:_0_5x_2", "action:_1x", "action:_1x_2", "action:1_5x_2", "action:_2x_2",
           "action:_3x_2", "action:_4x_2", "action:_5x_2", "action:_6x_2", "action:_7x_2", "action:_8x_2"]
add("display.scale", "planned", _scales, note="get/set: 0.5..8; the _2 actions are the second menu's copy of the same setting")
add("display.filter", "planned", ["action:Nearest", "action:Linear"], note="get/set")
add("display.fullscreen", "planned", ["action:Fullscreen"], note="get/set")
add("display.fullscreen.stretch", "planned",
    ["action:FullScreen_stretch", "action:FullScreen_43", "action:FullScreen_keepRatio", "action:FullScreen_int",
     "action:FullScreen_int43", "action:_Full_screen_stretch_gl", "action:_4_3_gl",
     "action:_Square_pixels_keep_ratio_gl", "action:_Integer_scale_gl", "action:4_3_Integer_scale_gl"],
    note="get/set: stretch|4:3|keep_ratio|int|int43 (software and GL menus)")
add("display.fullscreen.stretch_when_maximized", "planned", ["action:Apply_fullscreen_stretch_mode_when_maximized"], note="get/set")
add("display.renderer", "planned", ["action:Software_Renderer", "action:OpenGL", "action:Vulkan", "action:VNC"], note="get/set")
add("display.renderer.options", "planned", ["action:Renderer_options"], note="renderer-specific; via config.*")
add("display.aspect.force_4_3", "planned", ["action:Force_4_3_display_ratio"], note="get/set")
add("display.aspect.force_machine", "planned", ["action:Force_device_aspect_ratio"], note="get/set")
add("display.window.resizable", "planned", ["action:Resizable_window"], note="get/set")
add("display.window.hidpi", "planned", ["action:HiDPI_scaling"], note="get/set")
add("display.window.hide_statusbar", "planned", ["action:Hide_status_bar"], note="get/set")
add("display.window.hide_toolbar", "planned", ["action:Hide_tool_bar"], note="get/set")
add("display.window.remember_geometry", "planned", ["action:Remember_size_and_position"], note="get/set")
add("display.window.size", "planned", ["action:Specify_dimensions"], note="get/set {w,h}")
add("display.monitor.show_non_primary", "planned", ["action:Show_non_primary_monitors"], note="get/set")
add("display.osd", "planned", ["action:Toggle_OSD"], note="get/set")
add("display.statusbar.update_icons", "planned", ["action:Update_status_bar_icons"], note="get/set")
add("display.cga_composite", "planned", ["action:CGA_composite_settings"], note="via config.*")
add("capture.screenshot", "done", ["action:Take_screenshot", "action:Take_raw_screenshot"],
    note="{step?=1..8, wait_ms?=200} -> {w, h, src_w, src_h, step, format: rgb24, seq, fresh, data: base64}; monitor 0's "
         "frame copied on the blitter thread (video_rmm_capture), so the guest is never paused; running or paused; "
         "fresh=false when no frame was drawn within wait_ms (paused) and the last one is returned")
add("capture.clipboard", "planned", ["action:Copy_screenshot", "action:Copy_raw_screenshot"], note="needs a desktop clipboard; low value headless")
add("sound.mute", "done", ["action:Mute_Unmute"], note="get/set; triggers actionMute_Unmute")
add("sound.gain", "planned", ["action:Sound_gain"], note="get/set")

# -- app -----------------------------------------------------------------------
add("app.discord", "planned", ["action:Enable_Discord_integration"], note="get/set")
add("config.get", "done", ["settings:*"], note="{section, key?}; schema keys live (+ staged `pending`), other keys read-only raw strings; schema today: General/sound_gain, Machine/mem_size")
add("config.set", "done", ["settings:*"], note="applies: now -> set + saved; applies: reset -> staged in pending_reset_apply until the next hard reset (any reset, menu included)")
add("config.describe", "done", [], note="schema per key: type, min/max/step, default, applies: now|reset; hardware-name keys (Machine machine/cpu_family, Video gfxcard, Sound sndcard, Network net_NN_card) -> {type: enum, applies: reset, value, values}")
add("config.enums", "done", [], note="{section?} -> {keys:[{section, key, type: enum, applies: reset, value, values, labels}]}; every "
                                      "hardware-choice key of the Settings dialog (machine, CPU family/speed/FPU/wait states, video 1-2, sound "
                                      "1-4, MIDI out/in, FM driver, network 1-4, fdc, cdrom_interface, hdc_1-4, scsicard_1-4, isamem/mcamem 0-3, "
                                      "isarom 0-3, isartc, lpt 1-4, serial 1-7, keyboard, mouse, tablet, joystick), values filtered for the "
                                      "running machine as the dialog filters them, labels its display names, value the running one (the key's "
                                      "default when the cfg omits it); cpu_speed adds multi[]; config.describe answers each key alike and "
                                      "config.load checks them (error 10). Specified by rmm_spec.py's config group")
add("config.pending", "done", [], note="the pending_reset_apply queue: reset-only keys staged by config.set, applied by the next hard reset")
add("config.discard", "done", [], note="empty the pending_reset_apply queue -> {discarded: n}")
add("config.dump", "done", [], note="{section?} -> {cfg: {section: {key: str}}, path}; the whole VM cfg, running settings saved first")
add("config.load", "done", [], note="{cfg: {section: {key: value|null}}, replace?=true, validate_only?} -> {staged, applies: reset, keys, unchecked}; "
                                   "staged for the next hard reset, which rewrites the cfg file and re-reads it (config_rmm_reload); listed sections replace "
                                   "the running ones ([General] always merged; rmm_*/uuid refused); machine, cpu_family (fits the machine), gfxcard, sndcard, "
                                   "net_NN_card, mem_size and image paths checked first, error 10 {errors:[{section,key,value,why}]}; config.pending shows it "
                                   "as `load`, config.discard drops it, machine.reset reports config_loaded")
add("rpc.hello", "done", [], note="{protocol:int?, client:string?} -> {server, rmm_version, protocol, min_protocol, negotiated, capabilities, limits}; error 11 when the client protocol is older than min_protocol; optional, 86Box before protocol 1 answers -32601")
add("rpc.discover", "done", [], note="-> {methods:[{name, params, enums?, returns, paused?, errors?, doc, indices?, verbs?}], events:[{name, fields, enums?, doc}], errors:[{code, name}]}; the schema, from rmm.c's tables (rmm_schema.py is the Python copy); JSON-RPC framing, dialect by first byte")
for a in ("Settings", "Preferences", "MCA_devices"):
    add("(gui) " + a.lower(), "gui", ["action:" + a], note="dialog; every field is a config.* key")
for a in ("About_Qt", "About_86Box", "Documentation", "Open_printer_tray", "Open_screenshots_folder"):
    add("(gui) " + a.lower(), "gui", ["action:" + a], note="opens a window/folder on the desktop")

# -- debug: breakpoints, stepping, watchpoints (rmm_design.md section C) ---------
# addr is a linear address (cs_base + eip for code), so real mode and flat code agree.
add("debug.break.set", "done", note="{addr} linear -> {id}; works while running; arming swaps in exec386_rmm (386+ CPUs, else error 4); optional contract {pre, post: 'lhs op rhs && ...' over regs, argN (word at entry sp+2+2N), dargN, constants; ret near|far|near32; on_violation stop|count}: pre is checked on entry, post when the call returns (cdecl, sp-matched); a broken clause stops with {violation, clause, lhs, rhs}")
add("debug.break.clear", "done", note="{id}; last one cleared swaps the stock executor back (zero cost)")
add("debug.break.list", "done", note="-> {breakpoints:[{id,addr, +calls,violations,stop,last* for contracts}]}")
add("debug.step", "done", note="{count=1}; paused only; no IRQs between stepped instructions; emits machine.stopped {reason:step, addr}")
add("debug.watch.set", "done", note="{addr linear, len 1..4096, kind r|w|rw} -> {id}; writes via page_lookup override, reads via read_mapping override; trap semantics (stops after the access); RAM and device pages")
add("debug.watch.clear", "done", note="{id}")
add("debug.watch.list", "done", note="-> {watchpoints:[{id,addr,len,kind}]}")

# -- snapshot (phase 2, rmm_design.md "Phase 2" and D.8) ---------------------------------
add("snapshot.save", "done", [], "savevm", note="{tag}; paused only; replaces an existing tag; in-process (lost on exit, max 32); error 8 names any device without a visitor, an ATAPI ZIP/MO drive, a non-ATAPI or host-drive CD-ROM, or a non-raw floppy image; IDE hard disks covered by the image overlay (hdd_image.c), ATAPI CD-ROM drives by their own state (cdrom_rmm_snap)")
add("snapshot.restore", "done", [], "loadvm", note="{tag}; pauses first if running, leaves it paused; error 7 no such tag; error 8 after a reset, a floppy media change, a CD-ROM disc inserted or ejected (even the same image again) or a hard disk image closed; writable floppy images rolled back through their file bytes, hard disk images through the block overlay (only differing 4K blocks written); breakpoints/watches stay armed")
add("snapshot.list", "done", [], "snapshots", note="-> {snapshots:[{tag,tsc,state,bytes}]}; state is memory, queued, writing, disk or error (async writer)")
add("snapshot.delete", "done", [], "delvm", note="{tag}; error 7 if no such tag")
add("snapshot.writer", "done", [], note="{dir, budget_mb?, keep?, hold?}; starts the async writer (also RMM_SNAP_DIR): each snapshot's device/RAM buffer is copied to dir by a thread and dropped from memory; a restore reads it back; save over the budget (default 2 x RAM + 256 MB not yet on disk) is error 8 'writer busy', a lone snapshot is always allowed; hold is a test hook that parks the writer; error 8 on a directory it cannot create or a different dir than the one in use; files are removed at exit unless keep")
add("snapshot.flush", "done", [], note="{timeout_ms?=60000} -> {pending, errors}; waits for the writer to put every queued snapshot on disk; errors counts failed writes (those snapshots stay in memory, state 'error')")

add("snapshot.export", "done", [], note="{tag, path}; paused only; writes the snapshot as one file (rmm_snap.c 'files': identity, build, pointer table, hard disk blocks + base hash, device/RAM buffer) through path.part + rename; error 7 no such tag; error 8 when it has no pointer table (not Linux) or a hard disk image changed since; rmm_client/rmm_snapfile.py reads it")
add("snapshot.load", "done", [], note="{path, tag?, restore?=true} -> {tag}; pauses first; reads a snapshot file into slot tag (default: the file's) and restores it, in this or a fresh 86Box: error 8, before anything changes, unless the build (binary and mapped libraries, size+mtime), machine/CPU/RAM, device table, floppy and CD media and each hard disk's base image (the image before the guest first wrote to it; [General] rmm_disk_track = 1 makes that the image as opened) are the same; the slot stays restorable, always through the pointer table; the file is never removed")
add("snapshot.ptrscan", "done", [], note="paused only -> {hits:[{dev,region,len,off,code}], total}; development aid for the pointer-preserving restore: the 8-byte words of every struct a snapshot dumps whole that hold an address of this process (code: points into executable memory); Linux only (error 8 elsewhere); guest data (RAM, VRAM, media bytes: rmm_snap_data) is not scanned")

add("snapshot.reloc", "done", [], note="{force:bool}; test hook: snapshot.restore goes through the snapshot's pointer table (the address fix-up a fresh process needs) instead of copying the dumps back as they are; in the same process the result must be identical")

# -- removable media: <dev>.<n>.<verb> ----------------------------------------
# Built rows (rmm.c rpc_media_call, through MediaMenu on the UI thread). Error 2 slot
# empty, 3 no such drive; media.changed {dev, slot, path} on every change.
_MEDIA_DONE = {
    "fdd.N.insert": ("done", "insert {path, wp}; MediaMenu::floppyMount"),
    "fdd.N.eject": ("done", "MediaMenu::floppyEject"),
    "cdrom.N.insert": ("done", "insert {path}; MediaMenu::cdromMount. Spec: the no-drive error (core) and disc swaps on the snapcd VM's ATAPI drive"),
    "cdrom.N.eject": ("done", "MediaMenu::cdromEject; spec: snapcd.cd_media_change_refused"),
}
# What the planned verbs mean (specified by rmm_spec.py's `removable` group, passing on rmm_mock.py):
_MEDIA_VERB_NOTES = {
    "insert": "insert {path, wp?}; media.changed {dev, slot, path}; -32602 when the file is missing",
    "eject": "media.changed with path \"\"; error 2 when the slot is already empty",
    "new": "new {path, size?}: create a blank image (-32602 if path exists) and insert it",
    "reload": "re-insert the image that was last in the (empty) slot; error 2 when the slot is not empty or never held one",
    "record": "cassette only: set the slot's mode (media.list shows mode idle|play|record|rewind|ffwd); error 2 when empty",
    "play": "cassette only: as record",
    "rewind": "cassette only: as record",
    "ffwd": "cassette only: as record",
}
# slot counts from the source headers (FDD_NUM etc.); ui_coverage.py reads them.
for dev, verbs in {
    "fdd":       ["insert", "eject", "new", "export_86f", "reload"],
    "cdrom":     ["insert", "eject", "reload", "mute"],
    "rdisk":     ["insert", "eject", "new", "reload"],
    "mo":        ["insert", "eject", "new", "reload"],
    "cartridge": ["insert", "eject", "reload"],
    "cassette":  ["insert", "eject", "new", "reload", "record", "play", "rewind", "ffwd"],
    "tape":      ["insert", "eject", "new", "reload"],
}.items():
    ui_type = {"fdd": "floppy", "rdisk": "rdisk"}.get(dev, dev)
    for v in verbs:
        uiverb = {"insert": "mount", "export_86f": "exportTo86f"}.get(v, v)
        st, note = _MEDIA_DONE.get(f"{dev}.N.{v}", ("planned", _MEDIA_VERB_NOTES.get(v, "")))
        add(f"{dev}.N.{v}", st, [f"media:{ui_type}:{uiverb}"], note=note)
add("media.list", "done", [], note="slots, types, current image, write-protect (what the status bar icons show); fdd + cdrom slots from MachineStatus::iterate*")

# -- storage controllers and disks (specified by rmm_spec.py's `hdc` group) ----------------
add("storage.list", "planned", [], note="-> {controllers:[{kind, name, bus, ports:[io base...], irq?}], disks:[{n, bus, channel, cylinders, heads, spt, image}]}; what 86Box instantiated for the hard disk side, not what the cfg names: kind hdc (the [Storage controllers] hdc_1 card) | scsi | ide, name the card's internal name (st506_at, esdi_at, xta_wdxt150, ...), bus mfm|esdi|xta|ide|scsi, ports the base I/O addresses it decodes; one disk per hdd_NN the cfg attaches, geometry as the controller reports it")
