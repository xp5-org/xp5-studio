# SPDX-License-Identifier: MIT
"""Who writes this value? Attach to a write watchpoint and every stop is tallied by the function
that did the write, named from the loaded symbols (sym load game.map / a Ghidra export).

    sym load game.json            (then sym find, or psp= / base=)
    sym watch hero.health 2 w     -> watch #3
    hook w3 who_writes.py [stop_on=0] [every=20] [out=writers.txt]

86Box stops just AFTER the writing instruction, so cs:eip is the next one and cs:eip - 1 is the
writer's last byte: that is the address named ("hurt_+0xd" = the write ends at hurt_+0xe). Each new
writer is said once, with its file address (what Ghidra's Go To / a map file uses) and the value
change; the rest are counted.
  stop_on=V   stay paused when the value becomes V (e.g. health hits 0)
  every=N     print the tally every N hits
  out=FILE    on hook off: write "file_address  name  hits  last old->new" lines
"""

import rmm_86box  # noqa: F401  (hit.rmm is an rmm_86box.Rmm)


def _value(hit):
    raw = hit.read(hit.state["addr"], hit.state["len"], linear=True)
    return int.from_bytes(raw, "little")


def on_arm(hit):
    w = next((w for w in hit.rmm.call("debug.watch.list")["watchpoints"] if int(w["id"]) == hit.id), None)
    if w is None:
        raise RuntimeError(f"watch #{hit.id} is not set in 86Box")
    hit.state.setdefault("writers", {})
    hit.state["addr"], hit.state["len"] = int(w["addr"]), min(int(w.get("len", 2)), 8)
    try:
        hit.state["last"] = _value(hit)
    except Exception:  # noqa: BLE001 - running guest: the first hit reads it
        hit.state["last"] = None
    hit.say(f"watching {hit.sym(hit.state['addr'])} ({hit.state['len']} bytes)"
            + (f" = {hit.state['last']}" if hit.state["last"] is not None else ""))


def on_hit(hit):
    last_byte = hit.pc() - 1                   # inside the writing instruction, whatever follows it
    new = _value(hit)
    old = hit.state["last"]
    hit.state["last"] = new
    where = hit.sym(last_byte)
    func = where.split("+")[0]
    rec = hit.state["writers"].get(func)
    if rec is None:
        rec = hit.state["writers"][func] = {"hits": 0, "file": hit.file_addr(last_byte) or f"{last_byte:#x}",
                                            "at": where}
        hit.say(f"new writer {where}  (file {rec['file']})  {old} -> {new}")
    rec["hits"] += 1
    rec["change"] = f"{old}->{new}"
    every = int(hit.args.get("every", 0) or 0)
    if every and hit.count % every == 0:
        hit.say(_tally(hit))
    if "stop_on" in hit.args and new == hit.args["stop_on"]:
        hit.say(f"value is {new} (stop_on): staying paused in {where}")
        return "stay"
    return "resume"


def _tally(hit):
    rows = sorted(hit.state["writers"].items(), key=lambda kv: -kv[1]["hits"])
    return "writers: " + ", ".join(f"{name} x{r['hits']}" for name, r in rows)


def on_disarm(hit):
    if not hit.state.get("writers"):
        hit.say("no writes seen")
        return
    hit.say(_tally(hit))
    out = hit.args.get("out")
    if out:
        with open(out, "w") as f:
            for name, r in sorted(hit.state["writers"].items(), key=lambda kv: -kv[1]["hits"]):
                f.write(f"{r['file']}  {name}  {r['hits']}  {r.get('change', '')}\n")
        hit.say(f"wrote {out}")
