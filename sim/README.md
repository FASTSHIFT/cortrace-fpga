# Verilog test framework (orbtrace)

Unified Icarus Verilog regression harness for the Artix-7 port.

## What it does

- Discovers YAML test manifests under `sim/manifests/**/*.yml`
- For each manifest, compiles the listed Verilog with `iverilog -g2012`, runs
  `vvp`, matches the printed output against `expect.contains` / `expect.regex`
- Prints a coloured summary table and returns a non-zero exit code on any
  failure (CI-friendly)

## Adding a new test

Create a manifest under `sim/manifests/<group>/<name>.yml`:

```yaml
name: my_test                # required; unique
description: "one-line prose"
sources:                     # required; paths are relative to repo root
  - verilog/traceIF.v
  - verilog/testbeds/traceIF_tb.v
defines: [FOO=1]             # optional; each becomes iverilog -DFOO=1
parameters: {}               # optional; each becomes iverilog -Pmod.parm=val
expect:                      # required; at least one of contains/regex
  contains: "RESULT=ALL_PASS"
  # OR
  # regex: "OUTPUT=[0-9a-f]+"
timeout_s: 60                # optional; default 60
tags: [ring, ddr]            # optional; use with --tag to filter runs
```

Then run:

```bash
python3 sim/run_verilog_tests.py                 # run everything
python3 sim/run_verilog_tests.py --tag ring      # run only tagged tests
python3 sim/run_verilog_tests.py --name my_test  # run one by name
python3 sim/run_verilog_tests.py --keep          # keep .vvp on disk
python3 sim/run_verilog_tests.py --verbose       # show tb output on failure
```

## Local dev flow

```
$ python3 sim/run_verilog_tests.py
Running 8 tests
  [OK    3.4s] traceif/basic
  [OK    3.8s] traceif/resync
  [OK    3.5s] traceif/stim
  [OK    4.1s] traceif/dropedge_baseline
  [OK    4.1s] traceif/dropedge_drop
  [OK   12.7s] ring/la_ddr_writer
  [OK   14.2s] ring/la_ddr_ring
  [OK    5.6s] ring/iddr_gap
Summary: 8 passed, 0 failed in 52.4s
```

## Design notes

- **One tool per language**: Verilog uses iverilog+vvp, Python uses pytest,
  cortrace uses gtest. The runner is a thin layer for the Verilog side only.
- **Manifests over inline YAML jobs**: `.github/workflows/build.yml` now
  calls `run_verilog_tests.py` once. Adding a new tb = one YAML manifest,
  no CI edit.
- **No test discovery at import time**: manifests are static, so anyone can
  read the folder and know exactly what runs.

## Coverage / stats options

```bash
python3 sim/run_verilog_tests.py --coverage
```

prints a "which RTL files does each manifest exercise" table (file-level
coverage, not line-level). Line-level coverage is on the roadmap once we
migrate to Verilator (`verilator --coverage`); the iverilog+vvp path we
use today doesn't have native coverage instrumentation.

```bash
python3 sim/run_verilog_tests.py --junit results.xml
```

writes JUnit XML for CI test reporters (GitHub Actions test-reporter,
Jenkins, GitLab). The report already gets uploaded from CI as the
`sim-junit` artefact — you don't need to run this locally.

```bash
python3 sim/run_verilog_tests.py --slowest 10
```

changes the "slowest N" header from the default 5.

## Filtering

- `--tag ring` — only tests tagged with `ring`
- `--name 'ring/*'` — glob against `<group>/<name>`
- `--list` — dry-run: print the selection and exit
- `--verbose` — dump full log on failure (compile stderr + full vvp output)

## Legacy shell wrappers

`syn/artix7/bringup/sim/run_tb.sh`, `run_tb_ring.sh`, `build_tb_fixed.sh`,
`run_sim_regression.sh` are kept for local iteration; each maps to one
manifest today. They will be removed once no one needs the interactive
`vcd` toggles they carry.
