#!/usr/bin/env python3
"""Unified Icarus-Verilog regression harness.

Discovers YAML manifests under sim/manifests/**/*.yml, compiles and runs each,
matches the printed output against expected markers, and prints a summary.

Non-zero exit on any failure. Intended to be called by CI as a single step.

See sim/README.md for the manifest schema.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML — apt: python3-yaml
except ImportError:
    print("error: PyYAML required (apt install python3-yaml OR pip install pyyaml)",
          file=sys.stderr)
    sys.exit(2)


REPO = Path(__file__).resolve().parent.parent
MANIFEST_ROOT = REPO / "sim" / "manifests"


COL_GREEN = "\033[32m"
COL_RED   = "\033[31m"
COL_DIM   = "\033[2m"
COL_RESET = "\033[0m"
def _isatty() -> bool:
    return sys.stdout.isatty()
def _color(s: str, code: str) -> str:
    return f"{code}{s}{COL_RESET}" if _isatty() else s


@dataclass
class TestCase:
    name: str
    group: str                             # subdirectory
    manifest: Path
    description: str
    sources: list[str]
    defines: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    expect_contains: list[str] = field(default_factory=list)
    expect_regex: list[str] = field(default_factory=list)
    expect_not_contains: list[str] = field(default_factory=list)
    timeout_s: int = 60
    tags: list[str] = field(default_factory=list)
    top: str | None = None                 # override iverilog -s <top>

    def full_name(self) -> str:
        return f"{self.group}/{self.name}"


@dataclass
class TestResult:
    tc: TestCase
    passed: bool
    reason: str
    elapsed_s: float
    log: str


def load_manifest(path: Path) -> TestCase:
    """Parse one .yml manifest into a TestCase."""
    with path.open("r", encoding="utf-8") as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: manifest must be a YAML mapping")

    name = d.get("name")
    if not name:
        raise ValueError(f"{path}: missing 'name'")
    sources = d.get("sources") or []
    if not sources:
        raise ValueError(f"{path}: 'sources' required and non-empty")

    expect = d.get("expect") or {}
    contains = expect.get("contains")
    regex = expect.get("regex")
    not_contains = expect.get("not_contains")
    if not contains and not regex:
        raise ValueError(f"{path}: expect.contains or expect.regex required")

    def as_list(x) -> list[str]:
        if x is None:
            return []
        if isinstance(x, str):
            return [x]
        return list(x)

    group = path.parent.relative_to(MANIFEST_ROOT).as_posix()
    if group == ".":
        group = "root"
    return TestCase(
        name=name,
        group=group,
        manifest=path,
        description=d.get("description", ""),
        sources=list(sources),
        defines=list(d.get("defines") or []),
        parameters=dict(d.get("parameters") or {}),
        expect_contains=as_list(contains),
        expect_regex=as_list(regex),
        expect_not_contains=as_list(not_contains),
        timeout_s=int(d.get("timeout_s", 60)),
        tags=list(d.get("tags") or []),
        top=d.get("top"),
    )


def collect_tests() -> list[TestCase]:
    if not MANIFEST_ROOT.exists():
        return []
    tests: list[TestCase] = []
    for p in sorted(MANIFEST_ROOT.rglob("*.yml")):
        try:
            tests.append(load_manifest(p))
        except Exception as e:
            print(f"error loading {p}: {e}", file=sys.stderr)
            sys.exit(2)
    return tests


def _resolve_source(rel: str) -> Path:
    p = REPO / rel
    if not p.exists():
        raise FileNotFoundError(f"source not found: {rel} (looked at {p})")
    return p


def run_one(tc: TestCase, workdir: Path, keep: bool) -> TestResult:
    t0 = time.time()

    # Resolve sources
    try:
        srcs = [str(_resolve_source(s)) for s in tc.sources]
    except FileNotFoundError as e:
        return TestResult(tc, False, str(e), time.time() - t0, "")

    vvp_out = workdir / f"{tc.group.replace('/', '_')}_{tc.name}.vvp"

    ivl_cmd = ["iverilog", "-g2012", "-o", str(vvp_out)]
    for d in tc.defines:
        ivl_cmd += [f"-D{d}"]
    for k, v in tc.parameters.items():
        ivl_cmd += [f"-P{k}={v}"]
    if tc.top:
        ivl_cmd += ["-s", tc.top]
    ivl_cmd += srcs

    log = f"$ {' '.join(ivl_cmd)}\n"
    try:
        r = subprocess.run(ivl_cmd, capture_output=True, text=True,
                           timeout=tc.timeout_s)
        log += r.stdout + r.stderr
        if r.returncode != 0:
            return TestResult(tc, False,
                              f"iverilog exit {r.returncode}",
                              time.time() - t0, log)
    except subprocess.TimeoutExpired:
        return TestResult(tc, False, "compile timeout", time.time() - t0, log)

    # Run vvp
    try:
        r = subprocess.run(["vvp", str(vvp_out)], capture_output=True,
                           text=True, timeout=tc.timeout_s)
    except subprocess.TimeoutExpired:
        return TestResult(tc, False, "sim timeout", time.time() - t0, log)
    out = r.stdout + r.stderr
    log += f"\n$ vvp {vvp_out.name}\n{out}"

    if not keep:
        try: vvp_out.unlink()
        except OSError: pass

    # Match expectations
    for pat in tc.expect_contains:
        if pat not in out:
            return TestResult(tc, False, f"missing marker: {pat!r}",
                              time.time() - t0, log)
    for pat in tc.expect_regex:
        if not re.search(pat, out):
            return TestResult(tc, False, f"no regex match: {pat!r}",
                              time.time() - t0, log)
    for pat in tc.expect_not_contains:
        if pat in out:
            return TestResult(tc, False,
                              f"forbidden marker present: {pat!r}",
                              time.time() - t0, log)

    return TestResult(tc, True, "ok", time.time() - t0, log)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", action="append", default=[],
                    help="run only tests with any of these tags")
    ap.add_argument("--name", action="append", default=[],
                    help="run only tests whose full name matches this glob "
                         "(may repeat)")
    ap.add_argument("--keep", action="store_true",
                    help="keep .vvp files after run")
    ap.add_argument("--verbose", action="store_true",
                    help="print full log on failure")
    ap.add_argument("--list", action="store_true",
                    help="list matching tests and exit")
    ap.add_argument("--workdir", default=None,
                    help="working directory for build artefacts")
    ap.add_argument("--junit", default=None,
                    help="write JUnit XML report here")
    ap.add_argument("--coverage", action="store_true",
                    help="print manifest -> source file coverage summary")
    ap.add_argument("--slowest", type=int, default=5,
                    help="print top-N slowest tests in the summary (default 5)")
    args = ap.parse_args()

    tests = collect_tests()
    if not tests:
        print(f"no manifests under {MANIFEST_ROOT}", file=sys.stderr)
        return 2

    # Filter
    def matches(tc: TestCase) -> bool:
        if args.tag and not (set(args.tag) & set(tc.tags)):
            return False
        if args.name and not any(
                fnmatch.fnmatchcase(tc.full_name(), pat) for pat in args.name):
            return False
        return True
    selected = [t for t in tests if matches(t)]

    if args.list:
        for t in selected:
            tags = f" [{','.join(t.tags)}]" if t.tags else ""
            print(f"{t.full_name()}{tags}  {t.description}")
        return 0

    if not selected:
        print("no tests selected (all filtered out)", file=sys.stderr)
        return 2

    workdir_ctx = (tempfile.TemporaryDirectory() if not args.workdir
                   else _NopCtx(args.workdir))
    with workdir_ctx as wd:
        wd_path = Path(wd) if hasattr(wd, "__fspath__") else Path(str(wd))
        print(f"Running {len(selected)} tests (workdir={wd_path})")
        t0 = time.time()
        results: list[TestResult] = []
        for tc in selected:
            res = run_one(tc, wd_path, args.keep)
            results.append(res)
            tag = ("[OK   ]", COL_GREEN) if res.passed else ("[FAIL ]", COL_RED)
            elapsed = fmt_elapsed(res.elapsed_s)
            print(f"  {_color(tag[0], tag[1])} {elapsed}  {tc.full_name()}"
                  f"  {'' if res.passed else '- ' + res.reason}")
            if not res.passed and args.verbose:
                print(_color("  --- log ---", COL_DIM))
                for line in res.log.rstrip().splitlines():
                    print(f"    {line}")
                print(_color("  --- end log ---", COL_DIM))

        elapsed_total = time.time() - t0
        n_pass = sum(1 for r in results if r.passed)
        n_fail = len(results) - n_pass

        # Per-tag summary
        print()
        tag_stats: dict[str, tuple[int, int, float]] = {}
        for r in results:
            for tag in (r.tc.tags or ["untagged"]):
                p, f_, t = tag_stats.get(tag, (0, 0, 0.0))
                tag_stats[tag] = (p + (1 if r.passed else 0),
                                  f_ + (0 if r.passed else 1),
                                  t + r.elapsed_s)
        if tag_stats:
            print("By tag:")
            for tag in sorted(tag_stats):
                p, f_, t = tag_stats[tag]
                icon = _color("✓", COL_GREEN) if f_ == 0 else _color("✗", COL_RED)
                print(f"  {icon} {tag:20s}  {p}/{p+f_} passed  {fmt_elapsed(t)}")

        # Slowest
        if args.slowest > 0:
            slow = sorted(results, key=lambda r: -r.elapsed_s)[:args.slowest]
            print(f"\nSlowest {min(args.slowest, len(slow))}:")
            for r in slow:
                print(f"  {fmt_elapsed(r.elapsed_s)}  {r.tc.full_name()}")

        # Coverage: source-file -> which tests exercise it
        if args.coverage:
            print("\nSource coverage (files under RTL that appear in >=1 test):")
            covered: dict[str, list[str]] = {}
            for tc in tests:  # all tests, not just selected
                for src in tc.sources:
                    # skip tb files (under sim/ or testbeds/)
                    lower = src.lower()
                    if "/sim/" in lower or "/testbeds/" in lower or "/tb_" in lower or lower.endswith("_tb.v"):
                        continue
                    covered.setdefault(src, []).append(tc.full_name())
            # Sort by number of covering tests
            for src in sorted(covered, key=lambda s: (-len(covered[s]), s)):
                print(f"  [{len(covered[src])}]  {src}")
            print(f"\n  {len(covered)} RTL files touched by manifest tests")
            print("  (NOTE: this is file-level presence, not line-coverage — "
                  "for real line-coverage, migrate to Verilator with --coverage)")

        # JUnit XML output
        if args.junit:
            write_junit(results, elapsed_total, Path(args.junit))
            print(f"\nJUnit XML written to {args.junit}")

        summary = f"Summary: {n_pass} passed, {n_fail} failed in {fmt_elapsed(elapsed_total)}"
        if n_fail == 0:
            print(_color(summary, COL_GREEN))
            return 0
        else:
            print(_color(summary, COL_RED))
            if not args.verbose:
                for r in results:
                    if not r.passed:
                        print(f"  FAILED: {r.tc.full_name()} - {r.reason}"
                              f"  (rerun with --verbose --name '{r.tc.full_name()}')")
            return 1


def fmt_elapsed(s: float) -> str:
    if s < 0.1:
        return f"{s*1000:4.0f}ms"
    if s < 60:
        return f"{s:5.2f}s"
    m, s = divmod(s, 60)
    return f"{int(m)}m{s:04.1f}s"


def write_junit(results: list[TestResult], total_s: float, path: Path) -> None:
    """Emit a minimal but valid JUnit XML the GitHub-Actions test-reporter and
    Jenkins both accept."""
    from xml.sax.saxutils import escape
    n = len(results)
    n_fail = sum(1 for r in results if not r.passed)
    lines: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(f'<testsuite name="verilog" tests="{n}" failures="{n_fail}" '
                 f'time="{total_s:.3f}">')
    for r in results:
        cls = r.tc.group.replace("/", ".")
        lines.append(f'  <testcase classname="{escape(cls)}" '
                     f'name="{escape(r.tc.name)}" '
                     f'time="{r.elapsed_s:.3f}">')
        if not r.passed:
            log = escape(r.log or "")
            lines.append(f'    <failure message="{escape(r.reason)}">'
                         f'<![CDATA[\n{log}\n]]></failure>')
        lines.append('  </testcase>')
    lines.append('</testsuite>')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _NopCtx:
    """Trivial context manager wrapping a Path — no cleanup on exit."""
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        Path(self.path).mkdir(parents=True, exist_ok=True)
        return self.path
    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    sys.exit(main())
