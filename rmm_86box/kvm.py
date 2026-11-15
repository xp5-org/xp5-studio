# SPDX-License-Identifier: MIT
"""86Box VM config <-> a libvirt/KVM-style domain description.

86Box keeps a VM in an INI file of sections and flat keys (hdd_01_fn, net_01_card, ...).
People who run KVM think in a domain: a machine type, a CPU model, memory, and a list of
devices (disks with a bus and a target like hda/fda, network interfaces with a model). This
module translates between the two, losslessly: every cfg key that has no domain field rides
along in `extra` (per section) or in a device's `options`, so

    from_cfg(cfg) -> domain        to_cfg(domain) -> the same cfg

for the sections it covers (SECTIONS). The cfg side is what config.dump returns and
config.load takes: {section: {key: "value"}}.

A domain:
    {
      "name": "dos622-486",
      "machine": "ms4144",                         # 86Box internal machine name ("machine type")
      "cpu": {"model": "i486dx", "speed_hz": 25000000, "multi": "1", "fpu": "internal",
              "dynarec": true},
      "memory_kib": 6144,
      "video": {"model": "cl_gd5420_isa"},
      "sound": {"model": "sb16"},                  # or null
      "interfaces": [{"model": "ne2k", "type": "none", "options": {...}}],
      "disks": [                                   # "source": null = no image in the drive
        {"device": "floppy", "target": "fda", "source": "/img/dos.img", "type": "35_2hd", "options": {...}},
        {"device": "disk", "bus": "ide", "target": "hda", "source": "/img/c.img", "options": {...}},
        {"device": "cdrom", "bus": "atapi", "target": "hdc", "source": "/img/a.iso", "options": {...}}
      ],
      "extra": {"Machine": {...}, ...}             # keys of these sections with no field above
    }
"""

import json
import os
import re

SECTIONS = ("Machine", "Video", "Sound", "Network", "Hard disks", "Floppy and CD-ROM drives")
FDD = "Floppy and CD-ROM drives"
HDD = "Hard disks"
IDE_TARGETS = {"0:0": "hda", "0:1": "hdb", "1:0": "hdc", "1:1": "hdd",
               "2:0": "hde", "2:1": "hdf", "3:0": "hdg", "3:1": "hdh"}
CHANNELS = {v: k for k, v in IDE_TARGETS.items()}


def _truthy(v):
    return str(v).strip() not in ("0", "", "false", "False")


def _device_keys(sec, prefix):
    """{NN: {suffix: value}} for keys like <prefix>_NN_<suffix> in one section."""
    out = {}
    for k, v in sec.items():
        m = re.fullmatch(prefix + r"_(\d\d)_(.+)", k)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = v
    return out


def from_cfg(cfg, name=""):
    """{section: {key: value}} (config.dump's cfg) -> domain."""
    cfg = {s: dict(kv) for s, kv in cfg.items()}
    mach = cfg.get("Machine", {})
    dom = {"name": name, "machine": mach.pop("machine", "")}
    cpu = {"model": mach.pop("cpu_family", "")}
    if "cpu_speed" in mach:
        cpu["speed_hz"] = int(float(mach.pop("cpu_speed")))
    if "cpu_multi" in mach:
        cpu["multi"] = mach.pop("cpu_multi")
    if "fpu_type" in mach:
        cpu["fpu"] = mach.pop("fpu_type")
    if "cpu_use_dynarec" in mach:
        cpu["dynarec"] = _truthy(mach.pop("cpu_use_dynarec"))
    dom["cpu"] = cpu
    if "mem_size" in mach:
        dom["memory_kib"] = int(float(mach.pop("mem_size")))

    vid = cfg.get("Video", {})
    dom["video"] = {"model": vid.pop("gfxcard")} if "gfxcard" in vid else None
    snd = cfg.get("Sound", {})
    dom["sound"] = {"model": snd.pop("sndcard")} if "sndcard" in snd else None

    net = cfg.get("Network", {})
    ifaces = []
    for nn, keys in sorted(_device_keys(net, "net").items()):
        for k in keys:
            net.pop(f"net_{nn}_{k}")
        iface = {"slot": int(nn), "model": keys.pop("card", "none"), "type": keys.pop("net_type", "none")}
        if keys:
            iface["options"] = keys
        ifaces.append(iface)
    dom["interfaces"] = ifaces

    disks = []
    fdd = cfg.get(FDD, {})
    for nn, keys in sorted(_device_keys(fdd, "fdd").items()):
        for k in keys:
            fdd.pop(f"fdd_{nn}_{k}")
        d = {"device": "floppy", "target": "fd" + "abcdefgh"[int(nn) - 1], "source": keys.pop("fn", None)}
        if "type" in keys:
            d["type"] = keys.pop("type")
        if keys:
            d["options"] = keys
        disks.append(d)
    hdd = cfg.get(HDD, {})
    for nn, keys in sorted(_device_keys(hdd, "hdd").items()):
        for k in keys:
            hdd.pop(f"hdd_{nn}_{k}")
        ch = keys.pop("ide_channel", None)
        d = {"device": "disk", "bus": "ide" if ch else "other", "slot": int(nn),
             "target": IDE_TARGETS.get(ch, f"disk{nn}"), "source": keys.pop("fn", None)}
        if ch and ch not in IDE_TARGETS:
            keys["ide_channel"] = ch
        if keys:
            d["options"] = keys
        disks.append(d)
    for nn, keys in sorted(_device_keys(fdd, "cdrom").items()):
        for k in keys:
            fdd.pop(f"cdrom_{nn}_{k}")
        ch = keys.pop("ide_channel", None)
        d = {"device": "cdrom", "bus": "atapi" if ch else "other", "slot": int(nn),
             "target": IDE_TARGETS.get(ch, f"cd{nn}"), "source": keys.pop("image_path", None)}
        if ch and ch not in IDE_TARGETS:
            keys["ide_channel"] = ch
        if "speed" in keys:
            d["speed"] = keys.pop("speed")
        if keys:
            d["options"] = keys
        disks.append(d)
    dom["disks"] = disks

    extra = {s: kv for s, kv in cfg.items() if s in SECTIONS and kv}
    if extra:
        dom["extra"] = extra
    return dom


def to_cfg(dom):
    """domain -> {section: {key: str}} for config.load. Sections are complete (config.load
    replaces each section it names), so a device left out of the domain is removed."""
    cfg = {s: {} for s in SECTIONS}
    for s, kv in (dom.get("extra") or {}).items():
        cfg.setdefault(s, {}).update({k: str(v) for k, v in kv.items()})
    m = cfg["Machine"]
    if dom.get("machine"):
        m["machine"] = dom["machine"]
    cpu = dom.get("cpu") or {}
    if cpu.get("model"):
        m["cpu_family"] = cpu["model"]
    if "speed_hz" in cpu:
        m["cpu_speed"] = str(int(cpu["speed_hz"]))
    if "multi" in cpu:
        m["cpu_multi"] = str(cpu["multi"])
    if "fpu" in cpu:
        m["fpu_type"] = cpu["fpu"]
    if "dynarec" in cpu:
        m["cpu_use_dynarec"] = "1" if cpu["dynarec"] else "0"
    if "memory_kib" in dom:
        m["mem_size"] = str(int(dom["memory_kib"]))
    if dom.get("video"):
        cfg["Video"]["gfxcard"] = dom["video"]["model"]
    if dom.get("sound"):
        cfg["Sound"]["sndcard"] = dom["sound"]["model"]
    for i, iface in enumerate(dom.get("interfaces") or []):
        nn = f"{int(iface.get('slot', i + 1)):02d}"
        n = cfg["Network"]
        n[f"net_{nn}_card"] = iface.get("model", "none")
        n[f"net_{nn}_net_type"] = iface.get("type", "none")
        for k, v in (iface.get("options") or {}).items():
            n[f"net_{nn}_{k}"] = str(v)
    hd_n = cd_n = 0
    for d in dom.get("disks") or []:
        opts = {k: str(v) for k, v in (d.get("options") or {}).items()}
        dev = d.get("device")
        if dev == "floppy":
            nn = f"{'abcdefgh'.index(d['target'][-1]) + 1:02d}"
            f = cfg[FDD]
            if d.get("source") is not None:
                f[f"fdd_{nn}_fn"] = d["source"]
            if "type" in d:
                f[f"fdd_{nn}_type"] = d["type"]
            f.update({f"fdd_{nn}_{k}": v for k, v in opts.items()})
        elif dev in ("disk", "cdrom"):
            if dev == "disk":
                hd_n += 1
                nn, sec, pre, src = f"{int(d.get('slot', hd_n)):02d}", cfg[HDD], "hdd", "fn"
            else:
                cd_n += 1
                nn, sec, pre, src = f"{int(d.get('slot', cd_n)):02d}", cfg[FDD], "cdrom", "image_path"
            if d.get("source") is not None:
                sec[f"{pre}_{nn}_{src}"] = d["source"]
            if d.get("target") in CHANNELS:
                sec[f"{pre}_{nn}_ide_channel"] = CHANNELS[d["target"]]
            if dev == "cdrom" and "speed" in d:
                sec[f"{pre}_{nn}_speed"] = str(d["speed"])
            sec.update({f"{pre}_{nn}_{k}": v for k, v in opts.items()})
        else:
            raise ValueError(f"unknown disk device {dev!r} (floppy, disk, cdrom)")
    return cfg


def describe(dom):
    """A few lines a person can read, KVM style."""
    cpu = dom.get("cpu") or {}
    mhz = f" @ {cpu['speed_hz'] / 1e6:g} MHz" if cpu.get("speed_hz") else ""
    lines = [f"domain   {dom.get('name') or '(running)'}",
             f"machine  {dom.get('machine', '?')}",
             f"cpu      {cpu.get('model', '?')}{mhz}" + ("" if cpu.get("dynarec", True) else " (interpreter)"),
             f"memory   {dom.get('memory_kib', 0) // 1024} MiB ({dom.get('memory_kib', 0)} KiB)",
             f"video    {(dom.get('video') or {}).get('model', 'none')}",
             f"sound    {(dom.get('sound') or {}).get('model', 'none')}"]
    for i in dom.get("interfaces") or []:
        lines.append(f"nic      {i.get('model')} ({i.get('type')})")
    for d in dom.get("disks") or []:
        src = os.path.basename(d.get("source") or "") or "(empty)"
        bus = f"{d.get('bus')} " if d.get("bus") else ""
        lines.append(f"{d['device']:8} {d.get('target', '?')} {bus}{src}")
    return lines


def diff(a, b):
    """Changed cfg keys between two domains: [(section, key, old, new)]."""
    ca, cb = to_cfg(a), to_cfg(b)
    out = []
    for s in sorted(set(ca) | set(cb)):
        for k in sorted(set(ca.get(s, {})) | set(cb.get(s, {}))):
            va, vb = ca.get(s, {}).get(k), cb.get(s, {}).get(k)
            if va != vb:
                out.append((s, k, va, vb))
    return out


class DomainStore:
    """Domains as <dir>/<name>.json."""

    def __init__(self, path):
        self.path = path

    def _file(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            raise ValueError("a domain name is letters, digits, '.', '_' or '-'")
        return os.path.join(self.path, name + ".json")

    def list(self):
        if not os.path.isdir(self.path):
            return []
        return sorted(f[:-5] for f in os.listdir(self.path) if f.endswith(".json"))

    def load(self, name):
        with open(self._file(name)) as f:
            dom = json.load(f)
        dom.setdefault("name", name)
        return dom

    def save(self, name, dom):
        os.makedirs(self.path, exist_ok=True)
        dom = dict(dom, name=name)
        with open(self._file(name), "w") as f:
            json.dump(dom, f, indent=2)
            f.write("\n")
        return self._file(name)
