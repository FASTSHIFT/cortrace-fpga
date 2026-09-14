#!/usr/bin/env python3
"""Parse a DSView .dsl logic capture of the STM32 4-bit ETM trace and recover
the byte stream, for ground-truth cross-check against the FPGA/decoder.

.dsl layout (zip): header (ini), per-channel bit-packed sample blocks named
L-<ch>/<blk>; each byte holds 8 consecutive samples, LSB = earliest sample.
Channels here: 0=TRACECLK 1=TRACED0 2=TRACED1 3=TRACED2 4=TRACED3.

DDR sampling (one nibble per TRACECLK edge) has TWO degrees of freedom that we
learned the hard way must NOT be hardcoded:

  1. Nibble ordering: which of {rise,fall} carries the low vs high nibble.
     Ground truth (doc 14) is rise=low,fall=high — but we re-verify per file.
  2. Edge-PARITY offset: whether byte boundaries start on the first captured
     edge or the second. A capture that begins mid-DDR-pair is shifted by one
     edge, which silently corrupts every byte (this produced a bogus
     "TPIU formatter / 0xFFFFFF7F" stream on 172817.dsl until fixed).

So instead of trusting fixed settings, we try all 4 combinations
(parity 0/1 × order low/high), sample at MID-EYE (a few samples after the
edge, where the data is stable), score each by how many valid flash-range
Normal I-sync anchors it yields (etm35lib), and emit the winner.
"""
import sys
import zipfile
import collections
import configparser

import etm35lib as L

# Sample this many samples after each clock edge (mid-eye). At 50 MSa/s with a
# ~1.3 MHz DDR signal the half-bit is ~19 samples, so ~9 lands mid-eye. Scaled
# down automatically if the half-period is shorter.
DEFAULT_EYE_FRACTION = 0.5


def load_channels(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    hdr = z.read("header").decode("utf-8", "replace")
    cp = configparser.ConfigParser()
    cp.read_string(hdr)
    nprobes = int(cp["header"]["total probes"])
    srate = cp["header"]["samplerate"]
    chans = {}
    for ch in range(nprobes):
        blocks = sorted([n for n in names if n.startswith(f"L-{ch}/")],
                        key=lambda s: int(s.split("/")[1]))
        raw = b"".join(z.read(b) for b in blocks)
        chans[ch] = raw
    return chans, srate, nprobes


def unpack_bits(raw, nsamples):
    out = bytearray(nsamples)
    for i in range(nsamples):
        out[i] = (raw[i >> 3] >> (i & 7)) & 1
    return out


def find_edges(clk, nsamp):
    """Return the ordered list of TRACECLK edge sample-indices (both rising and
    falling) and the median half-period in samples."""
    edges = []
    for i in range(1, nsamp):
        if clk[i] != clk[i - 1]:
            edges.append(i)
    if len(edges) > 2:
        intervals = sorted(edges[k + 1] - edges[k]
                           for k in range(len(edges) - 1))
        half = intervals[len(intervals) // 2]
    else:
        half = 1
    return edges, half


def sample_nibbles(d, edges, eye):
    """Sample the 4 data lines `eye` samples after each edge -> one nibble per
    edge (DDR)."""
    n = len(d[0])
    out = bytearray(len(edges))
    for k, e in enumerate(edges):
        s = e + eye
        if s >= n:
            s = n - 1
        out[k] = (d[0][s] | (d[1][s] << 1) | (d[2][s] << 2) | (d[3][s] << 3))
    return out


def assemble(nibs, parity, order):
    """Assemble bytes from the nibble stream.
      parity: 0 -> pair edges (0,1)(2,3)...; 1 -> drop the first edge then pair.
      order:  0 -> first-of-pair = low nibble; 1 -> first-of-pair = high nibble.
    """
    seq = nibs[parity:]
    b = bytearray()
    for k in range(0, len(seq) - 1, 2):
        n0, n1 = seq[k], seq[k + 1]
        if order == 0:
            b.append((n1 << 4) | n0)
        else:
            b.append((n0 << 4) | n1)
    return bytes(b)


def score(data):
    """Score a candidate byte stream by the number of Normal I-sync packets
    that carry a valid flash code address — the strongest 'this alignment is
    correct' signal we have (etm35lib hardened anchor test)."""
    syncs = L.find_isyncs(data)
    flash = [s for s in syncs if L.is_flash(s.addr)]
    return len(flash), len(syncs), syncs


def main():
    path = sys.argv[1]
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 50_000_000
    chans, srate, nprobes = load_channels(path)
    nsamp = min(min(len(v) for v in chans.values()) * 8, cap)
    print(f"samplerate={srate} probes={nprobes} samples(used)={nsamp}")

    clk = unpack_bits(chans[0], nsamp)
    d = [unpack_bits(chans[ch], nsamp) for ch in range(1, 5)]

    edges, half = find_edges(clk, nsamp)
    eye = max(1, int(half * DEFAULT_EYE_FRACTION))
    print(f"TRACECLK edges={len(edges)}  median half-period={half} samp "
          f"({half*20} ns, ~{50e6/(2*half)/1e3:.0f} kHz)  mid-eye sample @+{eye}")

    nibs = sample_nibbles(d, edges, eye)

    # Try all 4 alignments; pick the one with the most flash I-sync anchors.
    best = None
    print("\nalignment search (parity × nibble-order):")
    for parity in (0, 1):
        for order in (0, 1):
            data = assemble(nibs, parity, order)
            flash, total, _ = score(data)
            lbl = f"parity={parity} order={'low' if order == 0 else 'high'}"
            print(f"  {lbl:28s} bytes={len(data)} "
                  f"I-sync={total} flash-anchors={flash}")
            if best is None or flash > best[0]:
                best = (flash, parity, order, data)

    flash, parity, order, data = best
    olbl = "rise=low,fall=high" if order == 0 else "rise=high,fall=low"
    print(f"\n==> chosen: parity={parity} {olbl}  "
          f"({flash} flash I-sync anchors)")
    if flash == 0:
        print("!! WARNING: no valid flash I-sync anchors in ANY alignment. "
              "Check wiring / capture / that ETM is actually emitting.")

    # If the TPIU formatter is active (HSYNC/FSYNC fillers present), deframe the
    # 16-byte CoreSight frames properly: skip sync fillers without consuming a
    # frame slot AND restore the even-byte LSBs from the aux byte. (The old
    # "just delete FF 7F" left the frame structure mangled -> ~16% of bytes fell
    # into the data-packet encoding space and decode derailed. Proper deframing
    # drops that to ~0.002%.) Without an FSYNC to lock phase, scan all 16 frame
    # start phases and pick the one with the most flash I-sync anchors.
    if L.has_tpiu_sync(data):
        phase, ph_flash = L.find_tpiu_phase(data)
        deframed = L.tpiu_deframe_hsync(data, phase)
        fl2, tot2, _ = score(deframed)
        print(f"TPIU formatter detected: deframed 16-byte frames "
              f"(phase={phase}); {len(data)} -> {len(deframed)} bytes; "
              f"flash anchors {flash} -> {fl2}")
        data = deframed

    # Report the recovered anchors.
    _, _, syncs = score(data)
    hist = collections.Counter(s.addr for s in syncs if L.is_flash(s.addr))
    print(f"A-sync={len(L.find_asyncs(data))}  "
          f"flash I-sync anchors={sum(hist.values())}")
    print("top anchor PCs:")
    for a, n in hist.most_common(10):
        print(f"   0x{a:08x} : {n}")

    out = "/tmp/dsl_bytes_0.bin"
    with open(out, "wb") as f:
        f.write(data)
    print(f"\nwrote {out} ({len(data)} bytes, chosen alignment)")


if __name__ == "__main__":
    main()
