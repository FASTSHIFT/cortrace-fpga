#!/usr/bin/env bash
# variant_check.sh — start a selftrace ETM variant over the UART CLI, capture
# 2s of FPGA trace, deframe, and report A-sync health. Usage:
#   variant_check.sh <tag> <bb> <stall> <systick>
set -u
TAG="$1"; BB="$2"; STALL="$3"; ST="$4"
SER=/dev/serial/by-id/usb-Arm_DAPLink_CMSIS-DAP_5b171bbdf930feea01-if00
NIC=enxc8a36266dcae
OUT=/tmp/real_${TAG}.bin
ETM=/tmp/etm_${TAG}.bin
HERE="$(dirname "$0")"

echo "=== variant $TAG: bb=$BB stall=$STALL systick=$ST ==="
# drive the CLI
python3 - "$SER" "$BB" "$STALL" "$ST" <<'PY'
import serial,sys,time
ser,bb,stall,st=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
s=serial.Serial(ser,115200,timeout=0.3); s.reset_input_buffer(); time.sleep(0.2)
# go idle first (clean re-setup), then start the variant
s.write(b"run idle\r\n"); s.flush(); time.sleep(0.3); s.read(2000)
cmd=f"run selftrace --bb {bb} --stall {stall} --systick {st}\r\n"
s.write(cmd.encode()); s.flush(); time.sleep(0.5)
print(s.read(2000).decode('latin1').strip().splitlines()[-1] if s.in_waiting or True else "")
s.close()
PY
sleep 0.5
pkill -9 -x cortrace-grab 2>/dev/null; fuser -k 5555/udp 2>/dev/null; sleep 1
cortrace-grab "$NIC" 2 "$OUT" 256 512 2>&1 | grep -E "seq-gap|written"
# Deframe + A-sync health: cortrace-decode --raw searches the nibble phase and
# --dump-etm writes the winning ETM stream, which is scored for A-sync here.
# It needs the firmware ELF (ELF=...) like any cortrace-decode run.
: "${ELF:?set ELF to the firmware ELF the target is running}"
DECODE="${CORTRACE_DECODE:-$HERE/../../../cortrace/build-rel/cortrace-decode}"
SYMS=/tmp/syms_${TAG}.nm
arm-none-eabi-nm -n "$ELF" > "$SYMS"
head -c 40000000 "$OUT" > "$OUT.head"
"$DECODE" "$OUT.head" "$SYMS" --elf "$ELF" --raw --dump-etm "$ETM" --memory-limit-mb 0 \
    >/dev/null 2>"$ETM.log" || { echo "cortrace-decode failed, see $ETM.log"; exit 1; }
PHASE=$(grep -oE "phase=\(parity=[0-9]+,order=[0-9]+\)" "$ETM.log" | head -1)
python3 - "$ETM" "$TAG" "$PHASE" <<'PY'
import sys
etm = open(sys.argv[1], "rb").read()
good = bad = zc = 0
for c in etm:
    if c == 0:
        zc += 1
    else:
        if c == 0x80 and zc >= 1:
            good += (zc >= 11); bad += (zc < 11)
        zc = 0
tot = good + bad
print(f"[{sys.argv[2]}] etm={len(etm)}B {sys.argv[3]} "
      f"good-async={good} bad-async={bad} bad%={100*bad/max(1,tot):.2f}%")
PY
