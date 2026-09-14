#!/usr/bin/env python3
"""clock_probe — read the STM32H743 RCC PLL registers over the DAP and compute
the live clock tree: sysclk, pll1_r, the TRACECLK pin rate, and the CoreSight
timestamp-generator (TSGEN) frequency.

The TSGEN counter (the source of the ETM global timestamps) is clocked by
rcc_hclk = sysclk / HPRE on this part, so cortrace-decode --tsgen-hz can be fed
the value this prints to convert raw TSGEN counts to real ns.

The registers MUST be read while the firmware is RUNNING (halt, don't reset):
a reset-halt stops before the firmware programs PLL1, so you'd read the
power-on defaults (M=32/HSI) instead of the real operating point.

Usage:
  clock_probe.py [--hse-hz 25000000] [--interface cmsis-dap.cfg]
                 [--target stm32h7x.cfg] [--tsgen] [--json]
  --tsgen  additionally MEASURE the TSGEN rate by timed CNTCVL reads (a
           cross-check of the computed hclk).
"""

import argparse
import json
import re
import subprocess
import sys

RCC_PLLCKSELR = 0x58024428
RCC_PLL1DIVR = 0x58024430
RCC_D1CFGR = 0x58024418
TSGEN_CNTCVL = 0x5C005008

# HPRE[3:0] -> AHB prescaler. 0-7 = div1; 8..15 = div 2,4,8,16,64,128,256,512.
_HPRE_DIV = {8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}


def _openocd(iface, target, body_cmds):
    cmd = [
        "openocd",
        "-f",
        f"interface/{iface}",
        "-c",
        "gdb_port disabled; tcl_port disabled; telnet_port disabled",
        "-f",
        f"target/{target}",
        "-c",
        "init; halt",
    ]
    for c in body_cmds:
        cmd += ["-c", c]
    cmd += ["-c", "resume; shutdown"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr


def _grab(text, key):
    m = re.search(rf"{key}\s*=\s*(0x[0-9a-fA-F]+|\d+)", text)
    if not m:
        raise SystemExit(f"could not read {key} from openocd output:\n{text}")
    return int(m.group(1), 0)


def compute(hse_hz, cksel, divr, d1cfgr):
    m = (cksel >> 4) & 0x3F
    n = ((divr >> 0) & 0x1FF) + 1
    p = ((divr >> 9) & 0x7F) + 1
    r = ((divr >> 24) & 0x7F) + 1
    if not m or not p or not r:
        raise SystemExit(
            f"implausible PLL dividers M={m} P={p} R={r} "
            "(did you read at reset-halt instead of running?)"
        )
    vco = hse_hz * n / m
    sysclk = vco / p
    pll1_r = vco / r
    hpre = d1cfgr & 0xF
    hclk = sysclk / _HPRE_DIV.get(hpre, 1)
    return {
        "M": m,
        "N": n,
        "P": p,
        "R": r,
        "vco_hz": vco,
        "sysclk_hz": sysclk,
        "pll1_r_hz": pll1_r,
        "traceclk_pin_hz": pll1_r / 2,
        "hclk_hz": hclk,
        "tsgen_hz": hclk,  # TSGEN is clocked by rcc_hclk on the H743
    }


def measure_tsgen(iface, target, ms=2000):
    out = _openocd(
        iface,
        target,
        [
            f"set t0 [mrw {hex(TSGEN_CNTCVL)}]",
            "resume",
            f"sleep {ms}",
            "halt",
            f"set t1 [mrw {hex(TSGEN_CNTCVL)}]",
            "echo [format {MEAS_DELTA = %u} [expr {$t1 - $t0}]]",
        ],
    )
    delta = _grab(out, "MEAS_DELTA")
    return delta / (ms / 1000.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hse-hz", type=int, default=25_000_000)
    ap.add_argument("--interface", default="cmsis-dap.cfg")
    ap.add_argument("--target", default="stm32h7x.cfg")
    ap.add_argument("--tsgen", action="store_true", help="also measure TSGEN rate")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = _openocd(
        a.interface,
        a.target,
        [
            f"echo [format {{PLLCKSELR = 0x%08x}} [mrw {hex(RCC_PLLCKSELR)}]]",
            f"echo [format {{PLL1DIVR = 0x%08x}} [mrw {hex(RCC_PLL1DIVR)}]]",
            f"echo [format {{D1CFGR = 0x%08x}} [mrw {hex(RCC_D1CFGR)}]]",
        ],
    )
    clocks = compute(
        a.hse_hz,
        _grab(out, "PLLCKSELR"),
        _grab(out, "PLL1DIVR"),
        _grab(out, "D1CFGR"),
    )

    if a.tsgen:
        meas = measure_tsgen(a.interface, a.target)
        clocks["tsgen_measured_hz"] = meas

    if a.json:
        print(json.dumps(clocks))
        return 0

    print(f"  M={clocks['M']} N={clocks['N']} P={clocks['P']} R={clocks['R']}")
    print(f"  sysclk        = {clocks['sysclk_hz']/1e6:.3f} MHz")
    print(f"  pll1_r        = {clocks['pll1_r_hz']/1e6:.3f} MHz")
    print(f"  TRACECLK pin  = {clocks['traceclk_pin_hz']/1e6:.3f} MHz")
    print(f"  hclk = TSGEN  = {clocks['hclk_hz']/1e6:.3f} MHz")
    if a.tsgen:
        meas = clocks["tsgen_measured_hz"]
        err = 100 * (meas - clocks["hclk_hz"]) / clocks["hclk_hz"]
        print(f"  TSGEN measured= {meas/1e6:.3f} MHz  ({err:+.1f}% vs computed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
