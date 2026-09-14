#!/usr/bin/env python3
"""stream_recv — receive the continuous UDP trace stream from trace_stream_top
(STREAM=1) and dump it to a file, checking the packet sequence numbers.

Wire format matches the packetiser in trace_stream_top.v:
  UDP payload = <32-bit BE seq><PAYLOAD trace bytes>   per packet
so a lost UDP frame shows up as a gap in seq. The FPGA also exposes a
capture-side drop count at status offsets DEPTH+34..37 (little endian), which is
the *other* way to lose data (clk200 -> clk125 async FIFO overrun); we poll it
before and after so the two numbers can be reconciled.

Usage:
  python3 stream_recv.py [--port 5555] [--out /tmp/stream.bin] [--seconds 3]
"""
import argparse
import socket
import struct
import sys
import time

try:
    import fpga_net
except ImportError:
    fpga_net = None


def read_lost_cnt(ip, depth, port=5001, retries=8, timeout=1.0, iface=None):
    """Read the four DEPTH+34..37 status bytes via the request/reply :5001 path.

    Retries because while STREAM is saturating the TX path (self_busy always
    high in fpga_core_net's FSM), a :5001 echo request must wait for a gap
    between UDP packets on our side. On a busy stream the gap is small, and the
    first few polls typically time out.

    iface, if given, pins the request socket to that interface (SO_BINDTODEVICE)
    so the poll egresses the wire the FPGA is actually on -- needed when a second
    NIC shares the 192.168.10.0/24 subnet (dock direct-attach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if iface:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                         (iface + "\0").encode())
        except PermissionError:
            pass                          # non-root: fall back to kernel routing
    s.settimeout(timeout)
    payload = struct.pack("<H", depth + 34) + bytes(8)
    for _ in range(retries):
        try:
            s.sendto(payload, (ip, port))
            d, _ = s.recvfrom(2048)
            s.close()
            return d[2] | (d[3] << 8) | (d[4] << 16) | (d[5] << 24)
        except socket.timeout:
            continue
    s.close()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5555,
                    help="UDP port the FPGA streams to (STREAM_DEST_PORT)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--out", default="/tmp/stream.bin")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--ip", default=None,
                    help="FPGA IP for polling status (default: auto-discover, "
                         "fallback 192.168.10.42)")
    ap.add_argument("--iface", default=None,
                    help="bind status poll to this interface "
                         "(default: auto-discover the wired NIC)")
    ap.add_argument("--no-discover", action="store_true",
                    help="skip ARP discovery, use --ip / kernel routing as-is")
    ap.add_argument("--depth", type=int, default=61440,
                    help="DEPTH parameter (for lost_cnt readback offset)")
    a = ap.parse_args()

    # Locate the FPGA: whichever NIC's ARP probe answers is the wired one. This
    # makes both topologies work with no flags -- router (single NIC, IP routing
    # reaches it) and dock direct-attach (second NIC on the same subnet, where
    # the status poll would otherwise egress the wrong port).
    ip = a.ip
    iface = a.iface
    if not a.no_discover and fpga_net is not None and (ip is None or iface is None):
        try:
            info = fpga_net.discover_fpga(ip=a.ip or fpga_net.DEFAULT_FPGA_IP)
        except PermissionError:
            info = None
            print("[stream_recv] note: run under sudo to auto-bind the status "
                  "poll to the direct-attach NIC", file=sys.stderr)
        if info:
            ip = ip or info["ip"]
            iface = iface or info["iface"]
            print(f"[stream_recv] FPGA on {info['iface']} at {info['ip']}")
    if ip is None:
        ip = fpga_net.DEFAULT_FPGA_IP if fpga_net else "192.168.10.42"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # 64 MB kernel receive buffer: at 100 MB/s peak, 8 MB tolerates only 80 ms
    # of userland scheduling latency before packets get dropped by the kernel
    # (measured on this box: 8 MB -> 19012 seq-gaps on a 2s capture, 64 MB -> 0).
    # NB: kernel silently caps this at net.core.rmem_max; bump it via
    #   sudo sysctl -w net.core.rmem_max=67108864
    # if seq-gaps persist.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
    actual = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if actual < 32 * 1024 * 1024:
        print(f"[stream_recv] warning: kernel capped SO_RCVBUF to {actual/1e6:.1f} MB "
              f"(net.core.rmem_max is low; expect seq-gaps)", file=sys.stderr)
    s.bind((a.bind, a.port))
    s.settimeout(0.5)

    # NB: do NOT poll the FPGA status port here. On a saturated self-TX stream
    # the :5001 request/reply can't get a gap for up to retries*timeout seconds,
    # during which the socket is bound but nobody calls recv() -> the kernel
    # buffer overflows and the WHOLE capture is lost to seq-gaps (measured: a 3s
    # window ballooned to 11.6s with 711578 gaps). Start receiving IMMEDIATELY;
    # the capture-side lost_cnt is optional diagnostics we read AFTER the stream
    # stops (when the TX path is idle and the poll succeeds).
    lost_before = None

    trace = bytearray()
    seq_prev = None
    gaps = 0
    npkt = 0
    nbytes = 0
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        try:
            pkt, _ = s.recvfrom(2048)
        except socket.timeout:
            continue
        if len(pkt) < 4:
            continue
        seq = struct.unpack(">I", pkt[:4])[0]
        payload = pkt[4:]
        # NB: fpga_core_net promises exactly STREAM_PKT_BYTES; if underrun, the
        # FSM pads with 0x00 to complete the packet. The seq check catches lost
        # UDP frames on the wire (the capture-drop counter catches capture-side
        # loss); combined they cover both failure modes.
        if seq_prev is not None and seq != (seq_prev + 1) & 0xFFFFFFFF:
            missing = (seq - seq_prev - 1) & 0xFFFFFFFF
            gaps += missing
        seq_prev = seq
        trace.extend(payload)
        npkt += 1
        nbytes += len(payload)
    s.close()
    elapsed = time.time() - t0        # measure BEFORE the (slow) status poll

    lost_after = read_lost_cnt(ip, a.depth, iface=iface)
    lost_delta = None
    if lost_before is not None and lost_after is not None:
        lost_delta = (lost_after - lost_before) & 0xFFFFFFFF

    open(a.out, "wb").write(trace)
    print(f"packets={npkt}  bytes={nbytes} ({nbytes/1e6:.2f} MB) "
          f"in {elapsed:.2f}s  -> {nbytes/elapsed/1e6:.2f} MB/s")
    print(f"seq-gap lost frames: {gaps}")
    if lost_delta is not None:
        print(f"capture-side lost bytes (clk200 FIFO overrun): {lost_delta}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
