"""Unit tests for itm_capture: grab, then delegate to cortrace_fuse."""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import itm_capture as ic  # noqa: E402


@pytest.fixture
def fake_cortrace(tmp_path):
    scripts = tmp_path / "cortrace" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "cortrace_fuse.py").write_text("", encoding="utf-8")
    return str(tmp_path / "cortrace")


def test_missing_iface_needs_raw_in(monkeypatch, capsys):
    monkeypatch.delenv("CORTRACE_IFACE", raising=False)
    with pytest.raises(SystemExit) as e:
        ic.parse_args(["--elf", "x.elf"])
    assert e.value.code == 2
    assert "--iface" in capsys.readouterr().err


def test_raw_in_needs_no_iface(monkeypatch):
    monkeypatch.delenv("CORTRACE_IFACE", raising=False)
    a, rest = ic.parse_args(["--raw-in", "r.bin", "--elf", "x.elf", "--open"])
    assert a.iface is None
    assert rest == ["--elf", "x.elf", "--open"]  # unknown options are forwarded


def test_cortrace_dir_from_environment(monkeypatch):
    monkeypatch.setenv("CORTRACE_DIR", "/some/cortrace")
    assert ic.default_cortrace_dir() == "/some/cortrace"
    monkeypatch.delenv("CORTRACE_DIR")
    assert ic.default_cortrace_dir().endswith("cortrace")


def test_grab_then_fuse(monkeypatch, tmp_path, fake_cortrace):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append([str(c) for c in cmd])
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    rc = ic.main(
        [
            "--iface", "eth9",
            "--cortrace-dir", fake_cortrace,
            "--out-dir", str(tmp_path / "out"),
            "--tag", "t",
            "--secs", "2",
            "--width", "2",
            "--elf", "x.elf",
        ]
    )  # fmt: skip
    assert rc == 0
    set_width, grab, fuse = calls
    assert set_width[-2:] == ["set-width", "2"]
    assert grab[1:4] == ["eth9", "2.0", str(tmp_path / "out" / "raw_t.bin")]
    assert fuse[1].endswith("scripts/cortrace_fuse.py")
    assert fuse[fuse.index("--raw") + 1] == str(tmp_path / "out" / "raw_t.bin")
    assert fuse[fuse.index("--width") + 1] == "2"
    assert fuse[-2:] == ["--elf", "x.elf"]


def test_raw_in_skips_the_grab(monkeypatch, tmp_path, fake_cortrace):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append([str(c) for c in cmd])
        return types.SimpleNamespace(returncode=3)

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    rc = ic.main(
        ["--raw-in", "r.bin", "--cortrace-dir", fake_cortrace]
        + ["--out-dir", str(tmp_path)]
    )
    assert rc == 3  # the fuse exit code is propagated
    assert len(calls) == 1


@pytest.mark.parametrize("failing", ["set-width", "stream_grab"])
def test_grab_failures_abort(monkeypatch, tmp_path, fake_cortrace, failing):
    def fake_run(cmd, **_kw):
        bad = any(failing in str(c) for c in cmd)
        return types.SimpleNamespace(returncode=1 if bad else 0)

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        ic.main(
            ["--iface", "e", "--cortrace-dir", fake_cortrace]
            + ["--out-dir", str(tmp_path)]
        )
    assert failing.split("_")[0] in str(e.value)


def test_missing_cortrace_checkout_is_reported(tmp_path):
    with pytest.raises(SystemExit) as e:
        ic.main(
            ["--raw-in", "r.bin", "--cortrace-dir", str(tmp_path / "nope")]
            + ["--out-dir", str(tmp_path)]
        )
    assert "cortrace_fuse.py not found" in str(e.value)
