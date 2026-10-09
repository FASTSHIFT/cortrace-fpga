"""Unit tests for itm_capture.fuse (Perfetto merge by sequence-id remap)."""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import itm_capture as ic  # noqa: E402


class FakePacket:
    def __init__(self, seq):
        self.trusted_packet_sequence_id = seq


class FakeTrace:
    """Stand-in for perfetto_trace_pb2.Trace: JSON on the wire, one field."""

    def __init__(self):
        self.packet = []

    def ParseFromString(self, data):
        self.packet = [FakePacket(s) for s in json.loads(data)]

    def SerializeToString(self):
        return json.dumps([p.trusted_packet_sequence_id for p in self.packet]).encode()


FAKE_PB2 = types.SimpleNamespace(Trace=FakeTrace)


def test_fuse_appends_remapped_note_trace_and_keeps_hw_bytes(tmp_path):
    hw = tmp_path / "hw.perfetto"
    hw_bytes = b"\x00\x01hardware-bytes\xff" * 1000
    hw.write_bytes(hw_bytes)
    note = tmp_path / "note.pftrace"
    note.write_bytes(json.dumps([1, 2, 0, 2]).encode())
    out = tmp_path / "fused.perfetto"

    remapped = ic.fuse(str(hw), str(note), str(out), pb2=FAKE_PB2)

    data = out.read_bytes()
    assert remapped == 3  # sequence id 0 (legacy) is left alone
    assert data.startswith(hw_bytes)  # hardware file is copied untouched
    assert json.loads(data[len(hw_bytes) :]) == [1001, 1002, 0, 1002]


def test_fuse_streams_large_hw_file(tmp_path):
    hw = tmp_path / "hw.perfetto"
    size = 17 * 1024 * 1024  # more than one 16 MiB chunk
    hw.write_bytes(b"\xab" * size)
    note = tmp_path / "note.pftrace"
    note.write_bytes(b"[]")
    out = tmp_path / "fused.perfetto"
    ic.fuse(str(hw), str(note), str(out), pb2=FAKE_PB2)
    assert out.stat().st_size == size + len(b"[]")


def test_missing_pynuttx_is_an_argument_error(monkeypatch, capsys):
    monkeypatch.delenv("PYNUTTX", raising=False)
    monkeypatch.setattr(sys, "argv", ["itm_capture.py", "--elf", "x.elf"])
    with pytest.raises(SystemExit) as e:
        ic.main()
    assert e.value.code == 2
    assert "--pynuttx" in capsys.readouterr().err


def test_missing_iface_needs_raw_in(monkeypatch, capsys):
    monkeypatch.delenv("CORTRACE_IFACE", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["itm_capture.py", "--elf", "x.elf", "--pynuttx", "/p"]
    )
    with pytest.raises(SystemExit) as e:
        ic.main()
    assert e.value.code == 2
    assert "--iface" in capsys.readouterr().err
