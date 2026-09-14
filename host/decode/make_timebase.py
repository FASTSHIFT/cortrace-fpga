#!/usr/bin/env python3
"""make_timebase — build a cortrace --time file (uint64 ns per ETM byte) for a
capture whose byte rate is constant (the deterministic selftrace loop has no
TRACECLK idle gaps).

cortrace's --time expects one uint64 ns value per ETM STREAM byte, indexed by
the byte_index cortrace tags each element with. We get the ETM-byte ->
source-RAW-byte mapping from the SAME official TPIU deframer cortrace's input
went through (tpiu_official.deframe with_offsets), then convert the RAW byte
index to ns using the known RAW byte cadence.

RAW cadence: the FPGA emits exactly one RAW byte per TRACECLK *period* (pin
edge rate). At R=4 the pin is 56.25 MHz -> one byte every 1/56.25e6 s = 17.778
ns. Pass --byte-ns to override for other clocks.

Usage:
  make_timebase.py <raw.bin> <out_time.bin> [--byte-ns 17.778] [--max N]
Writes little-endian uint64 ns, length = number of deframed ETM bytes, so it
lines up 1:1 with the etm.bin deframe_to_etm produced from the same raw.
"""

import argparse
import struct
import sys

import opencsd_etm4_run as R
import tpiu_official as T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("out")
    ap.add_argument(
        "--byte-ns",
        type=float,
        default=1e9 / 56.25e6,
        help="ns per RAW capture byte (default 56.25MHz pin)",
    )
    ap.add_argument("--max", type=int, default=40_000_000)
    a = ap.parse_args()

    raw = open(a.raw, "rb").read()
    if a.max:
        raw = raw[: a.max]
    # same recovery + deframer as deframe_to_etm, but keep per-ETM-byte source
    # RAW offsets so time lines up with cortrace's byte_index.
    score, parity, order, data, fl, v4a, fsync = R.recover_assemble(raw)
    etm, offs, stats = T.deframe(data, want_stream=2, with_offsets=True)
    # data[] index -> approximate RAW byte index. recover_assemble builds the
    # assembled byte stream 1:1 with RAW period bytes (parity/order only choose
    # nibble pairing, not count), so data index ~= RAW byte index.
    ns = bytearray(len(etm) * 8)
    for i, srcoff in enumerate(offs):
        t = int(round(srcoff * a.byte_ns))
        struct.pack_into("<Q", ns, i * 8, t)
    open(a.out, "wb").write(ns)
    span = offs[-1] * a.byte_ns / 1e6 if offs else 0
    print(
        f"etm={len(etm)}B  time entries={len(offs)}  byte_ns={a.byte_ns:.3f}  "
        f"span={span:.2f} ms -> {a.out}"
    )


if __name__ == "__main__":
    sys.exit(main())
