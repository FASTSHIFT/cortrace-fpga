"""fpga_timebase — turn the FPGA capture-time sidecar (<dump>.ts.json, written
by trace_dump.py --timebase) into a wall-clock time for any captured byte.

Background (doc 15 §22/§24): the STM32F429 ETM has no usable wall-clock — no
CoreSight TSGEN, no cycle-accurate cycle counts — so the trace stream carries no
time. Instead the FPGA snapshots a free-running 200 MHz counter (5 ns/tick) into
a small table, once every `stride` captured RAW bytes. This module interpolates
that table to give the wall-clock ns of any RAW capture-byte index.

Why a sparse table (not just first/last)? TRACECLK can idle mid-capture (the
target stops emitting -> the long all-zero regions). A first/last linear model
would smear time across such a pause. Per-stride snapshots record the REAL time
where each block of bytes was captured, so a pause shows up as a genuine time
gap exactly where it happened. It is also frequency-agnostic: we time the bytes
as they arrive, we never assume a byte rate.

Key terms:
  RAW index   : position in the FPGA capture buffer (what the table is keyed on)
  out index   : position in the file written by trace_dump (= RAW index - skip)
"""
from __future__ import annotations
import json
from bisect import bisect_right


class TimeBase:
    def __init__(self, stride, n, tick_ns, last_tick, ticks, skip=0, depth=None):
        self.stride = stride
        self.n = n
        self.tick_ns = tick_ns
        self.last_tick = last_tick
        self.ticks = list(ticks)
        self.skip = skip
        self.depth = depth
        self._unwrap()

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path):
        with open(path) as f:
            d = json.load(f)
        return cls(stride=d["stride"], n=d["n"], tick_ns=d["tick_ns"],
                   last_tick=d["last_tick"], ticks=d["ticks"],
                   skip=d.get("skip", 0), depth=d.get("depth"))

    # ------------------------------------------------------------------
    def _unwrap(self):
        """The hardware counter is 32-bit and free-running; over a long capture
        it can wrap (2^32 ticks * 5 ns ~ 21.5 s). Unwrap the snapshot list (and
        the tail tick) into a strictly non-decreasing 64-bit tick sequence so
        interpolation is monotonic."""
        WRAP = 1 << 32
        acc = 0
        prev = 0
        unwrapped = []
        for t in self.ticks:
            if t < prev:
                acc += WRAP
            unwrapped.append(t + acc)
            prev = t
        self._uticks = unwrapped
        # extend the tail (last captured byte) consistently
        lt = self.last_tick
        if unwrapped:
            base = unwrapped[-1] - (self.ticks[-1] if self.ticks else 0)
            if lt < (self.ticks[-1] if self.ticks else 0):
                base += WRAP
            self._ulast = lt + base
        else:
            self._ulast = lt
        # captured-byte index of each snapshot: snapshot k is taken at the
        # moment RAW byte (k*stride) is accepted.
        self._idx = [k * self.stride for k in range(len(unwrapped))]

    # ------------------------------------------------------------------
    def ns_for_raw(self, raw_index: int) -> float:
        """Wall-clock ns (relative to capture start) for a RAW capture-byte
        index, by linear interpolation between adjacent snapshots. Clamped to
        the snapshot range; the region after the last snapshot interpolates to
        the last-captured-byte tick."""
        if not self._uticks:
            return 0.0
        idx = self._idx
        ut = self._uticks
        if raw_index <= idx[0]:
            tick = ut[0]
        elif raw_index >= idx[-1]:
            # interpolate from last snapshot to the final captured byte
            last_raw = (self.depth - 1) if self.depth else idx[-1]
            if last_raw > idx[-1] and self._ulast > ut[-1]:
                frac = (raw_index - idx[-1]) / (last_raw - idx[-1])
                frac = min(1.0, max(0.0, frac))
                tick = ut[-1] + frac * (self._ulast - ut[-1])
            else:
                tick = ut[-1]
        else:
            k = bisect_right(idx, raw_index) - 1
            t0, t1 = ut[k], ut[k + 1]
            i0, i1 = idx[k], idx[k + 1]
            frac = (raw_index - i0) / (i1 - i0) if i1 > i0 else 0.0
            tick = t0 + frac * (t1 - t0)
        return (tick - ut[0]) * self.tick_ns

    def ns_for_out(self, out_index: int) -> float:
        """Wall-clock ns for an index into the trace_dump output file (which had
        the first `skip` RAW bytes dropped)."""
        return self.ns_for_raw(out_index + self.skip)

    # ------------------------------------------------------------------
    def span_ns(self) -> float:
        if not self._uticks:
            return 0.0
        return (self._ulast - self._uticks[0]) * self.tick_ns
