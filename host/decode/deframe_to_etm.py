#!/usr/bin/env python3
"""deframe_to_etm — one-shot: raw FPGA capture -> deframed ETMv4 bytes.

Does ONLY the capture-side recovery (nibble reassemble + official TPIU
deframe) and writes the bare ETMv4 byte stream. Hand the output to
cortrace-decode (fast C++), not the slow trc_pkt_lister python path.

Usage:
  deframe_to_etm.py <raw.bin> <out_etm.bin> [max_bytes]
      [--time <out.time.bin>] [--period-ns <ns>]

--time writes a cortrace time base: one little-endian uint64 ns per ETM byte,
in stream order (what cortrace-decode --time expects). The wall-clock is
reconstructed from the fixed TRACECLK: the assembled RAW stream is one byte per
TRACECLK period, so each ETM byte's ns = (its source assembled-byte offset) *
period_ns. --period-ns defaults to the 56.25 MHz TRACECLK pin (17.778 ns).
This is frequency-agnostic and needs no FPGA timestamp sidecar.
"""
import struct
import sys

import opencsd_etm4_run as R
import tpiu_official as T

argv = [a for a in sys.argv[1:] if not a.startswith("--")]
raw_path, out_path = argv[0], argv[1]
maxb = int(argv[2]) if len(argv) > 2 else 0

time_path = None
period_ns = 1e9 / 56.25e6   # 56.25 MHz TRACECLK pin -> 17.778 ns/period
for i, a in enumerate(sys.argv):
    if a == "--time":
        time_path = sys.argv[i + 1]
    elif a.startswith("--time="):
        time_path = a.split("=", 1)[1]
    elif a == "--period-ns":
        period_ns = float(sys.argv[i + 1])
    elif a.startswith("--period-ns="):
        period_ns = float(a.split("=", 1)[1])

raw = open(raw_path, "rb").read()
if maxb:
    raw = raw[:maxb]
score, parity, order, data, fl, v4a, fsync = R.recover_assemble(raw)

if time_path:
    # deframe WITH source offsets: each ETM byte -> its offset in `data` (the
    # assembled RAW stream, one byte per TRACECLK period). ns = offset*period.
    etm, offs, stats = T.deframe(data, want_stream=2, with_offsets=True)
    with open(time_path, "wb") as f:
        f.write(struct.pack(f"<{len(offs)}Q",
                            *[int(round(o * period_ns)) for o in offs]))
    span_us = (offs[-1] - offs[0]) * period_ns / 1e3 if offs else 0
    open(out_path, "wb").write(etm)
    print(f"raw={len(raw)} parity={parity} order={order} fsync={fsync} "
          f"-> etm={len(etm)} bytes  frames={stats['packets']}  "
          f"time: {len(offs)} entries span {span_us:.1f} us -> {time_path}")
else:
    etm, stats = T.deframe(data, want_stream=2)
    open(out_path, "wb").write(etm)
    print(f"raw={len(raw)} parity={parity} order={order} fsync={fsync} "
          f"pre-deframe-async={v4a} -> etm={len(etm)} bytes  "
          f"frames={stats['packets']}")
