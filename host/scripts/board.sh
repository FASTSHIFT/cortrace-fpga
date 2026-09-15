#!/usr/bin/env bash
# board.sh -- reliable one-shot board operations that NEVER hang.
#
# Every openocd invocation is wrapped in `timeout`, kills any stale openocd +
# frees the gdb port first, uses a self-terminating `-c shutdown`, and writes
# all output to a log file (never relies on terminal echo). No background
# openocd, no nc/tcl RPC (that protocol needs a 0x1a terminator and hangs on
# newline), no interactive prompts.
#
# Subcommands:
#   flash <fw.hex|fw.bin[@0xADDR]>   connect-under-reset, erase+program, run
#   mdw   <addr> [count]             read <count> words at <addr> (default 1)
#   poke  "<cmd>; <cmd>; ..."        run raw openocd -c commands (semicolon sep)
#   reset                            reset run
#   uart  <seconds> [pattern]        read DAPLink CDC UART; grep optional pattern
#   grab  <secs> <out.bin>           rearm FPGA + stream_grab a capture
#
# All openocd logs -> /tmp/board_openocd.log ; UART -> /tmp/board_uart.log
set -u

HERE="$(dirname "$(readlink -f "$0")")"
IFACE="${OOCD_IFACE:-interface/cmsis-dap.cfg}"
TARGET="${OOCD_TARGET:-target/stm32h7x.cfg}"
OOCD_TIMEOUT="${OOCD_TIMEOUT:-30}"
RESET_CFG='reset_config srst_only srst_nogate connect_assert_srst'
NIC="${NIC:-enxc8a36266dcae}"
UART="${UART:-/dev/ttyACM0}"
LOG=/tmp/board_openocd.log

_cleanup() {
    pkill -9 openocd 2>/dev/null
    fuser -k 3333/tcp 2>/dev/null
    sleep 1
}

# _oocd "<-c cmd>" "<-c cmd>" ...  -- run a self-terminating openocd session
_oocd() {
    _cleanup
    local args=(-f "$IFACE" -f "$TARGET")
    local c
    for c in "$@"; do args+=(-c "$c"); done
    args+=(-c shutdown)
    timeout "$OOCD_TIMEOUT" openocd "${args[@]}" > "$LOG" 2>&1
    local rc=$?
    _cleanup
    return $rc
}

cmd_flash() {
    local spec="$1" img="${1%@*}" addr=""
    case "$spec" in *@*) addr="${spec#*@}";; esac
    case "$img" in
        *.bin) [ -z "$addr" ] && addr=0x08000000
               _oocd "$RESET_CFG" "init" "reset halt" \
                     "flash write_image erase $img $addr" "reset run" ;;
        *)     _oocd "$RESET_CFG" "init" "reset halt" \
                     "flash write_image erase $img" "reset run" ;;
    esac
    grep -iE "wrote|error:|fail" "$LOG" || tail -3 "$LOG"
}

cmd_mdw() {
    local addr="$1" n="${2:-1}"
    _oocd "gdb_port disabled" "tcl_port disabled" "telnet_port disabled" \
          "init" "halt" "mdw $addr $n" "resume"
    grep -iE "0x[0-9a-f]+:" "$LOG"
}

cmd_poke() {
    # split the single arg on ';' into separate -c commands, wrapped in halt/resume
    local IFS=';' ; read -ra CMDS <<< "$1"
    local cargs=("gdb_port disabled" "tcl_port disabled" "telnet_port disabled" "init" "halt")
    local c
    for c in "${CMDS[@]}"; do c="$(echo "$c" | sed 's/^ *//;s/ *$//')"; [ -n "$c" ] && cargs+=("$c"); done
    cargs+=("resume")
    _oocd "${cargs[@]}"
    grep -iE "0x[0-9a-f]+:|error" "$LOG" || tail -5 "$LOG"
}

cmd_reset() {
    _oocd "$RESET_CFG" "init" "reset run"
    tail -2 "$LOG"
}

cmd_uart() {
    local secs="${1:-5}" pat="${2:-}"
    python3 - "$UART" "$secs" > /tmp/board_uart.log 2>&1 <<'PY'
import serial,sys,time
port,secs=sys.argv[1],float(sys.argv[2])
s=serial.Serial(port,115200,timeout=0.2)
t=time.time(); buf=b''
while time.time()-t<secs: buf+=s.read(4096)
s.close()
sys.stdout.write(buf.decode(errors="replace"))
PY
    if [ -n "$pat" ]; then grep -iE "$pat" /tmp/board_uart.log; else tail -20 /tmp/board_uart.log; fi
}

# uart_reset <secs> [pattern] -- start UART reader THEN reset, so boot banner is caught
cmd_uart_reset() {
    local secs="${1:-6}" pat="${2:-}"
    ( python3 - "$UART" "$secs" > /tmp/board_uart.log 2>&1 <<'PY'
import serial,sys,time
port,secs=sys.argv[1],float(sys.argv[2])
s=serial.Serial(port,115200,timeout=0.2)
t=time.time(); buf=b''
while time.time()-t<secs: buf+=s.read(4096)
s.close(); sys.stdout.write(buf.decode(errors="replace"))
PY
    ) & local rp=$!
    sleep 0.5
    _oocd "$RESET_CFG" "init" "reset run"
    wait $rp
    if [ -n "$pat" ]; then grep -iE "$pat" /tmp/board_uart.log; else tail -20 /tmp/board_uart.log; fi
}

cmd_grab() {
    local secs="$1" out="$2"
    python3 "$HERE/trace_ctrl.py" rearm >/dev/null 2>&1
    "$HERE/stream_grab" "$NIC" "$secs" "$out" 256 512 > /tmp/board_grab.log 2>&1
    grep -E "payload|seq-gap|ring-full" /tmp/board_grab.log
}

sub="${1:-}"; shift 2>/dev/null || true
case "$sub" in
    flash)      cmd_flash "$@" ;;
    mdw)        cmd_mdw "$@" ;;
    poke)       cmd_poke "$@" ;;
    reset)      cmd_reset "$@" ;;
    uart)       cmd_uart "$@" ;;
    uart-reset) cmd_uart_reset "$@" ;;
    grab)       cmd_grab "$@" ;;
    *) echo "usage: board.sh {flash <img[@addr]>|mdw <addr> [n]|poke \"c;c\"|reset|uart <s> [pat]|uart-reset <s> [pat]|grab <s> <out>}" ;;
esac
