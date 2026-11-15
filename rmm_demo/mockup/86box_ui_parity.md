# 86Box Qt UI → RMM console parity

Goal: the RMM console mirrors the 86Box Qt UI 1:1, so the old UI can be retired. This file lists every user-facing control in the A-slot 86Box Qt UI and what the console needs to reach it.

Source scanned: `GIT_SRC_ROOT/A/86Box/src/qt/*.ui`, `qt_mediamenu.cpp`, `config.c`, `rmm.c` (2026-10-17).

## How the console reaches a setting today
- **Whole VM config:** `config.dump` returns every cfg section and key as strings. `config.load {cfg}` accepts any section and key, and the values apply at the next hard reset.
  - Checked keys: every hardware-choice key `config.enums` lists (internal names that fit the machine being loaded; `cpu_speed`, `fpu_type` and `cpu_waitstates` against its CPU); `mem_size` against the machine's range; image paths for existence.
  - Every other key comes back `unchecked`.
  - `[General] rmm_*` and `uuid` are refused on purpose: the monitor's own settings can't be changed over the wire.
- **Live single keys:** `config.get` and `config.set` cover exactly two keys, `[Machine] mem_size` and `[General] sound_gain`, both integers (`rmm.c` `rmm_cfg[]`).
- **Choice lists (Phase 3.1, done):** `config.enums` returns every hardware-choice key of the Settings dialog in one call: value, values (filtered for the running machine the way the Settings page filters its combo) and labels; `cpu_speed` also carries `multi`. `config.describe` answers the same per key. The console's VM Settings page fills its combos from it.
- **Runtime actions:** `machine.*`, `sound.mute.*`, `cpu.force_interpreter.*`, `machine.fast_forward.*`, `fdd.N` / `cdrom.N` insert and eject, `media.list` (fdd and cdrom only), `input.*`, `capture.screenshot`.

So a settings page can already **write** almost any cfg key, through load and then reset. What the console cannot get today is **what to offer**:
- which machines, CPUs, cards and buses this build has
- each device's own option schema (the "Configure" buttons)
- which values are valid together

Without that, a 1:1 settings UI would have to hard-code 86Box tables.

## Gap summary: new RMM methods needed
| Need | Proposed method | Feeds |
|---|---|---|
| ~~Build catalog: machine types, CPU speeds and multipliers, FPUs, wait states~~ | **done (3.1):** `config.enums` / `config.describe` | Machine page |
| ~~Device lists filtered by the machine's buses (video #2, sound cards 2–4, hdc, scsi, fdc, cdrom-if, isamem/mcamem, isarom, isartc, lpt, com, mouse, keyboard, tablet, joystick, MIDI, FM driver)~~ | **done (3.1):** `config.enums`, checked by `config.load` | every combo box |
| Per-device options (`device_config_t`: selections, spinners, hex16/hex20 I/O, IRQ, DMA, file, MAC, BIOS) | `device.config.describe {device}` | every "Configure" button |
| Host-side lists: audio out/in devices, sample rates, MIDI devices, pcap interfaces, host CD drives, Vulkan devices | `host.audio`, `host.midi`, `host.net`, `host.cdrom` | Sound, Network, CD pages |
| Cfg validation before reset (bus slots, conflicts) | extend `config.load validate_only` checks beyond `mem_size` | all pages |
| Media beyond fdd/cdrom: MO, removable disk (ZIP), tape, cassette, cartridge; write-protect; host drive; folder-as-CD; new image; export 86F; image history | extend `media.list` and the `<dev>.N.*` verbs | Media menu, status bar |
| Live frontend state that changes the emulation: update mouse every CPU frame, Right Ctrl as left Alt, keyboard capture, CGA composite (hue, saturation, brightness, contrast, sharpness), grayscale, overscan, monochrome contrast, inverted VGA | add these to `rmm_cfg[]` live keys | Screen and Input |
| Status-bar activity: HDD, FDD, CD, NIC link, sound | `machine.status` additions or an `activity` event | status bar |
| Trace begin/end, MCA devices list, printer tray, VNC | `debug.trace.*`, `mca.devices`, `lpt.N.tray` | Tools menu |

The console handles some controls **client-side**, because a remote console draws its own frames from `capture.screenshot`:
- scale (0.5x–8x), HiDPI, nearest/linear filter
- 4:3 / machine aspect, fullscreen stretch modes, specify dimensions, remember size and position
- hide the status bar and toolbar
- renderer and shaders become the console's own viewer options
- screenshot and copy-screenshot (raw vs rendered = with or without the viewer's scale and filter)

They are listed below as "viewer".

## Settings dialog (VM cfg): 12 categories
✓ = settable today via `config.load` + reset; **C** = needs a catalog or options method to list choices; **L** = needs a live key.

| Category | Controls | Cfg keys | Status |
|---|---|---|---|
| Machine | machine type, machine, Configure, CPU type, frequency, FPU, wait states, PIT mode, RAM, time sync (off/local/UTC), softfloat FPU, 486 interpreter for 286/386, dynamic recompiler, accurate FPU env, CPU frame size | `[Machine] machine cpu_family cpu_speed cpu_multi fpu_type cpu_waitstates pit_mode mem_size time_sync fpu_softfloat cpu_override_interpreter cpu_use_dynarec cpu_dyn_accurate_fpu_env`, `[General] force_10ms` | ✓ write; **C** lists; mem_size live-checked |
| Display | video, video #2 (+Configure), Voodoo, 8514/A, XGA, PS/55 DA2, VGA screen type, grayscale conversion, EDID default/custom/export, overscan, inverted VGA, mono contrast | `[Video] gfxcard…`, `[General] enable_overscan video_grayscale video_graytype vid_cga_contrast` | ✓ write; **C**; overscan/gray **L** |
| Input devices | keyboard, mouse, tablet (+Configure), joystick type, joystick 1–4 mapping | `[Input devices] …` | ✓ write; **C**; joystick mapping is host-side |
| Sound | sound cards 1–4 (+Configure), FM driver, FLOAT32, output device, sample rate, audio input enable/device/rate, MIDI out/in (+Configure), standalone MPU-401 | `[Sound] sndcard..sndcard4 fm_driver sound_type sound_output_device sound_sample_rate sound_input_* midi_device midi_in_device mpu401_standalone` | ✓ write; **C**; host lists |
| Network | 4 NICs: mode (none/SLiRP/pcap/VDE/TAP/switch), adapter (+Configure), interface, VDE socket, TAP bridge, switch secret, hub mode, hostname | `[Network] net_NN_*` | ✓ write; **C**; `host.net` |
| Ports (COM & LPT) | internal LPT ECP DMA, LPT1–4 enable + device (+Configure), COM1–4 enable + device (+Configure) | `[Ports (COM & LPT)]` | ✓ write; **C** |
| Storage controllers | FDC, CD-ROM controller, HDC 1–4, SCSI 1–4 (each +Configure), cassette | `[Storage controllers]` | ✓ write; **C** |
| Hard disks | list; bus, channel, model, audio; New… (file, C/H/S, size, type, bus, channel, model, VHD format, block size), Existing…, Remove | `[Hard disks] hdd_NN_*` | ✓ write; image **creation** needs a method (`hdd.create`) or a host-side tool |
| Floppy & CD-ROM drives | per FDD: type, turbo timings, check BPB, audio; per CD: bus, channel, speed, type, EDC/ECC | `[Floppy and CD-ROM drives]` | ✓ write; **C** |
| Other removable devices | MO, removable disk (ZIP), tape: bus, channel, type | `[Other removable devices]` | ✓ write; **C** |
| Other peripherals | ISA RTC, ISABugger, POST card, unit tester, NetWare key card, soft power (IBM 5140), ISA memory cards 1–4, ISA ROM cards 1–4 (each +Configure), VFIO passthrough + custom ROM | `[Other peripherals]` | ✓ write; **C** |
| **Remote monitor** (new) | enable, port, listen, allow-from, pause on connect, disk track | `[General] rmm_port rmm_pause_on_connect rmm_disk_track` + proposed `rmm_listen rmm_allow` | 86Box-side Settings page only: `config.load` refuses `rmm_*` on purpose, so a remote client can't widen its own access |

Also in cfg, with no Settings page today: `gdbstub_port`. Note that the gdbstub binds `INADDR_ANY` (`gdbstub.c:1921`), unlike RMM's loopback-only bind. The same listen and allow work should cover it.

## Main window menus
| Menu | Items | Console mapping |
|---|---|---|
| Action | keyboard requires capture, Right Ctrl is left Alt, hard reset, Ctrl+Alt+Del, Ctrl+Alt+Esc, pause, NMI, ACPI shutdown, fast forward, force interpretation, update mouse every CPU frame, exit | reset/cad/cae/pause/nmi/acpi/ff/force-interp: **live today**; capture/rctrl/mouse-frame: **L** |
| View | hide status bar / toolbar, show non-primary monitors, resizable window, remember size, renderer (Qt / OpenGL / Vulkan / VNC), renderer options, specify dimensions, force 4:3, force machine ratio, scale 0.5–8x, HiDPI, filter nearest/linear, fullscreen + 5 stretch modes, apply stretch when maximized, CGA composite settings, OSD | viewer, except non-primary monitors (needs `capture.screenshot {monitor}`) and CGA composite (**L**) |
| Media (also status bar) | floppy: new image, existing, existing write-protected, host floppy, reload previous, image history, export to 86F, eject; CD: image, folder, host drive, reload previous, history, eject; MO / removable disk: new, existing, eject; tape: same; cassette: new, existing, record, play, rewind, fast forward, eject; cartridge: image, eject; NIC: connected toggle | fdd/cdrom insert/eject **live today**; rest: extend `media.*` |
| Tools | Settings…, Preferences…, update status bar icons, take screenshot, take raw screenshot, copy screenshot, copy raw screenshot, open screenshots folder, sound gain, mute, MCA devices, open printer tray, Discord, begin/end trace | screenshots: viewer + `capture.screenshot`; gain/mute **live today**; MCA, printer, trace: new methods; Discord: drop |
| Help | documentation, about 86Box, about Qt | console's own |

## Preferences (86Box global, not per VM)
- language
- select media from working dir
- auto-pause on focus loss and on dialog boxes
- confirm save / exit / hard reset
- CHD precache
- colour scheme
- mouse sensitivity
- inhibit multimedia keys
- key bindings

These are **console preferences**: they live in the console's Settings panel and never cross the wire. The exception is auto-pause on focus loss, which becomes a console option that sends `machine.pause`.

## VM manager (qt_vmmanager_*)
86Box's own VM manager window (list of VMs, details, preferences with the `vmm_path` store) is what the console's Inventory replaces. Its VM list maps to the domain store (`kvm.DomainStore`). The details sections map to the Summary page.

## Order of work (proposal)
1. More `config.describe` enums, plus `device.config.describe`. Without these every page is hard-coded.
2. More live keys in `rmm_cfg[]`: the frontend toggles that change emulation.
3. Media verbs for MO, ZIP, tape, cassette, cartridge.
4. RMM listen/allow (and gdbstub).
5. Host lists.
6. Trace, MCA, printer.

Each step gets a spec testlist on real 86Box before the console uses it.
