# AGENT.md — cortrace-fpga working conventions

## What this repo is
Artix-7 FPGA capture of a Cortex-M parallel ETM trace port, streamed over UDP
to the host, decoded by cortrace. See README.md and docs/00-migration-plan.md.

## Hardware / environment
- **FPGA**: A7-Lite, `xc7a35tfgg484-2`. Program via FT232H (`0403:6014`):
  `openFPGALoader -c ft232 --fpga-part xc7a35tfgg484 <bit>` (SRAM, volatile;
  add `-f` to write QSPI flash so it survives power-off).
- **FPGA IP = 192.168.10.42** (answers ARP only, no ICMP — normal).
- **Trace NIC** (direct-attached, e.g. `enxc8a36266dcae`) MUST hold
  **192.168.10.245**; the FPGA streams to `.245:5555`. After reboot:
  `sudo ip addr add 192.168.10.245/24 dev <nic>`. Wrong host IP => ARP ok,
  discover ok, but `:5555` stream is 0 bytes.
- **stream_grab without sudo**: `sudo setcap 'cap_net_raw,cap_net_admin+ep'
  host/scripts/stream_grab` (needs bind-to-device + large SO_RCVBUF). Also
  `sudo sysctl -w net.core.rmem_max=268435456` if not using caps.
- **Vivado 2021.1** at `/home/vifextech/tools/Vivado/2021.1/settings64.sh`.
  The DDR3/clock IP `.xci` are pinned to this version.
- **STM32 target**: firmware in the sibling `stm32h743-etm-trace-firmware`
  repo. OpenOCD reliable halt while selftrace runs: connect under reset —
  `-c "reset_config srst_only connect_assert_srst"` then `reset halt`.
- Port :5555 often held by a stale grab: `sudo fuser -k 5555/udp` or
  `pkill -9 -x stream_grab`.

## Build discipline
- Long Vivado builds: run in the background with the log redirected to a file;
  don't repeatedly foreground `vivado | grep`. Sweep parameters inside one
  vivado process, not one process per point.

## Verify before shipping
- RTL: `python3 sim/run_verilog_tests.py` (must be all-pass; la_ddr_ring TEST F
  gates the -1024 burst-reorder fix, prbs_cdc_pack gates the lane-skew fix).
- Host: `cd host/decode && python3 -m pytest`.
- Board byte-exact: `host/scripts/prbs_soak.py --minutes N` (framed PRBS, 0
  byte errors expected).
- Board end-to-end: `host/scripts/e2e_soak.py --minutes N` (real trace through
  cortrace: 0 fatal, balanced, 0 dropped calls).

## Git
- Author `VIFEX <vifextech@foxmail.com>` (set as repo-local config once).
- Conventional-Commit subjects enforced by `.githooks/commit-msg`
  (`scripts/install-hooks.sh` wires `core.hooksPath`). English commit messages.
- Never write scratch files under `.git/`. Write commit messages with
  `git commit -m` in the terminal.
- Push is the user's job.

## Key finding baked into the design
The STM32 TPIU drives the parallel port **centre-aligned** (data stable around
the TRACECLK edge, LA-confirmed), so the capture is plain IBUF→IDDR edge
sampling with **no IDELAY** — frequency-independent, one bitstream for any
TRACECLK the board SI supports (<=56 MHz pin clean; 112 MHz is board-SI-limited,
not a capture-logic limit). Do not re-introduce IDELAY taps.
