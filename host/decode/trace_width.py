#!/usr/bin/env python3
"""trace_width — reassemble a CAP_RAW capture into TPIU bytes for ANY parallel
port width (4, 2 or 1 bit), replacing the 4-bit-only nibble pairing in
dsl_parse.assemble().

Capture format (rtl/trace_stream_top.v, CAP_RAW=1)
-------------------------------------------------
The FPGA stores one byte per TRACECLK period:

    cap_byte = {trace_b[3:0], trace_a[3:0]}

  trace_a = TRACED sampled on the RISING  edge  (low nibble)
  trace_b = TRACED sampled on the FALLING edge  (high nibble)

This layout is width-independent: at 2 bits only TRACED[1:0] carry data (so
each nibble's upper 2 bits are noise from unconnected lanes), at 1 bit only
TRACED[0]. Hence the SAME bitstream and the SAME capture work for all widths --
only this reassembly needs to know the width.

Bit ordering
------------
Upstream traceIF.v shifts the two edges in LSB-first, rising edge first:

    2'b11: construct <= {traceDinb[3:0], traceDina[3:0], construct[35:8]};
    2'b10: construct <= {traceDinb[1:0], traceDina[1:0], construct[35:4]};
    default: construct <= {traceDinb[0], traceDina[0], construct[35:2]};

Shifting right means the earliest bits end up least significant, i.e. per
TRACECLK the time-ordered bit sequence is

    trace_a[0..W-1] then trace_b[0..W-1]

and bytes are filled LSB-first from that sequence. So a byte spans 8/(2*W)
TRACECLK periods: 1 period at 4-bit, 2 at 2-bit, 4 at 1-bit.

Phase ambiguity
---------------
Which edge starts a byte is not recoverable from the capture alone (the capture
may begin mid-byte), so callers search over `phase` -- the number of leading
half-symbols to drop, 0..(2*8/W - 1) -- and score the candidates. `bit_order`
covers the case where a lane mapping or sampler inverts the within-edge bit
order, which we cannot prove from the pin assignment alone.

At 4 bits this reduces to the old parity/order search:
    phase=0,bit_order=lsb  == assemble(parity=0, order=0)
"""

WIDTHS = (4, 2, 1)


def half_symbols(raw, width):
    """Split a CAP_RAW capture into the time-ordered sequence of half-symbols
    (one per TRACECLK edge), each `width` bits wide.

    Returns a list of ints, 2 per input byte: the rising-edge sample then the
    falling-edge sample, masked to the active lanes."""
    mask = (1 << width) - 1
    out = []
    for byte in raw:
        out.append(byte & mask)          # trace_a: rising edge (low nibble)
        out.append((byte >> 4) & mask)   # trace_b: falling edge (high nibble)
    return out


def phases(width):
    """Number of distinct byte-boundary phases for this width: a byte takes
    8/width half-symbols, so that many starting offsets are possible."""
    return 8 // width


def assemble_width(raw, width, phase=0, bit_order="lsb"):
    """Reassemble TPIU bytes from a CAP_RAW capture at the given port width.

    width:     4, 2 or 1
    phase:     leading half-symbols to drop (0 .. 8/width - 1)
    bit_order: 'lsb' -> the earliest bit of each half-symbol is the least
               significant (matches upstream traceIF); 'msb' -> reversed.
    """
    if width not in WIDTHS:
        raise ValueError(f"width must be one of {WIDTHS}, got {width}")

    syms = half_symbols(raw, width)[phase:]
    per_byte = 8 // width          # half-symbols per byte
    out = bytearray()

    for k in range(0, len(syms) - per_byte + 1, per_byte):
        acc = 0
        for j in range(per_byte):
            s = syms[k + j]
            if bit_order == "msb":
                # reverse the bits within the half-symbol
                s = int(f"{s:0{width}b}"[::-1], 2) if width > 1 else s
            acc |= s << (width * j)
        out.append(acc)
    return bytes(out)


def candidates(raw, width):
    """Every (phase, bit_order) reassembly for this width, for the caller to
    score. 1-bit has no within-symbol order to get wrong."""
    orders = ("lsb",) if width == 1 else ("lsb", "msb")
    for ph in range(phases(width)):
        for bo in orders:
            yield ph, bo, assemble_width(raw, width, ph, bo)


def main():
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    raw = open(sys.argv[1], "rb").read()
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    import etm35lib as L
    import tpiu_official as T

    print(f"raw {len(raw)}B, width={width}, "
          f"{phases(width)} phase(s) x order(s)")
    best = None
    for ph, bo, data in candidates(raw, width):
        fsync = data.count(bytes([0xFF, 0xFF, 0xFF, 0x7F]))
        etm = b""
        if L.has_tpiu_sync(data):
            etm, _ = T.deframe(data, want_stream=2)
        # A-sync count after deframing is the only signal that proves the whole
        # chain (bit phase -> TPIU frame phase -> stream demux) lines up.
        a = zc = 0
        for c in etm:
            if c == 0:
                zc += 1
            elif c == 0x80 and zc >= 11:
                a += 1
                zc = 0
            else:
                zc = 0
        score = a * 1000000 + len(etm) * 10 + fsync
        print(f"  phase={ph} order={bo}: bytes={len(data)} fsync={fsync} "
              f"deframed={len(etm)} A-sync={a} score={score}")
        if best is None or score > best[0]:
            best = (score, ph, bo, data)
    if best:
        print(f"best: phase={best[1]} order={best[2]} score={best[0]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
