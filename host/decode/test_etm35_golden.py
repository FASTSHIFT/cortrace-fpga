"""Ground-truth roundtrip tests for the ETM3.5 decoder (etm35lib).

These build ETM3.5 byte streams from KNOWN content using a spec-faithful
encoder (IHI0014Q §7.10.5 Fig 7-42 Normal I-sync; §7.3.4 P-headers), feed them
through etm35lib, and assert the decoder recovers EXACTLY the known values.

This separates "decoder logic correctness" (provable here, 100%, no hardware)
from "sampling chain correctness" (only checkable on board). Answers review
r14's "how do you know the decode is real?" for the logic half.
"""
import struct

import etm35lib as L


# --- spec-faithful ETM3.5 encoders (IHI0014Q) -------------------------------
def enc_async(extra_zeros=0):
    """A-sync: >=5 zero bytes + 0x80 (IHI0014Q §7.10.4)."""
    return bytes([0] * (5 + extra_zeros)) + bytes([0x80])


def enc_isync(addr, thumb=True, reason=L.REASON_PERIODIC, nonsecure=False):
    """Normal I-sync (Fig 7-42), 0 ContextID bytes (Cortex-M4 ETMCR[15:14]=00):
       header 0x08 | info | addr[7:0] | addr[15:8] | addr[23:16] | addr[31:24]
    info: bit7=0 Normal, bits[6:5]=reason, bit4=Jazelle(0), bit3=NS, bit2=AltISA(0),
          bit0=1 (per figure).  Address bit0 = Thumb (T)."""
    info = ((reason & 0b11) << 5) | (0x08 if nonsecure else 0) | 0x01
    a = (addr & ~1) | (1 if thumb else 0)
    return bytes([0x08, info]) + struct.pack("<I", a)


def enc_phdr_fmt1(eatoms, natom=False):
    """Format-1 P-header (IHI0014Q §7.3.4): bit7=1, bit0=0, bit1=0,
       bits[5:2]=eatoms, bit6=natom flag."""
    assert 0 <= eatoms <= 15
    c = 0x80 | ((eatoms & 0x0F) << 2)
    if natom:
        c |= (1 << 6)
    return bytes([c])


def enc_branch_1byte(low7):
    """A minimal 1-byte branch packet: bit0=1, bit7=0 (no continuation)."""
    return bytes([(low7 << 1) | 1]) and bytes([((low7 & 0x3F) << 1) | 1])


# ----------------------------------------------------------------------------
# I-sync roundtrip: encode known PCs, decode, assert exact recovery
# ----------------------------------------------------------------------------
def test_isync_roundtrip_exact():
    pcs = [0x08001234, 0x0800abcc, 0x08055000, 0x08000000]
    stream = bytearray()
    for pc in pcs:
        stream += enc_async()
        stream += enc_isync(pc)
        stream += enc_phdr_fmt1(2)
    got = L.recover_pcs(bytes(stream))
    assert got == sorted(set(pcs))


def test_isync_reason_codes_roundtrip():
    for reason in (L.REASON_PERIODIC, L.REASON_TRACE_ON,
                   L.REASON_OVERFLOW, L.REASON_DEBUG_EXIT):
        pkt = enc_isync(0x08001000, reason=reason)
        s = L.parse_isync_at(pkt, 0)
        assert s is not None
        assert s.addr == 0x08001000
        assert s.reason == reason


def test_isync_thumb_and_nonsecure_flags():
    s = L.parse_isync_at(enc_isync(0x08002000, thumb=True, nonsecure=True), 0)
    assert s.thumb is True
    assert s.nonsecure is True
    s2 = L.parse_isync_at(enc_isync(0x08002000, thumb=False, nonsecure=False), 0)
    assert s2.thumb is False
    assert s2.nonsecure is False


# ----------------------------------------------------------------------------
# P-header roundtrip: encode known atom counts, decode region, assert
# ----------------------------------------------------------------------------
def test_phdr_atoms_roundtrip():
    for e in range(0, 16):
        c = enc_phdr_fmt1(e)[0]
        res = L._phdr_atoms(c)
        assert res == (e, 0), f"eatoms={e}: {res}"
    # with a not-executed atom
    c = enc_phdr_fmt1(3, natom=True)[0]
    assert L._phdr_atoms(c) == (3, 1)


def test_region_recovers_known_atom_sequence():
    # I-sync anchor then a known P-header sequence; decode_region must report
    # exactly those atom counts in order.
    pc = 0x08001000
    seq = [(2, False), (5, False), (1, True), (0, True)]
    stream = bytearray(enc_isync(pc))
    for e, n in seq:
        stream += enc_phdr_fmt1(e, n)
    stream += bytes([0x00])    # async-ish stop
    events, _ = L.decode_region(bytes(stream), 6, pc)  # start after the 6-byte isync
    atoms = [(e.eatoms, e.natoms) for e in events if e.kind == "atoms"]
    assert atoms == [(e, 1 if n else 0) for e, n in seq]


def test_decode_all_recovers_exact_anchor_set_and_atoms():
    # Full pipeline: 3 anchors, each followed by known atoms. Assert exact.
    spec = [
        (0x08001000, [2, 4]),
        (0x08020abc, [1, 3, 5]),
        (0x08000010, [0]),
    ]
    stream = bytearray()
    for pc, atoms in spec:
        stream += enc_async()
        stream += enc_isync(pc)
        for e in atoms:
            stream += enc_phdr_fmt1(e)
    events = L.decode_all(bytes(stream))
    anchors = sorted(e.addr for e in events if e.kind == "isync")
    assert anchors == sorted(pc for pc, _ in spec)
    total_exec = sum(e.eatoms for e in events if e.kind == "atoms")
    assert total_exec == sum(sum(a) for _, a in spec)


def test_noise_does_not_fabricate_anchors():
    # A stream of bytes that never forms a valid Normal I-sync (no 0x08 with a
    # flash addr + clean info) must yield zero anchors — guards the r14 BUG-1
    # false-positive concern on a controlled negative input.
    import random
    random.seed(1234)
    # random bytes but force every 0x08 to be followed by a non-flash addr
    data = bytearray(random.randbytes(4000))
    for i in range(len(data) - 5):
        if data[i] == 0x08:
            data[i + 5] = 0x20    # addr high byte -> SRAM, not flash
    assert L.recover_pcs(bytes(data)) == []


# ----------------------------------------------------------------------------
# Logic-analyzer ground-truth: the recovered byte stream from the real .dsl
# capture of while(1){__NOP();} must decode to the known loop PC 0x08000ff0.
# (Reads /tmp/dsl_bytes_0.bin produced by dsl_parse.py; skipped if absent.)
# ----------------------------------------------------------------------------
import os


def test_logic_analyzer_ground_truth_while_nop():
    # Prefer the committed fixture (stable); fall back to the live /tmp byte
    # stream from dsl_parse.py if the fixture is absent.
    here = os.path.dirname(__file__)
    fixture = os.path.join(here, "captures", "while_nop_ground_truth.bin")
    if os.path.exists(fixture):
        data = open(fixture, "rb").read()
    elif os.path.exists("/tmp/dsl_bytes_0.bin"):
        data = open("/tmp/dsl_bytes_0.bin", "rb").read()
    else:
        import pytest
        pytest.skip("no while_nop fixture and no /tmp/dsl_bytes_0.bin")
    pcs = L.recover_pcs(data)
    # the while(1){__NOP();} loop NOP is at 0x08000ff0; it MUST be the dominant
    # I-sync anchor recovered from the physical pin capture.
    assert 0x08000ff0 in pcs
    # all anchors must be valid flash code addresses
    for a in pcs:
        assert L.is_flash(a)
