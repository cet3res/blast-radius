# Local validation

Validated on November 21, 2026, on macOS ARM64 with Python 3.13. These are local
results, not hidden-set or server scores. Docker is not installed on this
workstation, so `make sample` (container limits, linux/amd64) has not run.

## Sample commits (30)

| Selector | Simulated seconds | Missed commits |
| --- | ---: | ---: |
| Starter: all tests, three workers | 1210.470 | 0 |
| All tests, optimized worker count | 1104.510 | 0 |
| v1: function-level coverage, fallback on any import-time or comment edit | 555.959 | 0 |
| v2: syntax-tree diff, statement coverage, import-state fingerprints | 165.806 | 0 |
| v2 + operand-type profile (provably equivalent `is`/`==` swaps) | **115.565** | 0 |
| Oracle: exactly the failing tests, best split | 94.351 | 0 |

v1 ran every test on 11 of 30 commits (comment-only edits, class constants);
v2 runs none on comment-only edits and a few dialect tests on class constants. The
type profile proves the two `is`/`==` swaps in the sample (2haz, 66ks) cannot
change any comparison the tests evaluate; each had cost about 25 s because the
TPC-DS execution test runs that code.

Selector CPU (user+system, including child processes) on an idle machine:

| Version | 30 sample commits | 40 sample-like commits |
| --- | ---: | ---: |
| First submission (board: 11.5 s CPU on the practice set) | 1.65 s | 2.95 s |
| Baseline state and bytecode prepared at image build | 1.10 s | 2.04 s |
| + targeted compile check, memoized reference scans, fewer children | 1.09 s | 1.69 s |
| + fork server for patched imports | **0.83 s** | **1.35 s** |

The board ran about 4x the CPU this machine measures for similar work, so the
practice-set CPU should drop from 11.5 s to roughly 5-6 s. The output is about 3 KB.

## Synthetic mutations with real outcomes

`tools/mutants.py` applied each patch to a private worktree of the pinned commit
and ran the whole catalog (`SKIP_INTEGRATION=0`, per-test timeout 300 s).

| Set | Commits | Simulated seconds | Oracle | Missed commits |
| --- | ---: | ---: | ---: | ---: |
| Sample-like (code, comment, fixture, multi) | 135 | 745.2 | 294.0 | 0 |
| Extended (line delete/duplicate/swap, test files, docstrings, multi) | 120 | 863.0 | 273.1 | 0 |
| `is`/`==` swaps at sites the profile calls equivalent | 89 | 0.0 | 0.0 | 0 |
| `is`/`==` swaps at other sites (control) | 20 | 215.6 | 6.4 | 0 |

Running every test would cost about 37 s per commit (about 4,970 s on the 135
sample-like commits). None of the 89 swaps judged equivalent broke a test; 3 of
the 20 control swaps did, and were selected.

Expensive tests are worth their cost when coverage selects them: TPC-DS execution
(21.5 s) failed on 4 of the 21 commits that selected it, far above the roughly 7%
at which leaving it out would pay off against a 300 s miss.

## v3: TPC differential checks (November 22, 2026)

Measured on Linux AMD64 (a 4-core cloud container) with Python 3.13. The sample
(`sample/`, the official catalog with recorded durations) was not available there,
so a catalog was rebuilt by timing the pinned suite once with pytest
(`SKIP_INTEGRATION=0`, 1,620 tests, 100.2 s in total). Test numbers are the map's
test order, 0-based; this was confirmed against failing tests of the validation
sets. That machine runs the suite at about the labeling machine's speed
(`test_execute_tpcds` 21.06 s vs 21.45 s recorded), and the previous version
reproduces the earlier validation results closely (mutants-1 730.7 s vs 745.2 s),
so the numbers below are comparable but not identical to the official ones.

| Set | Commits | v2 simulated | v3 simulated | Oracle | Misses |
| --- | ---: | ---: | ---: | ---: | ---: |
| mutants-1 (sample-like) | 135 | 730.7 | **540.8** | 289.0 | 0 |
| mutants-2-extended | 120 | 849.6 | **702.7** | 266.9 | 0 |
| swaps | 109 | 211.2 | **160.2** | 6.4 | 0 |

All nine commits whose real outcome breaks `test_execute_tpcds` keep it (the
recomputed step results differ: pickling through `serde`, optimizer helpers,
planner, import-time `alias_`...). The checks also keep it, conservatively, on a few
commits where results differ but the test still passes (e.g. a simplify change that
alters optimized trees, a duckdb generator change that alters the transpiled SQL).

Selector CPU (user+system including children, same machine):

| Set | v2 (before) | v3 | of which checks | fingerprints |
| --- | ---: | ---: | ---: | ---: |
| mutants-1 | 13.1 s | 23.2 s | 11.3 s (17 checks) | 7.0 s (18) |
| mutants-2-extended | 20.5 s | 47.3 s | 26.0 s (14) | 14.4 s (38) |
| swaps | 5.8 s | 12.8 s | 7.8 s (5) | 0.4 s (1) |

The version scored on the board (`2594d55`, 11.5 s CPU on the 40 practice
commits) uses 22.6 s on mutants-1 on this machine: the scorer's CPU is about
1.7x this machine for this workload, so v3 should spend roughly 11-12 s on a
40-commit practice-like set, about what the board measured for v2, while
simulated seconds on sample-like changes drop by about a quarter.

**Container check.** `docker build` + `docker run` with the scorer's flags
(`--network none --cpus 2 --memory 4096m --read-only --tmpfs /tmp:size=64m
--user 65534:65534 --pids-limit 256`) on the 135 mutants-1 patches: the build-time
`--prepare` succeeds (baseline state, compiled modules and TPC step results),
output is identical to the local run for all 135 commits, peak memory 222 MB, 39 s
wall.

## Map

Built from unmodified sources with `tools/build_map.py`: 1,620 tests and 25,440
subtests pass; 250 Python files with line coverage in 8,712 distinct test sets;
69 files read by tests; 1,250 instantiated classes; one subprocess test (`import
sqlglot` only). The operand-type profile covers 1,626 of sqlglot's 1,673
single-operator identity/equality comparisons. Compressed size 1.9 MB, loaded in
about 0.05 s.

## Remaining risk

- The map was built on macOS ARM64; the scorer runs Linux AMD64 in the pinned
  image. sqlglot has no platform branches that the tests exercise, but this is
  unverified.
- Coverage cannot see effects through state that outlives a test unless the
  writing code is edited (handled) or the state is created at import (handled).
- A patched module that fails to import selects everything.
