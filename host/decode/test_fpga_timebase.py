"""Unit tests for fpga_timebase.TimeBase (doc 15 §24.2).

These verify the byte-index -> wall-clock mapping against synthetic snapshot
tables that model the three cases that matter:
  * steady TRACECLK (uniform tick spacing -> linear time),
  * a mid-capture TRACECLK idle pause (a flat run in the table -> a time gap
    that must NOT smear into neighbouring bytes),
  * a 32-bit counter wrap (long capture).
No hardware needed.
"""
import json
import os
import tempfile
import pytest

from fpga_timebase import TimeBase


def make(stride, ticks, last_tick=None, skip=0, depth=None, tick_ns=5.0):
    n = len(ticks)
    if last_tick is None:
        last_tick = ticks[-1]
    if depth is None:
        depth = (n - 1) * stride + 1
    return TimeBase(stride=stride, n=n, tick_ns=tick_ns, last_tick=last_tick,
                    ticks=ticks, skip=skip, depth=depth)


def test_steady_linear():
    # 100 ticks between every 256 bytes -> 100*5ns per 256 bytes
    stride = 256
    ticks = [k * 100 for k in range(5)]      # 0,100,200,300,400
    tb = make(stride, ticks)
    assert tb.ns_for_raw(0) == 0.0
    assert tb.ns_for_raw(256) == pytest.approx(100 * 5.0)
    # midway between snapshot 0 and 1
    assert tb.ns_for_raw(128) == pytest.approx(50 * 5.0)
    # at snapshot 4
    assert tb.ns_for_raw(4 * 256) == pytest.approx(400 * 5.0)


def test_monotonic_nondecreasing():
    stride = 256
    ticks = [0, 90, 250, 260, 1000]
    tb = make(stride, ticks)
    prev = -1.0
    for b in range(0, 4 * 256, 7):
        ns = tb.ns_for_raw(b)
        assert ns >= prev - 1e-9
        prev = ns


def test_idle_pause_shows_as_gap_not_smear():
    # TRACECLK idles between snapshot 1 and 2: the COUNTER keeps ticking, so the
    # table jumps a lot there. The pause must stay LOCALIZED to its own stride
    # block — it must not smear into the rate of neighbouring blocks (which a
    # naive first/last linear model would do).
    stride = 256
    # block 0->1 quick (100 ticks), 1->2 huge pause (10000 ticks), 2->3 quick
    ticks = [0, 100, 10100, 10200]
    tb = make(stride, ticks)
    # snapshot boundaries are exact regardless of the pause
    assert tb.ns_for_raw(1 * 256) == pytest.approx(100 * 5.0)
    assert tb.ns_for_raw(2 * 256) == pytest.approx(10100 * 5.0)
    # the huge jump is contained in block 1->2 only: blocks 0->1 and 2->3 keep
    # their own (slow) rate, ~100 ticks across 256 bytes each.
    rate_01 = tb.ns_for_raw(256) - tb.ns_for_raw(0)
    rate_23 = tb.ns_for_raw(3 * 256) - tb.ns_for_raw(2 * 256)
    assert rate_01 == pytest.approx(100 * 5.0)
    assert rate_23 == pytest.approx(100 * 5.0)
    # while block 1->2 carries essentially the entire elapsed time (the pause)
    rate_12 = tb.ns_for_raw(2 * 256) - tb.ns_for_raw(256)
    assert rate_12 == pytest.approx(10000 * 5.0)


def test_counter_wrap_unwrapped():
    stride = 256
    # wrap near 2^32: 4.2e9 -> small value
    WRAP = 1 << 32
    ticks = [WRAP - 300, WRAP - 200, WRAP - 100, 0 + 50, 0 + 150]
    # raw ticks wrap; unwrapped should be strictly increasing
    tb = make(stride, ticks, last_tick=150)
    seq = [tb.ns_for_raw(k * 256) for k in range(5)]
    assert seq == sorted(seq)
    # total span = (last_unwrapped - first) * 5ns
    # first = WRAP-300, last snapshot unwrapped = WRAP+150 -> 450 ticks
    assert tb.ns_for_raw(4 * 256) == pytest.approx(450 * 5.0)


def test_tail_interpolates_to_last_tick():
    stride = 256
    ticks = [0, 100]
    # depth says 1024 bytes captured; last byte at tick 400
    tb = make(stride, ticks, last_tick=400, depth=1024)
    # after the last snapshot (byte 256) time keeps growing toward last_tick
    t_end = tb.ns_for_raw(1023)
    assert t_end > tb.ns_for_raw(256)
    assert t_end == pytest.approx(400 * 5.0, rel=0.01)


def test_skip_offset():
    stride = 256
    ticks = [0, 100, 200]
    tb = make(stride, ticks, skip=128)
    # out index 0 == raw index 128 -> halfway in first block
    assert tb.ns_for_out(0) == pytest.approx(50 * 5.0)


def test_load_roundtrip():
    stride = 256
    ticks = [0, 100, 200, 300]
    d = {"stride": stride, "n": len(ticks), "tick_ns": 5.0,
         "last_tick": 300, "ticks": ticks, "skip": 0, "depth": 769}
    with tempfile.NamedTemporaryFile("w", suffix=".ts.json", delete=False) as f:
        json.dump(d, f)
        path = f.name
    try:
        tb = TimeBase.load(path)
        assert tb.ns_for_raw(256) == pytest.approx(100 * 5.0)
        assert tb.span_ns() == pytest.approx(300 * 5.0)
    finally:
        os.unlink(path)


def test_empty_table_safe():
    tb = TimeBase(stride=256, n=0, tick_ns=5.0, last_tick=0, ticks=[])
    assert tb.ns_for_raw(0) == 0.0
    assert tb.span_ns() == 0.0
