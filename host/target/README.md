# target/ — OpenOCD configs for the STM32F429 trace source

OpenOCD scripts that configure the STM32 (debug target) side. Run via the
`../scripts/*.sh` wrappers, or directly with
`openocd -f interface/stlink.cfg -f target/stm32f4x.cfg -f <this>.cfg`.

- `etm_enable.cfg` — full 4-bit parallel ETM enable (GPIO AF mux + DEMCR +
  DBGMCU + TPIU + ETM). Branch broadcast OFF (ETMCR 0x880). **F429 / ETM3.5.**
- `etm_enable_h743.cfg` — full 4-bit parallel ETM enable for the **STM32H743
  (Cortex-M7 / ETMv4)**. **Hardware-verified 2026-07-05** (all readbacks + all 4
  data lanes toggling on a logic analyser). Different register model
  (TRCPRGCTLR/TRCCONFIGR/TRCVICTLR at 0xE0041000) + H7 debug bus (DBGMCU_CR
  0x5C001004, TRACECLKEN) + D-domain trace components at system-bus 0x5C0xxxxx
  (TPIU 0x5C015000, funnel CSTF 0x5C013000 — the 0xE00Fxxxx debugger aliases
  read 0 from OpenOCD's MEM-AP) + GPIOE clock in RCC_AHB4ENR. Key gotchas vs
  F4: must set **TRCPDCR.PU** (ETM power-up) or PMSTABLE never asserts, and must
  enable the **CSTF ETM slave port** or no trace reaches the pins. Same trace
  pins (PE2..PE6, AF0). Branch broadcast ON (TRCCONFIGR.BB). This board uses a
  **DAPLink** (`interface/cmsis-dap.cfg`), not ST-Link. Run with
  `target/stm32h7x.cfg`. Refs: `docs/artix7-port/refs/DDI0494D_*` (ETM-M7 TRM),
  `DDI0489F_*` (M7 TRM).
- `tpiu_testpattern_h743.cfg` — H743 TPIU AA/55 test-pattern generator: drives
  all 4 data lanes independent of the ETM, for datapath/wiring/FPGA-front-end
  validation. Stop with `mww 0x5C015204 0`.
- `etm_itm_enable.cfg` — ETM + ITM variant.
- `etm_syncfreq.cfg` — ETM sync-frequency probing.
- `downclock.cfg` — lower HCLK (env `DIV`) so TRACECLK fits the 50 MSa/s logic
  analyser, without resetting (avoids firmware clock re-init).
- `gpio_toggle.cfg` — PE2 GPIO toggle smoke test (pin sanity).
- `jtag_probe.tcl`, `jtag_extest_probe.tcl` — JTAG boundary-scan probes.
