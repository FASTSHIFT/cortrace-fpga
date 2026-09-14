#!/usr/bin/env python3
"""e2e_soak — end-to-end streaming soak of REAL ETM trace through the full
decode stack: FPGA capture -> deframe -> cortrace-decode.

Unlike prbs_soak (byte-exact against a known PRBS), this runs the actual
firmware selftrace (any BB/SysTick config) and checks the strong structural
invariants cortrace reports per segment:
  * decode reached the end with NO fatal (whole segment consumed)
  * call stack balanced (begins == ends)
  * dropped_calls == 0  (no callee lost to a blind spot)
  * A-sync bad-rate == 0% on the deframed stream

mismatched_returns are NOT failed: with SysTick enabled an IRQ preempts a
function mid-body and the return-address heuristic legitimately re-balances,
which is expected (dropped_calls stays 0). Segments are captured, decoded, and
deleted one at a time (constant footprint). Runs for --minutes.

Usage: e2e_soak.py --minutes 5 [--seg-seconds 15] [--elf ...] [--iface] [--ip]
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DECODE = os.path.join(HERE, "..", "decode")
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", ".."))
CORTRACE = os.path.join(REPO, "cortrace", "build", "cortrace-decode")


def async_bad_rate(etm_path):
    b = open(etm_path, "rb").read()
    good = bad = zc = 0
    for c in b:
        if c == 0:
            zc += 1
        else:
            if c == 0x80 and zc >= 1:
                if zc >= 11:
                    good += 1
                else:
                    bad += 1
            zc = 0
    tot = good + bad
    return bad, tot


def decode_segment(raw, etm, mem, base, syms):
    # deframe
    r = subprocess.run([sys.executable, os.path.join(DECODE, "deframe_to_etm.py"),
                        raw, etm, "40000000"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return dict(ok=False, why="deframe failed: " + r.stderr[-200:])
    bad, tot = async_bad_rate(etm)
    # cortrace decode
    c = subprocess.run([CORTRACE, etm, mem, base, syms],
                       capture_output=True, text=True)
    out = c.stderr + c.stdout
    def num(pat):
        m = re.search(pat, out)
        return int(m.group(1)) if m else None
    fatal = "stopped: fatal" in out or "opencsd fatal" in out
    begins = num(r"begins / ends\s*:\s*(\d+)")
    ends = num(r"begins / ends\s*:\s*\d+\s*/\s*(\d+)")
    dropped = num(r"dropped calls\s*:\s*(\d+)")
    mism = num(r"mismatched returns\s*:\s*(\d+)")
    exc = num(r"exceptions rendered\s*:\s*(\d+)")
    proc = num(r"etm bytes processed\s*:\s*(\d+)")
    balanced = (begins is not None and begins == ends)
    ok = (not fatal and balanced and dropped == 0 and bad == 0)
    return dict(ok=ok, fatal=fatal, balanced=balanced, begins=begins,
                dropped=dropped, mism=mism, exc=exc, proc=proc,
                async_bad=bad, async_tot=tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--seg-seconds", type=int, default=15)
    ap.add_argument("--iface", default="enxc8a36266dcae")
    ap.add_argument("--ip", default="192.168.10.42")
    ap.add_argument("--elf", default=None, help="firmware ELF (for mem+syms)")
    ap.add_argument("--tmp", default="/tmp/e2e_seg.bin")
    a = ap.parse_args()

    # build mem.bin + syms.nm from the ELF once
    elf = a.elf or os.path.join(
        REPO, "stm32h743-etm-trace-firmware", "build", "H743_Blink.elf")
    mem = "/tmp/e2e_mem.bin"
    syms = "/tmp/e2e_syms.nm"
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                    "-j", ".isr_vector", "-j", ".text", "-j", ".rodata",
                    "-j", ".ARM", "-j", ".init_array", "-j", ".fini_array",
                    "-j", ".data", elf, mem], check=True)
    with open(syms, "w") as f:
        subprocess.run(["arm-none-eabi-nm", "-n", elf], stdout=f, check=True)
    print(f"ELF={elf}  mem={os.path.getsize(mem)}B")

    grab = os.path.join(HERE, "stream_grab")
    etm = "/tmp/e2e_etm.bin"
    t0 = time.time()
    t_end = t0 + a.minutes * 60
    seg = 0
    tot_proc = 0
    tot_begins = tot_exc = 0
    rc = 0
    while time.time() < t_end:
        seg += 1
        g = subprocess.run([grab, a.iface, str(a.seg_seconds), a.tmp, "256", "512"],
                           capture_output=True, text=True)
        grab_ok = ("seq-gap events=0" in g.stdout
                   and "ring-full dropped bytes=0" in g.stdout)
        res = decode_segment(a.tmp, etm, mem, "08000000", syms)
        el = time.time() - t0
        if not res.get("balanced") is None:
            tot_proc += res.get("proc") or 0
            tot_begins += res.get("begins") or 0
            tot_exc += res.get("exc") or 0
        print(f"[{el:6.1f}s] seg{seg:03d} proc={res.get('proc')} "
              f"bal={res.get('balanced')} drop={res.get('dropped')} "
              f"mism={res.get('mism')} exc={res.get('exc')} "
              f"async_bad={res.get('async_bad')}/{res.get('async_tot')} "
              f"grab={'ok' if grab_ok else 'GAP'} -> "
              f"{'OK' if (res['ok'] and grab_ok) else 'FAIL'}", flush=True)
        if not (res["ok"] and grab_ok):
            print(f"!! FAIL seg{seg}: {res}", flush=True)
            os.rename(a.tmp, a.tmp + f".bad_seg{seg}")
            rc = 1
            break
        os.remove(a.tmp)

    dur = time.time() - t0
    print("\n==== E2E SOAK SUMMARY ====")
    print(f"  duration       : {dur:.1f}s over {seg} segments")
    print(f"  ETM decoded     : {tot_proc/1e6:.1f} MB (0 fatal)")
    print(f"  slice begins    : {tot_begins}")
    print(f"  exceptions      : {tot_exc}")
    print("  VERDICT: " + ("PASS — every segment fully decoded, balanced, "
          "0 dropped, 0 bad A-sync" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
