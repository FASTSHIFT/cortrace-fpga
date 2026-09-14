#!/usr/bin/env python3
"""etm_with_time — attach the FPGA capture-time base to a decoded ETM stream.

Pipeline (doc 15 §24.2): the F429 ETM has no wall-clock, so the FPGA timestamps
RAW capture bytes (trace_dump --timebase -> <dump>.ts.json). Here we:
  1. deframe the RAW TPIU capture into ETM bytes, tracking the source RAW byte
     offset of each ETM byte (tpiu_official.deframe with_offsets),
  2. map each ETM byte's source offset -> wall-clock ns via the sidecar table
     (fpga_timebase.TimeBase),
  3. emit the clean ETM bytes (for etm_to_tpiu.py -> orbetto) AND a parallel
     <out>.time.json array of ETM-byte -> ns, so the downstream can place
     instructions on a real (frequency-agnostic) time axis instead of the
     degenerate ETM cycle-count path.

Usage:
  python3 etm_with_time.py <capture.bin> <capture.bin.ts.json> <out_etm.bin>
                           [--raw-per-byte N]

--raw-per-byte is needed for 2/1-bit captures. The timebase sidecar indexes RAW
capture bytes (one per TRACECLK period), but at 2/1 bit a TPIU byte spans 2/4
TRACECLK periods, so the byte stream handed to us has already been regrouped and
its offsets are 2x/4x smaller than the raw indices the timebase expects. Pass the
number of raw bytes per TPIU byte (4-bit: 1, 2-bit: 2, 1-bit: 4) to scale them
back; without it the whole trace collapses into the first fraction of the
timeline.
"""
import json
import sys

import etm35lib as L
import tpiu_official as T
from fpga_timebase import TimeBase


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(args) < 3:
        print(__doc__)
        return 2
    cap_path, ts_path, out_path = args[0], args[1], args[2]
    raw_per_byte = 1
    for i, x in enumerate(sys.argv):
        if x == "--raw-per-byte":
            raw_per_byte = int(sys.argv[i + 1])
        elif x.startswith("--raw-per-byte="):
            raw_per_byte = int(x.split("=", 1)[1])
    raw = open(cap_path, "rb").read()
    tb = TimeBase.load(ts_path)

    # Stage 1: assemble best-aligned byte stream is already done upstream
    # (trace_dump output is the correctly-assembled RAW byte stream). Deframe
    # with source-offset tracking so each ETM byte carries its RAW origin.
    #
    # MUST be the same deframer orbetto uses (official / orbuculum tpiuDecoder
    # semantics). The old tpiu_deframe_walk_offsets loses 16-bit frame phase on
    # odd-offset HSYNC and yields FEWER ETM bytes than orbetto's own deframer
    # (measured: 8734 vs 11816 on the same capture). The .time.bin array is
    # indexed by ETM byte position, so a length/index mismatch silently skews
    # every timestamp: orbetto then runs off the end of the table and the time
    # axis degenerates to a 1ns-per-event counter.
    etm, offs, st = T.deframe(raw, want_stream=2, with_offsets=True)

    # Stage 2: map RAW source offset -> wall-clock ns. The dump file is the RAW
    # buffer with the first `skip` bytes dropped; offsets index into that file,
    # so they are OUT indices (add skip inside TimeBase.ns_for_out).
    #
    # raw_per_byte rescales offsets for 2/1-bit captures: the timebase is indexed
    # by TRACECLK period (= raw capture byte), but our input has already been
    # regrouped so that one byte covers raw_per_byte periods.
    times_ns = [tb.ns_for_out(o * raw_per_byte) for o in offs]

    with open(out_path, "wb") as f:
        f.write(etm)
    side = out_path + ".time.json"
    with open(side, "w") as f:
        json.dump({"tick_ns": tb.tick_ns,
                   "span_ns": tb.span_ns(),
                   "n_etm_bytes": len(etm),
                   "times_ns": times_ns}, f)
    # Also emit a compact binary the C++ side (orbetto/Mortrall) can mmap/read:
    # one little-endian uint64 ns per ETM byte, in stream order. This is the
    # 1:1 companion to the ETM bytes orbetto's TPIU deframer delivers.
    import struct
    binside = out_path + ".time.bin"
    with open(binside, "wb") as f:
        f.write(struct.pack(f"<{len(times_ns)}Q",
                            *[int(round(t)) for t in times_ns]))

    # Report
    unk = sum(1 for c in etm if L._classify(c) == "unknown")
    print(f"deframed {len(raw)} RAW -> {len(etm)} ETM bytes "
          f"(official: frames={st['packets']} fsync={st['syncs']}, "
          f"unknown {100*unk/max(1,len(etm)):.3f}%)")
    if times_ns:
        span = times_ns[-1] - times_ns[0]
        nondec = sum(1 for k in range(1, len(times_ns))
                     if times_ns[k] < times_ns[k - 1] - 1e-6)
        print(f"time: {times_ns[0]/1e3:.1f}..{times_ns[-1]/1e3:.1f} us "
              f"(span {span/1e3:.1f} us), non-decreasing violations: {nondec}")
    print(f"wrote {out_path} + {side} + {binside}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
