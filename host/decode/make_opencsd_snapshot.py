#!/usr/bin/env python3
"""make_opencsd_snapshot — wrap our captured bare-ETM stream + ELF into an
OpenCSD "snapshot" directory so the ARM/Linaro reference decoder
(trc_pkt_lister) can decode it.

This is PURE GLUE — no trace-decode logic. It writes the 3 ini files the
OpenCSD snapshot format requires (spec: ARM-ECM-0611873, shipped in
/usr/share/doc/libopencsd-dev/specs/) and drops in the trace buffer + a memory
image (the ELF's executable sections) so the decoder can follow program flow.

Two protocols supported (select via --protocol):

  ETMv3.5 (default, STM32F429/Cortex-M4) — spec §4.2.1:
    ETMCR, ETMCCER, ETMIDR, ETMTRACEIDR

  ETMv4   (STM32H743/Cortex-M7) — spec §4.2.3:
    TRCIDR0, TRCIDR1, TRCIDR2, TRCIDR8..13, TRCTRACEIDR, TRCCONFIGR,
    TRCAUTHSTATUS

Live register values can be overridden via environment variables. Cortex-M7
default TRCIDR* values here are taken from the OpenCSD spec's ETMv4 example
(§3.2.5) which matches the published Cortex-M7 ETM implementation IDs.

Usage:
    python3 make_opencsd_snapshot.py <bare-etm.bin> <elf> <out_dir> \
        [--protocol etm4|etm35]           # default: etm35
        [--coresight]                      # input still has TPIU framing

Then:
    trc_pkt_lister -ss_dir <out_dir> -decode -logstdout
    (+ -tpiu if --coresight was used)
"""
import argparse
import os
import re
import subprocess
import sys

READELF = os.environ.get("READELF", "arm-none-eabi-readelf")
OBJCOPY = os.environ.get("OBJCOPY", "arm-none-eabi-objcopy")


def _lowest_load_lma(elf):
    """Return the lowest LMA among the sections objcopy -O binary emits, so the
    decoder's memory base matches mem.bin's true first byte. objcopy starts the
    flat binary at the lowest included section's LMA; hardcoding 0x08000000 when
    .isr_vector is absent skews every fetch. Parse `objdump -h` LMA column and
    take the min over allocatable, non-zero-LMA code/data sections."""
    try:
        out = subprocess.check_output(
            [OBJCOPY.replace("objcopy", "objdump"), "-h", elf]).decode()
    except Exception:
        return 0x08000000
    lmas = []
    for line in out.splitlines():
        # columns: Idx Name Size VMA LMA FileOff Algn
        m = re.match(r"\s*\d+\s+(\S+)\s+([0-9a-f]{8})\s+([0-9a-f]{8})\s+"
                     r"([0-9a-f]{8})", line)
        if m:
            name, size, vma, lma = m.group(1), int(m.group(2), 16), \
                int(m.group(3), 16), int(m.group(4), 16)
            if name in (".isr_vector", ".text", ".rodata", ".ARM.exidx",
                        ".init_array", ".fini_array", "ER_IROM1") \
                    and size > 0 and lma != 0:
                lmas.append(lma)
    return min(lmas) if lmas else 0x08000000

# ---- ETMv3.5 (Cortex-M4) live register defaults -----------------------------
# Live values for the STM32F429 Cortex-M4 ETM (override via env).
ETMCR       = int(os.environ.get("ETMCR",       "0x00000980"), 0)
ETMCCER     = int(os.environ.get("ETMCCER",     "0x18541800"), 0)
ETMIDR      = int(os.environ.get("ETMIDR",      "0x4114f250"), 0)
ETMTRACEIDR = int(os.environ.get("ETMTRACEIDR", "0x00000002"), 0)

# ---- ETMv4 (Cortex-M7) live register defaults -------------------------------
# Live values read from STM32H743 (Cortex-M7 r1p1) via OpenOCD (mdw with
# TRCLAR unlock). Read from THIS board on 2026-07-12. Override via env if
# using a different M7 SoC.
#
# NOTE on TRCCONFIGR: our capture stream contains NO Context-ID / VMID / TS /
# cycle-count packets (verified by hand-reading the ETM bytes after each
# Trace-Info), so CONFIGR must be near-zero at capture time. If you change the
# firmware to enable TS/CID/VMID/CC, update the env var so OpenCSD parses the
# extra fields correctly — otherwise it will mis-frame Atom/Address packets.
TRCIDR0        = int(os.environ.get("TRCIDR0",        "0x080006E1"), 0)
TRCIDR1        = int(os.environ.get("TRCIDR1",        "0x4100F401"), 0)
TRCIDR2        = int(os.environ.get("TRCIDR2",        "0x00000004"), 0)
TRCIDR8        = int(os.environ.get("TRCIDR8",        "0x00000001"), 0)
TRCIDR9        = int(os.environ.get("TRCIDR9",        "0x00000000"), 0)
TRCIDR10       = int(os.environ.get("TRCIDR10",       "0x00000000"), 0)
TRCIDR11       = int(os.environ.get("TRCIDR11",       "0x00000000"), 0)
TRCIDR12       = int(os.environ.get("TRCIDR12",       "0x00000001"), 0)
TRCIDR13       = int(os.environ.get("TRCIDR13",       "0x00000000"), 0)
TRCCONFIGR     = int(os.environ.get("TRCCONFIGR",     "0x00000000"), 0)
TRCTRACEIDR_V4 = int(os.environ.get("TRCTRACEIDR",    "0x00000002"), 0)
TRCAUTHSTATUS  = int(os.environ.get("TRCAUTHSTATUS",  "0x000000C0"), 0)


def text_sections(elf):
    """Return [(addr, size, off)] for executable (AX) flash sections."""
    p = subprocess.run([READELF, "-S", "-W", elf],
                       capture_output=True, text=True)
    out = []
    import re
    for line in p.stdout.splitlines():
        # [Nr] Name Type Addr Off Size ES Flg Lk Inf Al
        m = re.search(r"\]\s+(\S+)\s+\w+\s+([0-9a-fA-F]{8,16})\s+"
                      r"([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+\S+\s+([A-Zp]*)", line)
        if not m:
            continue
        addr = int(m.group(2), 16)
        off = int(m.group(3), 16)
        size = int(m.group(4), 16)
        flg = m.group(5)
        if "X" in flg and addr >= 0x08000000 and size > 0:
            out.append((addr, size, off))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("elf")
    ap.add_argument("out_dir")
    ap.add_argument("--protocol", choices=["etm35", "etm4"], default="etm35",
                    help="ETM protocol (etm35=Cortex-M4, etm4=Cortex-M7)")
    ap.add_argument("--coresight", action="store_true",
                    help="trace still has TPIU framing (format=coresight)")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    is_v4 = (a.protocol == "etm4")
    # OpenCSD 1.4.1's CoreArchProfileMap only knows Cortex-M0/M0+/M3/M4/M23/M33.
    # STM32H7's Cortex-M7 is ARMv7E-M / M-profile — identical to Cortex-M4 as
    # far as the ETMv4 decoder is concerned (same ISA, same exception model);
    # advertise it as "Cortex-M4" so OpenCSD accepts the snapshot.
    core_type = "Cortex-M4" if is_v4 else "Cortex-M4"
    desc = ("STM32H743 M7 ETMv4 capture (declared Cortex-M4 for OpenCSD)"
            if is_v4 else
            "STM32F429 M4 ETMv3.5 capture (logic-analyser)")

    # 1) Trace buffer: copy the captured bytes in.
    trace_bytes = open(a.trace, "rb").read()
    with open(os.path.join(a.out_dir, "etm.bin"), "wb") as f:
        f.write(trace_bytes)

    # 2) Memory image: dump flash code as a flat binary so the decoder can
    #    fetch opcodes. Try common section names first, then fall back to
    #    the whole ELF (objcopy will strip non-loadable).
    mem_bin = os.path.join(a.out_dir, "mem.bin")
    subprocess.run([OBJCOPY, "-O", "binary",
                    "--only-section=.isr_vector",
                    "--only-section=.text", "--only-section=.rodata",
                    "--only-section=.ARM.exidx", "--only-section=.init_array",
                    "--only-section=.fini_array",
                    "--only-section=ER_IROM1",
                    a.elf, mem_bin], capture_output=True)
    if not os.path.exists(mem_bin) or os.path.getsize(mem_bin) == 0:
        subprocess.run([OBJCOPY, "-O", "binary", a.elf, mem_bin],
                       capture_output=True)
    mem_size = os.path.getsize(mem_bin)

    # CRITICAL (r30-followup bugfix): objcopy -O binary starts the output at the
    # LOWEST included section's LMA and pads gaps, but does NOT pad before it.
    # If .isr_vector (@0x08000000) is omitted, mem.bin starts at .text's LMA
    # (0x080002a0) yet we used to hardcode base=0x08000000 -> a 0x2a0-byte skew:
    # the decoder fetched wrong opcodes for EVERY address. BB=1 masked this
    # (explicit address packets re-anchor every branch) but BB=0 relies on the
    # image to infer direct branches, so the skew corrupted the whole call graph
    # (the "false calls"/"blind-inference drift" seen in stage 3 / r29). Derive
    # the base from the ELF's lowest loadable section LMA instead of hardcoding.
    mem_base = _lowest_load_lma(a.elf)

    fmt = "coresight" if a.coresight else "source_data"

    # 3) snapshot.ini
    with open(os.path.join(a.out_dir, "snapshot.ini"), "w") as f:
        f.write("[snapshot]\n")
        f.write("version=1.0\n")
        f.write(f"description={desc}\n\n")
        f.write("[device_list]\n")
        f.write("device0=cpu.ini\n")
        f.write("device1=etm.ini\n\n")
        f.write("[trace]\n")
        f.write("metadata=trace.ini\n")

    # 4) cpu.ini (core device + memory dump)
    with open(os.path.join(a.out_dir, "cpu.ini"), "w") as f:
        f.write("[device]\n")
        f.write("name=cpu_0\n")
        f.write("class=core\n")
        f.write(f"type={core_type}\n\n")
        f.write("[dump]\n")
        f.write("file=mem.bin\n")
        f.write(f"address=0x{mem_base:08x}\n")
        f.write(f"length=0x{mem_size:08x}\n")

    # 5) etm.ini — protocol-dependent
    with open(os.path.join(a.out_dir, "etm.ini"), "w") as f:
        f.write("[device]\n")
        f.write("name=etm_0\n")
        f.write("class=trace_source\n")
        if is_v4:
            f.write("type=ETM4\n\n")
            f.write("[regs]\n")
            f.write(f"TRCCONFIGR(0x004)=0x{TRCCONFIGR:08x}\n")
            f.write(f"TRCTRACEIDR(0x010)=0x{TRCTRACEIDR_V4:08x}\n")
            f.write(f"TRCAUTHSTATUS(0x3EE)=0x{TRCAUTHSTATUS:08x}\n")
            f.write(f"TRCIDR0(0x078)=0x{TRCIDR0:08x}\n")
            f.write(f"TRCIDR1(0x079)=0x{TRCIDR1:08x}\n")
            f.write(f"TRCIDR2(0x07A)=0x{TRCIDR2:08x}\n")
            f.write(f"TRCIDR8(0x060)=0x{TRCIDR8:08x}\n")
            f.write(f"TRCIDR9(0x061)=0x{TRCIDR9:08x}\n")
            f.write(f"TRCIDR10(0x062)=0x{TRCIDR10:08x}\n")
            f.write(f"TRCIDR11(0x063)=0x{TRCIDR11:08x}\n")
            f.write(f"TRCIDR12(0x064)=0x{TRCIDR12:08x}\n")
            f.write(f"TRCIDR13(0x065)=0x{TRCIDR13:08x}\n")
        else:
            f.write("type=ETM3.5\n\n")
            f.write("[regs]\n")
            f.write(f"ETMCR=0x{ETMCR:08x}\n")
            f.write(f"ETMCCER=0x{ETMCCER:08x}\n")
            f.write(f"ETMIDR=0x{ETMIDR:08x}\n")
            f.write(f"ETMTRACEIDR=0x{ETMTRACEIDR:08x}\n")

    # 6) trace.ini (buffer + source/core association)
    with open(os.path.join(a.out_dir, "trace.ini"), "w") as f:
        f.write("[trace_buffers]\n")
        f.write("buffers=buffer0\n\n")
        f.write("[buffer0]\n")
        f.write("name=ETB_0\n")
        f.write("file=etm.bin\n")
        f.write(f"format={fmt}\n\n")
        f.write("[core_trace_sources]\n")
        f.write("cpu_0=etm_0\n\n")
        f.write("[source_buffers]\n")
        f.write("etm_0=ETB_0\n")

    print(f"snapshot written to {a.out_dir}/  (protocol={a.protocol}, core={core_type})")
    print(f"  trace: {len(trace_bytes)} bytes (format={fmt})")
    print(f"  mem:   {mem_size} bytes @ 0x{mem_base:08x}")
    if is_v4:
        print(f"  ETMv4 regs: CONFIGR=0x{TRCCONFIGR:08x} TRACEIDR=0x{TRCTRACEIDR_V4:02x} "
              f"IDR0=0x{TRCIDR0:08x} IDR1=0x{TRCIDR1:08x} IDR2=0x{TRCIDR2:08x}")
    else:
        print(f"  ETMv3.5 regs: CR=0x{ETMCR:08x} CCER=0x{ETMCCER:08x} "
              f"IDR=0x{ETMIDR:08x} TRACEID=0x{ETMTRACEIDR:02x}")
    tpiu_flag = "  (add -tpiu)" if a.coresight else ""
    print(f"\nrun: trc_pkt_lister -ss_dir {a.out_dir} -decode -logstdout" + tpiu_flag)


if __name__ == "__main__":
    sys.exit(main())
