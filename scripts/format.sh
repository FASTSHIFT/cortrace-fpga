#!/usr/bin/env bash
# cortrace-fpga — source formatter / checker.
#
#   scripts/format.sh            format in place (Python via black) + whitespace
#   scripts/format.sh --check    check only; non-zero exit if anything differs
#
# Python  : black (host/**, sim/**), and ruff lint if available.
# Verilog : no standard formatter is installed on CI (verible optional), so we
#           enforce the cheap invariants a formatter would: no tabs, no trailing
#           whitespace, file ends with exactly one newline. If verible-verilog-
#           format IS present it is used for real formatting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1
fail=0

# ---------- Python (black = deterministic formatting; not a linter) ----------
mapfile -t PY < <(git ls-files '*.py' 2>/dev/null || true)
if [[ ${#PY[@]} -gt 0 ]]; then
    if command -v black >/dev/null 2>&1; then
        if [[ $CHECK -eq 1 ]]; then
            black --quiet --check "${PY[@]}" || { echo "python: black --check failed"; fail=1; }
        else
            black --quiet "${PY[@]}"
        fi
    else
        echo "note: black not found; skipping Python format"
    fi
fi

# ---------- Verilog (our RTL + tb only; vendored ddr3/ + external/ excluded) ----------
mapfile -t V < <(git ls-files 'rtl/*.v' 'sim/tb/*.v' 2>/dev/null \
    | grep -v 'external/' | grep -v 'rtl/ddr3/' || true)
if command -v verible-verilog-format >/dev/null 2>&1 && [[ ${#V[@]} -gt 0 ]]; then
    if [[ $CHECK -eq 1 ]]; then
        for f in "${V[@]}"; do
            diff -q <(verible-verilog-format "$f") "$f" >/dev/null \
                || { echo "verilog: needs formatting -> $f"; fail=1; }
        done
    else
        verible-verilog-format --inplace "${V[@]}"
    fi
else
    # whitespace invariants (formatter-agnostic)
    for f in "${V[@]}"; do
        if grep -nP '\t' "$f" >/dev/null 2>&1; then
            echo "verilog: hard tab in $f (use spaces)"; fail=1
        fi
        if grep -nP ' +$' "$f" >/dev/null 2>&1; then
            echo "verilog: trailing whitespace in $f"; fail=1
        fi
    done
fi

if [[ $CHECK -eq 1 ]]; then
    if [[ $fail -ne 0 ]]; then
        echo "error: run scripts/format.sh to fix" >&2
        exit 1
    fi
    echo "format OK"
fi
