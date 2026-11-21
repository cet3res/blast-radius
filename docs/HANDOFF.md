# Handoff: Blast Radius selector

Context for whoever continues this (human or AI). Read this, `README.md`,
`SCORING.md` (from the starter kit) and `docs/results.md` first. Project rule
(`CLAUDE.md`): check `docs/todo.md` before coding and tick items when done.

## The task in one paragraph

For each patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`),
print which of the 1,620 catalog tests to run, split over at most 8 workers. Cost
per commit = busiest worker's recorded seconds + 2 s per worker (0 if nothing is
run); a commit whose failing tests are not all selected adds **300 s**; the
container's CPU time is added. Score = middle of 3 runs. The final (cutoff
**Sunday 10:00 AM CST**) runs unseen commits, including kinds of change not in
practice, with ~1.5x as many commits and 4.5 min wall limit. Runtime: no network,
2 cores, 4 GB, read-only disk except a 64 MB `/tmp`, user `nobody`, 256 processes.
Rule to respect: **no table from a patch or kind of edit to the tests it breaks**;
maps built from the pinned commit (coverage, imports, fixtures) are fine. Top
entries are read by the organizers.

## Where things stand

| Version | Commit | Sample (local) | Practice board |
| --- | --- | ---: | ---: |
| Starter (all tests, 3 workers) | — | 1210.5 s | 1614.1 s |
| Coverage selector v2 + type profile | `2594d55` | 115.6 s, 0 misses | **186.3 s** = 174.774 simulated + ~11.5 CPU, 0 misses in all 3 runs |
| CPU cut (same selections) | `0919b56` | 115.6 s, 0 misses | not submitted when this was written; expected ~180 |

Validation with real outcomes (see `validation/`): 0 misses on all 364 synthetic
changes. Docker is not installed on the machine this was built on, so `make
sample` has **never** been run; the board submission is the only container test.
The `--prepare` build step (added in `0919b56`) has not been through a real build
yet: if it fails the selector falls back to computing the baseline at run time.

## How it works (files)

- `selector.py` — the program the image runs (stdlib only). Parses each patch
  (hunk lengths, not `---` prefixes, so SQL `--` comments are safe), reconstructs
  the patched files, and selects:
  - Python files: AST diff (`Differ`). Identical trees (comments, formatting,
    docstrings) → nothing. Edits in function bodies → tests that executed the
    changed statement (line coverage over its span; insertions use the previous
    statement or the enclosing block). Scoping changes (`bindings`) → whole
    function. Run-time writes to UPPER_CASE class attributes or module globals →
    also every test running a line naming them. `is`/`==` swaps proven equal by the
    type profile → nothing. Import-time edits → import-state fingerprint (below).
  - Data files (fixtures) → tests that opened them (setUpClass reads count for the
    whole class). Config files, renames, deletions of modules, syntax errors,
    unparsable patches → every test.
  - Schedules with integer ms, LPT over 1..8 workers plus move/swap refinement.
- `fingerprint.py` — imports a (patched) sqlglot from memory and hashes every
  module global and class attribute (closures, generated-function defaults,
  wrappers, dict/set contents order-insensitively; not function bodies). Runs as a
  fork server (`--serve`): baseline bytecode + state loaded once, one fork per
  commit, diff returned. Changed class attributes → tests instantiating the class,
  a subclass or a class of the same dialect unit; plain-data attributes are
  further intersected with tests running a line naming the attribute. Changed
  globals → tests running a line naming them. Import failure → every test.
- `dependencies.json.gz` (~1.9 MB) — the map. Built by `tools/build_map.py`
  (line coverage per test with setup/call/teardown/collection contexts, import-time
  lines and code objects, per-test class instantiation, dialect units, MRO, reads,
  listings, subprocess commands, statement spans, sources) and then
  `tools/build_profile.py` (operand-type profile of every `is`/`is not`/`==`/`!=`).
  The builder resets per-process caches before each test (generator dispatch
  cache, dialect registry, lazy module attributes, unittest `cache` dicts) so
  first-use work is charged to every test that depends on it.
- `Dockerfile` — copies the three files, runs `selector.py --prepare` at build
  (baseline import state + compiled modules, stamped with sources/Python hashes).
- `tools/mutants.py` — synthetic patches and their real outcomes; `tools/evaluate.py`
  — scores the selector on any set with answers; `tests/test_selector.py` — unit tests.

## Rebuilding the map

Needs Python 3.13 with `requirements.lock` plus `coverage==7.10.7`:

```sh
python3.13 -m venv venv && venv/bin/pip install -r requirements.lock coverage==7.10.7
git clone https://github.com/tobymao/sqlglot.git /tmp/sqlglot
git -C /tmp/sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
make map SOURCE=/tmp/sqlglot PYTHON=venv/bin/python   # ~3-5 min map + ~1.5 min profile
```

Always re-run `make test`, `make sample-local PYTHON=python3.13`, and
`tools/evaluate.py` on every `validation/` set after changing selection logic or
the map. Ship only with **0 misses** everywhere.

## Measurements that matter

- Scoring server CPU ≈ 1.7x slower than the labeling machine at running sqlglot
  tests; this Mac ≈ 2.4x faster than the labeling machine; the board's CPU for our
  selector was ~4x this Mac's.
- Local CPU (incl. children) after `0919b56`: 0.83 s on the 30 samples, 1.35 s on
  40 sample-like commits (was 1.65 s / 2.95 s). Each patched-import fork ≈ 0.15 s
  locally.
- TPC-DS execution (21.45 s recorded) failed on 4 of 21 commits that selected it
  in `mutants-1`; break-even for dropping it is ~7%.

## What was tried or measured and rejected (with reasons)

| Idea | Finding |
| --- | --- |
| Rewrite in Rust/C++ | Only the CPU term (~5 s after the cut) is affected; simulated seconds depend on which tests are picked. Also loses CPython's exact `ast`. |
| Drop expensive tests (≥2 s) | 2 misses on 30 samples, 12 on 255 mutations: +600 s / +3600 s. |
| Run cheap selected tests in the container | On the server tests run ~1.7x slower than recorded plus pytest/sqlglot startup; commits whose cheap tests fail pay for nothing. Net ≈ 0 on ~40 commits. |
| Run only the TPC-DS queries a change touches | Measured per-query coverage: of 42 commits selecting TPC-DS, 21 touch all 57 executed queries, 18 give no query-level line info (import-time, data, setup), only 4 touch a few queries (1 of those really fails). ~5-10 s on 40 commits. Trap: one real TPC-DS failure (sample `3utz`) goes through the pickling between processes; a harness must reproduce it. |
| Track dict keys read by attribute name | The big cases (`NUMERIC_ESCAPES`, `SHOW_PARSERS`) are passed whole into other code and read under other names, so name-based key tracking cannot narrow them. |
| Profile comparison values (`<` vs `<=`) | Boundary swaps were 2 of 285 changes (4.6 s total), and per-site outcome profiles are close to the "kind of edit → tests it breaks" rule. |
| Better worker packing | Already optimal on every sample selection. |

Excess over the oracle on 285 known changes (sample + mutants-1 + mutants-2):
function-body edits 718 s (tests that really run the edited code), import-time
class/module data 294 s, fixtures 51 s.

## Reasonable next steps (estimates for a ~40-commit practice set)

1. **Class-keyed dict narrowing** (~5-9 s, sound, ~1 h): when a changed dict/set
   attribute's changed keys are expression classes (`TRANSFORMS`,
   `EXPRESSION_METADATA`, ...), only tests that instantiated a changed key class (or
   a subclass) can see it. Needs the fingerprint child to report changed keys
   (baseline per-key hashes can be written at `--prepare` time).
2. Finer handling of import failures in dialect-only modules (rare; currently
   every test).
3. Targeted TPC-DS verification (see table) only if everything else is done.

## Pitfalls hit during development

- `pgrep -f "pattern"` in a wait loop matches the loop's own shell; use `[p]attern`.
- Guard `rm -rf` targets built from variables (`"${VAR:?}"/...`).
- Fingerprints must be order-insensitive for dicts/sets and skip unreachable
  generated classes, or identical imports look different.
- The dialect metaclass and lazy loaders run on first use per process; never trust
  coverage from only the first test that triggered them (the builder resets them).
- Pushing requires a GitHub personal access token entered in the macOS Terminal
  app (the in-app terminal cannot paste into hidden password prompts). Never paste
  tokens into chat.
