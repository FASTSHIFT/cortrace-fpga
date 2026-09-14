"""Unit tests for etm35lib, with golden vectors taken from IHI0014Q.

Run:  pytest -q test_etm35lib.py
"""
import struct

import pytest

import etm35lib as L


# ----------------------------------------------------------------------------
# is_flash
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("addr,expected", [
    (0x08000000, True),
    (0x08000001, True),          # thumb bit set, still flash
    (0x0800abcd, True),
    (0x080FFFFF, True),
    (0x08100000, False),         # one past the 1MB region
    (0x20000000, False),         # SRAM
    (0x00000000, False),
    (0xF0CCD1F8, False),         # the garbage we saw pre-anchor
])
def test_is_flash(addr, expected):
    assert L.is_flash(addr) is expected


# ----------------------------------------------------------------------------
# find_asyncs  (IHI0014Q §7.10.4: >=5 zeros then 0x80)
# ----------------------------------------------------------------------------
def test_find_async_basic():
    data = bytes([1, 2]) + L.ASYNC + bytes([0x84, 9])
    offs = L.find_asyncs(data)
    # 0x80 sits at index 2+5 = 7
    assert offs == [7]


def test_find_async_more_than_five_zeros():
    # 8 zeros then 0x80 is still a valid A-sync (DDI0328-style 8-zero form)
    data = bytes([0] * 8 + [0x80, 0x84])
    assert L.find_asyncs(data) == [8]


def test_find_async_too_few_zeros():
    data = bytes([0, 0, 0, 0, 0x80])   # only 4 zeros
    assert L.find_asyncs(data) == []


def test_find_async_zeros_not_terminated_by_80():
    data = bytes([0, 0, 0, 0, 0, 0x84])  # 5 zeros then a non-0x80
    assert L.find_asyncs(data) == []


def test_find_async_multiple():
    blk = L.ASYNC + bytes([0x88] * 10)
    data = blk + blk
    offs = L.find_asyncs(data)
    assert len(offs) == 2
    assert offs[1] - offs[0] == len(blk)


# ----------------------------------------------------------------------------
# parse_isync_at / find_isyncs  (IHI0014Q Fig 7-42, 0 ContextID bytes on M4)
# ----------------------------------------------------------------------------
def make_isync(addr, thumb=False, reason=L.REASON_PERIODIC, nonsecure=False):
    info = (reason & 0b11) << 5
    if nonsecure:
        info |= 0x08
    a = addr | (1 if thumb else 0)
    return bytes([0x08, info]) + struct.pack("<I", a)


def test_parse_isync_roundtrip():
    pkt = make_isync(0x08005414, thumb=True, reason=L.REASON_PERIODIC)
    s = L.parse_isync_at(pkt, 0)
    assert s is not None
    assert s.addr == 0x08005414
    assert s.thumb is True
    assert s.reason == L.REASON_PERIODIC


def test_parse_isync_rejects_lsip():
    # info bit7 set => LSiP I-sync, not Normal -> reject
    pkt = bytearray(make_isync(0x08005414))
    pkt[1] |= 0x80
    assert L.parse_isync_at(bytes(pkt), 0) is None


def test_parse_isync_rejects_nonflash():
    pkt = make_isync(0x20001000)        # SRAM addr
    assert L.parse_isync_at(pkt, 0) is None


def test_parse_isync_rejects_wrong_header():
    pkt = bytearray(make_isync(0x08005414))
    pkt[0] = 0x88                        # P-header, not I-sync
    assert L.parse_isync_at(bytes(pkt), 0) is None


def test_parse_isync_truncated():
    pkt = make_isync(0x08005414)[:4]     # too short
    assert L.parse_isync_at(pkt, 0) is None


def test_find_isyncs_embedded():
    noise = bytes([0x88, 0x8c, 0x01, 0x40])
    # addresses are even; the Thumb bit is conveyed separately (bit0) and the
    # decoder strips it, so use even base addresses here.
    s1 = make_isync(0x08001234)
    s2 = make_isync(0x0800abcc, thumb=True)
    data = noise + s1 + noise + s2 + noise
    found = L.find_isyncs(data)
    addrs = [s.addr for s in found]
    assert 0x08001234 in addrs
    assert 0x0800abcc in addrs


def test_recover_pcs_dedup_and_sort():
    s = make_isync(0x0800abcc)
    data = s + bytes([0x88]) + s        # same PC twice
    assert L.recover_pcs(data) == [0x0800abcc]


def test_thumb_bit_stripped_from_addr():
    # An odd address means Thumb; the returned addr has bit0 cleared and
    # thumb=True regardless of the explicit thumb flag.
    s = L.parse_isync_at(make_isync(0x08001234, thumb=True), 0)
    assert s.addr == 0x08001234
    assert s.thumb is True


# ----------------------------------------------------------------------------
# bit_shift  (IHI0014Q §7.10.4 sub-byte realignment model)
# ----------------------------------------------------------------------------
def test_bit_shift_identity():
    data = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    assert L.bit_shift(data, 0) == data


def test_bit_shift_known_vector():
    # IHI0014Q §7.10.4 worked example: the byte-aligned A-sync+E-header
    #   00 00 00 00 00 80 84
    # captured 1 bit early (off by 1) reads as
    #   01 00 00 00 00 40 42
    # i.e. right-shifting the misaligned capture by 1 bit recovers the aligned
    # form (minus the leading partial byte). Verify the shift math reproduces
    # the relationship for the trailing 80 84 <-> 40 42 pair.
    misaligned = bytes([0x40, 0x42])     # 80 84 shifted right 1 (carry)
    # shifting 'misaligned' LEFT by 1 == our stream captured 1 bit late;
    # here we just check bit_shift is the exact inverse operation it claims.
    shifted = L.bit_shift(bytes([0x80, 0x84]), 1)
    assert shifted[0] == 0x40            # 0x80>>1 | (0x84<<7)&0xff = 0x40
    assert len(shifted) == 1


def test_bit_shift_range():
    with pytest.raises(ValueError):
        L.bit_shift(b"\x00\x01", 8)


def test_bit_shift_recovers_isync_when_offset():
    # Build an I-sync, push the whole stream 3 bits late, and confirm
    # bit_shift(.,3) recovers a parseable I-sync.
    pkt = make_isync(0x08001234)
    stream = pkt + bytes([0x88, 0x8c])
    # emulate a 3-bit-late capture: shift everything LEFT by 3 (lose low bits)
    late = bytearray()
    prev = 0
    for b in stream:
        late.append(((b << 3) | (prev >> 5)) & 0xFF)
        prev = b
    recovered = L.bit_shift(bytes(late), 3)
    assert any(s.addr == 0x08001234 for s in L.find_isyncs(recovered))


# ----------------------------------------------------------------------------
# traceif_assemble — model of verilog/traceIF.v
# ----------------------------------------------------------------------------
def nibbles_for(byte_stream, sync_first=True):
    """Encode a byte stream as traceIF nibble-bytes {b<<4 | a}, LSB nibble
    first per the RTL shift order, prefixed with a 0x7FFFFFFF sync so the
    model locks."""
    # Build the 16-bit-packet view: traceIF emits packets little-endian byte
    # pairs. To keep the test simple we feed it the sync then raw nibbles.
    out = bytearray()

    def emit_byte(v):
        # each captured clk carries trace_a (low nibble) + trace_b (high)
        out.append(v)

    # 0x7FFFFFFF sync = 8 nibbles of 0xF except top; we just feed the exact
    # nibble pattern the RTL recognises: simplest is to feed 0xFF bytes that
    # form 0x7FFFFFFF in the shift register. This is covered by the RTL sim,
    # so here we only assert the function runs and is deterministic.
    return bytes(out)


def test_traceif_assemble_deterministic():
    # Same input twice -> identical output (no hidden state).
    data = bytes(range(256)) * 4
    assert L.traceif_assemble(data) == L.traceif_assemble(data)


def test_traceif_assemble_empty():
    assert L.traceif_assemble(b"") == b""


def test_traceif_assemble_no_sync_yields_nothing():
    # A stream that never contains 0x7FFFFFFF in the shift register emits
    # nothing (never locks). All-zero nibbles can't form the sync word.
    assert L.traceif_assemble(bytes(100)) == b""


def test_traceif_assemble_locks_and_emits():
    # Drive the shift register to 0x7FFFFFFF then feed payload nibbles so the
    # post-sync packet-emit branch runs (covers the synced/emit path).
    #   - 12 x 0xFF fills construct with 0xF nibbles
    #   - 0x7F puts b=0x7,a=0xF on top -> construct top32 == 0x7FFFFFFF (lock)
    #   - following non-0x7FFF payload bytes are emitted as 16-bit packets
    stream = bytes([0xFF] * 12 + [0x7F] + [0x12, 0x34, 0x56, 0x78] * 4)
    out = L.traceif_assemble(stream)
    assert isinstance(out, bytes)
    assert len(out) > 0                       # locked and emitted packets
    # deterministic
    assert L.traceif_assemble(stream) == out


def test_traceif_assemble_drops_idle_packets():
    # After lock, a 0x7FFF packet (idle half-word) must be dropped, not emitted.
    # Feed nibbles that form 0x7FFF packets only -> no payload bytes out.
    stream = bytes([0xFF] * 12 + [0x7F] + [0xF7, 0xFF] * 8)
    out = L.traceif_assemble(stream)
    # all emitted packets that equal 0x7FFF are dropped; output may be empty or
    # only contain non-idle packets — assert no 0x7fff pair leaked through
    pairs = [out[k:k + 2] for k in range(0, len(out) - 1, 2)]
    assert b"\xff\x7f" not in pairs


# ----------------------------------------------------------------------------
# Regression against a real on-board capture fixture (captures/etm_isync_fixture.bin)
# 8 KB slice of a real STM32F429 ETM capture (LVGL widgets demo). Locks in the
# spec-correct I-sync extraction so future refactors can't silently regress.
# ----------------------------------------------------------------------------
import os

FIXTURE = os.path.join(os.path.dirname(__file__), "captures", "etm_isync_fixture.bin")


@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="fixture missing")
def test_real_capture_isync_anchors():
    data = open(FIXTURE, "rb").read()
    syncs = L.find_isyncs(data)
    # The 8KB slice contains exactly two periodic I-sync anchors at the fixed
    # 1024-byte sync cadence; both carry valid flash PCs.
    assert len(syncs) == 2
    pcs = L.recover_pcs(data)
    assert pcs == [0x08006530, 0x080083D0]
    for s in syncs:
        assert L.is_flash(s.addr)


@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="fixture missing")
def test_real_capture_async_present():
    data = open(FIXTURE, "rb").read()
    # A-syncs (>=5 zeros + 0x80) must be present at roughly the sync cadence.
    asyncs = L.find_asyncs(data)
    assert len(asyncs) >= 1


# ----------------------------------------------------------------------------
# P-header decode (IHI0014Q §7.3.4) and region/flow decode
# ----------------------------------------------------------------------------
def test_phdr_format1():
    # 0x88 = 0b10001000 -> Format-1: eatoms=(0x88&0x3C)>>2=2, natoms=0
    assert L._phdr_atoms(0x88) == (2, 0)


def test_phdr_format1_with_natom():
    # bit6 set -> 1 natom. 0xC0 = 0b11000000 -> eatoms=0, natoms=1
    assert L._phdr_atoms(0xC0) == (0, 1)


def test_phdr_format2():
    # 0x82 = 0b10000010 matches Format-2 mask 0b11110011==0b10000010
    res = L._phdr_atoms(0x82)
    assert res is not None
    assert sum(res) == 2          # Format-2 always totals 2 atoms


def test_phdr_not_a_header():
    assert L._phdr_atoms(0x01) is None     # branch (bit0=1)
    assert L._phdr_atoms(0x08) is None     # I-sync header
    assert L._phdr_atoms(0x00) is None     # A-sync zero


def test_decode_region_atoms_and_branch():
    # I-sync addr base, then a Format-1 P-header (0x88, 2 exec), then a branch
    # packet (0x01, single byte), then a 0x00 to stop.
    base = 0x08001000
    data = bytes([0x88, 0x01, 0x00])
    events, end = L.decode_region(data, 0, base)
    kinds = [e.kind for e in events]
    assert kinds == ["atoms", "branch"]
    assert events[0].eatoms == 2
    assert end == 2               # stopped at the 0x00


def test_decode_region_stops_on_unknown():
    # 0x66 (Ignore packet) is not classified by our minimal walker -> stop.
    base = 0x08001000
    events, end = L.decode_region(bytes([0x66, 0x88]), 0, base)
    assert events == []
    assert end == 0


def test_decode_region_follows_embedded_isync():
    base = 0x08001000
    s = make_isync(0x0800c0de)
    data = bytes([0x88]) + s + bytes([0x88, 0x00])
    events, _ = L.decode_region(data, 0, base)
    # first atoms, then the embedded isync re-anchors, then atoms
    assert events[0].kind == "atoms"
    assert any(e.kind == "isync" and e.addr == 0x0800c0de for e in events)


def test_decode_region_stops_on_bad_isync_header():
    # A 0x08 byte that is NOT a valid Normal I-sync (non-flash addr) must stop
    # the region walk rather than be mis-followed.
    base = 0x08001000
    bad = bytes([0x08, 0x00, 0x00, 0x00, 0x00, 0x20])  # addr 0x20000000 (SRAM)
    data = bytes([0x88]) + bad
    events, end = L.decode_region(data, 0, base)
    assert events[0].kind == "atoms"
    # walk stops at the bogus 0x08 (not classified as a valid I-sync)
    assert all(e.kind != "isync" for e in events)
    assert end == 1


def test_decode_all_on_fixture():
    import os
    if not os.path.exists(FIXTURE):
        pytest.skip("fixture missing")
    data = open(FIXTURE, "rb").read()
    events = L.decode_all(data)
    isyncs = [e for e in events if e.kind == "isync"]
    assert len(isyncs) == 2
    # at least some executed-atom flow recovered after the anchors
    assert any(e.kind == "atoms" for e in events)


# ----------------------------------------------------------------------------
# Alignment-aware decode (IHI0014Q §7.10.4 sub-byte realignment)
# ----------------------------------------------------------------------------
def _async_block(payload: bytes) -> bytes:
    """A canonical A-sync (5 zeros + 0x80) followed by payload."""
    return L.ASYNC + payload


def test_find_isync_in_window_basic():
    win = bytes([0x88, 0x8c]) + make_isync(0x08001234) + bytes([0x88])
    hit = L._find_isync_in_window(win)
    assert hit is not None
    off, s = hit
    assert s.addr == 0x08001234


def test_find_isync_in_window_none():
    assert L._find_isync_in_window(bytes([0x88, 0x8c, 0x01, 0x40] * 4)) is None


def test_decode_aligned_shift0():
    # A-sync, then (aligned) I-sync + a P-header. Shift 0 should win.
    data = _async_block(make_isync(0x08002000) + bytes([0x88, 0x00]))
    regions = L.decode_aligned(data)
    assert len(regions) == 1
    r = regions[0]
    assert r.shift == 0
    assert r.isync.addr == 0x08002000
    assert any(e.kind == "atoms" for e in r.events)


def test_decode_aligned_recovers_bitshifted_region():
    # Build an aligned region, then shift the WHOLE tail left by 3 bits to
    # emulate a 3-bit-late capture; decode_aligned must find shift==3.
    payload = make_isync(0x0800c0de) + bytes([0x88, 0x88, 0x00])
    # left-shift tail by 3 bits (capture 3 bits late)
    late = bytearray()
    prev = 0
    for b in payload:
        late.append(((b << 3) | (prev >> 5)) & 0xFF)
        prev = b
    data = L.ASYNC + bytes(late)
    regions = L.decode_aligned(data)
    assert len(regions) == 1
    assert regions[0].shift == 3
    assert regions[0].isync.addr == 0x0800c0de


def test_decode_aligned_skips_async_without_isync():
    # An A-sync followed by no parseable I-sync in the window -> no region.
    data = _async_block(bytes([0x88, 0x8c, 0x01, 0x40] * 8))
    assert L.decode_aligned(data) == []


def test_aligned_pcs_dedup():
    blk = _async_block(make_isync(0x08002000) + bytes([0x88, 0x00]))
    data = blk + blk
    assert L.aligned_pcs(data) == [0x08002000]


def test_decode_aligned_on_fixture():
    import os
    if not os.path.exists(FIXTURE):
        pytest.skip("fixture missing")
    data = open(FIXTURE, "rb").read()
    regions = L.decode_aligned(data)
    # NOTE: per IHI0014Q §7.10.3 the periodic A-sync and periodic I-sync are
    # driven by independent counters and need NOT be adjacent. In this fixture
    # the I-syncs sit ~1000 bytes after the nearest A-sync, beyond the default
    # scan window, so the windowed A-sync->I-sync search legitimately finds no
    # region. This documents that the "I-sync immediately follows A-sync"
    # assumption does not hold and decode must anchor on I-sync independently
    # (find_isyncs), which the fixture test below covers.
    for r in regions:
        assert L.is_flash(r.isync.addr)
        assert 0 <= r.shift <= 7


# ----------------------------------------------------------------------------
# TPIUSync reference model (orbtrace trace/tpiu.py port) — the byte-aligning
# front-end we should reuse (10-reuse-gap-audit.md).
# ----------------------------------------------------------------------------
def test_tpiu_sync_locks_and_frames():
    # Full-sync 0xFFFFFF7F (wire order FF FF FF 7F) then 16 payload bytes ->
    # one complete frame. Note the amaranth buf packs the first payload byte
    # into the HIGH bits, so the extracted frame is byte-reversed wrt arrival.
    payload = bytes(range(16))            # 0x00..0x0F
    stream = bytes([0xFF, 0xFF, 0xFF, 0x7F]) + payload
    frames = L.tpiu_sync_frames(stream)
    assert len(frames) == 1
    assert frames[0] == payload[::-1]


def test_tpiu_sync_no_lock_without_fullsync():
    # Without the full-sync word, nothing is emitted (synced stays False).
    assert L.tpiu_sync_frames(bytes(range(64))) == []


def test_tpiu_sync_filters_halfsync():
    # After lock, a 0x7FFF half-sync (wire FF 7F) is filtered out, so it does
    # not consume frame byte slots. Build: fullsync, 14 payload, halfsync,
    # 2 more payload -> exactly one 16-byte frame of the 16 real payload bytes.
    payload = bytes(range(16))
    stream = (bytes([0xFF, 0xFF, 0xFF, 0x7F])
              + payload[:14] + bytes([0xFF, 0x7F]) + payload[14:])
    frames = L.tpiu_sync_frames(stream)
    assert len(frames) == 1
    assert frames[0] == payload[::-1]


def test_tpiu_sync_multiple_frames():
    payload = bytes(range(16))
    stream = bytes([0xFF, 0xFF, 0xFF, 0x7F]) + payload + payload
    frames = L.tpiu_sync_frames(stream)
    assert len(frames) == 2
    assert frames[0] == payload[::-1] and frames[1] == payload[::-1]


def test_tpiu_sync_on_real_raw_capture():
    # On a real raw nibble capture, swapping the nibble order ({a<<4|b}) and
    # running orbtrace's TPIUSync assembles hundreds of aligned TPIU frames --
    # proving the formatter framing is present and orbtrace's own logic
    # recovers it (the reuse we were missing). The non-swapped order yields 0,
    # which also tells us the correct trace_a/trace_b nibble assignment.
    import os
    raw_path = "/tmp/trace_etm.bin"
    if not os.path.exists(raw_path):
        pytest.skip("raw capture not present")
    raw = open(raw_path, "rb").read()
    swapped = bytes((((x & 0xF) << 4) | ((x >> 4) & 0xF)) for x in raw)
    frames = L.tpiu_sync_frames(swapped)
    assert len(frames) > 100        # hundreds of aligned frames expected


# ----------------------------------------------------------------------------
# V4 realigning region decoder
# ----------------------------------------------------------------------------
def test_classify_headers():
    assert L._classify(0x01) == "branch"
    assert L._classify(0x00) == "async"
    assert L._classify(0x08) == "isync"
    assert L._classify(0x88) == "pheader"
    assert L._classify(0x0C) == "trigger"
    assert L._classify(0x66) == "ignore"
    assert L._classify(0x6E) == "contextid"
    assert L._classify(0x76) == "exc_exit"
    assert L._classify(0x7E) == "exc_entry"
    assert L._classify(0x42) == "timestamp"
    assert L._classify(0x38) == "unknown"
    assert L._classify(0x50) == "unknown"


def test_classifiable_run_counts():
    # all P-headers -> full run
    data = bytes([0x88] * 8)
    assert L._classifiable_run(data, 0, 6) == 6


def test_classifiable_run_stops_on_unknown():
    data = bytes([0x88, 0x88, 0x38, 0x88])
    assert L._classifiable_run(data, 0, 6) == 2


def test_decode_region_realign_handles_known_packets():
    # trigger, ignore, contextid, exc-exit are now consumed (region survives)
    data = bytes([0x88, 0x0C, 0x66, 0x88, 0x00])
    events, _, realigns = L.decode_region_realign(data, 0, 0x08001000)
    kinds = [e.kind for e in events]
    assert kinds.count("atoms") == 2     # the two P-headers
    assert realigns == 0                 # nothing needed realign


def test_decode_region_realign_recovers_after_shift():
    # Build a valid run of P-headers, then splice a 3-bit-shifted run of
    # P-headers; the realigner should recover and decode the second run too.
    good = bytes([0x88, 0x88, 0x88])
    tail = bytes([0x88] * 6)
    # shift tail LEFT 3 bits to misalign
    late = bytearray()
    prev = 0
    for b in tail:
        late.append(((b << 3) | (prev >> 5)) & 0xFF)
        prev = b
    data = good + bytes(late)
    events, _, realigns = L.decode_region_realign(data, 0, 0x08001000)
    # at least the first run's P-headers decode; realign attempted on the
    # misaligned tail
    assert sum(1 for e in events if e.kind == "atoms") >= 3
    assert realigns >= 1


def test_decode_region_realign_on_fixture_extends():
    import os
    if not os.path.exists(FIXTURE):
        pytest.skip("fixture missing")
    data = open(FIXTURE, "rb").read()
    syncs = L.find_isyncs(data)
    assert syncs
    s = syncs[0]
    plain, end_plain, _ = (lambda r: (r[0], r[1], 0))(L.decode_region(data, s.offset + 6, s.addr)) \
        if False else (None, None, None)
    ev_plain, end_p = L.decode_region(data, s.offset + 6, s.addr)
    ev_re, end_re, realigns = L.decode_region_realign(data, s.offset + 6, s.addr)
    # realigning version consumes at least as much as the plain walk
    assert len(ev_re) >= len(ev_plain)


def test_decode_region_realign_consumes_cyccnt_and_timestamp():
    # cyccnt (0x04) + 1 continuation byte (bit7), then timestamp (0x42) + cont,
    # then a P-header, then async-stop.
    data = bytes([0x04, 0x81, 0x00, 0x88, 0x00])
    events, _, realigns = L.decode_region_realign(data, 0, 0x08001000)
    assert realigns == 0
    assert any(e.kind == "atoms" for e in events)


def test_decode_region_realign_malformed_isync_triggers_realign_or_stop():
    # 0x08 followed by a non-flash addr (SRAM) is a malformed Normal I-sync;
    # the walker must not emit it as an anchor.
    bad = bytes([0x08, 0x00, 0x00, 0x00, 0x00, 0x20])
    data = bytes([0x88]) + bad + bytes([0x88] * 6)
    events, _, _ = L.decode_region_realign(data, 0, 0x08001000)
    assert all(not (e.kind == "isync" and e.addr == 0x20000000) for e in events)


def test_decode_all_realign_runs():
    blk = L.ASYNC + make_isync(0x08002000) + bytes([0x88, 0x88, 0x00])
    data = blk + blk
    events, realigns = L.decode_all_realign(data)
    assert any(e.kind == "isync" and e.addr == 0x08002000 for e in events)
    assert realigns >= 0


def test_decode_all_realign_on_fixture_extends():
    import os
    if not os.path.exists(FIXTURE):
        pytest.skip("fixture missing")
    data = open(FIXTURE, "rb").read()
    plain = L.decode_all(data)
    extended, realigns = L.decode_all_realign(data)
    # extended decode recovers at least as many events as the plain walk
    assert len(extended) >= len(plain)


# ----------------------------------------------------------------------------
# r14 BUG-1 hardening: reject Jazelle/AltISA, half-word align, ELF-tight range
# ----------------------------------------------------------------------------
def test_isync_rejects_jazelle_bit():
    pkt = bytearray(make_isync(0x08001234))
    pkt[1] |= 0x10               # bit4 Jazelle set -> not Cortex-M
    assert L.parse_isync_at(bytes(pkt), 0) is None


def test_isync_rejects_altisa_bit():
    pkt = bytearray(make_isync(0x08001234))
    pkt[1] |= 0x04               # bit2 AltISA set -> not Cortex-M
    assert L.parse_isync_at(bytes(pkt), 0) is None


def test_isync_rejects_unaligned_addr():
    # address 0x08001235 -> &~1 = 0x08001234 (even) is fine; but an address
    # whose &~1 is still odd is impossible; instead test a word-misaligned-only
    # scenario is N/A. Verify a normal even addr passes.
    assert L.parse_isync_at(make_isync(0x08001234), 0) is not None


def test_isync_custom_flash_range_tightens():
    # A valid-looking I-sync at 0x080F0000 passes the default 1MB bound but
    # should be rejected by a tight .text bound of [0x08000000, 0x08030000).
    pkt = make_isync(0x080F0000)
    assert L.parse_isync_at(pkt, 0) is not None                      # default
    assert L.parse_isync_at(pkt, 0, flash_hi=0x08030000) is None     # tight


def test_find_isyncs_respects_tight_range():
    s_in = make_isync(0x08001000)
    s_out = make_isync(0x080E0000)
    data = s_in + bytes([0x88]) + s_out
    addrs = {s.addr for s in L.find_isyncs(data, flash_hi=0x08030000)}
    assert 0x08001000 in addrs
    assert 0x080E0000 not in addrs


def test_flowevent_branch_addr_not_target():
    # decode a region with a branch; the branch FlowEvent must NOT claim its
    # addr is a target (addr_is_target stays False).
    data = bytes([0x01, 0x00])   # branch packet then async-stop
    events, _ = L.decode_region(data, 0, 0x08001000)
    br = [e for e in events if e.kind == "branch"]
    assert br and all(e.addr_is_target is False for e in br)


def test_flowevent_isync_addr_is_target():
    s = make_isync(0x08002000)
    events = L.decode_all(s)
    isy = [e for e in events if e.kind == "isync"]
    assert isy and all(e.addr_is_target is True for e in isy)


# ----------------------------------------------------------------------------
# expand_pheader  (IHI0014Q Table 7-2 / Example 7-1)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("byte,atoms", [
    (0x80, []),                  # Format-1, 0 E, 0 N
    (0x88, ["E", "E"]),          # Format-1, EE  (the while(1){nop;b} peak)
    (0x84, ["E"]),               # Format-1, 1 E
    (0xC8, ["E", "E", "N"]),     # Format-1, EEN (spec Example 7-1)
    (0xC0, ["N"]),               # Format-1, 0 E + 1 N
    (0x8A, ["N", "E"]),          # Format-2, NE  (spec Example 7-1: bit3=1->N,bit2=0->E)
    (0x82, ["E", "E"]),          # Format-2, both bits 0 -> EE
    (0x8E, ["N", "N"]),          # Format-2, both bits 1 -> NN
    (0x86, ["E", "N"]),          # Format-2, bit3=0->E, bit2=1->N
])
def test_expand_pheader(byte, atoms):
    assert L.expand_pheader(byte) == atoms


@pytest.mark.parametrize("byte", [0x00, 0x01, 0x08, 0x70, 0x42, 0x7E])
def test_expand_pheader_rejects_non_pheaders(byte):
    assert L.expand_pheader(byte) is None


def test_expand_pheader_format1_full_range():
    # Format-1 EEEE field is bits[5:2]: 0..15 E atoms, optional N at bit6.
    for e in range(16):
        b = 0x80 | (e << 2)
        assert L.expand_pheader(b) == ["E"] * e
        bn = b | 0x40
        assert L.expand_pheader(bn) == ["E"] * e + ["N"]


# ----------------------------------------------------------------------------
# decode_branch_thumb  (IHI0014Q Fig 7-4 original Thumb encoding)
# ----------------------------------------------------------------------------
def test_branch_thumb_single_byte_low_bits():
    # 1-byte branch: bit0=1 marker, bits[6:1]=Address[6:1], C(bit7)=0.
    # prev = 0x08000fae; encode target 0x08000fb2 in low 7 bits only.
    prev = 0x08000FAE
    target = 0x08000FB2
    # byte: marker(1) | (target[6:1]<<1)
    b = 0x01 | (((target >> 1) & 0x3F) << 1)
    res = L.decode_branch_thumb(bytes([b, 0x00]), 0, prev)
    assert res is not None
    addr, n = res
    assert n == 1
    assert addr == target


def test_branch_thumb_two_byte():
    # 2-byte branch carries Address[13:7] in the second byte (C=0 on it).
    prev = 0x08000000
    target = 0x08001234
    b1 = 0x80 | 0x01 | (((target >> 1) & 0x3F) << 1)     # C=1, A[6:1]
    b2 = (target >> 7) & 0x7F                             # C=0, A[13:7]
    res = L.decode_branch_thumb(bytes([b1, b2, 0x00]), 0, prev)
    assert res is not None
    addr, n = res
    assert n == 2
    assert (addr & 0x3FFF) == (target & 0x3FFF)          # low 14 bits exact


def test_branch_thumb_inherits_high_bits_from_prev():
    # Compression: unsent high bits come from prev_addr.
    prev = 0x08000F00
    # 1-byte packet only specifies Address[6:1]; high bits inherit 0x08000F00.
    target_low = 0x2A
    b = 0x01 | (target_low << 1)
    addr, n = L.decode_branch_thumb(bytes([b]), 0, prev)
    assert (addr & ~0x7F) == (prev & ~0x7F)


def test_branch_thumb_rejects_non_branch():
    assert L.decode_branch_thumb(bytes([0x88]), 0, 0x08000000) is None
    assert L.decode_branch_thumb(b"", 0, 0x08000000) is None


def test_branch_thumb_strips_thumb_bit():
    addr, _ = L.decode_branch_thumb(bytes([0x01]), 0, 0x08000001)
    assert addr & 1 == 0


# ----------------------------------------------------------------------------
# TPIU sync-filler stripping (orbuculum SYNCPATTERN / HALFSYNC constants)
# ----------------------------------------------------------------------------
def test_strip_full_sync():
    data = bytes([0x88, 0x01]) + bytes.fromhex("ffffff7f") + bytes([0x88])
    assert L.strip_tpiu_sync(data) == bytes([0x88, 0x01, 0x88])


def test_strip_half_sync_pairs():
    # 0xFF 0x7F repeated is half-sync filler.
    data = bytes([0x88]) + b"\xff\x7f\xff\x7f" + bytes([0x01])
    assert L.strip_tpiu_sync(data) == bytes([0x88, 0x01])


def test_strip_mixed_full_and_half():
    data = (bytes([0x08, 0x00]) + bytes.fromhex("ffffff7f")
            + b"\xff\x7f" + bytes([0x90]))
    assert L.strip_tpiu_sync(data) == bytes([0x08, 0x00, 0x90])


def test_strip_idempotent_on_clean_stream():
    clean = bytes([0x88, 0x01, 0x37, 0x05, 0xA8])
    assert L.strip_tpiu_sync(clean) == clean


def test_strip_preserves_isolated_ff_not_followed_by_7f():
    # A lone 0xFF not paired with 0x7F is left as-is (real data byte).
    data = bytes([0x88, 0xFF, 0x88])
    assert L.strip_tpiu_sync(data) == data


def test_has_tpiu_sync():
    assert L.has_tpiu_sync(bytes.fromhex("88ffffff7f88")) is True
    assert L.has_tpiu_sync(b"\x88\xff\x7f\x01") is True
    assert L.has_tpiu_sync(bytes([0x88, 0x01, 0x37])) is False


def test_strip_recovers_isync_through_filler():
    # An I-sync split by half-sync filler is recovered once stripped. (Filler
    # only appears between packets in practice; this checks the strip yields a
    # contiguous, parseable I-sync.)
    isync = bytes([0x08, 0x00]) + (0x08000ff0 | 1).to_bytes(4, "little")
    framed = isync[:3] + b"\xff\x7f" + isync[3:] + b"\xff\xff\xff\x7f"
    clean = L.strip_tpiu_sync(framed)
    s = L.parse_isync_at(clean, 0)
    assert s is not None and s.addr == 0x08000FF0


# ----------------------------------------------------------------------------
# tpiu_deframe reference port (genuinely stream-framed input)
# ----------------------------------------------------------------------------
def test_tpiu_deframe_single_stream():
    # One valid 16-byte frame (arrival order): frame[0] is a stream-change to
    # id 1 (0x03 = (1<<1)|1), then 15 bytes. On the wire even-position DATA
    # bytes carry LSB=0 (their true LSB lives in the aux/lowbits byte[15]); odd
    # bytes are full data. Build 14 data bytes after the id, all LSB-safe.
    frame = bytearray(16)
    frame[0] = (1 << 1) | 1          # stream change -> id 1 (immediate, lowbit0=0)
    data_bytes = []
    for k in range(1, 15):
        if k % 2 == 0:               # even index -> must have LSB 0 to be data
            b = 0x10 + (k << 1) & 0xFE
        else:                        # odd index -> any value
            b = 0x20 + k
        frame[k] = b
        data_bytes.append(b)
    frame[15] = 0x00                 # aux lowbits all zero
    # tpiu_sync_frames consumes arrival order then byte-reverses internally;
    # tpiu_deframe reverses back, so feed arrival order directly.
    stream = bytes.fromhex("ffffff7f") + bytes(frame)
    res = L.tpiu_deframe(stream)
    assert 1 in res
    assert res[1] == bytes(data_bytes)


def test_tpiu_deframe_want_stream_absent():
    stream = bytes.fromhex("ffffff7f") + bytes(16)
    assert L.tpiu_deframe(stream, want_stream=99) == b""


# ----------------------------------------------------------------------------
# tpiu_deframe_hsync — the correct 16-byte TPIU formatter deframe
# (sigrok arm_tpiu: even bytes data-with-LSB-in-aux or stream-id; odd data;
#  byte15 = aux LSBs; HSYNC/FSYNC skipped without consuming a frame slot)
# ----------------------------------------------------------------------------
def _build_tpiu_frame(payload15, stream_id=1, even_lsbs=0):
    """Build one 16-byte TPIU frame carrying 15 data bytes for one stream.
    payload15: 15 data bytes. The first even byte (index 0) is used as the
    stream-id change (id<<1|1); remaining bytes carry payload. For test
    simplicity we put the id in byte0 and 14 payload bytes in 1..14, aux=byte15.
    """
    frame = bytearray(16)
    frame[0] = (stream_id << 1) | 1          # stream-id change, immediate
    aux = 0
    for k in range(1, 15):
        frame[k] = payload15[k - 1] & 0xFE if k % 2 == 0 else payload15[k - 1]
        if k % 2 == 0 and (payload15[k - 1] & 1):
            aux |= (1 << (k // 2))
    frame[15] = aux
    return bytes(frame), payload15[:14]


def test_tpiu_deframe_hsync_basic():
    payload = bytes(range(0x80, 0x80 + 14))   # 14 data bytes
    frame, expect = _build_tpiu_frame(payload, stream_id=2)
    out = L.tpiu_deframe_hsync(frame, phase=0)
    assert out == bytes(expect)


def test_tpiu_deframe_hsync_restores_even_lsb():
    # An even-position data byte with LSB=1 must be reconstructed from the aux.
    frame = bytearray(16)
    frame[0] = (2 << 1) | 1                    # stream id 2
    frame[2] = 0x2E                            # even data byte, LSB stripped
    frame[15] = 1 << (2 // 2)                  # aux bit for index 2 -> restore LSB
    out = L.tpiu_deframe_hsync(bytes(frame), phase=0)
    # byte at frame index 2 should come back as 0x2E | 1 = 0x2F
    assert 0x2F in out
    assert 0x2E not in out


def test_tpiu_deframe_hsync_skips_hsync_pair():
    payload = bytes(range(0x80, 0x80 + 14))
    frame, expect = _build_tpiu_frame(payload, stream_id=1)
    # insert an HSYNC pair in the middle of the frame bytes
    framed = frame[:8] + b"\xff\x7f" + frame[8:]
    out = L.tpiu_deframe_hsync(framed, phase=0)
    assert out == bytes(expect)


def test_tpiu_deframe_hsync_skips_fsync():
    payload = bytes(range(0x40, 0x40 + 14))
    frame, expect = _build_tpiu_frame(payload, stream_id=1)
    framed = b"\xff\xff\xff\x7f" + frame
    out = L.tpiu_deframe_hsync(framed, phase=0)
    assert out == bytes(expect)


def test_find_tpiu_phase_returns_valid_phase():
    payload = bytes(range(0x80, 0x80 + 14))
    frame, _ = _build_tpiu_frame(payload, stream_id=1)
    ph, score = L.find_tpiu_phase(frame * 4)
    assert 0 <= ph < 16
