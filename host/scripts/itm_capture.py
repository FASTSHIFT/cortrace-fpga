#!/usr/bin/env python3
"""itm_capture.py -- ONE FPGA capture -> hardware trace + NuttX note trace.

The target writes scheduler notes to an ITM stimulus port (CONFIG_ARMV7M_NOTE_ITM)
so they leave through the same parallel TPIU as ETM and DWT. A single capture
therefore carries all three on one time base:

  stream_grab (raw)  ->  cortrace-decode --raw --nx-switch-stream 1
                           |-- hardware Perfetto (ETM call stacks + thread lanes)
                           `-- --itm-note-out notes.bin (the NuttX note byte stream)
  notes.bin          ->  nxtrace (pynuttx) -> note Perfetto (.pftrace) + text dump
  align_check.py     ->  switch-by-switch agreement between the two

Outputs land in perftrace/. fused_<tag>.perfetto holds the hardware trace and the
notes on ONE time axis; hw_/note_ are the same data as separate files.

usage: itm_capture.py --elf nuttx --pynuttx DIR --iface NIC [--secs 1]
                      [--tcbmap tcbmap.txt] [--open]
  --pynuttx and --iface default to $PYNUTTX and $CORTRACE_IFACE.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WORKSPACE = os.path.abspath(os.path.join(REPO, ".."))
CORTRACE = os.environ.get(
    "CORTRACE_DECODE",
    os.path.join(WORKSPACE, "cortrace", "build-rel", "cortrace-decode"),
)
STREAM_GRAB = os.path.join(HERE, "stream_grab")
TRACE_CTRL = os.path.join(HERE, "trace_ctrl.py")
PERFETTO_OPEN = os.path.join(WORKSPACE, "cortrace", "scripts", "perfetto_open.py")


def run(cmd, **kw):
    print("[itm-capture] $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd, **kw)


def fuse(hw, note_pf, out, pb2=None, pynuttx=None):
    """Merge two Perfetto traces into one file.

    A Perfetto trace is a sequence of TracePackets, so concatenating files is a
    valid merge as long as the packet sequences stay distinct: packets sharing a
    trusted_packet_sequence_id share incremental state, and the note trace's
    trace_config / clock_snapshot / state-clearing packets would otherwise land
    on the same sequence as the 6M hardware track events. The note trace is
    small, so its sequence ids are remapped (leaving the big hardware file's
    bytes untouched).
    """
    if pb2 is None:
        spec = importlib.util.spec_from_file_location(
            "perfetto_trace_pb2",
            os.path.join(pynuttx, "nxtrace", "perfetto_trace_pb2.py"),
        )
        pb2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pb2)
    tr = pb2.Trace()
    with open(note_pf, "rb") as f:
        tr.ParseFromString(f.read())
    remapped = 0
    for pkt in tr.packet:
        if pkt.trusted_packet_sequence_id:  # 0 = legacy "no sequence", leave alone
            pkt.trusted_packet_sequence_id += 1000
            remapped += 1
    with open(out, "wb") as o:
        with open(hw, "rb") as fin:
            while True:
                chunk = fin.read(16 * 1024 * 1024)
                if not chunk:
                    break
                o.write(chunk)
        o.write(tr.SerializeToString())
    return remapped


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--elf", required=True)
    ap.add_argument("--secs", type=float, default=1.0)
    ap.add_argument(
        "--iface",
        default=os.environ.get("CORTRACE_IFACE"),
        help="capture NIC (default: $CORTRACE_IFACE; not needed with --raw-in)",
    )
    ap.add_argument(
        "--pynuttx",
        default=os.environ.get("PYNUTTX"),
        help="pynuttx checkout providing the nxtrace package (default: $PYNUTTX)",
    )
    ap.add_argument(
        "--cortrace-decode",
        default=CORTRACE,
        help="cortrace-decode binary (default: $CORTRACE_DECODE, else the "
        "sibling cortrace/build-rel checkout)",
    )
    ap.add_argument("--width", type=int, choices=(4, 2, 1), default=4)
    ap.add_argument(
        "--sysclk-hz", type=float, default=150e6, help="CPU clock (note timestamps)"
    )
    ap.add_argument(
        "--tsgen-hz",
        type=float,
        default=75e6,
        help="ETM TSGEN clock (hw wall-clock base)",
    )
    ap.add_argument(
        "--itm-port", type=int, default=1, help="CONFIG_ARMV7M_NOTE_ITM_PORT"
    )
    ap.add_argument("--tcbmap", default=None, help="nx_tcbmap.py output (thread names)")
    ap.add_argument(
        "--raw-in", default=None, help="skip the grab, decode this raw capture"
    )
    ap.add_argument("--out-dir", default=os.path.join(WORKSPACE, "perftrace"))
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--open", action="store_true", help="open the result in Perfetto")
    ap.add_argument(
        "--no-fuse",
        action="store_true",
        help="do not write the merged fused_<tag>.perfetto",
    )
    a = ap.parse_args()
    if not a.pynuttx:
        ap.error("pynuttx checkout not given: pass --pynuttx DIR or set $PYNUTTX")
    if not a.raw_in and not a.iface:
        ap.error("capture NIC not given: pass --iface or set $CORTRACE_IFACE")
    a.pynuttx = os.path.abspath(a.pynuttx)
    # nxtrace runs with cwd=pynuttx, so every path it sees must be absolute.
    a.elf = os.path.abspath(a.elf)
    a.out_dir = os.path.abspath(a.out_dir)
    if a.tcbmap:
        a.tcbmap = os.path.abspath(a.tcbmap)
    if a.raw_in:
        a.raw_in = os.path.abspath(a.raw_in)

    os.makedirs(a.out_dir, exist_ok=True)
    p = lambda name: os.path.join(a.out_dir, name.format(tag=a.tag))
    raw = a.raw_in or p("raw_{tag}.bin")
    hw = p("hw_{tag}.perfetto")
    notes_bin = p("notes_{tag}.bin")
    note_pf = p("note_{tag}.pftrace")
    note_txt = p("note_{tag}.txt")
    runs_tsv = p("hwruns_{tag}.tsv")

    if not a.raw_in:
        if run([sys.executable, TRACE_CTRL, "set-width", str(a.width)]).returncode:
            sys.exit("set-width failed")
        if run([STREAM_GRAB, a.iface, str(a.secs), raw, "256", "512"]).returncode:
            sys.exit("stream_grab failed")

    syms = os.path.join(tempfile.gettempdir(), "itm_capture_syms.nm")
    with open(syms, "w") as f:
        subprocess.run(["arm-none-eabi-nm", "-n", a.elf], stdout=f, check=True)

    dec = [
        a.cortrace_decode,
        raw,
        syms,
        "--elf",
        a.elf,
        "--raw",
        "--trace-width",
        str(a.width),
        "--memory-limit-mb",
        "0",
        "--nx-switch-stream",
        "1",
        "--hybrid-time",
        "--tsgen-hz",
        str(a.tsgen_hz),
        "--sysclk-hz",
        str(a.sysclk_hz),
        "--itm-note-port",
        str(a.itm_port),
        "--itm-note-unwrap",
        "--itm-note-out",
        notes_bin,
        "--perf",
        hw,
        "--nx-runs-out",
        runs_tsv,
    ]
    if a.tcbmap:
        dec += ["--nx-tcbmap", a.tcbmap]
    c = run(dec, capture_output=True, text=True)
    for line in (c.stderr + c.stdout).splitlines():
        if any(
            k in line
            for k in (
                "itm notes",
                "DWT stream",
                "stream health",
                "wrote",
                "error",
                "fatal",
            )
        ):
            print("  " + line.strip(), file=sys.stderr)
    if c.returncode or not os.path.isfile(notes_bin):
        sys.exit("decode failed")
    if os.path.getsize(notes_bin) == 0:
        sys.exit(
            "no note bytes on ITM port %d -- is CONFIG_ARMV7M_NOTE_ITM set and ITM TER enabled?"
            % a.itm_port
        )

    env = dict(
        os.environ, PYTHONPATH=a.pynuttx + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    nx = [
        sys.executable,
        "-m",
        "nxtrace",
        "capture",
        "--elf",
        a.elf,
        "--freq",
        str(int(a.sysclk_hz)),
    ]
    if a.tcbmap:
        nx += ["--pid-names", a.tcbmap]

    # 1) text dump of the notes, used to fit the hw<->note clock offset.
    with open(note_txt, "w") as f:
        run(
            nx + ["--format", "dump", "file", notes_bin],
            cwd=a.pynuttx,
            env=env,
            stdout=f,
            stderr=subprocess.DEVNULL,
        )

    # 2) fit the constant offset between the two clocks (same capture, so the
    #    switches pair up one-to-one; the offset is only the clock origin).
    off_file = p("offset_{tag}.txt")
    cmd = [
        sys.executable,
        os.path.join(HERE, "align_check.py"),
        "--hw-runs",
        runs_tsv,
        "--note",
        note_txt,
        "--offset-out",
        off_file,
    ]
    if a.tcbmap:
        cmd += ["--tcbmap", a.tcbmap]
    align_rc = run(cmd).returncode
    offset = (
        int(open(off_file).read())
        if align_rc == 0 and os.path.isfile(off_file)
        else None
    )

    # 3) note Perfetto, moved onto the hardware time axis when the fit worked.
    if os.path.exists(note_pf):
        os.unlink(note_pf)  # nxtrace appends to an existing output file
    nxo = list(nx)
    if offset is not None:
        nxo += ["--ts-offset-ns", str(-offset)]  # note_ns - offset = hw_ns
    run(
        nxo + ["-o", note_pf, "file", notes_bin],
        cwd=a.pynuttx,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 4) one file with both: Perfetto traces are repeated TracePacket messages, so
    #    concatenation is a valid merge (separate tracks, one time axis).
    fused = None
    if offset is not None and not a.no_fuse:
        fused = p("fused_{tag}.perfetto")
        fuse(hw, note_pf, fused, pynuttx=a.pynuttx)

    print(
        f"\nhardware : {hw}\nnote     : {note_pf}"
        + (
            "  (on the hardware time axis)"
            if offset is not None
            else "  (UNALIGNED: no clock fit)"
        )
    )
    if fused:
        print(f"fused    : {fused}")
    print(f"note text: {note_txt}\nraw notes: {notes_bin}")
    if a.open:
        for f in ([fused] if fused else [hw, note_pf]):
            run([sys.executable, PERFETTO_OPEN, f, "--keep"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
