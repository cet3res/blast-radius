# blast-radius

Entry for the RWX Blast Radius track of the Builders Buildathon 2026. For each patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), print which tests to run and how to split them across up to 8 workers.

Track page: https://www.joinbuilders.org/buildathon/tracks/blast-radius

## Interface

- Input: `/data/commits/<id>.patch` and `/data/tests.json`
- Output: one JSON line per commit on stdout, e.g. `{"commit": "k7q2", "workers": [[12, 845], [3]]}`
- `"workers": []` runs nothing
- Logs go to stderr only

## Scoring

Simulated CI seconds, lower is better: busiest worker seconds + 2 s per worker + 300 s per missed failing commit + CPU seconds. Baseline (every test on 3 workers) is 1614.1 s.

## Local run

1. Download `blast-radius-sample.tar.gz` from the track page
2. Extract only the sample data and scorer: `tar xzf blast-radius-sample.tar.gz sample score.py`
3. `make sample` builds the image, runs it under the scorer limits and scores the output
4. `make check` validates the output format only
5. `make test` runs the selector's unit tests; `make sample-local PYTHON=python3.13` scores without Docker

## Status

- v0: starter selector, runs every test on 3 workers (1210.5 s on the 30 sample commits)
- v1: function-level coverage map (556.0 s)
- v2: syntax-tree diff, statement-level coverage, import-state fingerprints, operand-type profile: **115.6 s, 0 misses** on the sample; 0 misses on 364 synthetic changes with real test outcomes (see `docs/results.md`). Practice board: 186.3 s (174.8 simulated + 11.5 CPU)
- v3: differential checks for the TPC-DS execution test (21 s) and `test_optimized_tpch`: on the validation sets with real outcomes, simulated seconds drop 26% (mutants-1 730.7 -> 540.8 s), 0 misses (see `docs/results.md`)

## How it works

Everything the selector knows comes from one dependency map (`dependencies.json.gz`),
built by running the unmodified test suite of the pinned sqlglot once, and from the
patch itself. The image needs only the Python standard library.

### How a patch is judged

| Change | Tests selected |
| --- | --- |
| Python edit with an identical syntax tree (comments, blank lines, formatting) | none |
| Docstring or other constant-expression statement | none |
| `is`/`==` or `is not`/`!=` swap where every operand type pair the tests saw compares the same both ways | none |
| Statement inside a function body | tests that executed that statement (line coverage over its full span) |
| Statement inserted in a function | tests that executed the statement before it, or entered the enclosing block |
| Edit that changes a function's local names, `yield`, `global`... | every test that entered the function |
| Run-time code that writes class constants or module globals | also every test running a line that names them |
| Import-time edit (class/module bodies, decorators, defaults, code run while importing) | see below |
| Fixture or other data file | tests that opened it (setUpClass reads count for the whole class) |
| `pyproject.toml`, `setup.cfg`, `conftest.py`, renames, deleted modules, unparsable diffs | everything |

**Import-time edits.** The patched sqlglot is imported from memory in a forked
child of a small server process (`fingerprint.py --serve`); nothing is written.
Every module global and every class attribute is reduced to a hash (dict and set
contents, closures, defaults of generated functions, wrapper types, derived tries;
not function bodies, which coverage handles). The same is done once for the
pinned source while the image is built (`selector.py --prepare`; build time is not
charged), together with the compiled baseline modules, so a run only compiles the
patched files. Plain defs whose arguments or constant defaults changed skip this
step: their stored function object cannot differ. Attributes whose hash differs
are the change:

- changed class attributes select the tests that instantiated that class, a
  subclass, or another class of the same dialect (tokenizer, parser, generator);
  when every changed attribute is plain data, only those that also ran a line
  naming it;
- changed module globals select the tests that ran a line naming them;
- a module that no longer imports, or a change to process-wide state, selects
  everything; a change to what `import sqlglot` loads also selects the test that
  imports sqlglot in a subprocess.

### Expensive TPC tests: differential checks

`test_execute_tpcds` takes 21 s and runs most of the optimizer, so coverage selects
it for many edits that do not break it; on the validation sets it caused about 75%
of all cost above the oracle. The test depends on sqlglot only through a few
steps whose results are cheap to recompute, plus a run phase that is not:

| Step | What the test does with the result |
| --- | --- |
| transpile | `transpile(sql, write="duckdb")`, run by duckdb as the reference |
| parse | `parse_one(sql)`, pickled to the pool worker |
| optimize | `optimize(...)` and `.sql(pretty=True)` inside `execute()` |
| plan | `Plan(optimized)` |
| run | CSV loading, `find_tables`, the Python executor, frame comparison |

`tools/build_proxies.py` records, on the pinned source, the lines each step runs
for each query (caches reset before each query, so cold paths are included) and the
lines of the run phase. For a selected TPC test the selector keeps it when the edit
reaches a run-phase line, a line the test runs that no recorded step runs, writes
long-lived state, changes import-time state that a run-phase line names (or any
method), edits its test module or helpers, or changes a data file it reads.
Otherwise the fork child recomputes only the touched steps of only the touched
queries with the patched sources (for import-time data changes: the queries whose
step lines name a changed attribute) and compares a structural dump of every
result (all expression slots and args in order, parent links, plans step by step)
with the same dump computed while the image was built. Equal results mean the run
phase sees identical inputs and code, so the test is dropped; any difference or
exception keeps it. `test_optimized_tpch` (3.7 s) depends on sqlglot only through
its duckdb transpiles and is handled the same way. A TPC fixture edit that leaves a
test's inputs unchanged (for example only expected optimizer output) drops it with
no recomputation. All nine real TPC-DS failures in the validation sets are kept.

### The dependency map

`tools/build_map.py` runs the 1,620 catalog tests once under coverage with
`SKIP_INTEGRATION=0` and records:

- line coverage per test, with setup, call and teardown separated; lines run while
  collecting a test file are charged to all its tests, lines run in a setup phase
  (setUpClass) to all tests of the class;
- import-time lines and code objects: every sqlglot module is imported before
  collection, so import work is never hidden inside the first test that needed it;
- classes each test instantiated (all sqlglot `__init__`s are wrapped), class
  ancestry, and each dialect's tokenizer/parser/generator classes;
- files and directories each test opened or listed, and subprocesses it started;
- statement spans of every source file, used to expand a reference to its statement;
- per-step, per-query lines of the two TPC tests and the TPC fixtures
  (`tools/build_proxies.py`, see above).

Per-process caches that would hide work from later tests are reset before every
test: the generator dispatch cache, unittest `cache` dicts, the dialect registry,
and the lazy attributes of `sqlglot.dialects` and `sqlglot.optimizer`. Process
pools run synchronously, with pickle round trips, under the owning test's context.

`tools/build_profile.py` then runs the suite once more with every single-operator
`is`, `is not`, `==` and `!=` in sqlglot rewritten (in memory) to record the
types of its operands, and stores per comparison site the type pairs seen and how
each type decides equality (identity, builtin, or its own `__eq__`). A swap is a
no-op when, for every pair, identity and equality must agree: `None` against a
type with identity or builtin equality, two identity-compared types, or two
members of one enum with distinct values. Up to the first evaluation of the
edited comparison, a patched run is identical to the baseline, so its operands
have one of the recorded types.

The map holds no patch identities or failure labels. Sample answers are used only
to score.

```sh
python3 -m venv venv && venv/bin/pip install -r requirements.lock coverage==7.10.7
git clone https://github.com/tobymao/sqlglot.git /tmp/sqlglot
git -C /tmp/sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
make map SOURCE=/tmp/sqlglot PYTHON=venv/bin/python
```

### Validation beyond the samples

`tools/mutants.py` generates synthetic patches (operator and constant flips,
comment lines, fixture edits, line deletion/duplication/swaps, test-file and
docstring edits, combinations), applies each to a private worktree, runs the
whole catalog, and writes answers in the sample format. See `docs/results.md`.

### Scheduling

Durations are integer milliseconds. For each worker count from 1 to 8, tests are
placed longest first on the least loaded worker, then single moves and swaps off
the busiest worker are applied while they lower it. The cheapest count wins,
including 2 seconds per worker.

### Limits

Coverage is an empirical map, not a proof: an edit can change behaviour for a test
that never ran the edited code through state that outlives a test, through
dynamic attribute names, or through environment differences. The map was built
on macOS ARM64 with Python 3.13 and the locked packages; the scorer is Linux AMD64.

## Continuing the work

See `docs/HANDOFF.md` for the current state, measurements, rejected ideas and next
steps. `validation/` holds synthetic patches with real outcomes (never read by the
selector); score any set with `python tools/evaluate.py validation/mutants-1`.

## Code brought in

- Organizer starter kit: `Dockerfile`, `Makefile`, `selector.py`, `requirements.lock` (`score.py` and `sample/` are used locally and not committed)
- sqlglot at the pinned commit is the only other code involved; `dependencies.json.gz` is derived from running its unmodified tests

## License

MIT
