#!/usr/bin/env python3
"""recover — nibble reassemble + phase search for a raw FPGA capture.

Extracted from the retired opencsd_etm4_run.py so the few tools that still need
the host-side phase search (make_timebase, fpga_vs_etf golden cross-check) do
not depend on the slow trc_pkt_lister end-to-end path. The fast production path
is cortrace-decode --raw, which is a C++ port of exactly this logic.

The capture byte is {trace_a[k] hi, trace_b[k-1] lo}; recover the time-ordered
nibbles and parity/order-search the assembled, period-indexed byte stream,
ranking by post-deframe ETMv4 A-sync count (the only signal that proves the
whole chain — nibble phase -> TPIU frame phase -> stream demux — is aligned).
"""

import dsl_parse as D
import etm35lib as L
import tpiu_official as T

TPIU_FSYNC = bytes([0xFF, 0xFF, 0xFF, 0x7F])


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
    """Recover nibbles and parity/order-search assemble to the period-indexed
    byte stream. Returns (score, parity, order, data, flash_isyncs,
    pre_deframe_asyncs, fsync) for the best-scoring phase."""
    nibs = bytearray()
    for k in range(len(raw) - 1):
        nibs.append((raw[k] >> 4) & 0xF)
        nibs.append(raw[k + 1] & 0xF)

    best = None
    for parity in (0, 1):
        for order in (0, 1):
            data = D.assemble(nibs, parity, order)
            fsync = data.count(TPIU_FSYNC)
            try:
                if L.has_tpiu_sync(data):
                    etm, _ = T.deframe(data, want_stream=stream)
                else:
                    etm = b""
            except Exception:
                etm = b""
            v4d = count_async(etm)  # A-sync AFTER deframe: strongest signal
            v4a = count_async(data)  # A-sync before deframe
            fl = sum(1 for s in L.find_isyncs(data) if L.is_flash(s.addr))
            score = v4d * 1000000 + len(etm) * 10 + v4a * 100 + fsync + fl
            if best is None or score > best[0]:
                best = (score, parity, order, data, fl, v4a, fsync)
    return best
