# RMM Qt console: implementation plan

The mockup was approved on 2026-10-17 (`rmm_demo/mockup/console_mockup.py`; screenshots in the session).

The work runs in order:
1. jitter fix and the application shell
2. timeline lock/unlock
3. 86Box-side RMM methods from `mockup/86box_ui_parity.md`

Each phase ends usable and verified on real 86Box, shown on the user's display `:12`.

## Ground rules (from memory and the repo)
- **Anything shown to the user runs against real 86Box over TCP.** Use `groups/pair_launch.json` or a remote attach, never `--mock`. The mock is only for `rmm_demo/tests` and the build testlist's own checks.
- **No rebuild or regen while spec runs are in flight.** Check `/api/v1/cluster/activity` first.
- **86Box builds:** rebuild A, then register the build and update the A pin (POST builds + PUT ab pin); otherwise runs use the stale binary.
- **Spec-first for 86Box methods:**
  - add a `planned` row in `rmm_client/rmm_registry.py` and tests in `rmm_client/rmm_spec.py`
  - regenerate with `gen_spec_testlists.py`, then implement in `src/rmm.c`
  - flip the row to `done`; the strict xfail catches early passes
  - never bend a test to fit 86Box: fix the emulator
- **Unattended spec runs** go through `POST /api/v1/runs` and stay off the manager (`avoid_manager`). Watched runs pin `"host": "manager"`.

---

## Phase 1: jitter fix + shell

### 1a. Jitter (the bug the user reported)
Every panel moves on break/resume because these widgets change size:

| Widget (`rmmgui/view.py`) | What changes | Fix |
|---|---|---|
| `HaltBanner` | `show()`/`hide()` adds or removes a row above everything | always present, fixed height; idle text "running" in muted style |
| `MachinePanel.body` | one rich-text label whose line count grows when the register block appears on a stop | split into fixed grids (state, registers, config); registers become a fixed-width `QGridLayout` of fixed-width labels (like `regs_widget()` in the mockup); "—" while running |
| `NodeTab.note`, `.sub` | text appears and disappears | fixed width with elided text; reserve the row |
| `NodeBar.halt_on` | empty ↔ text | fixed width, elided |
| `ScreenPanel.rate` / `.info` | empty ↔ text | fixed widths; "—" placeholder |
| `ConsolePanel.prompt` | `"...  "` vs `"p2> "` | fixed width from the longest node name |

**Test:** a new `rmm_demo/tests/test_layout_stable.py`.
- Offscreen, with mock nodes (allowed here; nothing is shown).
- Records the geometry of every panel, drives `stopped → resumed → halt-all`, and asserts the geometry hasn't moved.
- Also check by eye on `:12` against `pair_launch`: F10 / F9 repeatedly.

### 1b. Shell
Move `MainWindow` (`rmmgui/app.py:354`) to the mockup's structure. Reuse the mockup code by moving the generic pieces into `rmmgui/`:

| New / changed file | Content |
|---|---|
| `rmmgui/icons.py` | `icon(kind)` from the mockup (drawn icons; no emoji font dependency) |
| `rmmgui/shell.py` | `MainWindow(QMainWindow)`: menus, three toolbars (Machine, Snapshots, Nodes), docks (Inventory, Console, Snapshot timeline, Recent tasks, Settings), central `QTabWidget` (Summary, Screen, Monitor/Debug, Snapshots, VM Settings), `QSettings` layout save, View → break out / dock back / reset |
| `rmmgui/view.py` | existing panels become dock or page contents; `NodeBar` is replaced by the Inventory tree (`rmmgui/inventory.py`) |
| `rmmgui/app.py` | `App` stays the controller. The toolbar and menu actions call the existing `App` methods (`pause_resume`, `step_one`, `snap_node`, `restore_picked`, `toggle_halt_all`, …), and the F-key bindings stay. |
| `rmmgui/tasks.py` | `Recent tasks` model fed from `App.say()` events with a target and result (replaces the free log lines) |

Scope of pages in Phase 1:

| Page | Status |
|---|---|
| Summary | live: `machine.status`, `media.list`, conn info |
| Screen | live: the current FrameView plus registers and banner |
| Monitor/Debug | live parts only: registers, hex (`mem.phys.read` / `mem.lin.read` / `mem.phys.write`), breakpoints/watchpoints (`debug.*`). The map, disassembly and device tabs show a "needs RMM method" placeholder until Phase 3. |
| Snapshots | the existing `snapview.SnapsPanel` tree plus list and details |
| VM Settings | `config.dump` → form; edits go to `config.load validate_only` then `config.load` + "Apply (reset)"; `config.describe` fills the five enum combos. Every other combo is free-text with a "needs list" badge until Phase 3. |

**Remote-first:**
- File → Attach (host:port) is the default way to add a node.
- "Launch on this host" uses `launch.Launched` and is gated by a Preferences switch.
- New Project writes `rmm-group/1` (`group.GroupSpec`).

**Project snapshot persistence:** snapshots live inside each 86Box (max 32, lost on exit; `snaps.py` docstring). Store/load per VM and per project uses the existing `snapshot.export {tag, path}` and `snapshot.load {path}`:
- The files sit on **each node's 86Box host** (its VM dir).
- The project file keeps the `SnapTree.to_json()` index plus `{node: host, path}` per record.
- No file transfer over RMM is needed for this phase.
- `box86_rmm/registry.py` is stale: it lacks `snapshot.export`, `snapshot.load`, `snapshot.flush` and `snapshot.writer`. Sync it with `rmm_client/rmm_registry.py` first.

**Phase 1 done when:**
- `rmm_demo/tests` and the `tool: xp5studio demo build (linux)` testlist pass (offscreen render of every page and dock).
- The layout-stable test passes.
- The user has driven `pair_launch` on `:12`: break/resume with no jitter; docks break out and dock back; layout survives a restart.

### Phase 1 status (2026-10-17)
Built:
- `rmmgui/shell.py`, `pages.py`, `inventory.py`, `tasks.py` and `icons.py`; fixed geometry in `view.py`.
- `NodeBar`, `NodeTab` and `MachinePanel` are gone.

Tests:
- `tests/test_layout_stable.py` passes. It also covers the project round trip, and the build testlist runs it with `{demo_python}`.
- `HostGroup.add_node` and `SnapTree.from_json` have unit tests.
- `tool: xp5studio demo build (linux)` passed: all 9 steps.

Running on `:12` against `pair_launch` (real 86Box): pause-all / resume-all moves nothing.

Changes from the plan above:
- The "Snapshot timeline" dock comes with Phase 2's `TrackPlayer`. Until then the tree is on the Snapshots page.
- Pages sit in scroll areas, so a 720 px desktop scrolls a page instead of the window growing past the screen.
- Step over and run-to-return are disabled until 3.6, which adds disassembly.
- Remote attach: 86Box still binds 127.0.0.1 only, so a remote host needs a tunnel until 3.5.

Open: the user's own drive on `:12` (break/resume, break out / dock back, layout kept across a restart).

---

## Phase 2: timeline lock/unlock (higher risk; most of the new tests go here)

### Model (no Qt; `rmmgui/snaps.py` + `rmmgui/hostgroup.py`)
The tree already has what this needs:
- per-node heads (`SnapTree.heads`)
- per-record member nodes and per-node parents
- `scope` of `"group"` / `"nodes"`
- `HostGroup.restore(rec, nodes=…)` on a subset

Lock/unlock is a layer on top:
- `HostGroup.locked: set[str]`; every node is locked by default.
- **`unlock(node)`:** records a `Fork(node, from_rec=heads[node], at=time)`. Group `snap`, `restore`, `halt` and `resume` default to `locked` nodes only. Explicit `@p2` still works.
- **Unlocked-node actions:** restore an older group record on that node alone (`restore(rec, [p2])`), snap it alone (scope `"nodes"`, parent = its head), play or pause it alone.
- **`relock(node)`:** the node keeps its current state. The next group snapshot includes it again, and its own records stay in the tree as a side branch.
- **`rollback_group(rec)`:** checks that every node holds `rec`, then restores all nodes (locked and unlocked) to it together. It discards the unlocked node's records made since the fork (`snapshot.delete` on that node, records marked gone) and relocks everything.
  - Refuses up front if any node can't restore, the same check `restore()` does today.
  - Asks for confirmation in the UI ("discard p2-try1").
- **Halt-all policy:** a stop on an unlocked node pauses only that node, and a stop on a locked node doesn't pause unlocked ones. This is configurable (default off for unlocked).
- Persist `locked` and forks in the project file next to the tree.

### View (`rmmgui/timeline.py`)
`TrackPlayer` from the mockup, driven by the model:
- **Layout:** one lane per node. Group records are columns, with a dotted line from the ruler through the locked lanes to the scrubber.
- **X axis:** the group's sequence of records, spaced by wall-clock time (`Snap.t`).
- **Unlocked node:** its own track starts at the column it was rewound to, and its length is its own elapsed run.
- **Clicks:**
  - a scrubber dot: "jump group here", i.e. `restore` on the locked nodes
  - a lane's lock icon: unlock / re-lock
  - a dot on an own-track: restore that node
- **Transport bar:** group transport, plus a per-unlocked-node transport, "Re-lock here" and "Roll group back to …".
- The existing tree view stays available as "Tree" in the dock's view picker.

### Tests
- **Unit** (`rmm_demo/tests/test_lockstep.py`; mock nodes are fine, nothing shown):
  - unlock → group snap skips p2
  - p2 alone: restore, snap, relock → next group snap includes p2 with the right parents
  - rollback discards p2's records and relocks
  - refused rollback changes nothing
  - halt-all scoping
  - project save/load round trip
- **Real 86Box** (new testlist `__testlist__rmm_demo_lockstep.py`, generated beside the others by `gen_demo_testlists.py`; `avoid_manager`):
  - `pair_launch` with MEMLOOP on both nodes
  - group snap S1 → unlock b → run → restore b to S1 → snap b alone → relock → group snap S2
  - check with `mem.phys.read` that b's MEMLOOP counter at S2 continues from S1 and a's doesn't
  - then `rollback_group(S1)`: both counters equal their S1 values, and b's own snapshot is gone from `snapshot.list`
- **Watched check on `:12`** with the user.

**Phase 2 done when** unit and real-86Box lockstep tests pass and the user has driven unlock / rewind / relock / rollback on `:12`.

### Phase 2 status (2026-10-17)
Built:
- **Model:** `HostGroup` has the following:
  - `locked` and `forks`
  - `unlock` and `relock`
  - `own_records` and `branch_point`
  - `rollback_plan` and `rollback_group`
  - `lock_state` and `set_lock_state`
- **Group commands:** group `snap`, `restore`, `halt` and `resume` default to locked nodes. Halt-all is scoped by the new policy key `halt_unlocked` (default off).
- **Console words:** `unlock NODE`, `lock [NODE]` and `rollback TAG|#ID`. `nodes` shows each node's lock state.
- **Project file:** gains a `"lock"` entry.
- **View:** `rmmgui/timeline.py` holds `TrackPlayer` and the "Snapshot timeline" dock, tabbed with Recent tasks.
  - The dock has a Tracks / Tree picker, plus Re-lock and Roll back buttons.
  - The transport bar holds the group buttons, then the selected node's.
  - Clicks work as planned: scrubber, lane lock icon, and dots on unlocked or own tracks.
  - Height depends on the node count only.
- **Elsewhere in the window:**
  - a lock toggle on the Group toolbar
  - Snapshots menu items
  - Inventory: an "unlocked" note and a menu item
  - "(unlocked)" in the banner

Tests:
- `tests/test_lockstep.py` covers the plan's six cases over mocks.
- `test_layout_stable` adds unlocked and relocked states (nothing moves) and lock state in the project round trip.
- `tests/lockstep_real.py` plus `__testlist__rmm_demo_lockstep.py` (`avoid_manager`) passed 5/5 on two real 86Box with MEMLOOP. A marker byte at 0000:04FC tells the states apart; MEMLOOP's counter wraps too fast for that and only shows the loop runs.

Changes from the plan above:
- The real-86Box list is handwritten, not generated by `gen_demo_testlists.py`, because that generator makes watched single-VM demos.
- Group prev/next steps between group columns around the locked nodes' common head.

Open: the user's own drive on `:12`.

---

## Phase 3: 86Box-side RMM methods (parity list order)
Each step:
- `planned` rows + spec tests → regenerate testlists → implement in A's `src/rmm.c` → build, register and pin A → spec runs pass → flip to `done`
- sync `box86_rmm/registry.py` → wire the console page and drop its "needs …" badge

| # | Methods | Console page that lights up |
|---|---|---|
| 3.1 | More `config.describe` enums (`rmm_enum_*` kinds): cpu_speed / fpu / waitstates per machine, video #2, sound cards 2–4, FM driver, hdc/scsi/fdc/cdrom-if, isamem/isarom/isartc, lpt/com devices, keyboard/mouse/tablet/joystick, MIDI; plus `config.load` checks for them | VM Settings combos |
| 3.2 | `device.config.describe {device}` (from `device_config_t`) and per-device keys in `config.load` | every "Configure" button |
| 3.3 | Live toggles as `rmm_props[]` rows (the same QAction-trigger path as `sound.mute`): update mouse every CPU frame, Right Ctrl is left Alt, keyboard requires capture; plus live keys in `rmm_cfg[]` for overscan, grayscale, mono contrast, CGA composite | Screen menu, VM menu |
| 3.4 | Media beyond fdd/cdrom in `media.list` and `<dev>.N.*`: MO, rdisk (ZIP), tape, cassette (record, play, rewind, ff), cartridge, write-protect, host drive, CD folder, reload previous, export 86F, NIC connected | Media menu, status bar |
| 3.5 | `rmm_listen` / `rmm_allow` + `--monitor-listen` / `--monitor-allow` (peer check after `accept()`), the same for the gdbstub (now `INADDR_ANY`), and the 86Box Settings "Remote monitor" page (`src/qt`). `config.load` keeps refusing `rmm_*`. | 86Box Settings |
| 3.6 | `mem.map` (the `mem_mapping` list), `io.map` (the `io_sethandler` owners), `devices.list`; disassembly done client-side over `mem.lin.read` unless 86Box's own disassembler is wanted | Monitor/Debug tabs |
| 3.7 | `host.audio`, `host.midi`, `host.net` (pcap), `host.cdrom` | Sound / Network / CD combos |
| 3.8 | `debug.trace.begin` / `end`, `mca.devices`, `lpt.N.tray`, `capture.screenshot {monitor}` | Debug / VM menus, non-primary monitors |

### 3.1 status (2026-10-18)
- **New method `config.enums {section?}`:** returns every hardware-choice key of the Settings dialog in one call: section, key, value, values, labels, plus `multi` for cpu_speed.
  - Values are filtered for the running machine the way each Settings page filters its combo.
  - The keys come from one row table in `rmm.c` (`rmm_enum_rows`).
- **`config.describe`** answers each of those keys the same way.
- **`config.load`** checks them against the machine, CPU and video being loaded (error 10).
- A name can be in a table twice, e.g. the LPT and COM `loopback`. Any entry with that name that fits is accepted.
- The memory and ROM card keys are 0-based (`isamem0_type`..`isamem3_type`). An MCA machine lists `mcamemN_type` instead.
- **Spec:** `config.enums_cover_the_settings_dialog` and `config.load_checks_device_keys` (core group, so also the `vm_core` matrix on every VM config). The registry row is `done`. A builds 142, then 143 pinned.
- **Console:** VM Settings fills combos from `config.enums`: display names shown, internal names stored, keys at their default listed. cpu_speed also sets cpu_multi. Against an 86Box without `config.enums` it falls back to `config.describe`. Covered by `test_layout_stable.test_settings_page_lists`.
- **Runs:** they go to docker hosts, and to c7f390a44ebf only when it is the sole docker host up (the user's rule, 2026-10-18).

The 3.5 spec tests cover: bind 0.0.0.0 and connect from a second cluster host; an allowlisted peer is accepted; any other peer is refused and logged; `config.load` refuses `rmm_listen`.

**Phase 3 is done when** every parity row is either served by RMM or marked "viewer / console-side" in `86box_ui_parity.md`, and the old 86Box Settings dialog has nothing the console lacks.

## Open questions for later (not blocking Phase 1)
- **Remote snapshot files:** files stay on each 86Box host. If the user wants a project copied to another machine, that needs a transfer method (`snapshot.read` in chunks) or a host-side copy.
- **Disassembler:** client-side (a small x86 decoder in `box86_rmm`) or an 86Box `cpu.disasm` method. Decide at 3.6.
