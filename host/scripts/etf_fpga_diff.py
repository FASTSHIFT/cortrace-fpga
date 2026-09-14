#!/usr/bin/env python3
"""etf_fpga_diff — byte-histogram cross-check between the DAP-golden ETF dump
and the FPGA-side deframed ETM stream from a deterministic selftrace loop.

Both inputs are RAW ETMv4 byte streams (pre-encoder / post-TPIU-deframe on
the FPGA side). In a deterministic loop the byte-value distribution should
match tightly -- any large divergence points at a specific layer:

    * byte-value 0xff / 0x7f only in FPGA side  -> TPIU sync padding leaked
                                                   past the deframer (bug)
    * byte-value X dominant in FPGA, zero in golden -> corrupted nibble pair
                                                   in FPGA IDDR sampling
                                                   (r38 tap-off-eye pattern)
    * byte-value X dominant in golden, zero in FPGA -> silently dropped bytes
                                                   between ETM and TPIU pin
                                                   (very unlikely)

Verdict:
    * within TOL for the top N most common bytes: PASS (both sides see same
      ETM output => any decode-side issue is inside libopencsd or the
      callstack machine, NOT the FPGA capture path)
    * out-of-TOL: FAIL, print the divergence and point at the layer

Usage:
    etf_fpga_diff.py golden.bin fpga_etm.bin [--tol 0.05] [--topn 20]
"""
import argparse
import sys
from collections import Counter


def hist(path: str) -> Counter:
    return Counter(open(path, "rb").read())


def rel(freq: dict, total: int) -> dict:
    return {k: v / total for k, v in freq.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("golden", help="ETF DAP-golden bytes (etf_dap_golden.py)")
    ap.add_argument("fpga", help="FPGA-side deframed ETM bytes (opencsd_etm4_run --keep)")
    ap.add_argument("--tol", type=float, default=0.05,
                    help="tolerance on relative frequency divergence (default 5%)")
    ap.add_argument("--topn", type=int, default=20,
                    help="how many top byte values to score (default 20)")
    a = ap.parse_args()

    g_hist = hist(a.golden)
    f_hist = hist(a.fpga)
    g_total = sum(g_hist.values())
    f_total = sum(f_hist.values())

    print(f"golden : {g_total} bytes, {len(g_hist)} unique values")
    print(f"fpga   : {f_total} bytes, {len(f_hist)} unique values")

    if g_total == 0 or f_total == 0:
        print("ERROR: empty input"); sys.exit(2)

    g_rel = rel(g_hist, g_total)
    f_rel = rel(f_hist, f_total)

    top_bytes = sorted(g_hist, key=g_hist.get, reverse=True)[:a.topn]
    print(f"\ntop-{a.topn} bytes by GOLDEN frequency:")
    print(f"  byte  |  golden%   fpga%    diff       verdict")
    print(f"  ------+-----------------------------------------")
    fail = False
    for b in top_bytes:
        gp = g_rel.get(b, 0.0) * 100
        fp = f_rel.get(b, 0.0) * 100
        d = fp - gp
        rel_dev = abs(d) / gp if gp else float("inf")
        ok = rel_dev <= a.tol
        if not ok:
            fail = True
        tag = "" if ok else f"  <-- OUT OF TOL (|d|/g={rel_dev:.1%})"
        print(f"  0x{b:02x}  | {gp:7.3f}  {fp:7.3f}  {d:+7.3f}  {tag}")

    # Bytes present in golden but rare/missing in fpga
    print(f"\nbytes present in golden but <10 ppm in fpga:")
    for b in sorted(g_hist):
        gp = g_rel[b]
        fp = f_rel.get(b, 0.0)
        if gp > 1e-4 and fp < 1e-5:
            print(f"  0x{b:02x}: golden={gp*100:.3f}%  fpga={fp*100:.5f}%   "
                  "!! byte lost in FPGA path")
            fail = True

    # Bytes present in fpga but not in golden -- most-likely TPIU sync residue
    print(f"\nbytes >0.5% in fpga but not in golden (candidate TPIU residue "
          "or FPGA-injected):")
    for b in sorted(f_hist, key=f_hist.get, reverse=True)[:10]:
        fp = f_rel[b]
        gp = g_rel.get(b, 0.0)
        if fp > 5e-3 and gp < 1e-5:
            print(f"  0x{b:02x}: fpga={fp*100:.3f}%  golden={gp*100:.5f}%   "
                  "!! byte injected on FPGA path (or TPIU deframer leaked)")
            fail = True

    print()
    if fail:
        print("VERDICT: FAIL  (histograms diverge; see rows tagged OUT OF TOL)")
        sys.exit(1)
    print(f"VERDICT: PASS  (top-{a.topn} golden bytes all within {a.tol*100:.1f}% "
          "relative deviation)")


if __name__ == "__main__":
    main()
