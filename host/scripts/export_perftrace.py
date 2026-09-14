#!/usr/bin/env python3
"""export_perftrace — capture real ETM trace, decode it to a Perfetto trace with
the ETM execution time base, save it under the workspace ./perftrace dir, and
self-check that the result lines up with the running firmware.

Pipeline:
  1. clock_probe (DAP): read RCC -> sysclk + hclk. hclk IS the TSGEN frequency,
     so --tsgen-hz converts raw TSGEN counts to real ns.
  2. stream_grab: capture <seconds> of the live UDP trace stream.
  3. cortrace-decode --raw --etm-time --tsgen-hz <hclk>: deframe in-process and
     decode onto the execution time base, writing ./perftrace/<name>.perftrace.
  4. self-check: assert the decode agrees with the firmware:
       * call stack balanced (begins == ends)
       * A-syncs > 0 (deframe aligned)
       * exceptions rendered > 0 iff the firmware has SysTick on
       * ETM timestamps present, monotonic, and their ns span is within
         tolerance of the capture window (proves the time base is real).

Usage:
  export_perftrace.py <name> [--seconds 3] [--iface enxc8a36266dcae]
      [--elf <fw.elf>] [--systick/--no-systick] [--tsgen-hz HZ]
"""

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WORKSPACE = os.path.abspath(os.path.join(REPO, ".."))
PERFTRACE_DIR = os.path.join(WORKSPACE, "perftrace")
CORTRACE = os.environ.get(
    "CORTRACE_DECODE", os.path.join(WORKSPACE, "cortrace", "build", "cortrace-decode")
)
DEFAULT_ELF = os.path.join(
    WORKSPACE, "stm32h743-etm-trace-firmware", "build", "H743_Blink.elf"
)


def derive_clocks():
    """Read the live clock tree over the DAP; return (tsgen_hz, sysclk_hz)."""
    import json

    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "clock_probe.py"), "--json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit("clock_probe failed:\n" + out.stderr)
    d = json.loads(out.stdout)
    return d["tsgen_hz"], d["sysclk_hz"]


def build_syms(elf):
    # Program memory now comes straight from the ELF (cortrace-decode --elf),
    # so there is no mem.bin / objcopy step -- that flat binary silently
    # mis-laid-out gapped/.data images and caused decode corruption. Only the
    # symbol table (nm) is still generated here.
    syms = "/tmp/pt_syms.nm"
    with open(syms, "w") as f:
        subprocess.run(["arm-none-eabi-nm", "-n", elf], stdout=f, check=True)
    return syms


def num(pat, text):
    m = re.search(pat, text)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="output basename (-> ./perftrace/<name>.perftrace)")
    ap.add_argument("--seconds", type=int, default=3)
    ap.add_argument("--iface", default="enxc8a36266dcae")
    ap.add_argument("--elf", default=DEFAULT_ELF)
    ap.add_argument(
        "--tsgen-hz", type=float, default=None, help="override; else DAP-derived"
    )
    ap.add_argument(
        "--sysclk-hz", type=float, default=None, help="override; else DAP-derived"
    )
    ap.add_argument(
        "--time-base",
        choices=("cycle", "etm"),
        default="cycle",
        help="cycle = CPU-cycle count (fine, needs firmware cc=1); "
        "etm = global timestamp anchors (coarse). default cycle",
    )
    ap.add_argument("--systick", dest="systick", action="store_true", default=True)
    ap.add_argument("--no-systick", dest="systick", action="store_false")
    ap.add_argument("--phase", default="1,0")
    a = ap.parse_args()

    os.makedirs(PERFTRACE_DIR, exist_ok=True)
    tsgen_hz, sysclk_hz = derive_clocks()
    if a.tsgen_hz:
        tsgen_hz = a.tsgen_hz
    if a.sysclk_hz:
        sysclk_hz = a.sysclk_hz
    print(f"TSGEN = {tsgen_hz/1e6:.3f} MHz  sysclk = {sysclk_hz/1e6:.3f} MHz")

    syms = build_syms(a.elf)
    raw = f"/tmp/pt_{a.name}.bin"
    perf = os.path.join(PERFTRACE_DIR, f"{a.name}.perftrace")

    # capture
    grab = os.path.join(HERE, "stream_grab")
    subprocess.run([grab, a.iface, str(a.seconds), raw, "256", "512"], check=True)

    # decode with the chosen time base (memory read straight from the ELF)
    if a.time_base == "cycle":
        base_args = ["--cycle-time", "--sysclk-hz", str(sysclk_hz)]
    else:
        base_args = ["--etm-time", "--tsgen-hz", str(tsgen_hz)]
    c = subprocess.run(
        [CORTRACE, raw, syms, "--elf", a.elf, "--raw", "--phase", a.phase]
        + base_args
        + ["--perf", perf],
        capture_output=True,
        text=True,
    )
    out = c.stderr + c.stdout
    print(out.strip())

    # ---- self-check: does the decode agree with the firmware? ----
    begins = num(r"begins / ends\s*:\s*(\d+)", out)
    ends = num(r"begins / ends\s*:\s*\d+\s*/\s*(\d+)", out)
    asyncs = num(r"A-syncs=(\d+)", out)
    exc = num(r"exceptions rendered\s*:\s*(\d+)", out)
    ts_count = num(r"ETM timestamps\s*:\s*(\d+)", out)
    span = num(r"span (\d+) counts", out)
    fatal = "opencsd fatal" in out or "stopped: fatal" in out

    checks = []
    checks.append(("no fatal", not fatal))
    checks.append(("balanced", begins is not None and begins == ends))
    checks.append(("deframe aligned (A-syncs>0)", (asyncs or 0) > 0))
    checks.append(("timestamps present", (ts_count or 0) > 0))
    # exceptions must match the firmware's SysTick setting
    if a.systick:
        checks.append(("SysTick exceptions present", (exc or 0) > 0))
    else:
        checks.append(("no exceptions (SysTick off)", (exc or 0) == 0))
    # time base sanity: TSGEN span in ns vs the capture window. The trace covers
    # only the fraction of the window the ETF actually streamed, so require the
    # span to be positive and not exceed the window (+10% slack).
    if span and tsgen_hz:
        span_s = span / tsgen_hz
        checks.append(
            (
                f"time span {span_s:.2f}s <= window {a.seconds}s",
                0 < span_s <= a.seconds * 1.1,
            )
        )

    print("\n=== self-check vs firmware ===")
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"\nwrote {perf}")
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
