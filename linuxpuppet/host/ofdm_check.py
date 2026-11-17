#!/usr/bin/env python3
"""OFDM-probe validation of 86Box's SB16 OUTPUT path, for the linuxpuppet snd personality.

  gen   build the reference stream with the OFDM engine itself (the vendored C core of 86box_mic_echo/prereqs/ofdm_dos,
        the same sources the DOS tools and analyze_ofdm_probe_wav.py use): REP copies of the channel probe separated by
        digital silence, 16-bit stereo (probe on both channels), at the sink's 44100 Hz.  Writes ref.pcm (what the guest
        plays), ref.json (geometry, offsets) in --out.
  check analyse the host recording of the sink against that reference.  Checks, each one line "OFDM <NAME> PASS|FAIL ...":
        ACQ      the engine finds every probe copy (valid, coherence >= 0.9)
        SNR      mean per-carrier SNR from the engine's estimate on the capture (clean digital path: high)
        JITTER   copy-to-copy spacing error and the within-probe symbol timing (pilot-phase slope), in samples
        PHASE    per-carrier phase error against the reference after removing the common delay, and L-R channel skew
        IMPULSE  residual after a 3-tap fit of the reference, per 16-sample block relative to the local signal: bursts (pops / ticks) stand out of the steady distortion
        GAPS     the silence between copies stays silent (no DC, no ticks)
Exit status 1 if any check fails.  The algorithm is the engine's own; what these measure is the sample path
(guest DMA ring -> SB16 emulation -> OpenAL -> PipeWire -> parecord).
"""
import argparse, ctypes, json, math, os, sys, wave
import numpy as np

MIC = "/testsrc/sourcedir/86box_mic_echo/prereqs"
sys.path.insert(0, MIC)
import analyze_ofdm_probe_wav as A          # OfdmCfg, ProbeInfo, build_lib, load_ini, ECC/CHUNK/SEQ

RATE = 44100
GEOM = dict(n_fft=256, cp_ratio=0.25, freq_lo=1000.0, freq_hi=8000.0, pilot_spacing=8, sample_rate=RATE,
            taper_ms=2.0, frame_gap_ms=20.0, n_probe_syms=48, n_meas_syms=16, sync_period=32)
REP = 4
GAP_S = 0.30
PEAK = 12000.0                              # int16 peak of the probe


def lib_and_cfg():
    lib = ctypes.CDLL(A.build_lib(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ofdm_hostlib")))
    dp = ctypes.POINTER(ctypes.c_double)
    lib.ofdm_cfg_build.restype = ctypes.c_int
    lib.ofdm_cfg_build.argtypes = [ctypes.POINTER(A.OfdmCfg), ctypes.c_int, ctypes.c_double, ctypes.c_double,
                                   ctypes.c_double, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_int]
    lib.ofdm_probe_workspace.restype = ctypes.c_size_t
    lib.ofdm_probe_workspace.argtypes = [ctypes.POINTER(A.OfdmCfg)] + [ctypes.c_int] * 3
    lib.ofdm_probe_init.restype = ctypes.c_int
    lib.ofdm_probe_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(A.OfdmCfg), ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.ofdm_probe_build.restype = ctypes.c_long
    lib.ofdm_probe_build.argtypes = [ctypes.c_void_p, dp, ctypes.c_long]
    lib.ofdm_probe_build_samples.restype = ctypes.c_long
    lib.ofdm_probe_build_samples.argtypes = [ctypes.c_void_p]
    lib.ofdm_probe_estimate.restype = ctypes.c_int
    lib.ofdm_probe_estimate.argtypes = [ctypes.c_void_p, dp, ctypes.c_int, ctypes.c_double,
                                        dp, dp, dp, dp, dp, ctypes.POINTER(A.ProbeInfo)]
    g = GEOM
    cfg = A.OfdmCfg()
    rc = lib.ofdm_cfg_build(ctypes.byref(cfg), g["n_fft"], g["cp_ratio"], g["freq_lo"], g["freq_hi"],
                            g["pilot_spacing"], g["sample_rate"], A.ECC_BYTES, A.CHUNK_BYTES, A.SEQ_BYTES,
                            g["taper_ms"], g["frame_gap_ms"], g["sync_period"])
    if rc:
        sys.exit(f"ofdm_cfg_build rc={rc}")
    return lib, cfg


def new_probe(lib, cfg, max_cap):
    ws = lib.ofdm_probe_workspace(ctypes.byref(cfg), GEOM["n_probe_syms"], GEOM["n_meas_syms"], max_cap)
    if not ws:
        sys.exit("ofdm_probe_workspace rejected the geometry")
    mem = ctypes.create_string_buffer(ws)
    p = ctypes.c_void_p()
    if lib.ofdm_probe_init(ctypes.byref(p), mem, ws, ctypes.byref(cfg), GEOM["n_probe_syms"],
                           GEOM["n_meas_syms"], max_cap):
        sys.exit("ofdm_probe_init failed")
    return p, mem


def cmd_gen(a):
    lib, cfg = lib_and_cfg()
    p, mem = new_probe(lib, cfg, 400000)
    n = lib.ofdm_probe_build_samples(p)
    buf = (ctypes.c_double * n)()
    got = lib.ofdm_probe_build(p, buf, n)
    if got != n:
        sys.exit(f"ofdm_probe_build wrote {got} of {n}")
    probe = np.ctypeslib.as_array(buf).copy()
    probe *= PEAK / np.max(np.abs(probe))
    lead = int(0.60 * RATE)      # silence first: the stream start (OpenAL/PipeWire settling) must not land on the signal
    gap = int(GAP_S * RATE)
    starts, parts, pos = [], [np.zeros(lead)], lead
    for k in range(REP):
        starts.append(pos)
        parts.append(probe)
        pos += n
        parts.append(np.zeros(gap))
        pos += gap
    mono = np.concatenate(parts)
    st = np.empty(len(mono) * 2, dtype="<i2")
    st[0::2] = st[1::2] = np.round(mono).astype("<i2")
    os.makedirs(a.out, exist_ok=True)
    st.tofile(os.path.join(a.out, "ref.pcm"))
    np.save(os.path.join(a.out, "ref_probe.npy"), probe)
    json.dump({"rate": RATE, "geom": GEOM, "probe_samples": int(n), "starts": starts, "total": int(len(mono)),
               "peak": PEAK}, open(os.path.join(a.out, "ref.json"), "w"))
    print(f"OFDM GEN {REP} probes of {n} samples, {len(mono)} frames ({len(mono)/RATE:.2f} s), {st.nbytes} bytes")


def xcorr_peak(rx, ref, lo, hi):
    """integer lag of ref inside rx within [lo, hi) by FFT cross-correlation"""
    seg = rx[lo:hi + len(ref)]
    nfft = 1 << int(math.ceil(math.log2(len(seg) + len(ref))))
    c = np.fft.irfft(np.fft.rfft(seg, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)[:hi - lo]
    return lo + int(np.argmax(c))


BURST_FLOOR_LSB = 8.0                       # a burst must also be audible in absolute terms: 16-bit rounding is 0.3 LSB rms
FIT_HALF = 31                                # taps -7..+7


def fit3(rx, ref, lag):
    """least-squares rx[lag+k] ~ sum_j h_j ref[k-j], j in -FIT_HALF..FIT_HALF: absorbs gain and a sub-sample delay
    across the whole 1-8 kHz band (three taps are too few for a fractional delay of a broadband probe: the fit leaves a
    burst at the sync symbol that is not a pop).  A click is not in the reference, so the fit cannot absorb it."""
    n = len(ref)
    x = rx[lag:lag + n]
    js = range(-FIT_HALF, FIT_HALF + 1)
    cols = np.stack([np.roll(ref, j) for j in js], 1)
    e = FIT_HALF
    h, *_ = np.linalg.lstsq(cols[e:-e], x[e:-e], rcond=None)
    fitted = cols[e:-e] @ h
    return x[e:-e] - fitted, h


def report(name, ok, text):
    print(f"OFDM {name} {'PASS' if ok else 'FAIL'} {text}")
    return ok


def cmd_check(a):
    ref = json.load(open(os.path.join(a.ref, "ref.json")))
    probe = np.load(os.path.join(a.ref, "ref_probe.npy"))
    with wave.open(a.wav, "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2 or w.getframerate() != RATE:
            sys.exit(f"need 16-bit stereo {RATE} Hz, got {w.getnchannels()}ch {w.getsampwidth()*8}-bit {w.getframerate()} Hz")
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
    L, R = raw[0::2], raw[1::2]
    n = len(probe)
    spacing = ref["starts"][1] - ref["starts"][0]
    ok_all = True

    # ---- ACQ / SNR: the engine on the capture -----------------------------------------------------------
    lib, cfg = lib_and_cfg()
    p, mem = new_probe(lib, cfg, len(L))
    arrs = {k: (ctypes.c_double * cfg.n_occupied)() for k in ("H_re", "H_im", "snr_lin", "snr_db", "snr_db_p")}
    info = A.ProbeInfo()
    rx = (ctypes.c_double * len(L))(*(L / PEAK * 3.0))
    rc = lib.ofdm_probe_estimate(p, rx, len(L), 0.0, arrs["H_re"], arrs["H_im"], arrs["snr_lin"], arrs["snr_db"],
                                 arrs["snr_db_p"], ctypes.byref(info))
    snr = np.array(list(arrs["snr_db"]))
    ok_all &= report("ACQ", rc == 0 and info.valid and info.coherence >= 0.9,
                     f"rc={rc} valid={info.valid} coherence={info.coherence:.3f} frame_start={info.frame_start}")
    if rc != 0 or not info.valid:
        print("OFDM DONE RESULT=FAIL 1")
        return 1
    dpp = ctypes.POINTER(ctypes.c_double)
    lib.ofdm_allocate.restype = ctypes.c_int
    lib.ofdm_allocate.argtypes = [dpp, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_int, ctypes.c_double,
                                  ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    beta = info.isi_ratio if info.has_beta else 0.0
    bits = (ctypes.c_int * cfg.n_data_carriers)()
    n_alloc = lib.ofdm_allocate(arrs["snr_db"], cfg.n_occupied, cfg.pilot_spacing, 6.0, 6, beta, bits, cfg.n_data_carriers)
    bat = list(bits)[:max(n_alloc, 0)]
    on = sum(1 for b_ in bat if b_ > 0)
    # the engine's verdict on whether this path can carry a modem: every data carrier loadable at the standard 6 dB margin
    ok_all &= report("SNR", bool(bat) and on == len(bat),
                     f"snr mean={snr.mean():.1f} min={snr.min():.1f} max={snr.max():.1f} dB; engine BAT {on}/{len(bat)} "
                     f"carriers loadable, {sum(bat)} bits/symbol (6 dB margin, max 6 bits)")
    if snr.mean() < 30.0:
        print(f"TESTRUNNER_WARN: output path SNR {snr.mean():.1f} dB is low for an all-digital path (expect > 30 dB); "
              "the deterministic error scales with the signal, which points at the emulator's output resampling")

    # ---- locate every copy: coarse from the engine's frame_start, fine by cross-correlation ------------------
    # global correlation: every copy is a peak; walk back to the earliest one at the nominal spacing
    nfft = 1 << int(math.ceil(math.log2(len(L) + n)))
    corr = np.fft.irfft(np.fft.rfft(L, nfft) * np.conj(np.fft.rfft(probe, nfft)), nfft)[:len(L) - n + 1]
    pk = int(np.argmax(corr))
    first = pk
    while first - spacing >= 0 and corr[max(0, first - spacing - 200):first - spacing + 200].max() > 0.5 * corr[pk]:
        first -= spacing
    lags = []
    for k in range(REP):
        e = first + k * spacing
        lo = max(0, e - 200)
        if lo + n > len(L) or e > len(corr):
            break
        w = corr[lo:e + 200]
        if len(w) == 0 or w.max() < 0.5 * corr[pk]:
            break
        lags.append(lo + int(np.argmax(w)))
    ok_found = len(lags) == REP
    ok_all &= report("COPIES", ok_found, f"{len(lags)}/{REP} probe copies located in the capture")
    if not lags:
        print("OFDM DONE RESULT=FAIL 1")
        return 1

    # ---- JITTER ------------------------------------------------------------------------------------------
    sp = np.diff(lags) - spacing
    sps = f"copy spacing error {list(map(int, sp))} samples" if len(sp) else "single copy"
    # within-probe timing: per-symbol delay from the pilot-phase slope against the reference symbol
    sym = cfg.symbol_samples
    nsym = n // sym
    lag0 = lags[0]
    f = np.fft.rfftfreq(cfg.n_fft, 1.0)
    bins = np.arange(cfg.bin_lo, cfg.bin_hi + 1)
    delays = []
    for s in range(2, nsym - 1):
        o = s * sym + cfg.cp_len
        a_ = np.fft.rfft(L[lag0 + o:lag0 + o + cfg.n_fft])[bins]
        b_ = np.fft.rfft(probe[o:o + cfg.n_fft])[bins]
        m = np.abs(b_) > 0.2 * np.abs(b_).max()
        if m.sum() < 8:
            continue
        ph = np.unwrap(np.angle(a_[m] * np.conj(b_[m])))
        slope = np.polyfit(bins[m], ph, 1)[0]
        delays.append(-slope * cfg.n_fft / (2 * math.pi))
    dl = np.array(delays)
    jit = float(dl.std()) if len(dl) else float("nan")
    ok_all &= report("JITTER", bool(len(sp) == 0 or np.max(np.abs(sp)) <= 1) and jit < 0.25,
                     f"{sps}; symbol delay mean={dl.mean():+.3f} std={jit:.3f} range={np.ptp(dl):.3f} samples "
                     f"(limits: spacing <= 1, std < 0.25)")

    # ---- PHASE -----------------------------------------------------------------------------------------
    errs = []
    for s in range(2, nsym - 1):
        o = s * sym + cfg.cp_len
        a_ = np.fft.rfft(L[lag0 + o:lag0 + o + cfg.n_fft])[bins]
        b_ = np.fft.rfft(probe[o:o + cfg.n_fft])[bins]
        m = np.abs(b_) > 0.2 * np.abs(b_).max()
        if m.sum() < 8:
            continue
        ph = np.unwrap(np.angle(a_[m] * np.conj(b_[m])))
        c = np.polyfit(bins[m], ph, 1)
        errs.append(np.degrees(ph - np.polyval(c, bins[m])))
    perr = np.concatenate(errs)
    prms = float(np.sqrt(np.mean(perr ** 2)))
    # channel skew: cross-spectrum phase slope between L and R over the first copy
    lr = np.fft.rfft(L[lag0:lag0 + n], 1 << 16) * np.conj(np.fft.rfft(R[lag0:lag0 + n], 1 << 16))
    fb = np.fft.rfftfreq(1 << 16, 1.0 / RATE)
    msk = (fb > GEOM["freq_lo"]) & (fb < GEOM["freq_hi"]) & (np.abs(lr) > 0.05 * np.abs(lr).max())
    skew = -np.polyfit(2 * math.pi * fb[msk], np.unwrap(np.angle(lr[msk])), 1)[0] * RATE
    ok_all &= report("PHASE", prms < 5.0 and abs(skew) < 0.1,
                     f"carrier phase residual rms={prms:.2f} deg (limit 5), L-R skew={skew:+.4f} samples (limit 0.1)")

    # ---- IMPULSE -----------------------------------------------------------------------------------------
    worst, nbad, rms_all = 0.0, 0, []
    W = 16
    for lg in lags:
        res, h = fit3(L, probe, lg)
        sig = probe[FIT_HALF:-FIT_HALF]
        m = (len(res) // W) * W
        er = np.sqrt((res[:m].reshape(-1, W) ** 2).mean(1))
        es = np.sqrt((sig[:m].reshape(-1, W) ** 2).mean(1)) + 1.0
        q = er / es                                  # distortion relative to the local signal: flat for a clean path
        med = float(np.median(q))
        z = q / (med + 1e-9)
        burst = (z > 5.0) & (er > BURST_FLOOR_LSB)
        worst = max(worst, float(z[burst].max()) if burst.any() else float(z.max()))
        nbad += int(burst.sum())
        rms_all.append(float(np.sqrt(np.mean(res ** 2))))
    ok_all &= report("IMPULSE", nbad == 0,
                     f"{nbad} 16-sample blocks with residual > 5x the median error-to-signal ratio and > {BURST_FLOOR_LSB:.0f} LSB rms, worst {worst:.1f}x, residual rms {np.mean(rms_all):.1f} "
                     f"of peak {PEAK:.0f} (fit gain {h.sum():.3f})")

    # ---- GAPS ------------------------------------------------------------------------------------------
    gaps = []
    for k in range(len(lags)):
        g0 = lags[k] + n + 200
        g1 = g0 + int(GAP_S * RATE) - 400
        if g1 <= len(L):
            gaps.append(np.concatenate([L[g0:g1], R[g0:g1]]))
    if gaps:
        gm = max(float(np.max(np.abs(x))) for x in gaps)
        gd = max(abs(float(np.mean(x))) for x in gaps)
        ok_all &= report("GAPS", gm <= 64 and gd <= 8, f"between copies max |x|={gm:.0f}, mean={gd:.1f} (limits 64 / 8)")
    print("OFDM DONE RESULT=" + ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--wav", required=True)
    c.add_argument("--ref", required=True)
    a = ap.parse_args()
    sys.exit((cmd_gen if a.cmd == "gen" else cmd_check)(a) or 0)


if __name__ == "__main__":
    main()
