#!/usr/bin/env bash
# probe_capture_nuttx — capture + decode health probe for the NuttX target.
#
# Same fast one-pass cortrace-decode flow as probe_capture.sh, but with the
# NuttX operating point baked in so we never trip the 400/150 = 2.67x cycle-
# time skew:
#
#   - SYSCLK  = 150 MHz  (NuttX nucleo-h743zi board.h: PLL1P. Lowered from the
#                         initial 400 MHz because full-rate ETM at 400 MHz
#                         saturated the 50 MB/s port and overflowed the ETF.)
#                         Cycle-count -> ns uses this.
#   - TRACECK = 50 MHz   (pll1_r_ck 100 MHz / 2 DDR), so the 4-bit port ceiling
#                         is 50 MB/s. Unchanged by the sysclk drop (R adjusted
#                         to keep TRACECLKIN at 100 MHz).
#   - ELF     = nuttx_test/nuttx/nuttx  (thread-switch demo + DWT trace path)
#
# It additionally reports whether the DWT thread-switch stream (ATB ID 1) shows
# up alongside the ETM instruction stream (ATB ID 2) -- the whole point of the
# NuttX bring-up.
#
# Usage: probe_capture_nuttx.sh [secs=2]
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
ELF="${ELF:-$WS/nuttx_test/nuttx/nuttx}"
SECS="${1:-2}"
PHASE="${PHASE:-1,0}"
SYSCLK="${SYSCLK:-150000000}"   # NuttX SYSCLK (PLL1P) = 150 MHz
PORT_MBS=50.0                   # 4-bit DDR @ 50 MHz TRACECK (pll1_r_ck/2)

raw=/tmp/probe_nx.bin
syms=/tmp/probe_nx_syms.nm

if [ ! -f "$ELF" ]; then
    echo "ELF not found: $ELF" >&2
    exit 1
fi
arm-none-eabi-nm -n "$ELF" > "$syms"

python3 "$HERE/trace_ctrl.py" rearm >/dev/null 2>&1
"$HERE/stream_grab" "$NIC" "$SECS" "$raw" 256 512 > /tmp/probe_nx_grab.log 2>&1
grep -E "payload|seq-gap|ring-full" /tmp/probe_nx_grab.log

# Full decode of the ETM instruction stream (stream 2), with cycle-time using
# the NuttX 400 MHz sysclk.
out=$("$CORTRACE" "$raw" "$syms" --elf "$ELF" --raw --phase "$PHASE" \
      --cycle-time --sysclk-hz "$SYSCLK" --log-level error 2>&1)

etm=$(echo "$out"   | grep -oP 'etm=\K[0-9]+')
asyncs=$(echo "$out"| grep -oP 'A-syncs=\K[0-9]+')
begins=$(echo "$out"| grep -oP 'begins / ends\s*:\s*\K[0-9]+')
ends=$(echo "$out"  | grep -oP 'begins / ends\s*:\s*[0-9]+ / \K[0-9]+')
dropped=$(echo "$out"| grep -oP 'dropped calls\s*:\s*\K[0-9]+')
funcs=$(echo "$out" | grep -oP 'functions rendered as slices\s*:\s*\K[0-9]+')

# Separately deframe the DWT thread-switch stream (ATB ID 1) to confirm the
# two streams co-exist on the one parallel TPIU. --want-stream selects the ATB
# ID; a non-empty stream-1 byte count is the P0 pass signal.
dwt=$("$CORTRACE" "$raw" "$syms" --raw --phase "$PHASE" \
      --want-stream 1 --log-level error 2>&1 | grep -oP 'etm=\K[0-9]+')

python3 - "$etm" "$SECS" "$asyncs" "$begins" "$ends" "$dropped" "$funcs" "$PORT_MBS" "$SYSCLK" "${dwt:-0}" <<'PY'
import sys
etm,secs,asyncs,begins,ends,dropped,funcs,port,sysclk,dwt=sys.argv[1:]
etm=int(etm or 0); secs=float(secs); port=float(port); dwt=int(dwt or 0)
rate=etm/secs/1e6
print("\n=== NuttX decode health (SYSCLK %s MHz) ===" % (int(sysclk)//1000000))
print(f"  ETM rate     : {rate:.1f} MB/s  ({100*rate/port:.0f}% of {port:.1f} MB/s port)")
print(f"  A-syncs      : {asyncs}")
print(f"  balanced     : {'YES' if begins==ends else 'NO'}  ({begins}/{ends})")
print(f"  dropped calls: {dropped}   <-- must be 0")
print(f"  functions    : {funcs}")
print(f"  DWT stream1  : {dwt} bytes  <-- thread-switch packets (must be > 0)")
ok = (begins==ends) and (dropped=='0') and dwt>0
print(f"  VERDICT      : {'PASS' if ok else 'FAIL'}")
PY
