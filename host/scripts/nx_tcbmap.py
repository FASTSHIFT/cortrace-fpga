#!/usr/bin/env python3
"""nx_tcbmap.py -- dump the NuttX pid/TCB -> thread-name map from a running
target, for cortrace's nxtrace overlay (doc 01 §4.3, path 2).

Heap-allocated TCBs are not in the ELF image, so their pid/entry fields cannot
be read statically. This helper reads the live g_pidhash table over SWD (a
one-shot read of SRAM, AFTER the trace capture, so it never perturbs the DWT
stream), and for each live TCB resolves entry -> function name via the ELF
symbol table. Only entries currently IN g_pidhash are read, so there is no
dangling-pointer/UAF window.

Output: a text map "0xTCB<TAB>pid<TAB>name" (one per line) that
cortrace-decode consumes with --nx-tcbmap.

Usage:
  nx_tcbmap.py --elf nuttx --out tcbmap.txt
    [--pid-off 0x30] [--entry-off 0x3c] [--nm arm-none-eabi-nm]
"""

import argparse
import bisect
import re
import subprocess
import sys

OOCD_IFACE = "interface/cmsis-dap.cfg"
OOCD_TARGET = "target/stm32h7x.cfg"


def sym(nm, elf, name):
    out = subprocess.check_output([nm, elf], stderr=subprocess.DEVNULL).decode()
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 3 and p[2] == name:
            return int(p[0], 16)
    return None


def build_symtab(nm, elf):
    out = subprocess.check_output([nm, "-n", elf], stderr=subprocess.DEVNULL).decode()
    addrs, names = [], []
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] in "tTwW":
            addrs.append(int(p[0], 16))
            names.append(p[2])
    return addrs, names


def fn_for(addrs, names, addr):
    i = bisect.bisect_right(addrs, addr) - 1
    return names[i] if i >= 0 else "?"


def oocd_read_words(addr, count):
    """Read `count` 32-bit words at `addr` via a one-shot openocd session."""
    cmd = [
        "openocd",
        "-f",
        OOCD_IFACE,
        "-f",
        OOCD_TARGET,
        "-c",
        "gdb_port disabled",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
        "-c",
        "init",
        "-c",
        "halt",
        "-c",
        f"mdw 0x{addr:08x} {count}",
        "-c",
        "resume",
        "-c",
        "shutdown",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    words = {}
    for line in (out.stdout + out.stderr).splitlines():
        m = re.match(r"\s*0x([0-9a-fA-F]+):\s+(.+)", line)
        if m:
            base = int(m.group(1), 16)
            for j, tok in enumerate(m.group(2).split()):
                try:
                    words[base + 4 * j] = int(tok, 16)
                except ValueError:
                    pass
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--out", default="tcbmap.txt")
    ap.add_argument("--pid-off", default="0x30")
    ap.add_argument("--entry-off", default="0x3c")
    ap.add_argument("--nm", default="arm-none-eabi-nm")
    a = ap.parse_args()
    pid_off = int(a.pid_off, 0)
    entry_off = int(a.entry_off, 0)

    g_pidhash = sym(a.nm, a.elf, "g_pidhash")
    g_npidhash = sym(a.nm, a.elf, "g_npidhash")
    if g_pidhash is None or g_npidhash is None:
        sys.exit("g_pidhash/g_npidhash not found in ELF symbols")

    addrs, names = build_symtab(a.nm, a.elf)

    # Read the g_pidhash pointer and the count.
    hdr = oocd_read_words(g_pidhash, 1)
    tab = hdr.get(g_pidhash)
    cnt = oocd_read_words(g_npidhash, 1).get(g_npidhash, 0)
    if not tab or not cnt or cnt > 4096:
        sys.exit(f"bad g_pidhash=0x{tab or 0:x} g_npidhash={cnt}")

    # Read the array of `cnt` TCB pointers.
    ptrs = oocd_read_words(tab, cnt)
    entries = []
    for i in range(cnt):
        tcb = ptrs.get(tab + 4 * i, 0)
        if 0x20000000 <= tcb < 0x40000000:  # plausible SRAM TCB
            entries.append(tcb)

    # For each live TCB read pid + entry in one batched read per TCB.
    lines = []
    for tcb in entries:
        w = oocd_read_words(tcb, 20)  # covers up to offset 0x4c
        pid = w.get(tcb + pid_off, 0)
        entry = w.get(tcb + entry_off, 0)
        name = fn_for(addrs, names, entry) if entry else "?"
        lines.append(f"0x{tcb:08x}\t{pid}\t{name}")

    with open(a.out, "w") as f:
        f.write("# tcb\tpid\tname  (from live g_pidhash, doc §4.3 path 2)\n")
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} TCB entries -> {a.out}")
    for ln in lines:
        print("  " + ln)


if __name__ == "__main__":
    main()
