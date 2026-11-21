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
