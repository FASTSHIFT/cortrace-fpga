#!/usr/bin/env python3
"""prbs_pkt_check — definitive per-packet verifier for the framed PRBS captured
WITH the 4-byte seq header (prbs_raw_grab.py output).

Each 1024-byte packet payload is validated against the emitted block model
  block = MARKER(8) + xorshift_payload(8184)   (PRBS reseeds each block)
A packet may lie fully inside one block's payload, or straddle a block boundary
(tail-payload | MARKER | head-payload). Both are valid. We reconstruct the
exact expected 1024 bytes for each packet by locating it in a 3-block reference
window (covers any straddle) and comparing byte-for-byte.

Skips the first --skip packets (ring fill / PRBS-enable startup transient).
Verdict: 0 byte errors across all non-startup packets => datapath byte-perfect.
"""
import argparse
import struct
import prbs_check as P


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--skip", type=int, default=20)
    a = ap.parse_args()
    d = open(a.capture, "rb").read()
    rec = 1028
    n = len(d) // rec

    block = bytes(P.MARKER) + P.payload_ref(P.PAYLOAD_LEN)
    ref = block * 3  # a 3-block window: any 1024B packet + its straddle fits

    def payload(i):
        return d[i * rec + 4: (i + 1) * rec]

    checked = 0
    good = 0
    startup_nolock = 0
    bad = []
    for i in range(a.skip, n):
        pl = payload(i)
        # locate the packet's first 32 bytes in the reference window
        j = ref.find(pl[:32])
        if j < 0:
            bad.append(i)
            checked += 1
            continue
        # normalise j into the first block so ref[j:j+1024] always has a full
        # block (+straddle) ahead of it inside the 3-block window.
        Lb = len(block)
        while j >= Lb:
            j -= Lb
        exp = ref[j:j + 1024]
        if exp == pl:
            good += 1
        else:
            bad.append(i)
        checked += 1

    print(f"packets total={n}  checked(skip {a.skip})={checked}")
    print(f"  byte-perfect packets : {good}")
    print(f"  bad packets          : {len(bad)}  {bad[:10]}")
    if not bad:
        print("\nPRBS BYTE-PERFECT: every non-startup packet matches the framed "
              "xorshift reference exactly.\n=> capture -> CDC -> DDR ring -> "
              "gearbox -> packetiser -> UDP path is clean end to end.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
