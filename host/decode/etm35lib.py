"""etm35lib — spec-grounded ETM3.5 (Cortex-M4) decode helpers, testable.

Consolidates the trace-decode logic that was scattered across the bring-up
scripts into one importable, unit-tested module. Everything here is grounded
in the ARM specs in docs/artix7-port/refs/:

  * IHI0014Q  Embedded Trace Macrocell Architecture Specification (ETMv3.5)
  * DDI0440C  Cortex-M4 ETM (ETM-M4) Technical Reference Manual

Cortex-M4 ETM facts used (DDI0440C Table 2-1, §2.1.3):
  * Architecture ETMv3.5
  * 0 ContextID comparators  -> ETMCR[15:14]=00 -> I-sync carries 0 ContextID
    bytes, so a Normal I-sync packet is exactly 6 bytes:
        0x08 | info | addr[7:0] | addr[15:8] | addr[23:16] | addr[31:24]
  * fixed synchronization frequency = 1024 bytes of trace (read-only ETMSYNCFR)

IHI0014Q facts used:
  * §7.10.4 A-sync = five-or-more 0x00 then 0x80; a 4-bit (sub-byte) port is
    NOT guaranteed byte-aligned, so a decoder must be able to realign.
  * Fig 7-42 Normal I-sync info byte: bit7=0 (Normal, not LSiP), bits[6:5]
    reason code, bit4 Jazelle, bit3 NonSecure, bit2 AltISA; the 4-byte address
    is uncompressed and address bit0 is the Thumb bit.
"""
from __future__ import annotations

from dataclasses import dataclass

# Cortex-M4 flash code region (STM32F4: 0x0800_0000 .. 0x080F_FFFF for 1MB).
FLASH_LO = 0x08000000
FLASH_HI = 0x08100000

ASYNC = bytes([0, 0, 0, 0, 0, 0x80])  # canonical 5-zero + 0x80 A-sync
ISYNC_HEADER = 0x08

# I-sync info-byte reason codes (IHI0014Q Fig 7-42 / Reason codes)
REASON_PERIODIC = 0b00
REASON_TRACE_ON = 0b01
REASON_OVERFLOW = 0b10
REASON_DEBUG_EXIT = 0b11


@dataclass(frozen=True)
class ISync:
    offset: int        # byte offset of the 0x08 header in the stream
    addr: int          # absolute instruction address (thumb bit stripped)
    thumb: bool        # address bit0 (Thumb state)
    reason: int        # info-byte reason code [6:5]
    nonsecure: bool


def is_flash(addr: int) -> bool:
    """True if addr is in the Cortex-M flash code region (thumb bit ignored)."""
    return FLASH_LO <= (addr & ~1) < FLASH_HI


def find_asyncs(data: bytes, min_zeros: int = 5) -> list[int]:
    """Return offsets of the terminating 0x80 of each A-sync.

    Per IHI0014Q §7.10.4 an A-sync is >=5 zero bytes followed by 0x80. We
    return the index of the 0x80 (the realign/IDLE point); the next packet
    header starts at offset+1.
    """
    out = []
    zeros = 0
    for i, c in enumerate(data):
        if c == 0x00:
            zeros += 1
        else:
            if c == 0x80 and zeros >= min_zeros:
                out.append(i)
            zeros = 0
    return out


def parse_isync_at(data: bytes, i: int,
                   flash_lo: int = FLASH_LO, flash_hi: int = FLASH_HI) -> "ISync | None":
    """Try to parse a Normal I-sync packet whose header is at offset i.

    Returns an ISync if the 6 bytes form a Normal I-sync with a flash address,
    else None. ContextID bytes = 0 on Cortex-M4 (DDI0440C).

    Validation (hardened per review r14 BUG-1, to reject noise/misaligned 0x08):
      * bit7 = 0 (Normal, not LSiP)
      * bit4 (Jazelle) = 0 and bit2 (AltISA) = 0 — Cortex-M is Thumb-only, so
        these are always 0; checking them is a free, strong false-positive
        filter (IHI0014Q Fig 7-42 / Table 7-20).
      * address in [flash_lo, flash_hi). flash_hi defaults to the full 1 MB
        region but callers should pass the ELF .text extent for a tighter bound.
    """
    if i + 5 >= len(data):
        return None
    if data[i] != ISYNC_HEADER:
        return None
    info = data[i + 1]
    if info & 0x80:          # bit7=1 => LSiP, not a Normal I-sync
        return None
    if info & 0x14:          # bit4 Jazelle or bit2 AltISA set => not Cortex-M
        return None
    addr = (data[i + 2] | (data[i + 3] << 8)
            | (data[i + 4] << 16) | (data[i + 5] << 24))
    a = addr & ~1            # strip Thumb bit -> half-word aligned PC
    if not (flash_lo <= a < flash_hi):
        return None
    return ISync(
        offset=i,
        addr=a,
        thumb=bool(addr & 1),
        reason=(info >> 5) & 0b11,
        nonsecure=bool(info & 0x08),
    )


def find_isyncs(data: bytes,
                flash_lo: int = FLASH_LO, flash_hi: int = FLASH_HI) -> "list[ISync]":
    """Scan the whole stream for Normal I-sync packets carrying a flash PC.

    This is the robust anchor extractor: every Normal I-sync gives an absolute
    PC (IHI0014Q Fig 7-42), independent of whether the variable-length parse
    between syncs stayed aligned. Pass the ELF .text [lo,hi) for a tight bound.
    """
    out = []
    for i in range(len(data) - 5):
        if data[i] != ISYNC_HEADER:
            continue
        s = parse_isync_at(data, i, flash_lo, flash_hi)
        if s is not None:
            out.append(s)
    return out


def recover_pcs(data: bytes,
                flash_lo: int = FLASH_LO, flash_hi: int = FLASH_HI) -> "list[int]":
    """Return the sorted distinct flash PCs anchored from I-sync packets."""
    return sorted({s.addr for s in find_isyncs(data, flash_lo, flash_hi)})


def bit_shift(data: bytes, shift: int) -> bytes:
    """Right-shift the whole byte stream by `shift` bits (0..7), LSB-first.

    Models the sub-byte misalignment of a 4-bit port (IHI0014Q §7.10.4): a
    capture off by N bits shifts every byte. Used to realign after an A-sync.
    """
    if shift == 0:
        return bytes(data)
    if not 0 <= shift <= 7:
        raise ValueError("shift must be 0..7")
    out = bytearray()
    for i in range(len(data) - 1):
        out.append(((data[i] >> shift) | (data[i + 1] << (8 - shift))) & 0xFF)
    return bytes(out)


# --- traceIF byte-assembly SIMPLIFIED MODEL (NOT a faithful RTL mirror) ---
# Approximates verilog/traceIF.v width==3: construct <= {dinb,dina,construct[35:8]};
# a 16-bit packet is emitted once synced on 0x7FFFFFFF; 0x7FFF packets dropped.
# NOTE (review r14 BUG-2): this is a SIMPLIFIED model, NOT a faithful mirror of
# the RTL. It detects only the RE-sync window (0x7FFFFFFF) and omits the RTL's
# FE-sync phase (isREsync), elemCount, and 128-bit cFrame assembly. Use only
# for coarse byte-ordering experiments, NOT to certify RTL correctness.
def traceif_assemble(nibble_bytes: bytes) -> bytes:
    """SIMPLIFIED model of traceIF (input = {trace_b<<4|trace_a} per clk).

    Returns an assembled byte stream after sync lock. This is a coarse
    approximation (single sync phase, no 128-bit frame assembly), NOT a
    faithful RTL mirror — do not use it to back RTL correctness claims.
    """
    construct = 0
    out = bytearray()
    synced = False
    rem = 0
    for byte in nibble_bytes:
        a = byte & 0x0F
        b = (byte >> 4) & 0x0F
        # 36-bit shift register: new nibbles enter the top
        construct = ((b << 32) | (a << 28) | (construct >> 8)) & 0xFFFFFFFFF
        top32 = (construct >> 4) & 0xFFFFFFFF
        if top32 == 0x7FFFFFFF:
            synced = True
            rem = 1
            continue
        if synced:
            if rem:
                rem -= 1
            else:
                rem = 1
                pkt = (construct >> 4) & 0xFFFF
                if pkt != 0x7FFF:
                    out.append(pkt & 0xFF)
                    out.append((pkt >> 8) & 0xFF)
    return bytes(out)


# ----------------------------------------------------------------------------
# Region decoder: from an I-sync anchor, walk P-headers + branch packets to
# extend the recovered trace until the parse derails. Grounded in IHI0014Q
# §7.3.4 (P-headers), §7.3.5 (branch packets). Cortex-M is Thumb-only, so
# branch addresses use the Thumb address-mode bit layout.
# ----------------------------------------------------------------------------
@dataclass
class FlowEvent:
    kind: str          # 'isync' | 'atoms' | 'branch'
    addr: int          # 'isync': absolute PC (ground truth). 'atoms': the
                       # current base PC (carried, not advanced). 'branch':
                       # the base PC at the branch — NOT the branch TARGET.
                       # Branch target-address decode is not implemented
                       # (review r14 BUG-3); branch events are COUNT-ONLY and
                       # addr is only the prevailing base, not the jump dest.
    eatoms: int = 0    # executed atoms (P-header)
    natoms: int = 0    # not-executed atoms
    addr_is_target: bool = False  # True only when addr is a decoded absolute
                                  # PC (isync). False for atoms/branch.


def _phdr_atoms(c: int):
    """Decode a non-cycle-accurate P-header byte. Returns (eatoms, natoms) or
    None if not a P-header. IHI0014Q §7.3.4."""
    if (c & 0b10000001) != 0b10000000:
        return None
    if (c & 0b10000011) == 0b10000000:        # Format-1
        eatoms = (c & 0x3C) >> 2
        natoms = 1 if (c & (1 << 6)) else 0
        return eatoms, natoms
    if (c & 0b11110011) == 0b10000010:        # Format-2
        eatoms = ((c & (1 << 2)) == 0) + ((c & (1 << 3)) == 0)
        natoms = 2 - eatoms
        return eatoms, natoms
    return None


def decode_region(data: bytes, start: int, base_addr: int, max_bytes: int = 4096):
    """Walk packets from `start` (just after an I-sync) until a byte we can't
    classify cleanly, returning the FlowEvents recovered. This is a best-effort
    forward extension from a known-good anchor; it deliberately stops at the
    first ambiguous byte rather than emitting garbage (the next anchor will
    re-establish the absolute PC).
    """
    events = []
    i = start
    end = min(len(data), start + max_bytes)
    while i < end:
        c = data[i]
        # branch packet (bit0 = 1): consume continuation bytes (bit7 set), max 5
        if c & 1:
            n = 1
            while i + n < end and (data[i + n - 1] & 0x80) and n < 5:
                n += 1
            events.append(FlowEvent("branch", base_addr))
            i += n
            continue
        ph = _phdr_atoms(c)
        if ph is not None:
            events.append(FlowEvent("atoms", base_addr, eatoms=ph[0], natoms=ph[1]))
            i += 1
            continue
        if c == 0x00:
            # could be start of next A-sync; stop and let caller re-anchor
            break
        if c == ISYNC_HEADER:
            s = parse_isync_at(data, i)
            if s is not None:
                events.append(FlowEvent("isync", s.addr, addr_is_target=True))
                base_addr = s.addr
                i += 6
                continue
            break
        # unknown / can't classify cleanly -> stop (don't emit garbage)
        break
    return events, i


def decode_all(data: bytes):
    """Anchor on every I-sync and extend each region. Returns list of FlowEvent.
    The set of 'isync' addresses are guaranteed-correct absolute PCs; 'atoms'
    and 'branch' events give the executed-instruction flow between anchors."""
    events = []
    for s in find_isyncs(data):
        events.append(FlowEvent("isync", s.addr, addr_is_target=True))
        region, _ = decode_region(data, s.offset + 6, s.addr)
        events.extend(region)
    return events


# ----------------------------------------------------------------------------
# Alignment-aware decode (design plan improvement #1, IHI0014Q §7.10.4).
#
# A 4-bit (sub-byte) port is not guaranteed byte-aligned: a single capture
# glitch offsets all subsequent bytes by N bits until the next A-sync, where
# the decompressor MUST realign. orbuculum does not do this. Here, at each
# A-sync we try all 8 bit-shifts of the following window and pick the shift
# that exposes a valid Normal I-sync, then decode that region in that
# alignment. This converts the "scattered anchors" into per-region aligned
# decode without any hardware/clock change.
# ----------------------------------------------------------------------------
@dataclass
class AlignedRegion:
    async_offset: int      # byte offset of the A-sync 0x80 terminator
    shift: int             # bit-shift (0..7) that aligned this region
    isync: ISync           # the I-sync that anchored it (addresses are absolute)
    events: list           # FlowEvent list for the region (incl. the isync)


def _find_isync_in_window(window: bytes, max_scan: int = 24):
    """Search the first max_scan bytes of `window` for a Normal I-sync header
    that parses to a flash address. Returns (offset, ISync) or None."""
    limit = min(len(window) - 5, max_scan)
    for off in range(max(0, limit)):
        if window[off] != ISYNC_HEADER:
            continue
        s = parse_isync_at(window, off)
        if s is not None:
            return off, s
    return None


def decode_aligned(data: bytes, scan: int = 24, region_bytes: int = 4096):
    """Full-stream alignment-aware decode.

    For each A-sync, try shifts 0..7 of the following bytes; the first shift
    whose window contains a valid I-sync wins. Decode that region from the
    I-sync in the chosen alignment. Returns a list[AlignedRegion].
    """
    regions = []
    for a_off in find_asyncs(data):
        tail = data[a_off + 1:]                 # bytes after the 0x80
        chosen = None
        for sh in range(8):
            shifted = bit_shift(tail, sh) if sh else tail
            hit = _find_isync_in_window(shifted, scan)
            if hit is not None:
                off, s = hit
                chosen = (sh, off, s, shifted)
                break
        if chosen is None:
            continue
        sh, off, s, shifted = chosen
        events = [FlowEvent("isync", s.addr, addr_is_target=True)]
        region, _ = decode_region(shifted, off + 6, s.addr, region_bytes)
        events.extend(region)
        regions.append(AlignedRegion(async_offset=a_off, shift=sh,
                                      isync=s, events=events))
    return regions


def aligned_pcs(data: bytes) -> list[int]:
    """Distinct absolute PCs recovered via alignment-aware decode (I-sync +
    any embedded re-anchors in each aligned region)."""
    pcs = set()
    for r in decode_aligned(data):
        for e in r.events:
            if e.kind == "isync":
                pcs.add(e.addr)
    return sorted(pcs)


# ----------------------------------------------------------------------------
# TPIUSync reference model (faithful port of orbtrace trace/tpiu.py TPIUSync).
#
# This is the byte-aligning front-end orbtrace uses (and that we had NOT been
# reusing — see 10-reuse-gap-audit.md). It consumes a byte stream, locks the
# TPIU full-sync 0xFFFFFF7F, filters 0x7FFF half-syncs, and emits aligned
# 16-byte TPIU frames. buf is a 129-bit register initialised to 1; the single
# sentinel '1' bit walks up as bytes shift in and reaches bit128 after exactly
# 16 payload bytes, at which point a frame is complete.
# ----------------------------------------------------------------------------
def tpiu_sync_frames(stream: bytes) -> list[bytes]:
    """Assemble 16-byte TPIU frames from a byte stream, orbtrace-faithfully.

    Returns a list of 16-byte frames (each as bytes, payload[0]..payload[15]).
    """
    buf = 1                      # 129-bit, init 1 (sentinel in bit0)
    synced = False
    frames = []
    for p in stream:
        # Cat(payload, buf): payload occupies the low 8 bits.
        cat = (buf << 8) | p
        if (cat & 0xFFFFFFFF) == 0xFFFFFF7F:
            synced = True
            buf = 1
        elif (cat & 0xFFFF) == 0xFF7F:
            buf >>= 8
        else:
            buf = (buf << 8) | p
        # output.valid = buf[128] & synced; on accept buf resets to 1.
        if synced and (buf >> 128) & 1:
            frame = bytes((buf >> (8 * i)) & 0xFF for i in range(16))
            frames.append(frame)
            buf = 1
    return frames


# ----------------------------------------------------------------------------
# TPIU deframer (faithful port of orbuculum tpiuDecoder.c _getPacket) for the
# case where the TPIU formatter is doing FULL stream-ID framing (multi-source
# trace). NOTE:
# our single-source STM32 ETM does NOT use stream framing — it emits bare ETM
# and the formatter only inserts sync FILLERS (see strip_tpiu_sync below).
# Running this deframer on that stream scatters bytes into dozens of bogus
# stream IDs and recovers 0 anchors; strip_tpiu_sync is the correct front-end
# here. This deframer is kept as a tested reference for genuinely framed input.
# Each 16-byte frame interleaves data with stream-ID bytes; byte[15] is an
# auxiliary "lowbits" byte supplying the LSB of each even-position data byte.
# Reference: ARM CoreSight TPIU formatter; orbuculum tpiuDecoder.c
# (SYNCPATTERN 0xFFFFFF7F, HALFSYNC FF/7F, 16-byte packet).
#
#   even byte E at index i (i even, i<14):
#     if E&1: stream change. new id = E>>1, applied immediately, UNLESS the
#             current lowbit is 1, in which case the change is DELAYED until
#             after the odd byte of this pair.
#     else  : data byte = E | lowbit  (lowbit restores the LSB clobbered to 0).
#   odd byte O at index i+1: always a data byte for the current stream.
#   the last even byte (index 14) is followed by no odd byte.
#   lowbits (frame[15]) is shifted right by 1 after each pair.
#   stream id 0 = padding/null -> dropped.
# ----------------------------------------------------------------------------
NO_CHANNEL_CHANGE = 0xFF


def tpiu_deframe(stream: bytes, want_stream: "int | None" = None):
    """Deframe a TPIU-formatted byte stream into per-stream payload bytes.

    Returns a dict {stream_id: bytes}. If want_stream is given, returns just
    that stream's bytes (bytes()). Faithful to orbuculum _getPacket.
    """
    out: dict[int, bytearray] = {}
    cur = 0                              # current stream id (0 = null/padding)
    for frame in tpiu_sync_frames(stream):
        # tpiu_sync_frames emits frames byte-reversed wrt arrival (the amaranth
        # buf packs the first arrived byte into the high bits). Restore arrival
        # order so frame[0]=first byte, frame[15]=aux lowbits, matching the
        # orbuculum _getPacket indexing below.
        frame = frame[::-1]
        lowbits = frame[15]
        delayed = NO_CHANNEL_CHANGE
        for i in range(0, 16, 2):
            e = frame[i]
            if e & 1:
                # stream change (immediate, or delayed past the odd byte)
                if lowbits & 1:
                    delayed = e >> 1
                else:
                    cur = e >> 1
            else:
                if cur:
                    out.setdefault(cur, bytearray()).append(e | (lowbits & 1))
            if i < 14:
                o = frame[i + 1]
                if cur:
                    out.setdefault(cur, bytearray()).append(o)
            if delayed != NO_CHANNEL_CHANGE:
                cur = delayed
                delayed = NO_CHANNEL_CHANGE
            lowbits >>= 1
    result = {k: bytes(v) for k, v in out.items()}
    if want_stream is not None:
        return result.get(want_stream, b"")
    return result


# ----------------------------------------------------------------------------
# TPIU half-sync / full-sync filler stripping (DEPRECATED — see tpiu_deframe_hsync).
#
# HISTORICAL NOTE: this just deletes the sync fillers and was WRONG. We later
# proved (sigrok arm_tpiu definition + cross-check) the STM32F4 parallel trace
# is genuine 16-byte CoreSight TPIU formatter output: even bytes carry data
# with their LSB stripped to 0 (real LSB stored in frame byte[15]), odd bytes
# are data, HSYNC (FF 7F) is inserted between frame bytes and must be SKIPPED
# without consuming a frame slot. Deleting FF 7F alone leaves the 16-byte frame
# structure intact-but-mangled -> ~16% of bytes land in the data-packet
# encoding space (0x2e='Normal data' header, etc.) and decode derails.
# tpiu_deframe_hsync() does the correct thing and drops the bad-byte fraction
# from 16% to ~0.002%. strip_tpiu_sync is kept only for old callers/tests.
# ----------------------------------------------------------------------------
def strip_tpiu_sync(stream: bytes) -> bytes:
    """DEPRECATED: deletes TPIU full/half sync fillers. Does NOT handle the
    16-byte frame structure or the even-byte LSB restore — use
    tpiu_deframe_hsync() instead. Kept for backward compatibility."""
    s = stream.replace(b"\xff\xff\xff\x7f", b"")
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        if i + 1 < n and s[i] == 0xFF and s[i + 1] == 0x7F:
            i += 2
            continue
        out.append(s[i])
        i += 1
    return bytes(out)


def has_tpiu_sync(stream: bytes) -> bool:
    """True if the stream contains TPIU formatter sync fillers (full or half).
    Used to decide whether TPIU deframing is needed before ETM decode."""
    return (b"\xff\xff\xff\x7f" in stream) or (b"\xff\x7f" in stream)


# ----------------------------------------------------------------------------
# TPIU 16-byte formatter deframe with HSYNC handling (the CORRECT front-end).
#
# CoreSight TPIU continuous-formatter protocol (sigrok arm_tpiu; ARM CoreSight
# Architecture Spec):
#   * Trace is grouped into 16-byte frames.
#   * Even bytes (index 0,2,..,14): if bit0=1 it is a stream-ID change
#     (id = byte>>1); if bit0=0 it is a DATA byte whose true LSB is taken from
#     frame byte[15] bit[index/2] (the formatter stole the LSB to carry the ID
#     flag, and stashed the real LSB in the aux byte).
#   * Odd bytes (1,3,..,13): always data.
#   * byte[15] is the aux byte holding the 7 even-byte LSBs (+ its own bit7).
#   * HSYNC half-sync (0xFF 0x7F) and FSYNC full-sync (0xFFFFFF7F) are inserted
#     into the octet stream and are NOT part of the 16-byte frame payload; they
#     must be removed WITHOUT consuming a frame slot.
#
# Without an FSYNC to lock frame phase (the STM32F4 parallel port we capture
# emits HSYNC but no FSYNC within a window), the caller scans all 16 candidate
# start phases and picks the one yielding the most valid flash I-sync anchors.
# ----------------------------------------------------------------------------
def tpiu_deframe_hsync(stream: bytes, phase: int = 0,
                       want_stream: "int | None" = None) -> bytes:
    """Deframe a 16-byte TPIU formatter stream starting at `phase`, skipping
    HSYNC/FSYNC fillers and restoring even-byte LSBs from the aux byte. Returns
    the recovered payload (single concatenation when want_stream is None, else
    only that stream-ID's bytes). See find_tpiu_phase() to choose `phase`."""
    out = bytearray()
    frame = []
    cur = 0
    i = phase
    n = len(stream)
    while i < n:
        # Skip HSYNC (FF 7F) and FSYNC (FF FF FF 7F) without consuming a slot.
        if stream[i] == 0xFF and i + 1 < n and stream[i + 1] == 0x7F:
            i += 2
            continue
        if (i + 3 < n and stream[i] == 0xFF and stream[i + 1] == 0xFF
                and stream[i + 2] == 0xFF and stream[i + 3] == 0x7F):
            i += 4
            continue
        frame.append(stream[i])
        i += 1
        if len(frame) == 16:
            aux = frame[15]
            for j in range(15):
                if j % 2 == 0:
                    if frame[j] & 1:
                        cur = frame[j] >> 1        # stream-ID change
                    else:
                        b = frame[j] | ((aux >> (j // 2)) & 1)
                        if want_stream is None or cur == want_stream:
                            out.append(b)
                else:
                    if want_stream is None or cur == want_stream:
                        out.append(frame[j])
            frame = []
    return bytes(out)


def _decode_frame16(frame, cur, out, want_stream):
    """Interpret one assembled 16-byte TPIU frame into `out`. Returns the
    (possibly updated) current stream id `cur`."""
    aux = frame[15]
    for j in range(15):
        if j % 2 == 0:
            if frame[j] & 1:
                cur = frame[j] >> 1            # stream-ID change
            else:
                b = frame[j] | ((aux >> (j // 2)) & 1)
                if want_stream is None or cur == want_stream:
                    out.append(b)
        else:
            if want_stream is None or cur == want_stream:
                out.append(frame[j])
    return cur


def _next_frame(stream, i, n):
    """Collect the next 16 non-filler bytes starting at `i`, skipping
    HSYNC/FSYNC. Returns (frame_list_or_None, new_i)."""
    frame = []
    while i < n:
        if stream[i] == 0xFF and i + 1 < n and stream[i + 1] == 0x7F:
            i += 2
            continue
        if (i + 3 < n and stream[i] == 0xFF and stream[i + 1] == 0xFF
                and stream[i + 2] == 0xFF and stream[i + 3] == 0x7F):
            i += 4
            continue
        frame.append(stream[i])
        i += 1
        if len(frame) == 16:
            return frame, i
    return None, i


def tpiu_deframe_walk(stream: bytes, want_stream: "int | None" = None,
                      check_frames: int = 6, bad_thresh: "float | None" = None,
                      scan: int = 48) -> bytes:
    """Seam-free continuous TPIU deframer with in-place re-lock (doc 15 §18).

    Unlike tpiu_deframe_local (fixed windows -> a frame lost per seam, ~1.5%
    floor), this walks frames continuously and re-aligns only on real lock
    loss: decode frames one at a time; peek the unknown-fraction of the next
    `check_frames` frames; if it exceeds `bad_thresh` (a dirty window shifted
    the boundary), scan the next `scan` byte offsets for the cleanest
    re-alignment, jump there, continue. Clean captures never trip it (0 seam
    loss); dirty windows re-lock right after, dropping only corrupt bytes.
    Use on an already-correctly-assembled stream.

    ADAPTIVE re-lock (doc 15 §27): when `bad_thresh is None` (default), pick the
    threshold from the stream's own baseline unknown-fraction. A capture WITHOUT
    a periodic TPIU full-sync (e.g. high-baud SWO where stall slows the CPU so
    sync packets almost never emit) drifts its frame boundary continuously, so
    the global unknown sits around ~10% and a fixed 0.30 threshold NEVER trips
    -> the walker coasts on one bad phase. Setting the threshold just above the
    achievable floor makes it re-lock aggressively at every drift, recovering
    the stream (measured 21 MHz SWO: 6.1% -> 0.36%, real PCs). For a clean
    full-sync stream the baseline is ~0 so the threshold stays tight and there
    are still zero spurious re-locks.
    """
    n = len(stream)
    if n < 32:
        ph, _ = find_tpiu_phase(stream)
        return tpiu_deframe_hsync(stream, ph, want_stream)

    def quality_ahead(pos, frames):
        tmp = bytearray()
        cur = 0
        i = pos
        for _ in range(frames):
            fr, i = _next_frame(stream, i, n)
            if fr is None:
                break
            cur = _decode_frame16(fr, cur, tmp, None)
        if not tmp:
            return 1.0, i
        unk = sum(1 for c in tmp if _classify(c) == "unknown")
        return unk / len(tmp), i

    phase, _ = find_tpiu_phase(stream[:min(n, 20000)])

    if bad_thresh is None:
        # Estimate the baseline unknown-fraction at the chosen phase over a
        # sample of the stream; relock threshold = baseline + margin, floored so
        # a genuinely clean stream keeps a wide (0.30) threshold and never
        # spuriously re-locks, while a drifting (no-full-sync) stream gets an
        # aggressive threshold just above its own floor.
        probe = tpiu_deframe_hsync(stream[:min(n, 40000)], phase)
        if probe:
            base = sum(1 for c in probe if _classify(c) == "unknown") / len(probe)
        else:
            base = 0.0
        # A high baseline (>2%) means there is NO periodic full-sync and the
        # frame boundary drifts -> the achievable floor is reached only by
        # re-locking AGGRESSIVELY, so use a low fixed threshold (just above the
        # ~0% floor a correctly-aligned window hits) rather than baseline+margin
        # (which would sit at the drift level and never trip). A low baseline is
        # a clean full-sync stream -> keep the wide 0.30 threshold (no spurious
        # re-locks). Measured 21 MHz SWO: baseline 17% -> thresh 0.05 -> 0.36%.
        # Only engage aggressive re-lock on a stream long enough for the
        # baseline estimate to be meaningful and for drift to actually occur
        # (a short synthetic stream has no drift; aggressive re-lock would just
        # fragment it).
        if n >= 8192 and base > 0.02:
            thresh = 0.05
        else:
            thresh = 0.30
    else:
        thresh = bad_thresh

    out = bytearray()
    cur = 0
    i = phase
    while i < n:
        frac, _ = quality_ahead(i, check_frames)
        if frac > thresh:
            best = (frac, i)
            for off in range(1, scan):
                if i + off >= n:
                    break
                f2, _ = quality_ahead(i + off, check_frames)
                if f2 < best[0]:
                    best = (f2, i + off)
                    if f2 <= 0.02:
                        break
            if best[1] != i and best[0] < frac:
                i = best[1]
        fr, i = _next_frame(stream, i, n)
        if fr is None:
            break
        cur = _decode_frame16(fr, cur, out, want_stream)
    return bytes(out)


def tpiu_deframe_walk_offsets(stream: bytes, want_stream: "int | None" = None,
                              check_frames: int = 6, bad_thresh: float = 0.30,
                              scan: int = 48):
    """Like tpiu_deframe_walk but ALSO returns, per output byte, the SOURCE
    byte offset in `stream` it was extracted from. Returns (out, offsets) where
    offsets[k] is the index into `stream` of the TPIU slot that produced out[k].

    This is the bridge for the FPGA capture-time base (doc 15 §24.2): the FPGA
    timestamps RAW capture bytes, so to put a wall-clock on a DECODED ETM byte
    we must know which RAW (pre-deframe) byte it came from. The frame's data
    slots map 1:1 to source positions, so we record the source index as each
    payload byte is emitted.
    """
    n = len(stream)

    def decode_frame_off(frame, fsrc, cur, out, offs):
        # fsrc[j] = source index of frame[j]; mirrors _decode_frame16.
        aux = frame[15]
        for j in range(15):
            if j % 2 == 0:
                if frame[j] & 1:
                    cur = frame[j] >> 1
                else:
                    b = frame[j] | ((aux >> (j // 2)) & 1)
                    if want_stream is None or cur == want_stream:
                        out.append(b)
                        offs.append(fsrc[j])
            else:
                if want_stream is None or cur == want_stream:
                    out.append(frame[j])
                    offs.append(fsrc[j])
        return cur

    def next_frame_off(i):
        frame = []
        fsrc = []
        while i < n:
            if stream[i] == 0xFF and i + 1 < n and stream[i + 1] == 0x7F:
                i += 2
                continue
            if (i + 3 < n and stream[i] == 0xFF and stream[i + 1] == 0xFF
                    and stream[i + 2] == 0xFF and stream[i + 3] == 0x7F):
                i += 4
                continue
            frame.append(stream[i])
            fsrc.append(i)
            i += 1
            if len(frame) == 16:
                return frame, fsrc, i
        return None, None, i

    def quality_ahead(pos, frames):
        tmp = bytearray()
        cur = 0
        i = pos
        for _ in range(frames):
            fr, _fs, i = next_frame_off(i)
            if fr is None:
                break
            cur = _decode_frame16(fr, cur, tmp, None)
        if not tmp:
            return 1.0, i
        unk = sum(1 for c in tmp if _classify(c) == "unknown")
        return unk / len(tmp), i

    if n < 32:
        ph, _ = find_tpiu_phase(stream)
        out = bytearray(tpiu_deframe_hsync(stream, ph, want_stream))
        offs = [min(n - 1, (k * n) // max(1, len(out))) for k in range(len(out))]
        return bytes(out), offs

    phase, _ = find_tpiu_phase(stream[:min(n, 20000)])
    out = bytearray()
    offs = []
    cur = 0
    i = phase
    while i < n:
        frac, _ = quality_ahead(i, check_frames)
        if frac > bad_thresh:
            best = (frac, i)
            for off in range(1, scan):
                if i + off >= n:
                    break
                f2, _ = quality_ahead(i + off, check_frames)
                if f2 < best[0]:
                    best = (f2, i + off)
                    if f2 <= 0.02:
                        break
            if best[1] != i and best[0] < frac:
                i = best[1]
        fr, fsrc, i = next_frame_off(i)
        if fr is None:
            break
        cur = decode_frame_off(fr, fsrc, cur, out, offs)
    return bytes(out), offs


def tpiu_deframe_local(stream: bytes, window: int = 5000,
                       want_stream: "int | None" = None) -> bytes:
    """Deframe with PER-WINDOW local frame phase (doc 15 §16/§17).

    A single global TPIU phase cannot span an occasional corrupt ~1KB capture
    window: the window inserts/drops bytes, shifting the 16-byte frame boundary
    for the whole tail, so one bad window scores the entire capture ~10-14%
    unknown. The nibble-pairing parity is CONSISTENT across the window (the
    window is locally damaged but self-heals), so only the FRAME phase needs to
    re-lock after each bad window.

    This deframes in `window`-byte segments, each with its own best frame phase
    (find_tpiu_phase), CONTAINING each dirty window's frame-shift to that
    window. Worst-case captures improve from ~14% to a few %, with all program
    anchors recovered. KNOWN LIMITATION: a frame is lost at each window seam
    (~1.5% floor even on otherwise-clean captures); a seam-free continuous
    re-aligning walker is future work (doc 15 §17). Use on an already-correctly
    -assembled stream (globally-best parity/order).
    """
    out = bytearray()
    n = len(stream)
    if n < 32:
        ph, _ = find_tpiu_phase(stream)
        return tpiu_deframe_hsync(stream, ph, want_stream)
    st = 0
    while st < n:
        seg = stream[st:st + window]
        if len(seg) < 32:
            break
        ph, _ = find_tpiu_phase(seg)
        out += tpiu_deframe_hsync(seg, ph, want_stream)
        st += window
    return bytes(out)


def find_tpiu_phase(stream: bytes, scorer=None) -> "tuple[int, int]":
    """Try all 16 TPIU frame start phases; return (best_phase, score). Default
    scorer = number of flash-range Normal I-sync anchors in the deframed
    output (the strongest 'this phase is right' signal)."""
    best = (0, -1)
    for ph in range(16):
        pl = tpiu_deframe_hsync(stream, ph)
        if scorer is None:
            score = sum(1 for s in find_isyncs(pl) if is_flash(s.addr))
        else:
            score = scorer(pl)
        if score > best[1]:
            best = (ph, score)
    return best



# ----------------------------------------------------------------------------
# V4: realigning region decoder. When the plain region walk derails on an
# unclassifiable byte (the sub-byte misalignment of a 4-bit port, IHI0014Q
# §7.10.4), try the 8 bit-shifts of the remaining bytes and resume from the
# first shift that yields a run of classifiable packets. This extends the
# short anchored regions toward continuous flow without any hardware change.
# ----------------------------------------------------------------------------
def _classify(c: int) -> str:
    """Classify an ETM3.5 IDLE-state header byte. 'unknown' if not a valid
    packet header (used as the realign trigger)."""
    if c & 1:
        return "branch"
    if c == 0x00:
        return "async"
    if c == 0x04:
        return "cyccnt"
    if c == 0x08:
        return "isync"
    if c == 0x70:
        return "isync_cyc"
    if c == 0x0C:
        return "trigger"
    if c == 0x3C:
        return "vmid"
    if (c & 0xFB) == 0x42:
        return "timestamp"
    if c == 0x66:
        return "ignore"
    if c == 0x6E:
        return "contextid"
    if c == 0x76:
        return "exc_exit"
    if c == 0x7E:
        return "exc_entry"
    if (c & 0x81) == 0x80:
        return "pheader"
    return "unknown"


def _classifiable_run(data: bytes, start: int, n: int = 6) -> int:
    """Count how many consecutive bytes from `start` look like valid packet
    headers (skipping each packet's body crudely by 1 byte). A high count means
    this alignment is plausible. Used to score candidate bit-shifts."""
    i = start
    good = 0
    while i < len(data) and good < n:
        k = _classify(data[i])
        if k == "unknown":
            break
        good += 1
        # crude skip: branch/cyccnt/timestamp consume continuation bytes
        if k in ("branch", "cyccnt", "timestamp"):
            i += 1
            while i < len(data) and (data[i - 1] & 0x80) and i - start < 6:
                i += 1
        elif k == "isync":
            i += 6
        else:
            i += 1
    return good


def decode_region_realign(data: bytes, start: int, base_addr: int,
                          max_bytes: int = 4096, min_run: int = 3):
    """Like decode_region, but on hitting an 'unknown' byte, try bit-shifts to
    realign and continue. Returns (events, consumed_bytes, realign_count)."""
    events = []
    realigns = 0
    work = data[start:start + max_bytes]
    i = 0
    base = base_addr
    while i < len(work):
        c = work[i]
        k = _classify(c)
        if k == "branch":
            n = 1
            while i + n < len(work) and (work[i + n - 1] & 0x80) and n < 5:
                n += 1
            events.append(FlowEvent("branch", base))
            i += n
            continue
        if k == "pheader":
            ph = _phdr_atoms(c)
            if ph is not None:
                events.append(FlowEvent("atoms", base, eatoms=ph[0], natoms=ph[1]))
            i += 1
            continue
        if k == "isync":
            s = parse_isync_at(work, i)
            if s is not None:
                events.append(FlowEvent("isync", s.addr, addr_is_target=True))
                base = s.addr
                i += 6
                continue
            # malformed isync -> treat as derail below
            k = "unknown"
        if k == "async":
            break          # next A-sync; caller re-anchors
        if k in ("trigger", "vmid", "ignore", "contextid", "exc_exit", "exc_entry"):
            i += 1
            continue
        if k in ("cyccnt", "timestamp"):
            i += 1
            while i < len(work) and (work[i - 1] & 0x80):
                i += 1
            continue
        # unknown -> attempt sub-byte realignment on the remaining bytes
        rest = work[i:]
        best = None
        for sh in range(1, 8):
            shifted = bit_shift(rest, sh)
            run = _classifiable_run(shifted, 0)
            if run >= min_run and (best is None or run > best[1]):
                best = (sh, run, shifted)
        if best is None:
            break
        realigns += 1
        work = best[2]
        i = 0
    return events, i, realigns


def decode_all_realign(data: bytes):
    """Anchor on every I-sync and extend each region with sub-byte realignment.
    Returns (events, total_realigns).

    NOTE on trust level: the I-sync 'isync' events are guaranteed-correct
    absolute PCs. The 'atoms'/'branch' events recovered after a realignment are
    PLAUSIBLE (they pass the ETM3.5 header classifier in the realigned phase)
    but are NOT independently corroborated unless a later flash-address I-sync
    re-anchors. Treat anchors as ground truth and realigned flow as indicative.
    """
    events = []
    total_realigns = 0
    for s in find_isyncs(data):
        events.append(FlowEvent("isync", s.addr, addr_is_target=True))
        region, _, ra = decode_region_realign(data, s.offset + 6, s.addr)
        events.extend(region)
        total_realigns += ra
    return events, total_realigns


# ----------------------------------------------------------------------------
# Step ①: continuous per-instruction reconstruction primitives.
#
# Two facts make per-instruction PC reconstruction from a known anchor possible
# (both spec-grounded and empirically confirmed on the logic-analyser ground
# truth, doc 14):
#
#   * One atom per executed instruction. IHI0014Q §7.3.4: a P-header is "a
#     sequence of Atoms that indicate the execution of instructions"; E = an
#     instruction that passed its condition codes, N = one that failed. The
#     nop;b while(1) loop traced as a steady 0x88 (Format-1, E=2) = exactly the
#     two instructions per iteration, confirming per-instruction atoms.
#   * Direct branch targets are inferred from the code image; only INDIRECT
#     branches emit a Branch Address packet (IHI0014Q §4.5.2 / §4.10.3). With
#     ETMCR bit8 ("branch output", value 0x980) set, every taken branch is
#     reported, which keeps the walk anchored.
#
# So: walk atoms against the disassembled image. For each atom, look up the
# instruction at the current PC; a direct branch with E jumps to its (image-
# derived) target, N falls through; an indirect branch consumes the next Branch
# Address packet for its target; any other instruction advances by its width.
# ----------------------------------------------------------------------------

def expand_pheader(c: int) -> "list[str] | None":
    """Expand a non-cycle-accurate P-header byte into an ordered atom list.

    Returns a list of 'E'/'N' in execution order, or None if `c` is not a
    non-CA P-header. IHI0014Q Table 7-2 / Example 7-1:

      * Format-1 (b1NEEEE00): EEEE E-atoms followed by 0/1 N-atom
        (e.g. 0xC8 -> EEN).
      * Format-2 (b1000FF10): bit3 = first instruction, bit2 = second;
        a 0 bit = E (passed), 1 bit = N (failed) (e.g. 0x8A -> NE).
    """
    if (c & 0b10000011) == 0b10000000:          # Format-1
        eatoms = (c & 0x3C) >> 2
        natoms = 1 if (c & (1 << 6)) else 0
        return ["E"] * eatoms + ["N"] * natoms
    if (c & 0b11110011) == 0b10000010:          # Format-2
        first = "E" if not (c & (1 << 3)) else "N"
        second = "E" if not (c & (1 << 2)) else "N"
        return [first, second]
    return None


def decode_branch_thumb(data: bytes, i: int, prev_addr: int):
    """Decode a Thumb-state branch address packet starting at offset i.

    Returns (target_addr, nbytes) or None if not a valid branch header / runs
    off the end. Implements the original (standard) encoding, IHI0014Q Fig 7-4:

        byte1: bit0=1 marker, bits[6:1]=Address[6:1],  C=bit7
        byte2: bits[6:0]=Address[13:7],                C=bit7
        byte3: bits[6:0]=Address[20:14],               C=bit7
        byte4: bits[6:0]=Address[27:21],               C=bit7
        byte5: bits[3:0]=Address[31:28],               C=bit6 (exception info)

    Compression (IHI0014Q §7.3.5): only the low-order changed bits are sent;
    unsent high bits are inherited from prev_addr (the last branch/I-sync PC).
    Address bit0 (Thumb) is always 0. Exception Information Bytes (byte5 C=1)
    are NOT consumed here (none occur in straight-line/loop code); callers that
    need them must handle the continuation.
    """
    if i >= len(data) or not (data[i] & 1):
        return None
    addr = prev_addr & 0xFFFFFFFF
    c = data[i]
    addr = (addr & ~0x7F) | (c & 0x7E)          # Address[6:1]
    n = 1
    cont = bool(c & 0x80)
    while cont and n < 5:
        if i + n >= len(data):
            return None
        c = data[i + n]
        if n < 4:
            start = 7 * n                        # 7,14,21
            addr = (addr & ~(0x7F << start)) | ((c & 0x7F) << start)
            cont = bool(c & 0x80)
        else:                                    # byte 5: Address[31:28]
            addr = (addr & ~(0xF << 28)) | ((c & 0xF) << 28)
            cont = bool(c & 0x40)                # exception info follows
        n += 1
    return addr & 0xFFFFFFFE, n                  # strip Thumb bit
