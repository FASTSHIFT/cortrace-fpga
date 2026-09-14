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
sudo pkill -9 -x stream_grab 2>/dev/null; sudo fuser -k 5555/udp 2>/dev/null; sleep 1
sudo "$HERE/stream_grab" "$NIC" 2 "$OUT" 256 512 2>&1 | grep -E "seq-gap|written"
python3 "$HERE/../decode/deframe_to_etm.py" "$OUT" "$ETM" 40000000 2>&1 | tail -1
python3 - "$ETM" "$TAG" <<'PY'
import sys
b=open(sys.argv[1],'rb').read()
good=bad=zc=0
for c in b:
    if c==0: zc+=1
    else:
        if c==0x80 and zc>=1:
            good+=(zc>=11); bad+=(zc<11)
        zc=0
tot=good+bad
print(f"[{sys.argv[2]}] etm={len(b)}B  good-async={good} bad-async={bad} bad%={100*bad/max(1,tot):.2f}%")
PY
