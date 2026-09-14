#!/usr/bin/env python3
"""h743_serial — pyserial wrapper for the argparse CLI baked into the H743
selftrace firmware (Core/coremark_port/cli.c). Runs over the DAPLink CDC VCP.

Why prefer this over the openocd DIVR1 poke:
  - the CPU is running normally; PLL glitch during reprogram doesn't wedge the
    debug AP (2026-09-07 sweep confirmed openocd poke left SystemCoreClock
    inconsistent so downstream peripherals ran at the WRONG assumed rate,
    meaning "the freq wasn't actually changed" from the ETM's perspective);
  - one code path (pll_ctrl_apply) shared with cold init;
  - text protocol is easy to reproduce by hand for triage.

The firmware CLI prompts with '> ' and echoes typed characters. We drive it
line at a time and collect output until we see the prompt again.

Standalone usage:
    h743_serial.py --port /dev/serial/by-id/usb-Arm_DAPLink_*-if00 id
    h743_serial.py pll --show
    h743_serial.py pll --m 2 --n 24 --p 3 --r 2 --apply
    h743_serial.py reset
"""
import argparse
import glob
import re
import shlex
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    print("error: python3 -m pip install pyserial", file=sys.stderr)
    raise


DEFAULT_PORT_GLOB = "/dev/serial/by-id/usb-Arm_DAPLink_*-if00"


def find_port() -> str:
    """Prefer the DAPLink CDC by-id symlink (stable across reboots)."""
    hits = sorted(glob.glob(DEFAULT_PORT_GLOB))
    if hits:
        return hits[0]
    hits = sorted(glob.glob("/dev/ttyACM*"))
    if hits:
        return hits[0]
    raise SystemExit("no serial port found; pass --port explicitly")


class H743CLI:
    """Line-based dialogue with the firmware CLI.

    The firmware emits '\r\n> ' as its prompt. We treat any response ending in
    '> ' (or a bare newline followed by '> ') as end-of-reply. Timeouts fail
    loudly rather than silently returning half a reply.
    """
    PROMPT = "> "
    READ_CHUNK = 128

    def __init__(self, port: str, baud: int = 115200, timeout: float = 3.0):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout
        self.s = serial.Serial(port, baudrate=baud, timeout=0.1,
                               write_timeout=1.0)
        # Drain anything already in the buffer (previous session's output).
        time.sleep(0.05)
        try:
            self.s.reset_input_buffer()
        except Exception:
            pass
        # Send a bare newline to force the firmware to re-print the prompt so
        # we know we're in a clean state.
        self._drain_until_prompt(bootstrap=True, timeout=1.5)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _drain_until_prompt(self, *, bootstrap=False, timeout=None) -> str:
        """Read until we see the prompt at the end of a line, or timeout."""
        deadline = time.monotonic() + (timeout if timeout is not None
                                                else self.timeout)
        buf = bytearray()
        if bootstrap:
            self.s.write(b"\r\n")
            self.s.flush()
        while time.monotonic() < deadline:
            chunk = self.s.read(self.READ_CHUNK)
            if chunk:
                buf.extend(chunk)
                # Are we at a prompt?
                text = buf.decode(errors="replace")
                if text.endswith(self.PROMPT) or text.endswith("> \x00"):
                    return text
        # timed out
        text = buf.decode(errors="replace")
        raise TimeoutError(
            f"CLI silent for {timeout or self.timeout}s\n"
            f"partial output:\n{text!r}")

    def send(self, line: str, timeout: float | None = None) -> str:
        """Send one CLI command; return the response text (without prompt)."""
        # Strip the local echo of the command we send so callers get just the
        # reply body.
        clean_line = line.strip()
        if clean_line:
            self.s.write((clean_line + "\r\n").encode())
            self.s.flush()
        response = self._drain_until_prompt(
            timeout=(timeout if timeout is not None else self.timeout))
        # Remove any leading echo of the command + trailing prompt.
        body = response
        # First occurrence of the echo -> strip up to and including the CR/LF
        # after it.
        i = body.find(clean_line)
        if i >= 0:
            j = body.find("\n", i)
            if j >= 0:
                body = body[j + 1:]
        # Remove trailing prompt.
        if body.endswith(self.PROMPT):
            body = body[:-len(self.PROMPT)]
        return body.strip("\r\n")


# -- convenience API used by fpga_vs_etf.py ---------------------------------

def pll_apply(cli: H743CLI, *, m=None, n=None, p=None, q=None, r=None,
              timeout=5.0) -> str:
    """Compose and execute a `pll --apply` command. Returns full response."""
    parts = ["pll"]
    if m is not None: parts += [f"--m", str(m)]
    if n is not None: parts += [f"--n", str(n)]
    if p is not None: parts += [f"--p", str(p)]
    if q is not None: parts += [f"--q", str(q)]
    if r is not None: parts += [f"--r", str(r)]
    parts += ["--apply"]
    return cli.send(" ".join(parts), timeout=timeout)


def pll_show(cli: H743CLI) -> dict:
    """Parse the `pll --show` output into a dict of ints (Hz where applicable)."""
    resp = cli.send("pll --show")
    out = {}
    for k, pat in [
        ("m", r"M=(\d+)"), ("n", r"N=(\d+)"), ("p", r"P=(\d+)"),
        ("q", r"Q=(\d+)"), ("r", r"R=(\d+)"),
        ("hse_hz",     r"HSE\s*=\s*(\d+)"),
        ("vco_hz",     r"VCO\s*=\s*(\d+)"),
        ("sysclk_hz",  r"sysclk\s*=\s*(\d+)"),
        ("pll1r_hz",   r"pll1_r_ck\s*=\s*(\d+)"),
    ]:
        m = re.search(pat, resp)
        if m: out[k] = int(m.group(1))
    return out


# -- standalone CLI ---------------------------------------------------------

def main():
    # Manual argv scan (not argparse) because the whole point is to forward
    # arguments unmolested to the firmware CLI: `pll --r 4 --apply` would
    # otherwise be eaten by our own argparse.
    argv = sys.argv[1:]
    port = None
    baud = 115200
    timeout = 3.0
    while argv and argv[0].startswith("--"):
        opt = argv[0]
        if opt == "--":
            argv.pop(0); break
        if opt == "-h" or opt == "--help":
            print(__doc__)
            print("usage: h743_serial.py [--port P] [--baud B] [--timeout T] "
                  "<cmd> [args...]")
            return
        if opt in ("--port", "--baud", "--timeout"):
            if len(argv) < 2:
                sys.exit(f"{opt} needs a value")
            val = argv[1]
            if opt == "--port":    port = val
            elif opt == "--baud":  baud = int(val)
            elif opt == "--timeout": timeout = float(val)
            argv = argv[2:]
        else:
            break
    if not argv:
        sys.exit("no command given; try `h743_serial.py id` or `--help`")

    if port is None:
        port = find_port()
    cli = H743CLI(port, baud, timeout)
    try:
        line = " ".join(shlex.quote(x) if " " in x else x for x in argv)
        resp = cli.send(line, timeout=timeout)
        print(resp)
    finally:
        cli.close()


if __name__ == "__main__":
    main()
