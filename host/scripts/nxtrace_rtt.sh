#!/usr/bin/env bash
# nxtrace_rtt.sh -- resident openocd session for the H743 NuttX bring-up:
#   * arms ETM + DWT thread-switch trace from the DAP (nxtrace_dap.cfg)
#   * serves SEGGER RTT up-channel(s) over TCP so nxtrace can read NuttX notes
#
# Symbol addresses (g_running_tasks, _SEGGER_RTT) move on every rebuild, so they
# are read from the ELF each time -- never hard-code them.
#
# Keep this running: it holds C_DEBUGEN (NuttX then won't clear the DWT) and
# polls the RTT buffers. Ctrl-C to stop.
#
# usage: [NX_ETM=0] nxtrace_rtt.sh <nuttx.elf> [rtt_tcp_port=9091] [rtt_channel=1]
#   NX_ETM=0 holds the ETM disabled (ITM/DWT-only capture, to test the note
#   path without ETM contention on the TPIU).
#   NuttX must be built with CONFIG_NOTE_RTT=y, CONFIG_NOTE_RTT_CHANNEL=<channel>
#   and CONFIG_DRIVERS_NOTE_MAX>=2 when NoteRAM is also enabled.
set -eu
ELF="${1:?usage: $0 <nuttx.elf> [port] [channel]}"
PORT="${2:-9091}"
CHAN="${3:-1}"
HERE="$(dirname "$(readlink -f "$0")")"
NM="${NM:-arm-none-eabi-nm}"

sym() { "$NM" "$ELF" | awk -v s="$1" '$3==s {print "0x"$1; exit}'; }
G="$(sym g_running_tasks)"
RTT="$(sym _SEGGER_RTT)"
[ -n "$G" ] && [ -n "$RTT" ] || { echo "missing g_running_tasks/_SEGGER_RTT in $ELF" >&2; exit 1; }
echo "g_running_tasks=$G  _SEGGER_RTT=$RTT  rtt tcp :$PORT ch$CHAN" >&2

pkill -9 openocd 2>/dev/null || true

# NOTE: the RTT control block is (re)initialised by NuttX during boot, and
# openocd reads the channel table only once at `rtt start`, so RTT is set up
# after a short run (sleep 1500) -- starting it right after reset sees up=0.
#
# RTT throughput is set by OpenOCD's server poll_period (default 100 ms; each
# wake moves ~2 KB), NOT by the probe: 100 ms -> ~20 KB/s, 10 ms -> ~124 KB/s,
# 2 ms -> ~224 KB/s (raw SWD memory read on this DAPLink is ~290 KB/s).
#
# 480 kHz + vector catch gets the H7 halted at reset reliably; then run fast so
# RTT polling keeps up with the note rate.
exec openocd -f interface/cmsis-dap.cfg -f target/stm32h7x.cfg -f "$HERE/nxtrace_dap.cfg" \
    -c "adapter speed 480" \
    -c "reset_config srst_only srst_nogate connect_assert_srst" \
    -c init \
    -c "cortex_m vector_catch reset" \
    -c "proc nx_reset_halt {} { for {set i 0} {\$i < 4} {incr i} { if {![catch {reset halt}]} { return } ; echo \"nx_reset_halt: retry \$i\" } ; error \"reset halt failed\" }" \
    -c "nx_reset_halt" \
    -c "adapter speed 4000" \
    -c "set NX_ETM_ENABLE ${NX_ETM:-1}" \
    -c "nxtrace_arm $G" \
    -c "sleep 1500" \
    -c "rtt setup $RTT 0x400 \"SEGGER RTT\"" \
    -c "poll_period 2" \
    -c "rtt polling_interval 2" \
    -c "rtt start" \
    -c "rtt server start $PORT $CHAN" \
    -c "nxtrace_run"
