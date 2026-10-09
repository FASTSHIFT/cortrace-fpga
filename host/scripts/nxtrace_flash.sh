#!/usr/bin/env bash
# nxtrace_flash.sh -- flash an H743 image reliably (the plain `board.sh flash`
# often times out waiting for the core to halt).
#
# 480 kHz SWD + connect-under-reset + reset vector catch gets the halt every
# time; then the image is written and the core released.
#
# usage: nxtrace_flash.sh <nuttx.hex>
set -eu
HEX="${1:?usage: $0 <image.hex>}"
pkill -9 openocd 2>/dev/null || true
timeout 90 openocd -f interface/cmsis-dap.cfg -f target/stm32h7x.cfg \
    -c "adapter speed 480" \
    -c "reset_config srst_only srst_nogate connect_assert_srst" \
    -c init \
    -c "cortex_m vector_catch reset" \
    -c "reset halt" \
    -c "flash write_image erase $HEX" \
    -c "reset run" \
    -c shutdown > /tmp/nxtrace_flash.log 2>&1 || true
grep -E "wrote|rror" /tmp/nxtrace_flash.log || { tail -5 /tmp/nxtrace_flash.log; exit 1; }
