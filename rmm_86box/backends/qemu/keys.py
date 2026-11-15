# SPDX-License-Identifier: MIT
"""RMM key names and set 1 scancodes as QEMU input-send-event keys.

QMP takes a key as {"type": "number", "data": qnum}; a qnum is the set 1 scancode with the
E0 prefix folded into bit 7 (E0 1D, rctrl, is 0x9D). QEMU maps it on to whatever keyboard
the guest has (AT, ADB, Sun), so one table serves every machine.
"""

from ..._vendor import rmm_schema

KEYS = rmm_schema.KEYS

# US layout: characters that need shift, and the key that carries them
SHIFTED = dict(zip('!@#$%^&*()_+{}|:"~<>?', "1234567890-=[]\\;'`,./"))
TYPED = {"\n": "enter", " ": "space", "\t": "tab", "\b": "backspace"}


def scancode(key):
    """A key name or a set 1 scancode (0x1xx = E0) -> the scancode."""
    if isinstance(key, str):
        if key not in KEYS:
            raise KeyError(f"unknown key {key!r}")
        return KEYS[key]
    if not 0 < key <= 0x1FF or (key & 0xFF) > 0x7F:
        raise KeyError(f"bad scancode {key:#x}")
    return key


def qnum(key):
    s = scancode(key)
    return (s & 0x7F) | (0x80 if s & 0x100 else 0)


def qkey(key, down):
    return {"type": "key", "data": {"down": bool(down), "key": {"type": "number", "data": qnum(key)}}}


def combo(text):
    """"ctrl+alt+del" -> ["ctrl", "alt", "del"]; aliases as input.key.send takes them."""
    alias = {"delete": "del", "insert": "ins", "escape": "esc", "return": "enter", "pageup": "pgup",
             "pagedown": "pgdn", "plus": "="}
    return [alias.get(k, k) for k in text.lower().split("+") if k]


def text_keys(text):
    """Text -> [(key, shift)] in US layout; raises KeyError on a character with no key."""
    out = []
    for ch in text:
        if ch in TYPED:
            out.append((TYPED[ch], False))
        elif ch in SHIFTED:
            out.append((SHIFTED[ch], True))
        elif ch.isupper() and ch.lower() in KEYS:
            out.append((ch.lower(), True))
        elif ch in KEYS:
            out.append((ch, False))
        else:
            raise KeyError(f"no key types {ch!r}")
    return out
