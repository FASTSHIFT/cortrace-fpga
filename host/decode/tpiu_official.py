#!/usr/bin/env python3
"""tpiu_official — faithful Python port of orbuculum's Src/tpiuDecoder.c.

Our home-grown tpiu_deframe_walk scans for HSYNC (0xFF 0x7F) byte-by-byte,
which mis-aligns when HSYNC lands on an odd byte offset -- fatal on this
stream where HSYNC is ~30% of the bytes. The official decoder instead:
  * collects bytes in 16-bit pairs (got_lowbits), filtering HSYNC only on the
    pair boundary -> never loses 16-bit frame phase;
  * re-syncs hard on the full SYNCPATTERN 0xFFFFFF7F;
  * drops data whose currentStream==0 (padding) and only keeps a requested
    stream, with the delayed-stream-change semantics from _getPacket.

This is a direct transcription so we can compare byte-for-byte against the
reference without wiring up the orbuculum network pipeline.

Usage: tpiu_official.py <raw_tpiu.bin> [want_stream=2] -> writes <in>.s<N>.bin
"""
import sys

SYNCPATTERN = 0xFFFFFF7F
HALFSYNC_HIGH = 0x7F
HALFSYNC_LOW = 0xFF
TPIU_PACKET_LEN = 16
NO_CHANGE = 0xFF


def _get_packet(rxed, want_stream, cur_stream, rxed_off=None):
    """Port of _getPacket: interpret a full 16-byte frame. Returns
    (list_of_bytes, new_cur_stream) — or (list_of_bytes, list_of_source_offsets,
    new_cur_stream) when rxed_off (the source offset of each rxed byte) is
    given."""
    out = []
    out_off = []
    delayed = NO_CHANGE
    lowbits = rxed[TPIU_PACKET_LEN - 1]
    cur = cur_stream
    for i in range(0, TPIU_PACKET_LEN, 2):
        if rxed[i] & 1:
            # stream change - before or after the data byte
            if lowbits & 1:
                delayed = rxed[i] >> 1
            else:
                cur = rxed[i] >> 1
        else:
            if cur:  # currentStream != 0 (0 = padding, dropped)
                b = rxed[i] | (lowbits & 1)
                if want_stream is None or cur == want_stream:
                    out.append(b)
                    if rxed_off is not None:
                        out_off.append(rxed_off[i])
        # second byte of the pair (always data), for i < 14
        if i < 14:
            if cur:
                if want_stream is None or cur == want_stream:
                    out.append(rxed[i + 1])
                    if rxed_off is not None:
                        out_off.append(rxed_off[i + 1])
        if delayed != NO_CHANGE:
            cur = delayed
            delayed = NO_CHANGE
        lowbits >>= 1
    if rxed_off is not None:
        return out, out_off, cur
    return out, cur


def deframe(stream, want_stream=2, with_offsets=False):
    """Port of TPIUPump: sync on SYNCPATTERN, collect 16-bit pairs filtering
    HSYNC, assemble 16-byte frames, decode via _get_packet.

    with_offsets=True additionally returns, for every emitted ETM byte, the
    offset of the source byte it came from in `stream`. Needed to carry the FPGA
    capture time base (one ns per RAW byte) through to the ETM byte stream --
    the time array MUST be produced by the same deframer orbetto uses, or the
    two get index-skewed and the timestamps degenerate.

    Returns (etm, stats) or (etm, offsets, stats).
    """
    out = bytearray()
    out_off = [] if with_offsets else None
    state_synced = False
    sync_monitor = 0
    rxed = bytearray(TPIU_PACKET_LEN)
    rxed_off = [0] * TPIU_PACKET_LEN if with_offsets else None
    byte_count = 0
    got_lowbits = False
    cur_stream = 0
    npackets = 0
    nsync = 0

    for pos, d in enumerate(stream):
        sync_monitor = ((sync_monitor << 8) | d) & 0xFFFFFFFF
        if sync_monitor == SYNCPATTERN:
            state_synced = True
            byte_count = 0
            got_lowbits = False
            nsync += 1
            continue
        if not state_synced:
            continue
        if not got_lowbits:
            got_lowbits = True
            rxed[byte_count] = d
            if with_offsets:
                rxed_off[byte_count] = pos
            continue
        got_lowbits = False
        if d == HALFSYNC_HIGH and rxed[byte_count] == HALFSYNC_LOW:
            continue  # halfsync, ignore
        byte_count += 1
        rxed[byte_count] = d
        if with_offsets:
            rxed_off[byte_count] = pos
        byte_count += 1
        if byte_count == TPIU_PACKET_LEN:
            npackets += 1
            byte_count = 0
            if with_offsets:
                pkt, pkt_off, cur_stream = _get_packet(
                    rxed, want_stream, cur_stream, rxed_off)
                out.extend(pkt)
                out_off.extend(pkt_off)
            else:
                pkt, cur_stream = _get_packet(rxed, want_stream, cur_stream)
                out.extend(pkt)
    stats = dict(packets=npackets, syncs=nsync)
    if with_offsets:
        return bytes(out), out_off, stats
    return bytes(out), stats


def main():
    path = sys.argv[1]
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raw = open(path, "rb").read()
    etm, stats = deframe(raw, want_stream=want)
    outp = f"{path}.s{want}.bin"
    open(outp, "wb").write(etm)
    # A-sync check
    a = 0; zc = 0; ti = 0
    for i, c in enumerate(etm):
        if c == 0:
            zc += 1
        elif c == 0x80 and zc >= 11:
            a += 1
            if i + 1 < len(etm) and etm[i + 1] == 0x01:
                ti += 1
            zc = 0
        else:
            zc = 0
    print(f"in={len(raw)} frames={stats['packets']} fsync={stats['syncs']} "
          f"-> stream{want}={len(etm)} bytes")
    print(f"  ETMv4 A-sync={a}  trace-info-after(0x01)={ti}")
    print(f"  wrote {outp}")


if __name__ == "__main__":
    sys.exit(main())
