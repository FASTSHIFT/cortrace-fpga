#!/usr/bin/env python3
"""prbs_check — verify the FRAMED trace_clk xorshift32 PRBS captured through the
IDDR-side CDC FIFO (CSR 0x0D = iddr-prbs).

Frame format (per trace_capture_a7 g_iddr):
  every 8192-byte block:
    [0..7]    marker A5 5A C3 3C F0 0F 99 66   (PRBS held at seed 0x1)
    [8..8191] xorshift32 low-byte payload, emit-then-advance from seed 0x1:
              emit 0x01; s^=s<<13; s^=s>>17; s^=s<<5; emit(s&0xFF); ...
  Then blkpos wraps and the marker + reseed repeat.

Why framed: a free-running PRBS is unlockable after a single dropped byte (the
drop skips a state, desyncing forever). With a periodic marker + reseed we:
  * lock on the marker,
  * check every payload byte against the known reseeded sequence,
  * measure marker-to-marker spacing: 8192 => no loss in that block; a shorter
    gap means (8192 - gap) bytes were dropped by the CDC/DDR/UDP path in that
    block; a longer gap means duplicated bytes.

Verdict BYTE-PERFECT (all gaps == 8192, all payloads exact) => the datapath
downstream of the IDDR sample is clean, so residual real-trace corruption is
the IDDR sampling itself.

Usage: prbs_check.py <capture.bin> [--max N]
"""
import argparse
import sys

MARKER = bytes([0xA5, 0x5A, 0xC3, 0x3C, 0xF0, 0x0F, 0x99, 0x66])
BLK_LEN = 8192
PAYLOAD_LEN = BLK_LEN - len(MARKER)
M = 0xFFFFFFFF


def payload_ref(n):
    """emit-then-advance xorshift32 low bytes from seed 0x1, n bytes."""
    s = 0x1
    out = bytearray(n)
    for i in range(n):
        out[i] = s & 0xFF
        s ^= (s << 13) & M
        s &= M
        s ^= (s >> 17)
        s ^= (s << 5) & M
        s &= M
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--max", type=int, default=0)
    a = ap.parse_args()

    cap = open(a.capture, "rb").read()
    if a.max:
        cap = cap[:a.max]
    if len(cap) < BLK_LEN:
        print("capture too small")
        return 2

    ref_payload = payload_ref(PAYLOAD_LEN)

    # find all marker positions
    marks = []
    i = cap.find(MARKER)
    while i >= 0:
        marks.append(i)
        i = cap.find(MARKER, i + 1)
    print(f"markers found: {len(marks)}")
    if len(marks) < 2:
        print("NO/insufficient markers -> stream is not the framed PRBS, or so "
              "corrupt the 8-byte marker never survives intact.")
        print("  first 32 cap bytes:", cap[:32].hex())
        return 1

    # marker-to-marker spacing distribution
    gaps = [marks[k + 1] - marks[k] for k in range(len(marks) - 1)]
    from collections import Counter
    gap_hist = Counter(gaps)
    perfect_gaps = gap_hist.get(BLK_LEN, 0)
    print(f"block gaps: {len(gaps)}  perfect(=={BLK_LEN}): {perfect_gaps} "
          f"({100*perfect_gaps/len(gaps):.2f}%)")
    print("  gap histogram (top 12):",
          [(g, c) for g, c in gap_hist.most_common(12)])

    # per-block payload verification (only for exact-length blocks)
    blocks = 0
    clean_blocks = 0
    total_payload_bytes = 0
    total_payload_bad = 0
    drops = 0
    dups = 0
    for k in range(len(marks) - 1):
        gap = gaps[k]
        if gap < BLK_LEN:
            drops += (BLK_LEN - gap)
        elif gap > BLK_LEN:
            dups += (gap - BLK_LEN)
        if gap != BLK_LEN:
            continue
        blocks += 1
        payload = cap[marks[k] + len(MARKER): marks[k + 1]]
        bad = sum(1 for x, y in zip(payload, ref_payload) if x != y)
        total_payload_bytes += len(payload)
        total_payload_bad += bad
        if bad == 0:
            clean_blocks += 1

    print(f"\nexact-length blocks checked : {blocks}")
    print(f"  fully clean payloads       : {clean_blocks}")
    print(f"  payload byte errors        : {total_payload_bad}/"
          f"{total_payload_bytes}")
    print(f"estimated dropped bytes      : {drops}")
    print(f"estimated duplicated bytes   : {dups}")

    if perfect_gaps == len(gaps) and total_payload_bad == 0:
        print("\nBYTE-PERFECT: every block is exactly 8192 bytes and every "
              "payload matches the reseeded xorshift reference.\n=> IDDR-side "
              "CDC FIFO + DDR ring + gearbox + packetiser + UDP are CLEAN.\n"
              "=> the residual real-trace corruption is the IDDR SAMPLING "
              "itself.")
        return 0
    print("\nDIRTY: the datapath drops/dups/corrupts bytes even for a clean "
          "trace_clk-domain framed source.\n=> fault is in the CDC FIFO "
          "handshake / DDR ring / gearbox, NOT the IDDR sampling.")
    # show a couple of short-gap examples
    shorts = [(marks[k], gaps[k]) for k in range(len(gaps)) if gaps[k] != BLK_LEN][:5]
    for pos, g in shorts:
        print(f"  block@{pos}: gap={g} (delta {g-BLK_LEN:+d})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
