#!/bin/bash
# Host half of the puppet snd OFDM validation: record the 86Box sink (box1_out.monitor) while the guest streams the
# reference file through its SB16 ring, leaving the VM alone (every RMM look pauses it).
# Usage: snd_ofdm_session.sh '<puppet cli>' <out_dir> <mic_echo prereqs dir>
set -u
LPC="$1"; OUT="$2"; MIC="$3"
rm -f "$OUT"/ofdm.wav "$OUT"/play.log "$OUT"/ofdm_capture.log
bash "$MIC/capture_audio_probe.sh" box1_out.monitor 9 "$OUT/ofdm.wav" > "$OUT/ofdm_capture.log" 2>&1 &
CP=$!
sleep 1.5
eval "$LPC --json '$OUT/play.json' run --hold 5 --run-timeout 60 --expect-exit 0 --expect-out 'SND PLAY END' \
      /pp/snd/bin/pp_snd play file=/pp/tmp/ofdm.pcm rate=44100" > "$OUT/play.log" 2>&1
echo "lp exit $?" >> "$OUT/play.log"
wait $CP
