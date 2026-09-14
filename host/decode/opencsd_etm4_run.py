#!/usr/bin/env python3
"""opencsd_etm4_run — end-to-end pipeline that decodes an H743 ETMv4 capture
with the ARM/Linaro reference decoder (trc_pkt_lister) and reports coverage
against func_test.c.

The traceif4 FPGA build (proposal 33-retired) hands the PC a stream of
bare ETMv4 bytes: FPGA-side `traceIF.v` locks onto the TPIU 4-byte sync
and `tpiu_demux` extracts the stream-2 ETM bytes, so the PC no longer needs
to guess parity/order or run tpiu_deframe_walk.

The tool auto-detects which stream format it is looking at:
    - if the first bytes contain any TPIU FF FF FF 7F sync => run legacy
      nibble-assemble + tpiu_deframe_walk (old raw path)
    - otherwise treat the input as already-demuxed bare ETM bytes

Reports:
    • byte counts at each stage
    • # of A-syncs (11 zero bytes + 0x80) and Trace-Info (0x01) packets
    • # of PC ranges the reference decoder produced
    • unique PCs (from stdout parse) + which land in flash
    • coverage of the expected func_test user functions

Usage:
    python3 opencsd_etm4_run.py <raw-stream.bin> <elf> [--period-ns 20.0] \
        [--keep <out_dir>]  # keep snapshot dir + intermediate ETM bin
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

import etm35lib as L
import dsl_parse as D
import tpiu_official as T

HERE = os.path.dirname(os.path.abspath(__file__))
PACKER = os.path.join(HERE, "make_opencsd_snapshot.py")
LISTER = os.environ.get("TRC_PKT_LISTER", "trc_pkt_lister")

# libopencsd 1.8.3 has a pathological allocation on some input patterns: RSS
# can jump from ~40 MB to 22+ GB across a small input-size increase (same bug
# cortrace_decode.cpp caps with RLIMIT_AS). trc_pkt_lister is that same
# libopencsd, so cap its address space too or it OOMs the host. Override with
# env LISTER_MEM_MB (0 = no cap).
LISTER_MEM_MB = int(os.environ.get("LISTER_MEM_MB", "4096"))


def _mem_limit_preexec(mb):
    """Return a preexec_fn that caps the child's virtual address space to `mb`
    MB via RLIMIT_AS, so a libopencsd allocation blowup fails malloc (handled
    as a normal decode error) instead of OOM-killing the machine."""
    if not mb or mb <= 0:
        return None
    import resource

    def _apply():
        nbytes = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
    return _apply

# The expected user functions in func_test.c (see verify_func_test.py).
EXPECTED = {
    "main_loop", "level_a", "level_b", "level_c", "frame_func",
    "leaf_add", "leaf_mul", "indirect_caller", "callback_test",
    "dispatch_callback", "factorial", "deep1", "conditional", "mixed_test",
}


TPIU_FSYNC = bytes([0xFF, 0xFF, 0xFF, 0x7F])


def recover_assemble_width(raw, width, stream=2):
    """Width-aware variant of recover_assemble for 2- and 1-bit captures.

    The CAP_RAW byte layout ({trace_b,trace_a} per TRACECLK) is the same at every
    width -- only the number of active lanes differs -- so a 2-bit byte spans 2
    TRACECLK periods and a 1-bit byte spans 4. trace_width.assemble_width does
    that regrouping; here we search its (phase, bit_order) candidates with the
    same post-deframe A-sync ranking used at 4 bits.

    Returns the same tuple shape as recover_assemble:
      (score, phase, order_index, data, flash_isyncs, pre_deframe_asyncs, fsync)
    """
    import trace_width as TW

    best = None
    for ph, bo, data in TW.candidates(raw, width):
        fsync = data.count(TPIU_FSYNC)
        try:
            if L.has_tpiu_sync(data):
                etm, _ = T.deframe(data, want_stream=stream)
            else:
                etm = b""
        except Exception:
            etm = b""
        v4d = count_async(etm)
        v4a = count_async(data)
        fl = sum(1 for s in L.find_isyncs(data) if L.is_flash(s.addr))
        score = v4d * 1000000 + len(etm) * 10 + v4a * 100 + fsync + fl
        if best is None or score > best[0]:
            best = (score, ph, 0 if bo == "lsb" else 1, data, fl, v4a, fsync)
    return best


def count_async(buf):
    """ETMv4 A-sync: >=11 zero bytes then 0x80."""
    n = 0
    zc = 0
    for c in buf:
        if c == 0:
            zc += 1
        elif c == 0x80 and zc >= 11:
            n += 1
            zc = 0
        else:
            zc = 0
    return n


def recover_assemble(raw, stream=2):
    """Streamed byte = {trace_a[k] hi, trace_b[k-1] lo}. Recover time-ordered
    half-bit nibbles and parity/order-search assemble to the period-indexed
    byte stream.

    Ranking: score the stream AFTER TPIU-deframing with the OFFICIAL
    (orbuculum tpiuDecoder.c port) deframer. The post-deframe A-sync count is
    the only signal that proves the whole chain (nibble phase -> TPIU frame
    phase -> stream demux) is aligned. Pre-deframe fsync/A-sync counts are
    unreliable: a half-nibble misalignment still produces spurious FF FF FF 7F
    out of HSYNC filler.

    The deframer choice matters as much as the phase: tpiu_deframe_walk scans
    HSYNC byte-by-byte and loses 16-bit frame phase when HSYNC lands on an odd
    offset (~30% HSYNC density here). Measured on the same capture:
    walk trace-info-after=0 / 6 decoded PCs vs official=57 / 781.
    """
    nibs = bytearray()
    for k in range(len(raw) - 1):
        nibs.append((raw[k] >> 4) & 0xF)
        nibs.append(raw[k + 1] & 0xF)

    best = None
    for parity in (0, 1):
        for order in (0, 1):
            data = D.assemble(nibs, parity, order)
            fsync = data.count(TPIU_FSYNC)
            # DEFRAME first (official deframer), then score.
            try:
                if L.has_tpiu_sync(data):
                    etm, _ = T.deframe(data, want_stream=stream)
                else:
                    etm = b""
            except Exception:
                etm = b""
            v4d = count_async(etm)              # A-sync AFTER deframe: strongest
            v4a = count_async(data)             # A-sync before deframe
            fl = sum(1 for s in L.find_isyncs(data) if L.is_flash(s.addr))
            # Ranking: post-deframe A-sync dominates (it is the only signal that
            # proves the whole chain aligned), then deframed payload size, then
            # pre-deframe A-sync, raw fsync, and flash I-sync as tie-breakers.
            score = (v4d * 1000000 + len(etm) * 10 + v4a * 100
                     + fsync + fl)
            if best is None or score > best[0]:
                best = (score, parity, order, data, fl, v4a, fsync)
    return best


def count_v4_syncs(etm):
    """Count ETMv4 A-syncs (>=11 zeros + 0x80) and Trace-Info (0x01) headers
    right after each A-sync."""
    asyncs = 0
    trinfo = 0
    zc = 0
    for i, c in enumerate(etm):
        if c == 0:
            zc += 1
        elif c == 0x80 and zc >= 11:
            asyncs += 1
            zc = 0
            # peek next non-empty byte
            if i + 1 < len(etm) and etm[i + 1] == 0x01:
                trinfo += 1
        else:
            zc = 0
    return asyncs, trinfo


def deframe(data):
    """TPIU-deframe if syncs are present; otherwise pass through."""
    if L.has_tpiu_sync(data):
        return L.tpiu_deframe_walk(data)
    return bytes(data)


STRICT_RESERVED = frozenset({
    0x83, 0x84, 0x87, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F,
    0x93, 0x94, 0x97, 0x98, 0x99, 0x9C, 0x9F,
})
ATOM_HDRS = frozenset(range(0xC0, 0x100))


def diagnose_bitflip(etm):
    """MEASURE, DON'T FIX. Report the rate of Atom→reserved transitions and
    whether flipping bit6 (0x40) would recover a legal Atom-header. If yes,
    a specific data-lane bit is being flipped in ~30% of TRACECLK cycles
    (see PROPOSAL 3x: capture-front-end fault, not decoder fault)."""
    n_atom_next = 0
    n_reserved  = 0
    n_recoverable = 0
    for i in range(len(etm) - 1):
        if etm[i] in ATOM_HDRS:
            nb = etm[i + 1]
            n_atom_next += 1
            if nb in STRICT_RESERVED:
                n_reserved += 1
                if (nb ^ 0x40) in ATOM_HDRS:
                    n_recoverable += 1
    return dict(
        after_atom=n_atom_next,
        reserved=n_reserved,
        recoverable=n_recoverable,
        reserved_pct=100 * n_reserved / max(1, n_atom_next),
        recoverable_pct=100 * n_recoverable / max(1, n_reserved),
    )


def fix_async_alignment(etm):
    """Every ETMv4 A-sync (>=11 zeros + 0x80) MUST be followed immediately by a
    Trace-Info packet (header 0x01). On our capture we see ~85% of A-syncs
    followed by an extra Atom byte (0xdb / 0xf6 / 0xf7) BEFORE the expected
    0x01. That single stray byte comes from a 1-byte phase slip in the
    capture pipeline (nibble-assemble boundary / DDR write-window edge —
    verified: never inside the Trace-Info payload, always exactly one byte
    between the 0x80 and the 0x01).

    We normalise by scanning for A-sync ends and, when the next byte is not
    0x01, dropping ONE byte to snap the alignment back. This never corrupts
    valid streams because a correct capture already has 0x01 at that offset.
    """
    out = bytearray()
    i = 0
    zc = 0
    n = len(etm)
    fixed = 0
    kept = 0
    while i < n:
        c = etm[i]
        out.append(c)
        if c == 0:
            zc += 1
        elif c == 0x80 and zc >= 11:
            # We just wrote the A-sync terminator. Look ahead: if the next
            # byte is not 0x01 but the byte AFTER it is 0x01, drop the stray.
            if i + 2 < n and etm[i + 1] != 0x01 and etm[i + 2] == 0x01:
                i += 1               # skip one byte
                fixed += 1
            elif i + 1 < n and etm[i + 1] == 0x01:
                kept += 1
            zc = 0
        else:
            zc = 0
        i += 1
    return bytes(out), fixed, kept


def parse_lister_output(text):
    """Extract PCs from trc_pkt_lister -decode output.

    OpenCSD-1.4.1 emits INSTR_RANGE lines like:
      OCSD_GEN_TRC_ELEM_INSTR_RANGE(exec range=0x8001aea:[0x8001af6] num_i(5) ...)
    We collect the range [start, end) as the exercised PC set."""
    pcs = set()
    n_ranges = 0
    n_isync = 0
    n_pe_ctx = 0
    n_exc = 0
    # Two accepted forms:
    #   "range=0x<start>:[0x<end>]"
    #   "st_addr=0x<start>...en_addr=0x<end>"
    re_new = re.compile(r"range=0x([0-9a-fA-F]+):\[0x([0-9a-fA-F]+)\]")
    re_old = re.compile(r"st_addr=0x([0-9a-fA-F]+).*en_addr=0x([0-9a-fA-F]+)")
    for line in text.splitlines():
        if "INSTR_RANGE" in line:
            n_ranges += 1
            m = re_new.search(line) or re_old.search(line)
            if m:
                st = int(m.group(1), 16)
                en = int(m.group(2), 16)
                p = st
                while p < en and p - st < 0x400:
                    pcs.add(p)
                    p += 2
        elif "PE_CONTEXT" in line or "I_ADDR" in line:
            n_pe_ctx += 1
        elif "TRACE_ON" in line or "ISYNC" in line:
            n_isync += 1
        elif "EXCEPTION" in line:
            n_exc += 1
    return pcs, dict(instr_ranges=n_ranges, pe_context=n_pe_ctx,
                     isync=n_isync, exception=n_exc)


def nm_symbols(elf):
    for tool in ("arm-none-eabi-nm", "nm"):
        try:
            out = subprocess.check_output([tool, "-n", elf]).decode()
            break
        except FileNotFoundError:
            continue
    else:
        return []
    syms = []
    for line in out.splitlines():
        m = re.match(r"([0-9a-fA-F]{8}) [tTwW] (\S+)", line)
        if m:
            syms.append((int(m.group(1), 16), m.group(2)))
    return syms


def func_of(syms, pc):
    lo = None
    for a, n in syms:
        if a <= pc:
            lo = n
        else:
            break
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("elf")
    ap.add_argument("--period-ns", type=float, default=20.0,
                    help="TRACECLK period (50MHz=20)")
    ap.add_argument("--deframer", choices=("official", "walk"),
                    default="official",
                    help="TPIU deframer: official (orbuculum port, default) or "
                         "walk (legacy home-grown)")
    ap.add_argument("--stream", type=int, default=2,
                    help="TPIU stream/tag to extract (ETM=2)")
    ap.add_argument("--width", type=int, choices=(4, 2, 1), default=4,
                    help="TPIU parallel port width of the capture (default 4). "
                         "At 2/1 bit a TPIU byte spans 2/4 TRACECLK periods, so "
                         "the raw capture is regrouped accordingly")
    ap.add_argument("--keep", type=str, default=None,
                    help="keep the OpenCSD snapshot dir (and etm.bin) here")
    ap.add_argument("--dump-lister", type=str, default=None,
                    help="save trc_pkt_lister stdout to this file")
    a = ap.parse_args()

    raw = open(a.raw, "rb").read()
    print(f"[1] raw stream: {len(raw)} bytes ({len(raw) * a.period_ns / 2e6:.2f} ms)")

    # Two supported inputs:
    #   (a) FPGA-side traceIF emits already-aligned TPIU 16-byte frames as
    #       plain bytes. PC does TPIU deframe (etm35lib) directly, NO nibble
    #       assemble. Detected by has_tpiu_sync AND absence of the raw {a,b}
    #       double-byte cadence.
    #   (b) Legacy raw {a,b} stream (old bit): 2 bytes / TRACECLK. Detected
    #       when has_tpiu_sync returns false on the raw bytes but true on the
    #       parity=1 assembled bytes.
    # Path selection by RESULT, not by a has_tpiu_sync() guess on the raw.
    # The old code branched on has_tpiu_sync(raw): if the raw CAP_RAW bytes
    # happened to contain FF FF FF 7F (all-1 lanes are common), it assumed the
    # stream was already 16-byte-frame-aligned TPIU and deframed it directly --
    # skipping the {trace_b,trace_a} nibble reassembly the raw actually needs.
    # On the direct-capture bit this false-positives and yields 0 ETM, while
    # the nibble path yields hundreds of A-syncs (2026-09-07). Fix: compute
    # BOTH candidate ETM streams and keep whichever has more ETMv4 A-syncs.
    def _direct_deframe(src):
        if not L.has_tpiu_sync(src):
            return b""
        if a.deframer == "official":
            e, _ = T.deframe(src, want_stream=a.stream)
        else:
            e = L.tpiu_deframe_walk(src)
        return e

    # Candidate 1: treat raw as already-framed TPIU.
    etm_direct = _direct_deframe(raw)
    a_direct, _ = count_v4_syncs(etm_direct) if etm_direct else (0, 0)

    # Candidate 2: nibble-reassemble ({trace_b,trace_a} per period) then deframe.
    if a.width == 4:
        score, parity, order, data, fl, v4a, fsync = recover_assemble(
            raw, stream=a.stream)
        what = f"parity={parity} order={order}"
    else:
        score, parity, order, data, fl, v4a, fsync = recover_assemble_width(
            raw, a.width, stream=a.stream)
        what = f"{a.width}-bit phase={parity} order={'lsb' if not order else 'msb'}"
    etm_asm = _direct_deframe(data)
    a_asm, _ = count_v4_syncs(etm_asm) if etm_asm else (0, 0)

    print(f"[2] candidate A-syncs: direct-deframe={a_direct}  "
          f"nibble-reassemble({what})={a_asm}  fsync={fsync}")
    if a_asm >= a_direct:
        etm = etm_asm
        print(f"[2] chose NIBBLE-REASSEMBLE ({what}); "
              f"assembled={len(data)}B -> ETM {len(etm)}B")
    else:
        etm = etm_direct
        print(f"[2] chose DIRECT-DEFRAME; ETM {len(etm)}B")
    if not etm:
        print("[2] neither path found TPIU sync; passing bytes through as-is")
        etm = raw

    asyncs, trinfo = count_v4_syncs(etm)
    print(f"    ETMv4 A-syncs={asyncs}, Trace-Info(0x01) after A-sync={trinfo}")

    if a.keep:
        os.makedirs(a.keep, exist_ok=True)
        snapdir = a.keep
        tmpctx = None
    else:
        tmpctx = tempfile.TemporaryDirectory()
        snapdir = tmpctx.name

    etm_path = os.path.join(snapdir, "input_etm.bin")
    open(etm_path, "wb").write(etm)

    r = subprocess.run(
        [sys.executable, PACKER, etm_path, a.elf, snapdir,
         "--protocol", "etm4"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("[FAIL] snapshot build:", r.stderr, file=sys.stderr)
        return 1
    print("[4] snapshot dir:", snapdir)
    for line in r.stdout.splitlines()[:6]:
        print("   ", line)

    print(f"[5] running trc_pkt_lister -decode (RLIMIT_AS={LISTER_MEM_MB} MB) ...")
    try:
        r = subprocess.run(
            [LISTER, "-ss_dir", snapdir, "-decode", "-logstdout"],
            capture_output=True, text=True,
            preexec_fn=_mem_limit_preexec(LISTER_MEM_MB),
        )
    except MemoryError:
        print("[5] lister hit the RLIMIT_AS cap (libopencsd allocation blowup); "
              "treat as a decode failure, not an OOM. Raise LISTER_MEM_MB to "
              "retry.", file=sys.stderr)
        if tmpctx:
            tmpctx.cleanup()
        return 3
    if a.dump_lister:
        open(a.dump_lister, "w").write(r.stdout)

    text = r.stdout
    tail = text.splitlines()[-40:]
    if r.returncode != 0:
        print("    lister exited with", r.returncode)

    pcs, stats = parse_lister_output(text)
    print(f"[6] decoder: INSTR_RANGE={stats['instr_ranges']}  "
          f"PE_CONTEXT/I_ADDR={stats['pe_context']}  "
          f"ISYNC/TRACE_ON={stats['isync']}  EXCEPTION={stats['exception']}")
    print(f"    unique PCs: {len(pcs)}")

    if not pcs:
        print("\n---- lister tail ----")
        for line in tail:
            print("   ", line)
        if tmpctx:
            tmpctx.cleanup()
        return 2

    in_flash = [p for p in pcs if 0x08000000 <= p < 0x08200000]
    print(f"    in flash: {len(in_flash)}/{len(pcs)} "
          f"({100 * len(in_flash) / len(pcs):.1f}%)")

    syms = nm_symbols(a.elf)
    if syms:
        from collections import Counter
        cov = Counter(func_of(syms, p) for p in in_flash)
        cov.pop(None, None)
        seen = set(cov)
        hit = EXPECTED & seen
        print(f"    functions covered: {len(cov)}")
        print(f"    func_test user funcs seen: {len(hit)}/{len(EXPECTED)}")
        miss = EXPECTED - seen
        if miss:
            print(f"      missing: {sorted(miss)}")
        print("    top-15 functions by PC count:")
        for n, c in cov.most_common(15):
            tag = "  <-- func_test" if n in EXPECTED else ""
            print(f"      {n:<30} {c}{tag}")

    if tmpctx:
        tmpctx.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
