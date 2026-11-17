#!/bin/bash
# Host half of the puppet snd INPUT (ADC) validation.  The host plays a known stereo signal into the PipeWire null sink
# box1_mic, which the VM reads as its capture device (sound_input_dev_name = box1_mic.monitor); the guest records it
# with `pp_snd rec`; the raw file comes back with `get` and host/adc_check.py judges it.  Every guest recording runs with
# --hold so the VM is left alone while the ADC runs (an RMM look pauses the VM).
#   matrix:  the source-select / width / rate cases of the DOS echo box (86box_mic_echo __testlist__86box_micecho_*)
#   ofdm:    the OFDM reference stream through the ADC, judged by ofdm_check.py (acquisition, jitter, phase, impulses)
# Usage: snd_adc_session.sh '<puppet cli>' <out_dir> <host dir>
# Writes <out_dir>/adc.done when finished; <out_dir>/adc_matrix.log and adc_ofdm.log carry the verdicts.
set -u
LPC="$1"; OUT="$2"; HOST="$3"
D="$OUT/adc"; mkdir -p "$D"; rm -f "$OUT"/adc.done "$D"/*.raw "$D"/*.log
CHK="python3 $HOST/adc_check.py"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:$XDG_RUNTIME_DIR/pulse/native}"

$CHK gen --out "$D" --secs 400 > "$D/gen.log" 2>&1
paplay --device=box1_mic "$D/tone.wav" > "$D/inject.log" 2>&1 &
INJ=$!
sleep 1

fails=0
# label src bits rate
CASES="linel8_22050,linel,8,22050 liner8_22050,liner,8,22050 mic8_22050,mic,8,22050 linelr8_22050,linelr,8,22050
 stereo8_44100,stereo,8,44100 stereo8_22050,stereo,8,22050 stereo16_22050,stereo,16,22050 stereox8_22050,stereox,8,22050
 stereo16_44100,stereo,16,44100 linel16_22050,linel,16,22050 linel8_44100,linel,8,44100 linel8_11025,linel,8,11025
 linel8_8000,linel,8,8000"
: > "$OUT/adc_matrix.log"
for c in $CASES; do
    IFS=, read -r label src bits rate <<< "$c"
    eval "$LPC --json '$D/$label.json' run --hold 5 --run-timeout 60 --expect-exit 0 --expect-out 'SND REC END' \
        /pp/snd/bin/pp_snd rec file=/pp/tmp/adc.raw src=$src bits=$bits rate=$rate secs=3" > "$D/$label.run.log" 2>&1
    rc=$?
    eval "$LPC get /pp/tmp/adc.raw '$D/$label.raw'" >> "$D/$label.run.log" 2>&1 || rc=1
    grep -h "SND REC" "$D/$label.run.log" | sed "s/^/guest $label: /" >> "$OUT/adc_matrix.log"
    if [ $rc -ne 0 ] || [ ! -s "$D/$label.raw" ]; then
        echo "ADC $label RUN FAIL (lp exit $rc)" >> "$OUT/adc_matrix.log"; fails=$((fails+1)); continue
    fi
    $CHK check --raw "$D/$label.raw" --src "$src" --bits "$bits" --rate "$rate" --label "$label" >> "$OUT/adc_matrix.log" 2>&1 \
        || fails=$((fails+1))
done
echo "ADC MATRIX DONE FAILS=$fails" >> "$OUT/adc_matrix.log"
kill $INJ 2>/dev/null; wait $INJ 2>/dev/null
sleep 1

# OFDM reference through the ADC: stereo 16-bit 44100, the reference played into the sink 1.5 s after the guest arms
: > "$OUT/adc_ofdm.log"
eval "$LPC --json '$D/ofdm.json' run --hold 8.5 --run-timeout 60 --expect-exit 0 --expect-out 'SND REC END' \
    /pp/snd/bin/pp_snd rec file=/pp/tmp/adc.raw src=stereo bits=16 rate=44100 secs=7" > "$D/ofdm.run.log" 2>&1 &
LP=$!
sleep 3
paplay --raw --rate=44100 --format=s16le --channels=2 --device=box1_mic "$OUT/ofdm_ref/ref.pcm" >> "$D/inject.log" 2>&1
wait $LP; echo "lp exit $?" >> "$D/ofdm.run.log"
eval "$LPC get /pp/tmp/adc.raw '$D/ofdm.raw'" >> "$D/ofdm.run.log" 2>&1
grep -h "SND REC" "$D/ofdm.run.log" >> "$OUT/adc_ofdm.log"
$CHK wav --raw "$D/ofdm.raw" --out "$D/ofdm.wav" >> "$OUT/adc_ofdm.log" 2>&1
python3 "$HOST/ofdm_check.py" check --wav "$D/ofdm.wav" --ref "$OUT/ofdm_ref" >> "$OUT/adc_ofdm.log" 2>&1
touch "$OUT/adc.done"
