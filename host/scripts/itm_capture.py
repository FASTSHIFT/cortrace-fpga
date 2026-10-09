#!/usr/bin/env python3
"""itm_capture.py -- grab ONE raw capture from the FPGA, then hand it to cortrace.

This script only does the capture-hardware part: set the trace width on the
FPGA and stream the raw UDP capture to a file. Everything that interprets the
capture (decode, NuttX note extraction, clock alignment, Perfetto fusion) lives
in the cortrace repo as scripts/cortrace_fuse.py, which this script runs on the
file it just grabbed. Options this script does not know are passed through to
cortrace_fuse.py, so e.g. --elf, --pynuttx, --tcbmap, --open work as there.

usage: itm_capture.py --iface NIC --cortrace-dir DIR --elf nuttx --pynuttx DIR
                      [--secs 1] [--width 4] [--tcbmap map.txt] [--open]
  --iface defaults to $CORTRACE_IFACE, --cortrace-dir to $CORTRACE_DIR (else the
  sibling ../cortrace checkout), --pynuttx to $PYNUTTX (read by cortrace_fuse).
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WORKSPACE = os.path.abspath(os.path.join(REPO, ".."))
STREAM_GRAB = os.path.join(HERE, "stream_grab")
TRACE_CTRL = os.path.join(HERE, "trace_ctrl.py")


def run(cmd, **kw):
    print("[itm-capture] $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd, check=False, **kw)


def default_cortrace_dir():
    return os.environ.get("CORTRACE_DIR") or os.path.join(WORKSPACE, "cortrace")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--iface",
        default=os.environ.get("CORTRACE_IFACE"),
        help="capture NIC (default: $CORTRACE_IFACE; not needed with --raw-in)",
    )
    ap.add_argument(
        "--cortrace-dir",
        default=default_cortrace_dir(),
        help="cortrace checkout holding scripts/cortrace_fuse.py "
        "(default: $CORTRACE_DIR, else the sibling cortrace/)",
    )
    ap.add_argument("--secs", type=float, default=1.0)
    ap.add_argument("--width", type=int, choices=(4, 2, 1), default=4)
    ap.add_argument(
        "--raw-in", default=None, help="skip the grab, fuse this raw capture"
    )
    ap.add_argument("--out-dir", default=os.path.join(WORKSPACE, "perftrace"))
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    a, passthrough = ap.parse_known_args(argv)
    if not a.raw_in and not a.iface:
        ap.error("capture NIC not given: pass --iface or set $CORTRACE_IFACE")
    a.out_dir = os.path.abspath(a.out_dir)
    return a, passthrough


def grab(a, raw):
    if run([sys.executable, TRACE_CTRL, "set-width", str(a.width)]).returncode:
        sys.exit("set-width failed")
    cmd = [STREAM_GRAB, a.iface, str(a.secs), raw, "256", "512"]
    if run(cmd).returncode:
        sys.exit("stream_grab failed")


def fuse_command(a, raw, passthrough):
    fuse = os.path.join(a.cortrace_dir, "scripts", "cortrace_fuse.py")
    return (
        [sys.executable, fuse, "--raw", raw, "--tag", a.tag]
        + ["--out-dir", a.out_dir, "--width", str(a.width)]
        + passthrough
    )


def main(argv=None):
    a, passthrough = parse_args(argv)
    a.cortrace_dir = os.path.abspath(a.cortrace_dir)
    if not os.path.isfile(os.path.join(a.cortrace_dir, "scripts", "cortrace_fuse.py")):
        sys.exit(
            f"cortrace_fuse.py not found under {a.cortrace_dir}; use --cortrace-dir"
        )
    os.makedirs(a.out_dir, exist_ok=True)
    raw = os.path.abspath(a.raw_in) if a.raw_in else None
    if raw is None:
        raw = os.path.join(a.out_dir, f"raw_{a.tag}.bin")
        grab(a, raw)
    return run(fuse_command(a, raw, passthrough)).returncode


if __name__ == "__main__":
    sys.exit(main())
