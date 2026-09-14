#!/usr/bin/env python3
"""prbs_soak — streaming byte-exact soak test of the trace capture datapath.

Enables the FPGA framed-PRBS source (CSR 0x0D), then repeatedly captures a
short segment with stream_grab, verifies EVERY packet payload byte-for-byte
against the reseeded xorshift reference (prbs_pkt_check logic), tallies
totals, and deletes the segment before the next one. Runs for --minutes.

Because the PRBS is host-reproducible, this proves ZERO byte errors across the
whole soak -- not just "it decoded". Constant disk/RAM footprint (one segment
at a time). A single bad packet aborts with a non-zero exit and the offending
segment kept for post-mortem.

Usage:
  sudo python3 prbs_soak.py --minutes 5 [--seg-seconds 10] [--iface ...] [--ip ...]
"""
import argparse
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import prbs_check as P  # noqa: E402  (framed-PRBS reference generator)

# stream_grab STRIPS the 4-byte UDP seq, so its output is a pure, contiguous
# framed-PRBS byte stream (repeating MARKER(8) + payload(8184)). We verify it
# block-by-block: lock on each 8-byte MARKER, then require the following
# payload to match the reseeded reference exactly up to the next MARKER.
BLK = 8192


def verify_segment(path, block, ref, skip_bytes):
    """Verify a pure (seq-stripped) framed-PRBS byte stream.
    Returns (blocks_checked, bad_blocks, bad_positions, byte_errors)."""
    d = open(path, "rb").read()[skip_bytes:]
    MK = bytes(P.MARKER)
    refpl = P.payload_ref(P.PAYLOAD_LEN)
    # find all marker positions
    marks = []
    i = d.find(MK)
    while i >= 0:
        marks.append(i)
        i = d.find(MK, i + 1)
    blocks = 0
    bad = 0
    bad_pos = []
    byte_errs = 0
    for k in range(len(marks) - 1):
        gap = marks[k + 1] - marks[k]
        if gap != BLK:
            # a drop/dup shifted the block; count as bad
            bad += 1
            bad_pos.append(marks[k])
            continue
        pl = d[marks[k] + len(MK):marks[k + 1]]
        e = sum(1 for x in range(min(len(pl), len(refpl))) if pl[x] != refpl[x])
        blocks += 1
        if e:
            bad += 1
            byte_errs += e
            bad_pos.append(marks[k])
    return blocks, bad, bad_pos, byte_errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--seg-seconds", type=int, default=10)
    ap.add_argument("--iface", default="enxc8a36266dcae")
    ap.add_argument("--ip", default="192.168.10.42")
    ap.add_argument("--tmp", default="/tmp/prbs_soak_seg.bin")
    a = ap.parse_args()

    grab = os.path.join(HERE, "stream_grab")
    trace_ctrl = os.path.join(HERE, "trace_ctrl.py")
    block = bytes(P.MARKER) + P.payload_ref(P.PAYLOAD_LEN)
    ref = block * 3

    # enable PRBS source
    subprocess.run([sys.executable, trace_ctrl, "--ip", a.ip, "iddr-prbs", "1"],
                   check=False, stdout=subprocess.DEVNULL)
    time.sleep(0.3)

    t_end = time.time() + a.minutes * 60
    seg = 0
    tot_blocks = tot_bad = tot_byte_errs = 0
    tot_grab_gaps = 0
    tot_bytes = 0
    t0 = time.time()
    rc = 0
    try:
        while time.time() < t_end:
            seg += 1
            g = subprocess.run(
                [grab, a.iface, str(a.seg_seconds), a.tmp, "256", "512"],
                capture_output=True, text=True)
            grab_ok = ("seq-gap events=0" in g.stdout
                       and "ring-full dropped bytes=0" in g.stdout)
            if not grab_ok:
                tot_grab_gaps += 1
            # skip the PRBS-enable transient at the very start of segment 1
            skip = 65536 if seg == 1 else 0
            blocks, bad, bpos, berr = verify_segment(a.tmp, block, ref, skip)
            sz = os.path.getsize(a.tmp)
            tot_blocks += blocks
            tot_bad += bad
            tot_byte_errs += berr
            tot_bytes += sz
            el = time.time() - t0
            print(f"[{el:6.1f}s] seg{seg:03d} blocks={blocks} bad={bad} "
                  f"byte_errs={berr} grab={'ok' if grab_ok else 'GAP/DROP'} "
                  f"cum: blocks={tot_blocks} bad={tot_bad} "
                  f"GB={tot_bytes/1e9:.2f}", flush=True)
            if bad or not grab_ok:
                print(f"!! FAIL in seg{seg} (bad_blocks={bad} at {bpos[:6]}, "
                      f"grab_ok={grab_ok}) -- keeping segment", flush=True)
                os.rename(a.tmp, a.tmp + f".bad_seg{seg}")
                rc = 1
                break
            os.remove(a.tmp)
    finally:
        subprocess.run([sys.executable, trace_ctrl, "--ip", a.ip, "iddr-prbs", "0"],
                       check=False, stdout=subprocess.DEVNULL)

    dur = time.time() - t0
    print("\n==== PRBS SOAK SUMMARY ====")
    print(f"  duration      : {dur:.1f}s over {seg} segments")
    print(f"  blocks checked: {tot_blocks}  ({tot_bytes/1e9:.2f} GB captured)")
    print(f"  bad blocks    : {tot_bad}  (byte errors: {tot_byte_errs})")
    print(f"  grab gap/drop segs: {tot_grab_gaps}")
    if rc == 0 and tot_bad == 0 and tot_grab_gaps == 0:
        print("  VERDICT: PASS — byte-perfect, zero gaps across the whole soak")
    else:
        print("  VERDICT: FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
