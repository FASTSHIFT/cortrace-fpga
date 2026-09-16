#!/usr/bin/env python3
"""cortrace_live -- one command: capture -> decode -> open in Perfetto.

P2 of docs/01-perfetto-live-bridge.md. Chains the pieces that already exist so
a single invocation goes from the live trace stream to a rendered Perfetto
timeline in the browser, no manual file wrangling:

  1. (optional) set the FPGA TPIU port width          (trace_ctrl.py set-width)
  2. capture N seconds of the UDP trace stream         (stream_grab)
  3. decode raw -> Perfetto with the chosen time base  (cortrace-decode)
  4. open it in ui.perfetto.dev                         (perfetto_open.py)

This orchestrator intentionally does NOT arm the trace path: the DAP-only
bring-up (nxtrace_dap.cfg) is a one-time, must-stay-resident session (it keeps
C_DEBUGEN=1 so NuttX won't clear the DWT). Arm that separately and leave it
attached; this script just captures against the already-armed hardware.

Thread names: heap-allocated TCBs are not in the ELF, so pass --nx-tcbmap with
a map dumped by nx_tcbmap.py (which needs the probe, so dump it while the DAP
session is momentarily free). Without it, threads show as tcb@0x<addr>.

Usage:
  cortrace_live.py [--secs 1] [--iface enxc8a36266dcae] [--width {4,2,1}]
      --elf <nuttx> [--sysclk-hz 150000000] [--time-base cycle|etm]
      [--nx-switch-stream 1] [--nx-tcbmap map.txt] [--raw-in FILE]
      [--save out.perfetto] [--keep] [--no-open]
"""

import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WORKSPACE = os.path.abspath(os.path.join(REPO, ".."))


def _default_cortrace():
    rel = os.path.join(WORKSPACE, "cortrace", "build-rel", "cortrace-decode")
    dbg = os.path.join(WORKSPACE, "cortrace", "build", "cortrace-decode")
    return rel if os.path.exists(rel) else dbg


CORTRACE = os.environ.get("CORTRACE_DECODE", _default_cortrace())
PERFETTO_OPEN = os.path.join(WORKSPACE, "cortrace", "scripts", "perfetto_open.py")
STREAM_GRAB = os.path.join(HERE, "stream_grab")
TRACE_CTRL = os.path.join(HERE, "trace_ctrl.py")


def _run(cmd, **kw):
    print("[cortrace-live] $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd, **kw)


def build_syms(elf):
    syms = os.path.join(tempfile.gettempdir(), "cortrace_live_syms.nm")
    with open(syms, "w") as f:
        subprocess.run(["arm-none-eabi-nm", "-n", elf], stdout=f, check=True)
    return syms


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="capture -> decode -> open in Perfetto, in one command"
    )
    # capture
    ap.add_argument("--secs", type=float, default=1.0, help="capture seconds")
    ap.add_argument("--iface", default="enxc8a36266dcae", help="capture NIC")
    ap.add_argument(
        "--width",
        type=int,
        choices=(4, 2, 1),
        default=None,
        help="set FPGA TPIU port width before capture (default: leave as-is)",
    )
    ap.add_argument(
        "--raw-in",
        default=None,
        help="skip capture; decode this existing raw .bin instead",
    )
    ap.add_argument(
        "--sudo",
        action="store_true",
        help="run stream_grab under sudo (raw socket needs privilege)",
    )
    # decode
    ap.add_argument("--elf", required=True, help="NuttX/firmware ELF")
    ap.add_argument(
        "--trace-width",
        type=int,
        choices=(4, 2, 1),
        default=None,
        help="deframe width (default: follow --width, else 4)",
    )
    ap.add_argument("--time-base", choices=("cycle", "etm"), default="cycle")
    ap.add_argument("--sysclk-hz", type=float, default=150_000_000)
    ap.add_argument("--tsgen-hz", type=float, default=None)
    ap.add_argument("--nx-switch-stream", type=int, default=1)
    ap.add_argument("--nx-tcbmap", default=None, help="TCB->name map (nx_tcbmap.py)")
    ap.add_argument(
        "--phase", default=None, help="lock deframe phase, e.g. 1,0 (default: search)"
    )
    # visualize
    ap.add_argument("--save", default=None, help="also keep the .perfetto here")
    ap.add_argument("--keep", action="store_true", help="perfetto_open --keep")
    ap.add_argument("--no-open", dest="do_open", action="store_false")
    ap.add_argument("--port", type=int, default=0, help="perfetto_open port")
    a = ap.parse_args(argv)

    if not os.path.exists(CORTRACE):
        sys.exit(f"cortrace-decode not found: {CORTRACE} (set CORTRACE_DECODE)")

    trace_width = a.trace_width or a.width or 4

    # 1. optional FPGA width
    if a.width is not None:
        r = _run([sys.executable, TRACE_CTRL, "set-width", str(a.width)])
        if r.returncode != 0:
            sys.exit("set-width failed")

    # 2. capture (or reuse a raw file)
    if a.raw_in:
        raw = a.raw_in
        if not os.path.isfile(raw):
            sys.exit(f"--raw-in not found: {raw}")
        print(f"[cortrace-live] using existing raw {raw}", file=sys.stderr)
    else:
        raw = os.path.join(tempfile.gettempdir(), "cortrace_live.bin")
        grab_cmd = [STREAM_GRAB, a.iface, str(a.secs), raw, "256", "512"]
        if a.sudo:
            grab_cmd = ["sudo"] + grab_cmd
        r = _run(grab_cmd)
        if r.returncode != 0:
            sys.exit("stream_grab failed")

    # 3. decode -> perfetto
    if a.save:
        perf = a.save
    else:
        perf = os.path.join(tempfile.gettempdir(), "cortrace_live.perfetto")
    syms = build_syms(a.elf)

    if a.time_base == "cycle":
        base = ["--cycle-time", "--sysclk-hz", str(a.sysclk_hz)]
    else:
        if not a.tsgen_hz:
            sys.exit("--time-base etm needs --tsgen-hz")
        base = ["--etm-time", "--tsgen-hz", str(a.tsgen_hz)]

    dec = (
        [
            CORTRACE,
            raw,
            syms,
            "--elf",
            a.elf,
            "--raw",
            "--trace-width",
            str(trace_width),
            "--memory-limit-mb",
            "0",
            "--nx-switch-stream",
            str(a.nx_switch_stream),
        ]
        + base
        + ["--perf", perf]
    )
    if a.phase:
        dec += ["--phase", a.phase]
    if a.nx_tcbmap:
        dec += ["--nx-tcbmap", a.nx_tcbmap]

    c = _run(dec, capture_output=True, text=True)
    out = c.stderr + c.stdout
    # Surface only the summary lines (avoid flooding on big captures).
    for line in out.splitlines():
        if any(
            k in line
            for k in (
                "stream health",
                "begins / ends",
                "dropped calls",
                "switch events",
                "wrote",
                "loaded",
                "error",
                "fatal",
            )
        ):
            print("  " + line.strip(), file=sys.stderr)
    if c.returncode != 0 or not os.path.isfile(perf):
        sys.exit("decode failed")

    # 4. open in Perfetto
    if not a.do_open:
        print(perf)
        print(
            f"[cortrace-live] decoded {perf}; --no-open, not launching browser.",
            file=sys.stderr,
        )
        return 0

    open_cmd = [sys.executable, PERFETTO_OPEN, perf, "--port", str(a.port)]
    if a.keep:
        open_cmd.append("--keep")
    return _run(open_cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
