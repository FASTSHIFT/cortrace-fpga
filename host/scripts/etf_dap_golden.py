#!/usr/bin/env python3
"""etf_dap_golden — dump the H743 ETF (embedded trace FIFO) over SWD via OpenOCD
and write a byte-for-byte golden file.

The ETF sits in the CoreSight bus at 0x5C014000 (TMC / SoC-400). Its RAM is
drained one 32-bit word at a time via the RRD register at +0x010. SWD reads
carry a hardware parity + retry, so the bytes read here are the TRUE ETM
output BEFORE the TPIU parallel-port + FPGA IDDR sampling that we want to
characterise -- i.e. the golden reference.

Byte order per word: the DAP dumps a 32-bit word as ASCII hex `dcba` where the
LSB (a) is the FIRST byte in ETF FIFO order. This matches CoreSight TMC's
little-endian native word layout (DDI0461B §2.1) and the on-board FPGA capture
byte order.

Endianness verified against the AGENT.md 2026-08-23 blind-test: 0x9f=0 in the
golden matches 0x9f≈0 in a good-tap FPGA capture; conservative tap 17 shows
massive 0x9f count on the FPGA side ONLY -- proving the byte order below is
right (else the histograms couldn't match on tap=2 and diverge on tap=17).

Usage:
    etf_dap_golden.py --out golden.bin [--words 1024]

Assumes:
    * ETM is already enabled and producing (a firmware selftrace loop, or
      etm_enable_h743.cfg has been run)
    * The ETF is currently in HW-FIFO mode feeding the TPIU (default after
      etm_enable_h743). This tool RE-POINTS it into CIRCULAR + freezes to
      drain -- see the manual restore section at the end of the report.

Prints:
    - RSZ / STS / RRP / RWP / MODE at the freeze instant (state you must trust
      when comparing to an FPGA capture)
    - How many valid bytes drained (words != 0xFFFFFFFF marker = "empty")
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BRINGUP = Path(__file__).resolve().parents[1]         # syn/artix7/bringup
REPO_ROOT = BRINGUP.parents[2]                        # orbtrace/


def run_openocd(cfg_env: dict, cfg_path: Path, timeout=30) -> str:
    """Run OpenOCD with the etf_dump config and return stdout."""
    cmd = [
        "openocd",
        "-f", "interface/cmsis-dap.cfg",
        "-f", "target/stm32h7x.cfg",
        "-f", str(cfg_path),
    ]
    env = os.environ.copy()
    env.update(cfg_env)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       cwd=REPO_ROOT, timeout=timeout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"openocd failed rc={r.returncode}")
    # OpenOCD writes echo output to stderr, not stdout
    return r.stderr


def parse_dump(log: str) -> tuple[bytes, dict]:
    """Extract 32-bit words between the RAM DUMP markers, convert to bytes."""
    m_begin = re.search(r"==== ETF RAM DUMP", log)
    m_end = re.search(r"==== END DUMP ====", log)
    if not m_begin or not m_end:
        raise SystemExit("etf dump markers missing in openocd output")
    body = log[m_begin.end():m_end.start()]
    words = []
    for line in body.splitlines():
        line = line.strip()
        if re.fullmatch(r"[0-9a-fA-F]{8}", line):
            words.append(int(line, 16))

    # 0xFFFFFFFF = "read past write pointer" marker (RRD returns this when
    # RRP == RWP or the buffer never got that far). Trim trailing sentinels.
    while words and words[-1] == 0xFFFFFFFF:
        words.pop()

    # Little-endian byte order: LSB first
    out = bytearray()
    for w in words:
        out.append(w & 0xFF)
        out.append((w >> 8) & 0xFF)
        out.append((w >> 16) & 0xFF)
        out.append((w >> 24) & 0xFF)

    # Also grab the reported state
    def _hex_val(name):
        mm = re.search(rf"{name}\s+=\s+(0x[0-9a-fA-F]+)", log)
        return int(mm.group(1), 16) if mm else None

    state = {
        "RSZ": _hex_val("RSZ"),
        "STS": _hex_val("STS"),
        "RRP": _hex_val("RRP"),
        "RWP": _hex_val("RWP"),
        "MODE": _hex_val("MODE"),
    }
    return bytes(out), state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="write golden bytes here")
    ap.add_argument("--words", type=int, default=1024,
                    help="how many RRD reads (each = 4 bytes; ETF is 4KB total)")
    ap.add_argument("--no-restore", action="store_true",
                    help="do NOT restore ETF to HW-FIFO after the dump "
                         "(default restores it). The dump sequence freezes the "
                         "ETF in circular mode with TraceCaptEn=0, which STOPS "
                         "data flowing to the TPIU pins -- so the FPGA/LA then "
                         "see only HSYNC filler until HW-FIFO is restored. "
                         "Auto-restoring here prevents the recurring 'only "
                         "HSYNC / only CLK+D3 moving' confusion.")
    a = ap.parse_args()

    cfg = BRINGUP / "target" / "etf_dump_h743.cfg"
    if not cfg.exists():
        raise SystemExit(f"missing {cfg}")

    log = run_openocd({"DUMP_WORDS": str(a.words)}, cfg)
    data, state = parse_dump(log)

    Path(a.out).write_bytes(data)
    print(f"wrote {len(data)} bytes -> {a.out}")
    print(f"ETF state at freeze: RSZ={state['RSZ']}  STS={state['STS']}  "
          f"RRP={state['RRP']}  RWP={state['RWP']}  MODE={state['MODE']}")
    if state.get("RSZ"):
        print(f"  (RSZ*4 = {state['RSZ']*4} bytes of ETF RAM)")

    if not a.no_restore:
        restore = BRINGUP / "target" / "etf_hw_fifo_restore.cfg"
        if restore.exists():
            run_openocd({}, restore, timeout=20)
            print("ETF restored to HW-FIFO (TPIU pins live again). "
                  "Use --no-restore to skip.")
        else:
            print(f"WARNING: {restore} missing; ETF left frozen -- FPGA/LA "
                  f"will see only HSYNC until you restore HW-FIFO manually.")


if __name__ == "__main__":
    main()
