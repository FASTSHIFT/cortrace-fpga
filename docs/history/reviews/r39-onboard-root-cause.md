# r39 — S1b 0.39% Pollution: On-Board Root Cause + Fix

**Date**: 2026-09-04
**Context**: r36 + r37 + r38 evaluated H1..H7 hypotheses about RTL races between
`la_ddr_writer`, `la_ddr_ring_streamer`, and the vendor `ddr3_wr_ctrl`. r38
demonstrated that H7 was self-refuted in sim once the tb MIG cmd/wdf pairing
was corrected — all six sim variants passed with zero pollution. Blue team
withdrew F1 and requested advancing to P0-4 (on-board ILA capture).
**Result**: the on-board root cause turned out to be a **top-level parameter
mismatch**, not any RTL race. Fixed in one line.

---

## 1. TL;DR

`ddr_ring_selftest_top` instantiated `fpga_core_net` with the default
`STREAM_PKT_BYTES = 16'd1024`, but the internal packetiser emitted
`PKT = STREAM_PAYLOAD + 4 = 1028` bytes per packet (4-byte BE `latched_seq`
header + 1024 payload data). Every packet the seq header drifted 4 bytes
into the next UDP frame's payload, producing exactly the 0.39% pollution
pattern observed on-board (fixed offsets 740, 744, 748, ... = seq-header
byte drifting through the payload).

**Fix** (one-line, in `ddr_ring_selftest_top.v`):

```verilog
fpga_core_net #(
    ...,
    .STREAM_PKT_BYTES(PKT[15:0])   // must match packetiser output length
) u_eth ( ... );
```

**Verification** (post-fix, using `stream_grab.c` + `stream_pulses` FPGA
counters):

| Config | Bytes | Errors |
|--------|-------|--------|
| Fixed 0x42, 10 s | 1.16 GB | **0 bad bytes** |
| Ramp mode, 10 s (skip 8 KB mode-switch transient) | 1.15 GB | **0 breaks** |
| stream_grab seq-gap events | — | 0 |
| stream_grab lost-frames | — | 0 |
| stream_grab ring-full drops | — | 0 |
| FPGA-side stream_pulses count | 700 M+ | latch bad_count = 0 |

---

## 2. P0-4 Path That Got Here

r38 §5.5 committed to running P0-4 = on-board ILA on wire-side signals. The
plan turned out to be too heavyweight — Vivado batch-mode hw_manager kept
tripping on TCL/probe-name/trigger-syntax issues that were unrelated to the
actual investigation. After two failures on the ILA path, we switched
approach (per AGENT.md §6 "two-fails, change track") to **in-fabric
diagnostic latches**:

- `bad_byte_val[7:0]`      — first observed non-0x42 `pkt_tdata`
- `bad_stream_td[7:0]`     — `stream_tdata` (upstream of mux) at the same clk
- `bad_byte_pos[7:0]`      — packetiser `pos[7:0]` at latch time
- `bad_byte_seq[31:0]`     — `latched_seq` at latch time
- `bad_byte_count[31:0]`   — running count of bad bytes
- `stream_pulses[31:0]`    — total `(stream_tvalid & stream_tready)` events
- `pkt_active_pulses[31:0]`— rising edges of `pkt_active` (packets started)

Exposed at CSR addresses `0xFF80..0xFF94`, cleared by `CSR 0x0C = 1`.

**First read (after 3 s of streaming, ~700 million payload bytes):**

```
magic=0xd2 flags=0x02 (latched=0 src_fixed=1)
bad_count=0
stream_pulses=707,368,911
pkt_active_pulses=182,221
```

**`bad_count = 0` at the FPGA side, but the PC saw 0.3884% non-0x42.** That
was the smoking gun: **the packetiser is emitting only 0x42 bytes** — the
corruption is downstream of `pkt_tdata`, i.e. inside `fpga_core_net` or on
the wire.

---

## 3. Wire-Side Byte Pattern Analysis

`stream_grab -o /tmp/cap.bin` (10 s, 1.16 GB, zero UDP-level loss). Analysis:

```
571041 packets (1020 bytes/packet after 4-byte seq strip)
99.22% of packets have exactly 4 bad bytes, at consecutive offsets
```

**Per-packet bad-byte offsets** (first 30 packets):

| pkt | offsets                | values                       |
|-----|------------------------|------------------------------|
| 0   | (740, 741, 742, 743)   | 0x03 0x30 0xFA 0x74          |
| 1   | (744, 745, 746, 747)   | 0x03 0x30 0xFA 0x75          |
| 2   | (748, 749, 750, 751)   | 0x03 0x30 0xFA 0x76          |
| 3   | (752, 753, 754, 755)   | 0x03 0x30 0xFA 0x77          |
| ... | ...                    | ... increments by 1/pkt      |
| 29  | (856, 857, 858, 859)   | 0x03 0x30 0xFA 0x91          |

**Structure**:

- Same 4-byte pattern each packet, only the low byte increments
- The 4-byte offset within the packet drifts by exactly +4 per packet
- The pattern `0x0330FA74` = 53,608,564, monotonically incrementing = a
  packet index (i.e. `latched_seq` in BE encoding)

**Diagnosis**: the packetiser's 4-byte BE `latched_seq` header is being
written into the payload of the following UDP frame, offset by 4 bytes per
packet.

---

## 4. Root Cause

`ddr_ring_selftest_top.v` line ~266:

```verilog
localparam integer PKT = STREAM_PAYLOAD + 4;  // = 1024 + 4 = 1028
```

The packetiser emits `PKT = 1028` bytes per packet (4-byte seq header +
1024-byte trace payload), advancing `pos` from 0 to 1027 while `pkt_active`
is high.

`fpga_core_net.v`, in the `g_stream` branch, drives its UDP framer from
`stream_tvalid / stream_tready`. It counts `bcnt = 0..STREAM_PKT_BYTES-1`.
When `bcnt == STREAM_PKT_BYTES - 1` and the byte is accepted, it asserts
`tlast` and returns to `ST_IDLE`.

`STREAM_PKT_BYTES` has a default of `16'd1024`. `ddr_ring_selftest_top`
did NOT override this parameter → `fpga_core_net` cut every UDP frame at
1024 bytes.

**Timing per packet** (relative to a continuous wire byte stream):

```
packet 0:   pos =    0 .. 1027  (packetiser)
             bcnt =    0 .. 1023 for UDP frame 0     [seq0][data0][data1]..[data1019]
                     accepts bytes 0..1023            (bytes 1020..1023 are trace bytes)

packet 0 tail: pos = 1024 .. 1027 (still emitting = pkt_tdata = stream_tdata = trace)
              bcnt =    0 ..    3 for UDP frame 1     [data1020..1023]

packet 1:   pos =    0 .. 1027
             bcnt =    4 .. 1027 for UDP frame 1     [seq1_B3][seq1_B2][seq1_B1][seq1_B0][data1024..2039]
                                                       ^^^^^^^ THESE 4 bytes of seq1 land at UDP frame offset 4..7!

packet 2:   pos =    0 .. 1027
             bcnt =    8 .. 1023 + 1024..1027       seq2 lands at UDP frame 2 offset 8..11
```

So `seq_N` header ends up at offset `4*N mod 1024` inside UDP frame `N`.
That's exactly the 4-byte drift observed.

---

## 5. Fix

Single-line RTL change in `ddr_ring_selftest_top.v`:

```verilog
fpga_core_net #(
    .TARGET("XILINX"), .STREAM(1),
    .STREAM_DEST_IP(DEST_IP), .STREAM_DEST_PORT(DEST_PORT),
    .STREAM_PKT_BYTES(PKT[15:0])   // <-- NEW: match packetiser length
) u_eth (...);
```

`PKT = STREAM_PAYLOAD + 4 = 1028`. Now UDP frame boundaries line up with
packetiser output boundaries and the seq header stays where it belongs
(bytes 0..3 of each UDP frame).

---

## 6. Vindication of the r38 Sim Analysis

**Everything r36..r38 debated about la_ddr_writer / la_ddr_ring_streamer /
vendor ddr3_wr_ctrl RTL races was orthogonal to the actual on-board bug.**

- r38 sim showed 6 variants all ALL_PASS: correct — the streamer/writer
  RTL is fine.
- The FPGA-side `bad_byte_count = 0` over 700 M payload bytes confirms:
  the packetiser handoff to `fpga_core_net.stream_tdata` is clean.
- The on-board pollution is 100% attributable to the size-mismatch between
  packetiser output and `fpga_core_net` UDP-frame length.

**Methodology takeaway (also flagged in r38 §4.3)**: when sim ALL_PASS but
on-board fails, the bug is almost always in the **top-level wiring** —
parameter overrides, port bindings, clock domains — not in the sub-modules
themselves. r36's list of hypotheses only considered RTL internals; the
"top parameter mismatch" class was not on the list.

The lesson: for any top module that wires up sub-modules with parameters,
**every parameter that BOTH modules care about must be explicitly bound**.
Defaulting = latent bug.

---

## 7. Next Steps

- ✅ Commit the fix (top-level parameter binding + diag latches).
- ✅ Update AGENT.md changelog to record the r39 finding.
- **S1b closed at 100% zero-loss** — the P0-4 gate is green.
- **Advance to S2** (real trace source instead of ramp/fixed): connect
  `la_ddr_writer.cap_byte / cap_valid_in` to the CAP output of `trace_capture_a7`
  and repeat with actual ETM trace data. Since the packetiser + fpga_core_net
  are now known-good, any residual pollution in S2 is attributable to the
  writer's `cap_byte` handling or the trace source itself.
- **S3 (NACK RX path)** and **S4 (P4 injected loss)** can proceed as
  originally planned in doc 21.

**Note on F1**: the F1 patch (change `la_ddr_writer.W_DONE` condition from
`end_cmd_cnt` to `end_data_cnt`) is **no longer relevant**. H7 was refuted
in sim (r38) and the real bug was the top parameter binding. `la_ddr_writer`
stays untouched.

---

## 8. Evidence Strength

| Claim | Evidence | Strength |
|-------|----------|----------|
| FPGA-side `bad_count = 0` over 700 M bytes | CSR read 0xFF89..0xFF8C | 🟢 measured |
| Wire-side 0.39% pollution before fix | stream_grab 0.3884% | 🟢 measured |
| Bad bytes are 4-byte seq header drifting | offset+value pattern | 🟢 measured |
| STREAM_PKT_BYTES default was 1024 | fpga_core_net.v L58 | 🟢 RTL |
| Packetiser emits 1028 bytes/packet | ddr_ring_selftest_top.v L266 | 🟢 RTL |
| Post-fix pollution = 0 over 1.16 GB | stream_grab post-fix | 🟢 measured |
| Sim was clean = subsystems fine | r38 6-variant sweep | 🟢 measured |
| Bug is top-wiring, not sub-module RTL | subsystems + PC = pollution, subsystems + top-fix + PC = clean | 🟢 deductive |
