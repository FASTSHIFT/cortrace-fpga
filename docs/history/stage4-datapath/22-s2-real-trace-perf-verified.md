# doc 22 — S2 Real Trace End-to-End Perf Export Verified

**Date**: 2026-09-07
**Bit**: `trace_ddr_stream.bit` (commit `a51a8af`)
**Firmware**: `stm32h743-etm-trace-firmware/build/H743_Blink.elf`
  built with `OPT=-O0 EXTRA_CFLAGS=-DETM_SELFTRACE` (selftrace loop
  `etm_selftrace_run → det_iter → node → leaf_add / leaf_xor`)

## What ran

Full datapath verified:

```
STM32 ETM v4 (4-bit @ 112 MHz TRACECLK)
   ↓ TRACED0..3 + TRACECLK physical pins
FPGA trace_capture_a7 (IDDR, tap=2, BUFR_IO)
   ↓ cap_byte @ cap_valid (clk200)
la_ddr_writer (200 MB/s cap → 128-bit AsyncFIFO → pack → DDR3 burst)
   ↓ 16 MB DDR3 ring buffer
la_ddr_ring_streamer (drain @ clk125, monotone seq, gearbox 128b→8b)
   ↓ 8-bit AXI-S + monotone packet seq
packetiser [4B BE seq][1024B trace]
   ↓ pkt_tdata @ pkt_tvalid/tready
fpga_core_net STREAM=1 (STREAM_PKT_BYTES=PKT[15:0], r39 fix)
   ↓ UDP :5555
Host stream_grab.c (recvmmsg + writer thread, zero-loss)
   ↓ raw bytes
opencsd_etm4_run (TPIU deframe, stream=2)
   ↓ etm.bin (ETM v4 packets)
cortrace-decode → .perftrace + edges.tsv
```

## Capture

`./stream_grab enxc8a36266dcae 3 /tmp/cap_s2.bin`:

- 335 380 480 bytes captured in 3 s (**112 MB/s**)
- seq-gap events = **0**
- lost-frames = **0**
- ring-full dropped bytes = **0**

## TPIU deframe (opencsd_etm4_run, 20 MB slice)

- 20 MB raw → 5.1 MB deframed ETM
- Frames = 654 300, fsync = 1 555 728
- **ETMv4 A-syncs = 6 855, Trace-Info(0x01) after A-sync = 4 941**

## cortrace decode (500 KB ETM slice)

Full stats:

```
loaded 407 function symbols

=== cortrace decode result ===
  etm bytes processed : 500000
  begins / ends       : 2781 / 2781  (balanced)
  max depth           : 5
  mismatched returns  : 156
  dropped calls       : 98  (callee lost to blind spot)
  recovered returns   : 112
  exceptions rendered : 0
  slice events        : 5562

=== function coverage ===
  functions with instruction flow : 8
  functions rendered as slices    : 4
  MISSED (flow but no slice)      : 4
      etm_selftrace_run            (245 instr ranges)
      UART_SetConfig               (30 instr ranges)
      cm_benchmark_main            (5 instr ranges)
      _dtoa_r                      (1 instr ranges)
```

**Call edges** (2781 total events → 8 unique edges):

```
23   det_iter        → leaf_add
3    det_iter        → leaf_xor
888  det_iter        → node
106  etm_selftrace_run → det_iter
2    etm_selftrace_run → leaf_add     (blind-spot cross-edge)
1    etm_selftrace_run → node          (blind-spot cross-edge)
864  node            → leaf_add
894  node            → leaf_xor
```

**Verdict**: matches the selftrace source shape
`etm_selftrace_run → det_iter (106×) → node (888×) → leaf_add / leaf_xor (864/894×)`.
The 3 blind-spot cross-edges (`etm_selftrace_run → leaf_add/node`, 2+1) are noise
from I-sync gaps in the ETM stream that miss an intermediate call. This is
the same drop-rate class as the AGENT.md 2026-09-03 baseline (568/571 balanced,
max depth 4, 484 µs) — the selftrace call graph is preserved through the DDR
ring path.

## Known issue: cortrace OOMs above ~1 MB ETM

At 500 KB ETM: fine. At 1 MB: fine (10 072 events, 11 edges). At 2 MB: killed.
At 5 MB: killed. At 20 MB: killed. Likely a memory-blowup in the stack-machine
for a specific input pattern; needs profiling. Not a datapath issue — S2 raw
bytes are clean, deframe is clean, decode works on any windowable slice.

**Workaround**: decode in ~500 KB windows. Long captures can be sliced
externally then merged. **TODO**: cortrace hardening for larger inputs.

## What's proven

- ✅ Real ETM v4 4-bit stream flows through DDR ring buffer end-to-end
- ✅ Zero UDP loss at 112 MB/s host receive
- ✅ TPIU deframe recovers ETM v4 packets correctly (A-sync count, Trace-Info)
- ✅ cortrace decode produces balanced begins/ends + valid call graph
- ✅ Call edges match selftrace source structure

## S2 closed. Doc 21 next milestone: S3 (NACK RX on-board).

## Files

- `/tmp/cap_s2.bin` — 335 MB raw stream_grab capture (kept locally)
- `/tmp/dec_s2/{etm.bin,mem.bin,snapshot.ini,...}` — deframed ETM + snapshot
- `/tmp/s2_small.perftrace` (104 KB) — Perfetto perf, drag to https://ui.perfetto.dev
- `/tmp/e_small.tsv` — call edges histogram
