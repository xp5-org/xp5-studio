# 86box-rmm
remote machine monitor & snapshot breakpoint engine for 86box in QT6 for GUI , Python for cli & library

## Licensing

This repository (Python library, CLI, Qt6 GUI) is MIT licensed, see LICENSE. The 86Box emulator and its RMM monitor code (src/rmm*.c, include/86box/rmm*.h) are GPL-2.0 and live in the 86Box source tree, not here.

## Building

### The Qt6 GUI (`rmm_demo/`)

The GUI is Python on PySide6 (Qt6), so there is nothing to compile. Install PySide6 and run it:

```
python -m venv /opt/venv && /opt/venv/bin/pip install PySide6
/opt/venv/bin/python rmm_demo/rmm_demo.py --port 55860     # one VM
/opt/venv/bin/python rmm_demo/rmm_demo.py --mock           # no VM
```

To package it, `python make_dist.py OUT_DIR` (stdlib only) writes the `rmm_86box` wheel, a single-file `rmm_demo.pyz` (GUI plus library; run it with `/opt/venv/bin/python rmm_demo.pyz --port N`) and the unpacked `rmm_demo/` folder.

### 86Box with the monitor (the `86box-rmm` custom branch, build slot "pin-A")

The monitor lives in a fork of 86Box (`https://github.com/xp5-org/86Box.git`, branch `rmm_tcp_console`). In the test tree this is slot A: `sourcedir/86box_remote_monitor/GIT_SRC_ROOT/A/86Box`. Slot B is upstream or a second candidate, and slot C is upstream without RMM (the build testlists skip `-D RMM=ON` for C).

```
git clone -b rmm_tcp_console https://github.com/xp5-org/86Box.git 86Box && cd 86Box
cmake -S . -B build/regular --preset regular -D RMM=ON -D USE_QT6=on
cmake --build build/regular -j"$(nproc)"
# binary: build/regular/src/86Box
```

- `-D RMM=ON` is what compiles in the monitor (`src/rmm*.c`). Check it took: `grep -c -a monitor-port build/regular/src/86Box` must be non-zero.
- `-D USE_QT6=on` selects the Qt6 UI for the emulator window. The test build testlists use `-D USE_QT6=off` (Qt5) instead. On Windows (MSYS2 UCRT64) they also add `-D STATIC_BUILD=ON`, which needs `mingw-w64-ucrt-x86_64-qt5-static`, so a static Qt6 package is needed if you switch that build to Qt6.
- Install the usual 86Box build dependencies first (Qt dev packages, SDL2, OpenAL, FreeType, libpng, ninja).
- In the test manager, build it with the container's `monitor-86box-build-linux` / `-win10` / `-mac` / `-freebsd` testlists as "Build A". That registers the binary. Then PUT the A pin so later runs use it (see the test manager's A/B pin API), otherwise runs use a stale binary.

# rmm_86box

A Python client for 86Box's remote machine monitor (RMM). It uses only the standard library, so you can drop it into other software. The Qt (PySide6) demo in `../rmm_demo/` shows how to use it.

## Getting a VM to listen

Build 86Box with `-D RMM=ON`. Then either:

- add `rmm_port = 55860` under `[General]` in the VM's `86box.cfg`, or
- start it with `86Box --monitor-port 55860`.

The monitor binds 127.0.0.1 only and takes one client at a time. A cfg-port VM pauses while a client attaches. `Rmm.connect()` resumes it, unless you pass `resume_on_connect=False`.

## Use

```python
from rmm_86box import Rmm, FramePoller

with Rmm(55860) as rmm:
    rmm.machine.status()                      # {"state": "running", "tsc": ...}
    rmm.input.text.type(text="dir\n")
    f = rmm.screenshot(step=2)                # Frame: w, h, rgb (RGB24); never pauses the guest
    with rmm.paused():                        # registers, memory and ports need a paused guest
        rmm.cpu.regs.get()
        rmm.read_mem(0xB8000, 160)
    rmm.snapshot.save(tag="a")                # in-memory, paused only
    rmm.snapshot.restore(tag="a")             # time travel; leaves it paused
    rmm.turbo = True                          # unthrottled run (machine.fast_forward); False = real speed
    rmm.fdd[0].insert(path="/img/disk.img")
    rmm.vm.save("mybox")                      # running config -> ~/.rmm_86box/domains/mybox.json
    rmm.vm.load("mybox")                      # stage it + hard reset: the VM becomes that config
```

Any method the server lists is an attribute chain. Run `dir(rmm)` or `dir(rmm.snapshot)` to see them, or `rpc.discover` for the raw table.

### Keyboard and mouse

`input.events` takes an ordered list of key, motion and button events. 86Box queues them and spaces keys (2 ms) and button changes (30 ms) in guest time, so a whole sequence fits in one call and nothing overruns the keyboard FIFO. The helpers on `rmm.input` send it:

```python
from rmm_86box import key_event, btn_event, rel_event

rmm.input.tap("lctrl", "lalt", "del")         # press in order, release in reverse
rmm.input.press("lshift"); rmm.input.release("lshift")
rmm.input.move(10, -5)                        # relative motion; dz= for the wheel
rmm.input.click()                             # left button down and up
rmm.input.send(key_event("lctrl", True), btn_event(1), btn_event(0), key_event("lctrl", False))
rmm.input.devices()["pointer"]                # "relative": grab the host pointer and send rel
                                              # "absolute": a tablet; rmm.input.point(x, y), 0..1
rmm.input.release_all()                       # let go of every held key and button
```

Keys are named as in `input.key.send` or given as set 1 scancodes (`0x1xx` = E0 prefix). Disconnecting releases whatever the client still holds.

### QEMU machines (backends)

`rmm_86box.backends.qemu` runs a local, headless QEMU behind the same surface as `Rmm`, so the console, `FramePoller` and the GUI's node code can drive it. It speaks RMM method names and offers only the ones QEMU can honour: anything else (registers, memory, breakpoints, media for now) is -32601.

```python
from rmm_86box.backends import qemu
m = qemu.start("isapc", fda="/img/dos.img")                              # qemu-system-i386 -M isapc
m = qemu.start("q800", bios="Quadra-650.ROM", scsi=["pram.img", "hd.qcow2"])   # qemu-system-m68k
m = qemu.start("ss5", hda="sol.qcow2", cdrom="sol.iso")                  # qemu-system-sparc -M SS-5
m.machine.status(); m.input.text.type(text="dir\n"); m.screenshot().save_png("s.png")
m.qmp.execute(command="query-cpus-fast")                                 # raw QMP passthrough
m.close()                                                                # also stops the QEMU it started
```

Each VM gets a run dir with `qmp.sock` (and `vnc.sock` with `vnc=True`); nothing listens on TCP unless you pass `gdb_port=`. Frames come from `screendump` for now; `qemu/vnc.py` (RFB frames) and `qemu/gdb.py` (gdbstub debugging) are placeholders. All three machines have relative-only mice, so `input.devices` reports `pointer: "relative"`.

In the Qt manager a group node with a `qemu` block is a QEMU machine (Project → Add node: QEMU…, or a group file; see `rmm_demo/groups/qemu_trio.json`):

```json
"pc":  {"qemu": {"profile": "isapc", "fda": "/img/dos.img", "run_dir": "/tmp/rmm_groups/{group}/{node}", "wait": 10}},
"sun": {"qemu": {"profile": "ss5", "qmp": "/run/sun/qmp.sock"}}
```

Without `qmp` the manager starts QEMU and quits it on close (`on_exit: keep` leaves it running); with `qmp` it attaches and leaves it alone. A qemu node takes no `port`, `launch`, `vm_config`, `domain` or `box_cfg`. It joins halt-all like any node: a guest pause, a QEMU exit and a crash (QEMU killed by a signal) all count. The registers panel stays empty. `rmmctl session load` skips qemu nodes.

## Modules

| module | what |
|---|---|
| `rpc` | `RpcClient`: thread-safe JSON-RPC over one socket, with a reader thread and event callbacks |
| `client` | `Rmm` facade, `Frame`, `VmConfig` (`rmm.vm`) |
| `registry` | methods, params, result fields and value hints; merged with `rpc.discover` at connect |
| `complete` | `Completer`: Tab completion for console lines and `rmm.…`/`_[…]` expressions |
| `console` | `Console`: parse a line, run it, format the result (the demo and the REPL both use it) |
| `frames` | `FramePoller`: background `capture.screenshot` with on/off and an adjustable rate |
| `kvm` | `86box.cfg` ⇄ libvirt-style domain (machine, cpu, memory_kib, disks hda/fda/hdc, interfaces), lossless |
| `mock` | start the reference mock (`rmm_client/rmm_mock.py`) for running without a VM |

## rmmctl: list and drive instances

A docker/virsh-style command line over several 86Box instances (`rmmctl` once installed, else `python -m rmm_86box.ctl`):

```
rmmctl start dos1 --vm-dir ~/vms/dos1 --turbo   # launch with --monitor-port auto, register as "dos1"
rmmctl attach lab2 10.0.0.7:55860               # register an 86Box running elsewhere (attach web 55861: this machine)
rmmctl ps                                       # NAME STATE TURBO WHERE KIND PID UP  (-a: also down, --json)
rmmctl status dos1                              # state, turbo, sticky, media
rmmctl pause dos1 lab2 | resume --all
rmmctl sticky dos1 on                           # keep a pause after rmmctl disconnects (start does this by default)
rmmctl turbo dos1 on                            # on|off|toggle; no mode prints it
rmmctl break dos1 int8 f000:fff0 --wait 20      # arm, run to a hit, show registers (ADDR: 0x.., SEG:OFF, intN)
rmmctl media dos1                               # fdd/cdrom/... slots; insert fdd.0 A.img [--wp] | eject cdrom.0 | new fdd.1 B.img --size 1474560
rmmctl config dos1 get General | set Machine mem_size 8192 | pending | discard
rmmctl screenshot dos1 -d shots                 # dos1-001.png, then -002, ... (-o 'boot-%d.png' or 'boot-{n}.png' to name it; a plain name is overwritten)
rmmctl session save lab | load lab | show lab | ls | rm lab
rmmctl console dos1 'snapshot.save before'      # one-shot; no line = interactive console
rmmctl stop dos1 | rm lab2 | vm list
```

Records live in `~/.rmm_86box/instances/` (`--home` or `$RMM_86BOX_HOME`). Launched instances use `--monitor-port`, which never pauses on connect; an instance whose cfg sets `rmm_port` pauses while `ps` or `status` probes it.

**Sticky pause.** By default 86Box resumes a guest its client paused when the client disconnects, so a one-shot `rmmctl pause` would be undone at once. With sticky pause a pause the client asked for (`machine.pause`, a breakpoint or step stop) outlives the connection; the automatic pause-on-connect never does. Settings: `[General] rmm_sticky_pause = 1` in 86box.cfg, `--monitor-sticky-pause` on the command line, or at run time `rmm.sticky_pause.set {value}` (`rmm.sticky_pause = True` in Python, `rmmctl sticky`). `rmmctl start` passes the flag unless `--no-sticky`. Breakpoints and watchpoints still end with the connection, so `rmmctl break --wait` arms and waits in one command.

**Sessions.** `session save` writes the registered instances as the Qt manager's group file (`rmm-group/1`, in `<home>/sessions/NAME.json`): attached ones as host and port, launched ones as a `launch` block. The manager's Open/Save group dialogs start in that folder, so a session saved here opens there and the other way round; `session load` attaches or launches each node that is not up already (mock and `vm_config` nodes need the manager).

## Console / REPL

```
python -m rmm_86box 55860               # interactive, Tab completes
python -m rmm_86box 55860 machine.status 'snapshot.save before'
python -m rmm_86box --mock              # against the mock, no VM
```

Line forms:

- `method name=value ...`, for example `mem.phys.read addr=0xB8000 len=64`.
- One-parameter methods take the rest of the line, for example `input.text.type dir a:`.
- `rmm.…` Python expressions. `_` holds the last result, and `_["` completes its keys.
- Builtins: `help`, `vm`, `autopause` (pause around register/memory/`snapshot.save` calls), `autoresume`
  (resume after `snapshot.restore`, which leaves the guest paused; off by default, on in the demo).

## VM config in KVM terms

`config.dump` returns the cfg as `{section: {key: value}}`. `kvm.from_cfg` turns it into a domain:

```json
{"machine": "ms4144", "cpu": {"model": "i486dx", "speed_hz": 25000000}, "memory_kib": 6144,
 "video": {"model": "cl_gd5420_isa"}, "sound": {"model": "sb16"},
 "interfaces": [{"model": "ne2k", "type": "none"}],
 "disks": [{"device": "floppy", "target": "fda", "source": "/img/dos.img"},
           {"device": "disk", "bus": "ide", "target": "hda", "source": "/img/c.img"},
           {"device": "cdrom", "bus": "atapi", "target": "hdc", "source": "/img/a.iso"}]}
```

`kvm.to_cfg` maps it back. Keys with no domain field ride along in `extra` or a device's `options`, so nothing is lost. `config.load` stages a cfg and the next hard reset applies it. Before staging, 86Box checks machine, CPU family, cards, RAM size and image paths, and refuses with error 10, listing every bad key.

## Tests

```
python -m unittest discover -s tests       # against the mock; no VM
```
