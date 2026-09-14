#!/usr/bin/env bash
# cortrace-fpga — install git hooks by pointing core.hooksPath at .githooks.
# Run once after cloning:  scripts/install-hooks.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks
echo "git hooks installed (core.hooksPath = .githooks)"
echo "  pre-commit  : block unformatted staged Python (black) / Verilog (whitespace)"
echo "  commit-msg  : enforce Conventional-Commit subject lines"
echo "run 'scripts/format.sh' to format in place."
