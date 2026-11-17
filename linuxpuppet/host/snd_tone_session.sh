#!/bin/bash
# Host half of the puppet snd tone validation. One session, because the measurements must all happen while the
# guest's pp_snd tone is playing and the puppet must not be touched meanwhile (every RMM look pauses the VM and
# a paused DAC is an underrun in the capture):
#   1. EXEC pp_snd tone (stereo 1000 / 1800 Hz) and leave the VM alone for --hold seconds
#   2. record the 86Box sink monitor (box1_out.monitor)  -> tone.wav       (glitch analysis reads it)
#   3. per-channel crosstalk on the same sink            -> crosstalk.log
#   4. after the tone ends: idle DC of the sink          -> idledc.log
# Usage: snd_tone_session.sh '<puppet cli>' <out_dir> <mic_echo prereqs dir>
# Writes <out_dir>/session.done when finished (success or not); the testlist steps read the logs.
set -u
LPC="$1"; OUT="$2"; MIC="$3"
SRC=box1_out.monitor
mkdir -p "$OUT"; rm -f "$OUT"/session.done "$OUT"/tone.* "$OUT"/crosstalk.log "$OUT"/idledc.log "$OUT"/capture.log

eval "$LPC --json '$OUT/tone.json' run --hold 17 --run-timeout 90 --expect-exit 0 --expect-out 'SND TONE END' \
      /pp/snd/bin/pp_snd tone hz=1000 secs=16 rate=44100 amp=12000" > "$OUT/tone.log" 2>&1 &
LP=$!
sleep 2.5                                            # EXEC done, tone running
bash "$MIC/capture_audio_probe.sh" $SRC 9 "$OUT/tone.wav" > "$OUT/capture.log" 2>&1
bash "$MIC/check_output_crosstalk.sh" $SRC 4 1000 1800 > "$OUT/crosstalk.log" 2>&1
wait $LP; echo "lp exit $?" >> "$OUT/tone.log"
bash "$MIC/check_idle_dc.sh" $SRC 4 > "$OUT/idledc.log" 2>&1
touch "$OUT/session.done"
