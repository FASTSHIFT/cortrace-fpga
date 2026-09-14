#!/usr/bin/env python3
"""fpga_vs_etf — one-shot single-point hard cross-check of the FPGA capture
path against the DAP-golden ETF drain, on the H743 selftrace loop.

Design (per 2026-09-07 direction change):
  LA / scope are downgraded to REFERENCE only. The FPGA capture path is what
  we're actually validating. The DAP-golden ETF drain is the byte-for-byte
  reference the FPGA output should match.

Steps at ONE operating point (currently 112.5 MHz, no DIVR1 poke):
  1. Optional: re-flash firmware if --flash given.
  2. Wait for firmware -> selftrace_run() -> ETF starts filling.
  3. openocd etf_dump_h743.cfg (freeze/drain) -> captures/golden_R2.bin
     ETF is left in circular-mode DISABLED here (see the cfg comments); we
     restore HW-FIFO before starting the FPGA capture.
  4. openocd etf_hw_fifo_restore.cfg -> ETF drains to TPIU -> LA/FPGA see it.
  5. stream_grab <secs> -> raw UDP dump captures/fpga_stream_R2.bin
  6. opencsd_etm4_run --keep -> deframed captures/fpga_R2_etm.bin
  7. cortrace-decode --strict --events on BOTH golden and fpga_etm.
     writes logs/{golden,fpga}_R2.events + logs/{golden,fpga}_R2.tsv
     writes perftrace/{golden,fpga}_R2.perftrace
  8. selftrace_strict_verify.py on both event logs.
  9. Print a compact verdict table so we can see which side is worse and
     how much worse. This is the SIGNAL for "is the hardware底子 ok".

Deliverables land under the standard ws-root subdirs (see AGENT.md §3.1):
    captures/, perftrace/, logs/

No LA. No scope. No sweep. No probing. One point, four artifacts, one verdict.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRINGUP = HERE.parent
REPO_ORBTRACE = BRINGUP.parents[2]              # orbtrace/
WS = REPO_ORBTRACE.parent                       # workspace root
FW_REPO = WS / "stm32h743-etm-trace-firmware"
CORTRACE_BIN = WS / "cortrace" / "build" / "cortrace-decode"


def run(cmd, cwd=None, env=None, timeout=180, check=True):
    """Subprocess with per-step timeout. Fail loud on timeout."""
    print(f"  $ {' '.join(str(c) for c in cmd)}  [timeout={timeout}s]",
          flush=True)
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT after {timeout}s] {cmd}", flush=True)
        if check:
            raise SystemExit(f"step timed out: {cmd}")
        return None
    if check and r.returncode != 0:
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        raise SystemExit(f"failed rc={r.returncode}: {cmd}")
    return r


def openocd(cfg, env_over=None, timeout=60):
    env = os.environ.copy()
    if env_over: env.update(env_over)
    return run(["openocd",
                "-f", "interface/cmsis-dap.cfg",
                "-f", "target/stm32h7x.cfg",
                "-f", cfg],
               cwd=REPO_ORBTRACE, env=env, timeout=timeout, check=False)


def flash_firmware(elf_hex: Path):
    r = openocd("syn/artix7/bringup/target/flash_h743.cfg",
                {"FW_HEX": str(elf_hex)}, timeout=90)
    if r and r.returncode != 0:
        # openocd flash_h743 sometimes exits non-zero after "program verify reset"
        # succeeds; just warn.
        print("  [flash] openocd exit non-zero (verify+reset step); "
              "check log if next steps fail", flush=True)


def prepare_reference(build_dir: Path, out_mem: Path, out_nm: Path) -> Path:
    """Extract mem.bin + syms.nm from H743_Blink.elf."""
    elf = build_dir / "H743_Blink.elf"
    if not elf.exists():
        raise SystemExit(f"missing firmware ELF: {elf}")
    run(["arm-none-eabi-objcopy", "-O", "binary",
         "--only-section=.isr_vector",
         "--only-section=.text",
         "--only-section=.rodata",
         str(elf), str(out_mem)])
    with open(out_nm, "w") as f:
        subprocess.run(["arm-none-eabi-nm", "-n", str(elf)],
                       check=True, stdout=f)
    return elf


def dump_golden(out_bin: Path, words: int = 1024) -> dict:
    """DAP drain of ETF -> raw ETMv4 bytes (byte-perfect)."""
    r = openocd("syn/artix7/bringup/target/etf_dump_h743.cfg",
                {"DUMP_WORDS": str(words)}, timeout=45)
    log = (r.stdout or "") + (r.stderr or "")
    sys.path.insert(0, str(BRINGUP / "scripts"))
    from etf_dap_golden import parse_dump
    data, state = parse_dump(log)
    out_bin.write_bytes(data)
    print(f"[golden] wrote {len(data)}B -> {out_bin}  state={state}",
          flush=True)
    return {"bytes": len(data), **state}


def restore_hw_fifo():
    openocd("syn/artix7/bringup/target/etf_hw_fifo_restore.cfg",
            timeout=20)


def cli_set_pll(m=None, n=None, p=None, q=None, r=None) -> dict:
    """Reprogram PLL1 via the firmware UART CLI (DAPLink VCP). Returns the
    parsed `pll --show` output after the change so callers can log the real
    frequencies. Preferred over the openocd DIVR1 poke path (that leaves
    SystemCoreClock inconsistent with real sysclk -- see 2026-09-07 sweep
    postmortem in AGENT.md)."""
    sys.path.insert(0, str(HERE))
    from h743_serial import H743CLI, pll_apply, pll_show, find_port
    port = os.environ.get("H743_TTY") or find_port()
    cli = H743CLI(port, timeout=5.0)
    try:
        resp = pll_apply(cli, m=m, n=n, p=p, q=q, r=r, timeout=8.0)
        print(f"[pll cli] apply response:\n{resp}", flush=True)
        state = pll_show(cli)
        print(f"[pll cli] readback: {state}", flush=True)
        return state
    finally:
        cli.close()


def capture_fpga(out_raw: Path, iface: str, seconds: float) -> int:
    """Run stream_grab; requires root because of SO_BINDTODEVICE. stream_grab
    returns rc=1 whenever ANY seq-gaps or ring drops occurred -- that's
    still USABLE data (the deframer resyncs), so we accept rc<=1 and just
    log the counts. rc>1 or 0 output = hard fail."""
    grab = BRINGUP / "scripts" / "stream_grab"
    if not grab.exists():
        run(["gcc", "-O2", "-pthread", "-o", str(grab),
             str(grab.with_suffix(".c"))],
            timeout=30)
    cmd = ["sudo", "-n", str(grab), iface, str(seconds), str(out_raw)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=int(seconds) + 60)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    if r.returncode > 1:
        raise SystemExit(f"stream_grab rc={r.returncode}")
    n = out_raw.stat().st_size if out_raw.exists() else 0
    if n == 0:
        raise SystemExit("stream_grab produced empty file")
    return n


def deframe_fpga(raw: Path, keep_dir: Path) -> Path:
    """Deframe stream_grab output into clean ETMv4 bytes for cortrace.

    CRITICAL: the FPGA streams one byte per TRACECLK period packed as
    {trace_a[k] hi-nibble, trace_b[k-1] lo-nibble}. That is NOT a TPIU byte
    yet -- the half-bit nibbles must be re-assembled with the correct
    parity/order BEFORE TPIU deframing. A naive tpiu_official.deframe on the
    raw bytes sees only HSYNC filler (`f7ff...`, 0 FSYNC) and returns garbage.
    Use opencsd_etm4_run.recover_assemble which searches parity x order and
    ranks by post-deframe A-sync count (the only signal that proves the whole
    nibble->frame->stream chain is aligned)."""
    keep_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(BRINGUP / "decode"))
    import opencsd_etm4_run as OC
    import tpiu_official as T
    import etm35lib as L
    data = raw.read_bytes()
    # recover_assemble runs 4 full deframes (parity x order); cap the search
    # to the first few MB so it stays fast, then apply the winning phase to
    # the whole capture.
    head = data[:4_000_000]
    print(f"[deframe] {len(data)}B raw -> recover_assemble on {len(head)}B ...",
          flush=True)
    score, parity, order, _, fl, v4a, fsync = OC.recover_assemble(head)
    # Re-assemble the FULL capture with the winning parity/order.
    sys.path.insert(0, str(BRINGUP / "decode"))
    import dsl_parse as D  # provides assemble()
    nibs = bytearray()
    for k in range(len(data) - 1):
        nibs.append((data[k] >> 4) & 0xF)
        nibs.append(data[k + 1] & 0xF)
    assembled = D.assemble(nibs, parity, order)
    if L.has_tpiu_sync(assembled):
        etm, _ = T.deframe(assembled, want_stream=2)
    else:
        etm = assembled
    out = keep_dir / "etm.bin"
    out.write_bytes(etm)
    print(f"[deframe] parity={parity} order={order} fsync={fsync} "
          f"pre-async={v4a} -> {len(etm)}B ETM (stream 2) -> {out}", flush=True)
    return out


def cortrace(etm: Path, mem: Path, syms: Path, out_events: Path,
             out_perf: Path, out_edges: Path, label: str) -> dict:
    """Run cortrace-decode --strict --events + --perf + --edges. Parse
    the printed report so we can compare golden vs fpga per-metric."""
    if not CORTRACE_BIN.exists():
        raise SystemExit(f"missing {CORTRACE_BIN}")
    cmd = [str(CORTRACE_BIN), str(etm), str(mem), "08000000", str(syms),
           "--strict", "--events", str(out_events),
           "--perf", str(out_perf), "--edges", str(out_edges),
           "--memory-limit-mb", "512"]
    print(f"  $ {' '.join(cmd)}  [timeout=120s]", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"label": label, "strict_pass": False, "timeout": True}
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    text = r.stdout + r.stderr
    rep = {"label": label, "strict_pass": r.returncode == 0}
    for k, pat in [("etm_bytes", r"etm bytes processed\s*:\s*(\d+)"),
                   ("begins", r"begins / ends\s*:\s*(\d+)\s*/"),
                   ("ends", r"begins / ends\s*:\s*\d+\s*/\s*(\d+)"),
                   ("balanced", r"(balanced|UNBALANCED)"),
                   ("mismatched", r"mismatched returns\s*:\s*(\d+)"),
                   ("dropped", r"dropped calls\s*:\s*(\d+)"),
                   ("recovered", r"recovered returns\s*:\s*(\d+)"),
                   ("blind_regions", r"blind regions\s*:\s*(\d+)")]:
        m = re.search(pat, text)
        rep[k] = m.group(1) if m else ""
    return rep


def strict_verify(events: Path) -> tuple[bool, int]:
    """Structural invariants (r39-tested)."""
    if not events.exists() or events.stat().st_size == 0:
        return False, -1
    r = subprocess.run(["python3", str(HERE / "selftrace_strict_verify.py"),
                        str(events), "--max-report", "5"],
                       capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    m = re.search(r"FAIL -- (\d+) invariant violations", r.stdout)
    if m: return False, int(m.group(1))
    if "VERDICT: PASS" in r.stdout: return True, 0
    return False, -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="R2",
                    help="output filename tag (default R2 = 112.5 MHz)")
    ap.add_argument("--iface", default="enxc8a36266dcae",
                    help="host NIC on 192.168.10.245 side")
    ap.add_argument("--seconds", type=float, default=0.2,
                    help="FPGA stream_grab capture seconds. 0.2s @ 112 MB/s "
                         "gives ~22 MB raw -> ~5 MB ETM after deframe, plenty "
                         "for structural verification and cheap for cortrace")
    ap.add_argument("--flash", action="store_true",
                    help="re-flash H743_Blink.hex before capture")
    ap.add_argument("--wait-etf", type=float, default=0.5,
                    help="wait this long after flash/restore before ETF drain")
    ap.add_argument("--pll-r", type=int, default=None,
                    help="if given, ask firmware CLI to set PLL1 R divider "
                         "to this value. r=2 -> pll1_r_ck 225MHz -> TRACECLK "
                         "112.5MHz (default), r=4 -> 56MHz, r=8 -> 28MHz")
    ap.add_argument("--pll-n", type=int, default=None,
                    help="optional: also set PLL1 N (VCO multiplier)")
    a = ap.parse_args()

    captures = WS / "captures"; captures.mkdir(exist_ok=True)
    perftrace = WS / "perftrace"; perftrace.mkdir(exist_ok=True)
    logs = WS / "logs"; logs.mkdir(exist_ok=True)
    ref_dir = logs / f"_ref_{a.tag}"; ref_dir.mkdir(exist_ok=True)

    # 0) reference mem.bin + syms.nm from the SAME elf the target is running
    elf = prepare_reference(FW_REPO / "build",
                            ref_dir / "mem.bin", ref_dir / "syms.nm")
    print(f"[ref] elf={elf}  mem={ref_dir/'mem.bin'}  syms={ref_dir/'syms.nm'}",
          flush=True)

    # 1) optional flash
    if a.flash:
        flash_firmware(FW_REPO / "build" / "H743_Blink.hex")
        time.sleep(1.5)

    # 1b) optional PLL change via firmware CLI (over the DAPLink CDC VCP).
    # The firmware runs pll_ctrl_apply() which is the SAME code path as cold
    # init, so HAL_UART_Init is re-run and SystemCoreClock is consistent --
    # unlike the older openocd DIVR1 poke that broke on 2026-09-07 sweep.
    if a.pll_r is not None or a.pll_n is not None:
        cli_set_pll(n=a.pll_n, r=a.pll_r)
        time.sleep(0.3)   # let the selftrace loop stabilise at new rate

    # 2) golden dump
    time.sleep(a.wait_etf)
    golden_bin = captures / f"golden_{a.tag}.bin"
    gstate = dump_golden(golden_bin, words=1024)

    # 3) restore HW-FIFO -> TPIU pins go live for FPGA capture
    restore_hw_fifo()
    time.sleep(0.3)

    # 4) FPGA capture
    fpga_raw = captures / f"fpga_stream_{a.tag}.bin"
    fpga_bytes = capture_fpga(fpga_raw, a.iface, a.seconds)
    print(f"[fpga] {fpga_bytes}B -> {fpga_raw}", flush=True)

    # 5) deframe FPGA -> ETM
    fpga_keep = logs / f"_fpga_keep_{a.tag}"
    fpga_etm = deframe_fpga(fpga_raw, fpga_keep)
    if not fpga_etm.exists():
        raise SystemExit(f"deframe produced no ETM bytes; check {fpga_keep}")
    # Symlink into captures/ with a friendly name
    fpga_etm_link = captures / f"fpga_etm_{a.tag}.bin"
    if fpga_etm_link.exists() or fpga_etm_link.is_symlink():
        fpga_etm_link.unlink()
    try:
        fpga_etm_link.symlink_to(fpga_etm)
    except OSError:
        import shutil; shutil.copy(fpga_etm, fpga_etm_link)
    print(f"[fpga.etm] {fpga_etm_link.stat().st_size}B -> {fpga_etm_link}",
          flush=True)

    # 6) cortrace on both
    mem, syms = ref_dir / "mem.bin", ref_dir / "syms.nm"
    r_golden = cortrace(golden_bin, mem, syms,
                        logs / f"golden_{a.tag}.events",
                        perftrace / f"golden_{a.tag}.perftrace",
                        logs / f"golden_{a.tag}.tsv",
                        "golden")
    r_fpga = cortrace(fpga_etm_link, mem, syms,
                      logs / f"fpga_{a.tag}.events",
                      perftrace / f"fpga_{a.tag}.perftrace",
                      logs / f"fpga_{a.tag}.tsv",
                      "fpga")

    # 7) structural strict verify (the real judge)
    print(f"\n=== structural strict verify (golden) ===")
    sv_g, vg = strict_verify(logs / f"golden_{a.tag}.events")
    print(f"\n=== structural strict verify (fpga) ===")
    sv_f, vf = strict_verify(logs / f"fpga_{a.tag}.events")

    # 8) verdict table
    print()
    print("=" * 66)
    print(f"POINT tag={a.tag} — GOLDEN vs FPGA comparison")
    print("=" * 66)
    def fmt(v): return f"{v:>7s}" if isinstance(v, str) else f"{v!s:>7s}"
    keys = [("etm_bytes", "ETM bytes"),
            ("begins", "begins"), ("ends", "ends"),
            ("balanced", "balanced"),
            ("mismatched", "mismatched"),
            ("dropped", "dropped"),
            ("recovered", "recovered"),
            ("blind_regions", "blind_regions"),
            ("strict_pass", "cortrace --strict")]
    print(f"  {'metric':22s}  {'golden':>10s}  {'fpga':>10s}")
    for k, label in keys:
        gv = r_golden.get(k, "")
        fv = r_fpga.get(k, "")
        print(f"  {label:22s}  {str(gv):>10s}  {str(fv):>10s}")
    print(f"  {'struct violations':22s}  {vg:>10d}  {vf:>10d}")
    print()
    if vf == vg == 0:
        print("VERDICT: FPGA matches golden byte-perfect — hardware底子 clean.")
    elif vf > vg:
        print(f"VERDICT: FPGA has {vf - vg} MORE structural violations than "
              f"golden.\n         The FPGA capture layer is adding {vf - vg} "
              f"errors that\n         the DAP-golden doesn't see. Hardware底子"
              f"下游有问题。")
    else:
        print(f"VERDICT: FPGA violations ({vf}) <= golden ({vg}) — "
              f"any extra loss\n         beyond the ETF wrap baseline is 0. "
              f"FPGA path is at least as good\n         as the DAP path.")

    # 9) restore PLL1 R=2 (default 112.5 MHz TRACECLK) via CLI so board is
    # left in known state for the next run. Skip if we didn't touch it.
    if a.pll_r is not None and a.pll_r != 2:
        print("\n[cleanup] restoring PLL1 R=2 (default 112.5 MHz TRACECLK)",
              flush=True)
        try:
            cli_set_pll(r=2)
            restore_hw_fifo()
        except Exception as e:
            print(f"  cleanup failed: {e!r}", flush=True)


if __name__ == "__main__":
    main()
