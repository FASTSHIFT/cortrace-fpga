#!/usr/bin/env bash
# probe_capture — fast capture + decode health probe. Captures <secs> of live
# trace and decodes it in ONE cortrace-decode --raw --elf pass (C++; no slow
# Python deframe), then prints the generation rate and the decode-health
# numbers that matter for a bring-up: ETM rate vs the port ceiling, dropped
# calls, balance, exceptions, cycle-count span.
#
# Usage: probe_capture.sh [secs=2] [--elf <fw.elf>]
set -u
HERE="$(dirname "$(readlink -f "$0")")"
REPO="$(readlink -f "$HERE/../..")"
WS="$(readlink -f "$REPO/..")"
# Prefer the Release build (OpenCSD decode is ~5x faster than the Debug build).
if [ -n "${CORTRACE_DECODE:-}" ]; then
    CORTRACE="$CORTRACE_DECODE"
elif [ -x "$WS/cortrace/build-rel/cortrace-decode" ]; then
    CORTRACE="$WS/cortrace/build-rel/cortrace-decode"
else
    CORTRACE="$WS/cortrace/build/cortrace-decode"
fi
NIC="${NIC:-enxc8a36266dcae}"
ELF="${ELF:-$WS/stm32h743-etm-trace-firmware/build/H743_Blink.elf}"
SECS="${1:-2}"
PHASE="${PHASE:-1,0}"
PORT_MBS=56.25   # 4-bit DDR @ 56.25 MHz TRACECLK

raw=/tmp/probe.bin
syms=/tmp/probe_syms.nm
arm-none-eabi-nm -n "$ELF" > "$syms"

python3 "$HERE/trace_ctrl.py" rearm >/dev/null 2>&1
"$HERE/stream_grab" "$NIC" "$SECS" "$raw" 256 512 > /tmp/probe_grab.log 2>&1
grep -E "payload|seq-gap|ring-full" /tmp/probe_grab.log

out=$("$CORTRACE" "$raw" "$syms" --elf "$ELF" --raw --phase "$PHASE" \
      --cycle-time --sysclk-hz 150000000 --log-level error 2>&1)

etm=$(echo "$out"   | grep -oP 'etm=\K[0-9]+')
asyncs=$(echo "$out"| grep -oP 'A-syncs=\K[0-9]+')
begins=$(echo "$out"| grep -oP 'begins / ends\s*:\s*\K[0-9]+')
ends=$(echo "$out"  | grep -oP 'begins / ends\s*:\s*[0-9]+ / \K[0-9]+')
dropped=$(echo "$out"| grep -oP 'dropped calls\s*:\s*\K[0-9]+')
mism=$(echo "$out"  | grep -oP 'mismatched returns\s*:\s*\K[0-9]+')
exc=$(echo "$out"   | grep -oP 'exceptions rendered\s*:\s*\K[0-9]+')
funcs=$(echo "$out" | grep -oP 'functions rendered as slices\s*:\s*\K[0-9]+')
ccyc=$(echo "$out"  | grep -oP 'cycle counts\s*:\s*[0-9]+\s*\(\K[0-9]+')

python3 - "$etm" "$SECS" "$asyncs" "$begins" "$ends" "$dropped" "$mism" "$exc" "$funcs" "$ccyc" "$PORT_MBS" <<'PY'
import sys
etm,secs,asyncs,begins,ends,dropped,mism,exc,funcs,ccyc,port=sys.argv[1:]
etm=int(etm or 0); secs=float(secs); port=float(port)
rate=etm/secs/1e6
print("\n=== decode health ===")
print(f"  ETM rate     : {rate:.1f} MB/s  ({100*rate/port:.0f}% of {port:.1f} MB/s port)")
print(f"  A-syncs      : {asyncs}")
print(f"  balanced     : {'YES' if begins==ends else 'NO'}  ({begins}/{ends})")
print(f"  dropped calls: {dropped}   <-- must be 0")
print(f"  mismatched   : {mism}")
print(f"  exceptions   : {exc}")
print(f"  functions    : {funcs}")
if ccyc:
    print(f"  cpu cycles   : {ccyc}  ({int(ccyc)/150e6:.3f} s @150MHz vs {secs:.0f}s window)")
ok = (begins==ends) and (dropped=='0')
print(f"  VERDICT      : {'PASS' if ok else 'FAIL'}")
PY
