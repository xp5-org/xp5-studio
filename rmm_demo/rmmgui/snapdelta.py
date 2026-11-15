# SPDX-License-Identifier: MIT
"""Delta form of 86Box snapshot files (rmm_snapfile.py, "RMMSNAP1"), for the full-tree bundle. No Qt.

Every .rmmsnap is self-contained, but a child snapshot repeats most of its parent: the RAM/device
buffer (`DATA`) changes in some pages, the hard disk overlay (`DISK`) holds the parent's 4K blocks
again plus the newly written ones. `encode(parent, child)` keeps only what differs and
`decode(parent, delta)` gives back the child byte for byte.

  RMMDLT1\\0 | u32 header length | the child's file header | u32 section count |
  per section: 4s id, u32 reserved, u64 length, u8 mode, u64 payload length, payload

  mode 0  the section as it is
  mode 1  any other section, in 4096-byte chunks: u32 chunk, u32 changed, u32 index per changed chunk, their bytes;
          a chunk not listed is the parent's chunk of the same section at that index
  mode 2  DISK: u32 disks; per disk the 32-byte header, then (for its n blocks, in file order)
          n x u32 block number, n x u8 stored (0: the parent's bytes for that block), the stored blocks
"""

import struct

MAGIC = b"RMMDLT1\0"
SNAP_MAGIC = b"RMMSNAP1"
CHUNK = 4096


class DeltaError(ValueError):
    pass


def is_snapshot(raw):
    return raw[:8] == SNAP_MAGIC


def _parse(raw):
    """(header bytes, [(id, reserved, body)]) of a snapshot file."""
    if not is_snapshot(raw) or len(raw) < 16:
        raise DeltaError("not an RMMSNAP1 file")
    hdrlen, = struct.unpack_from("<I", raw, 12)
    o, secs = hdrlen, []
    while True:
        if o + 16 > len(raw):
            raise DeltaError("truncated (no END section)")
        sid, resv, n = struct.unpack_from("<4sIQ", raw, o)
        body = raw[o + 16:o + 16 + n]
        if len(body) != n:
            raise DeltaError("truncated section")
        secs.append((sid, resv, body))
        o += 16 + n
        if sid == b"END ":
            break
    if o != len(raw):
        raise DeltaError("bytes after the END section")
    return raw[:hdrlen], secs


def _disks(blob):
    """[(header32, hid, bs, [(block, bytes)])] of a DISK section."""
    cnt, = struct.unpack_from("<I", blob, 0)
    o, out = 4, []
    for _ in range(cnt):
        hdr = blob[o:o + 32]
        hid, n, _sectors, _h, _ok, bs = struct.unpack("<IIQQII", hdr)
        o += 32
        recs = []
        for _ in range(n):
            recs.append((struct.unpack_from("<I", blob, o)[0], blob[o + 4:o + 4 + bs]))
            o += 4 + bs
        out.append((hdr, hid, bs, recs))
    if o != len(blob):
        raise DeltaError("DISK section has trailing bytes")
    return out


def _enc_data(pb, body):
    n = len(body)
    changed = [i for i in range(-(-n // CHUNK)) if body[i * CHUNK:(i + 1) * CHUNK] != pb[i * CHUNK:(i + 1) * CHUNK]]
    return (struct.pack("<II", CHUNK, len(changed)) + struct.pack(f"<{len(changed)}I", *changed)
            + b"".join(body[i * CHUNK:(i + 1) * CHUNK] for i in changed))


def _dec_data(pb, n, payload):
    chunk, k = struct.unpack_from("<II", payload, 0)
    idx = struct.unpack_from(f"<{k}I", payload, 8)
    o = 8 + 4 * k
    new = {}
    for i in idx:
        ln = min(chunk, n - i * chunk)
        new[i] = payload[o:o + ln]
        o += ln
    out = bytearray()
    for i in range(-(-n // chunk)):
        out += new[i] if i in new else pb[i * chunk:(i + 1) * chunk]
    if len(out) != n:
        raise DeltaError("DATA does not rebuild to its length (wrong parent?)")
    return bytes(out)


def _enc_disk(pb, body):
    have = {hid: dict(recs) for _, hid, _, recs in _disks(pb)}
    out = [struct.pack("<I", len(_disks(body)))]
    for hdr, hid, bs, recs in _disks(body):
        par = have.get(hid, {})
        stored = [par.get(b) != d for b, d in recs]
        out += [hdr, struct.pack(f"<{len(recs)}I", *[b for b, _ in recs]), bytes(stored)]
        out += [d for (b, d), s in zip(recs, stored) if s]
    return b"".join(out)


def _dec_disk(pb, payload):
    have = {hid: dict(recs) for _, hid, _, recs in _disks(pb)}
    cnt, = struct.unpack_from("<I", payload, 0)
    o, out = 4, [struct.pack("<I", cnt)]
    for _ in range(cnt):
        hdr = payload[o:o + 32]
        hid, n, _s, _h, _ok, bs = struct.unpack("<IIQQII", hdr)
        o += 32
        blocks = struct.unpack_from(f"<{n}I", payload, o)
        o += 4 * n
        flags = payload[o:o + n]
        o += n
        out.append(hdr)
        for b, st in zip(blocks, flags):
            if st:
                d = payload[o:o + bs]
                o += bs
            else:
                d = have.get(hid, {}).get(b)
                if d is None:
                    raise DeltaError(f"disk {hid} block {b}: not in the parent (wrong parent?)")
            out.append(struct.pack("<I", b) + d)
    return b"".join(out)


def encode(parent, child):
    """Delta of snapshot file bytes `child` against `parent`. Raises DeltaError for anything that is not
    two RMMSNAP1 files; a section it cannot diff is kept whole."""
    _, psecs = _parse(parent)
    hdr, csecs = _parse(child)
    pmap = {sid: body for sid, _, body in psecs}
    out = [MAGIC, struct.pack("<I", len(hdr)), hdr, struct.pack("<I", len(csecs))]
    for sid, resv, body in csecs:
        mode, payload = 0, body
        pb = pmap.get(sid)
        if pb is not None and sid == b"DISK":
            try:
                mode, payload = 2, _enc_disk(pb, body)
            except (struct.error, DeltaError):
                mode, payload = 1, _enc_data(pb, body)
        elif pb is not None and sid != b"END ":
            mode, payload = 1, _enc_data(pb, body)
        out += [struct.pack("<4sIQBQ", sid, resv, len(body), mode, len(payload)), payload]
    return b"".join(out)


def decode(parent, delta):
    """The child file for `delta`, given the parent's bytes."""
    if delta[:8] != MAGIC:
        raise DeltaError("not a snapshot delta")
    _, psecs = _parse(parent)
    pmap = {sid: body for sid, _, body in psecs}
    hl, = struct.unpack_from("<I", delta, 8)
    out = [delta[12:12 + hl]]
    o = 12 + hl
    cnt, = struct.unpack_from("<I", delta, o)
    o += 4
    for _ in range(cnt):
        sid, resv, n, mode, pl = struct.unpack_from("<4sIQBQ", delta, o)
        o += 25
        payload = delta[o:o + pl]
        o += pl
        if mode == 0:
            body = payload
        elif mode == 1:
            body = _dec_data(pmap.get(sid, b""), n, payload)
        elif mode == 2:
            body = _dec_disk(pmap[b"DISK"], payload)
        else:
            raise DeltaError(f"unknown section mode {mode}")
        out += [struct.pack("<4sIQ", sid, resv, len(body)), body]
    return b"".join(out)
