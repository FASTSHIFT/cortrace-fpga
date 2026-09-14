#!/usr/bin/env python3
"""prbs_raw_grab — capture raw UDP trace packets (KEEP the 4-byte BE seq) and
dump (seq, payload) records, so we can inspect packet ordering + per-packet
PRBS contiguity independent of stream_grab's seq-stripping concatenation.

Usage: sudo python3 prbs_raw_grab.py <iface> <seconds> <out.npz-ish txt>
Writes a simple binary: repeated [4B seq][1024B payload].
"""
import socket
import sys
import time

iface = sys.argv[1]
secs = float(sys.argv[2])
out = sys.argv[3]

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024 * 1024)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, (iface + "\0").encode())
except PermissionError:
    pass
s.bind(("192.168.10.245", 5555))
s.settimeout(2.0)

f = open(out, "wb")
t0 = time.time()
n = 0
while time.time() - t0 < secs:
    try:
        d, _ = s.recvfrom(4096)
    except socket.timeout:
        break
    f.write(d)  # keep seq + payload as-is
    n += 1
f.close()
s.close()
print(f"captured {n} packets -> {out}")
