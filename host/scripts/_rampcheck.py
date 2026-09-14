#!/usr/bin/env python3
"""Byte-level integrity check for the FPGA ramp stress capture: every byte must
be (prev+1) mod 256. Zero breaks => hardware delivered the predictable stream
with zero byte errors."""
import sys

import numpy as np

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ramp_rx.bin"
d = np.fromfile(path, dtype=np.uint8)
print(f"payload bytes: {d.size}")
if d.size < 2:
    sys.exit("no payload")
diff = (d[1:].astype(np.int16) - d[:-1].astype(np.int16)) & 0xFF
breaks = int(np.count_nonzero(diff != 1))
print(f"ramp breaks (byte != prev+1 mod 256): {breaks} / {d.size}")
if breaks == 0:
    print("BYTE-PERFECT: monotone mod-256 across entire payload, ZERO byte errors")
else:
    idx = int(np.argmax(diff != 1))
    print(f"first break at byte {idx}: {d[max(0,idx-3):idx+4].tolist()}")
    print(f"break rate {100*breaks/d.size:.5f}%")
