"""Unit tests for nx_tcbmap.py, incl. the resident-OpenOCD telnet read path."""

import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nx_tcbmap as tm  # noqa: E402


class FakeOpenOcd:
    """Telnet-ish server: greets with a prompt, answers `mdw` from a memory dict."""

    def __init__(self, mem):
        self.mem = mem
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.cmds = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.sendall(b"Open On-Chip Debugger\r\n> ")
            buf = b""
            while not buf.endswith(b"\n"):
                d = c.recv(4096)
                if not d:
                    break
                buf += d
            cmd = buf.decode().strip()
            self.cmds.append(cmd)
            _, addr, cnt = cmd.split()
            addr, cnt = int(addr, 16), int(cnt)
            lines = []
            for i in range(0, cnt, 4):
                n = min(4, cnt - i)
                words = " ".join(
                    "%08x" % self.mem.get(addr + 4 * (i + j), 0) for j in range(n)
                )
                lines.append("0x%08x: %s" % (addr + 4 * i, words))
            c.sendall(("\x00" + "\r\n".join(lines) + "\r\n> ").encode())
            c.close()

    def close(self):
        self.srv.close()


@pytest.fixture
def fake_oocd(monkeypatch):
    servers = []

    def make(mem):
        s = FakeOpenOcd(mem)
        servers.append(s)
        monkeypatch.setattr(tm, "OOCD_TELNET", "127.0.0.1:%d" % s.port)
        return s

    yield make
    for s in servers:
        s.close()


def test_read_words_over_telnet_spans_multiple_rows(fake_oocd):
    mem = {0x20000000 + 4 * i: 0x1000 + i for i in range(6)}
    s = fake_oocd(mem)
    got = tm.oocd_read_words(0x20000000, 6)
    assert got == mem
    assert s.cmds == ["mdw 0x20000000 6"]


def test_fn_for_picks_containing_symbol():
    addrs, names = [0x100, 0x200, 0x300], ["a", "b", "c"]
    assert tm.fn_for(addrs, names, 0x2FF) == "b"
    assert tm.fn_for(addrs, names, 0x300) == "c"
    assert tm.fn_for(addrs, names, 0x50) == "?"


def test_sym_and_symtab_parse_nm_output(monkeypatch):
    nm_out = (
        b"08000100 T alpha\n"
        b"08000200 t beta\n"
        b"20000000 D g_pidhash\n"
        b"         U undefined_sym\n"
    )
    monkeypatch.setattr(tm.subprocess, "check_output", lambda *a, **k: nm_out)
    assert tm.sym("nm", "elf", "g_pidhash") == 0x20000000
    assert tm.sym("nm", "elf", "missing") is None
    addrs, names = tm.build_symtab("nm", "elf")
    assert addrs == [0x08000100, 0x08000200]
    assert names == ["alpha", "beta"]
