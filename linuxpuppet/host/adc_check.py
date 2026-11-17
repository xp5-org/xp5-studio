#!/usr/bin/env python3
"""Validation of 86Box's SB16 INPUT path (ADC) for the linuxpuppet snd personality.

The host injects a known two-tone signal into the VM's capture device (the PipeWire null sink box1_mic, which the VM
reads as box1_mic.monitor): 1000 Hz on the left channel, 1800 Hz on the right, 12000/32768 full scale.  The guest
records it with `pp_snd rec` and the raw file comes back with `linuxpuppet get`.  This script judges the recording.

  gen  --out DIR            tone.wav (the injected signal, long enough for the whole matrix)
  check --raw F --src S --bits B --rate R [--label NAME]
        S = mic | linel | liner | linelr | stereo | stereox.  Per recorded channel: the wanted tone must be present, the
        unwanted tone must be rejected by the channel separation limit of the sample width, the record must not clip,
        must not have DC, and must not have holes (steps).   Prints "ADC <LABEL> <CHECK> PASS|FAIL ..." lines, last
        line "ADC <LABEL> DONE RESULT=PASS|FAIL".
  wav  --raw F --out W      16-bit stereo raw -> wav (for ofdm_check.py check)

Limits are physical, not tuned: an 8-bit sample has a quantisation floor of about -48 dBFS rms per bin spread, so the
unwanted tone has to be at least 30 dB below the wanted one for 8-bit and 40 dB for 16-bit (the loopback itself
measures about -99 dB L->R on the output side, so the hardware path leaves a lot of room).
"""
import argparse, math, struct, sys, wave
import numpy as np

HZ_L, HZ_R, AMP = 1000.0, 1800.0, 12000.0
RATE_INJECT = 44100
WANT = {                      # source -> per recorded channel: set of tones it must carry
    "linel":   [{HZ_L}, {HZ_L}],
    "liner":   [{HZ_R}, {HZ_R}],
    "linelr":  [{HZ_L, HZ_R}, {HZ_L, HZ_R}],
    "mic":     [{HZ_L, HZ_R}, {HZ_L, HZ_R}],
    "stereo":  [{HZ_L}, {HZ_R}],
    "stereox": [{HZ_R}, {HZ_L}],
}


def cmd_gen(a):
    secs = a.secs
    t = np.arange(int(secs * RATE_INJECT)) / RATE_INJECT
    st = np.stack([AMP * np.sin(2 * np.pi * HZ_L * t), AMP * np.sin(2 * np.pi * HZ_R * t)], 1)
    with wave.open(a.out + "/tone.wav", "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE_INJECT)
        w.writeframes(np.round(st).astype("<i2").tobytes())
    print(f"gen {secs} s -> {a.out}/tone.wav")


def load_raw(path, bits, stereo):
    b = open(path, "rb").read()
    if bits == 8:
        x = (np.frombuffer(b, dtype=np.uint8).astype(float) - 128.0) / 128.0
    else:
        b = b[: len(b) & ~1]
        x = np.frombuffer(b, dtype="<i2").astype(float) / 32768.0
    if stereo:
        x = x[: len(x) & ~1]
        return [x[0::2], x[1::2]]
    return [x, x]


def tone_amp(x, rate, hz):
    """Peak amplitude of the tone at hz (Hann-windowed single-bin DFT, band +-3 bins)."""
    n = len(x)
    w = np.hanning(n)
    spec = np.fft.rfft(x * w)
    k = hz * n / rate
    lo, hi = int(k) - 3, int(k) + 4
    mag = np.abs(spec[lo:hi])
    return 2.0 * mag.max() / np.sum(w)


def cmd_check(a):
    stereo = a.src in ("stereo", "stereox")
    chans = load_raw(a.raw, a.bits, stereo)
    rate = a.rate
    label = a.label or a.src
    skip = int(0.5 * rate)
    ok_all = True

    def rep(name, ok, text):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(f"ADC {label} {name} {'PASS' if ok else 'FAIL'} {text}")

    need = 1.0 * rate
    if len(chans[0]) < skip + need:
        rep("LENGTH", False, f"only {len(chans[0])} frames recorded")
        print(f"ADC {label} DONE RESULT=FAIL 1")
        return 1
    rej_limit = 30.0 if a.bits == 8 else 40.0
    for ci in range(2 if stereo else 1):
        x = chans[ci][skip:]
        names = "LR"[ci] if stereo else "M"
        want = WANT[a.src][ci]
        amps = {hz: tone_amp(x, rate, hz) for hz in (HZ_L, HZ_R) if hz < rate / 2 * 0.95}
        mx = max(amps.values())
        floor = np.median(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * 2.0 / np.sum(np.hanning(len(x)))
        for hz in (HZ_L, HZ_R):
            if hz not in amps:
                continue
            if hz in want:
                lvl = 20 * math.log10(max(amps[hz], 1e-9))
                snr = 20 * math.log10(max(amps[hz], 1e-9) / max(floor, 1e-9))
                rep(f"{names}_{int(hz)}_PRESENT", snr >= 40.0, f"level {lvl:.1f} dBFS, {snr:.1f} dB over the median bin")
            else:
                wmax = max(amps[h] for h in want if h in amps) if any(h in amps for h in want) else mx
                rej = 20 * math.log10(max(wmax, 1e-9) / max(amps[hz], 1e-9))
                rep(f"{names}_{int(hz)}_REJECT", rej >= rej_limit, f"unwanted tone {rej:.1f} dB under the wanted (limit {rej_limit:.0f})")
        peak = np.max(np.abs(x))
        rep(f"{names}_CLIP", peak < 0.98 if a.bits == 16 else peak < 0.99, f"peak {peak:.3f} FS")
        dc = float(np.mean(x))
        rep(f"{names}_DC", abs(dc) < 0.01, f"mean {dc:+.4f} FS")
        # holes: a sine's adjacent-sample step is at most A*2*sin(pi*f/fs) for the sum of the present tones
        fmax = max(want)
        a_tot = sum(amps.get(h, 0.0) for h in want)
        step_lim = a_tot * 2 * math.sin(math.pi * fmax / rate) * 1.25 + (2.0 / (128.0 if a.bits == 8 else 32768.0)) * 4
        step = float(np.max(np.abs(np.diff(x))))
        rep(f"{names}_STEP", step <= step_lim, f"max step {step:.4f} FS, limit {step_lim:.4f}")
    print(f"ADC {label} DONE RESULT={'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


def cmd_wav(a):
    b = open(a.raw, "rb").read()
    b = b[: len(b) & ~3]
    with wave.open(a.out, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(a.rate)
        w.writeframes(b)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen"); g.add_argument("--out", required=True); g.add_argument("--secs", type=float, default=200.0)
    c = sub.add_parser("check")
    c.add_argument("--raw", required=True); c.add_argument("--src", required=True, choices=sorted(WANT))
    c.add_argument("--bits", type=int, required=True, choices=(8, 16)); c.add_argument("--rate", type=int, required=True)
    c.add_argument("--label")
    w = sub.add_parser("wav"); w.add_argument("--raw", required=True); w.add_argument("--out", required=True)
    w.add_argument("--rate", type=int, default=44100)
    a = ap.parse_args()
    r = {"gen": cmd_gen, "check": cmd_check, "wav": cmd_wav}[a.cmd](a)
    sys.exit(r or 0)


if __name__ == "__main__":
    main()
