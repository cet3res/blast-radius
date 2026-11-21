# Priorities

- [x] Inspect starter kit and scoring rules.
- [x] Implement integer-millisecond worker optimization and conservative fallback.
- [x] Generate permitted coverage and fixture dependency maps from pinned sqlglot.
- [x] Validate selector against sample scorer and focused regression tests.
- [x] Map v2: line-level coverage per test phase, setUpClass reads charged to the class, import-time lines, class instantiation, dialect units, statement spans.
- [x] Reset per-process caches and lazy loaders before each test while mapping (dispatch cache, dialect registry, lazy module attributes, unittest `cache` dicts).
- [x] Selector v2: syntax-tree diff (comment/whitespace/docstring edits select nothing), statement-level selection in function bodies.
- [x] Import-time edits judged by fingerprinting the patched import state in a child process.
- [x] Shared-state writes in run-time code widen the selection to every reader.
- [x] Synthetic mutation harness (`tools/mutants.py`) with real test outcomes.
- [x] Operand-type profile of `is`/`==` comparisons; skip provably equivalent swaps (sample 165.8 s -> 115.6 s).
- [x] Validate on 364 mutations with real outcomes (sample-like, unseen kinds, swaps): 0 misses.
- [x] Cut selector CPU about 50% (board run 1: 11.5 s CPU): baseline prepared at build, fork server, targeted compile, memoized scans.
- [ ] Verify Docker image under competition limits (requires Docker; not installed here).
- [ ] Rebuild the map on Linux AMD64 in the pinned image and compare with the macOS map.
- [x] Handoff notes (`docs/HANDOFF.md`), validation sets with outcomes (`validation/`), `tools/evaluate.py`.
- [ ] Submit `0919b56` (CPU cut) and record the board result in `docs/results.md`.
- [ ] Class-keyed dict narrowing for import-time dict/set changes (estimated 5-9 s on ~40 commits).
