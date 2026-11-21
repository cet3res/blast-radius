# Validation sets

Synthetic patches against the pinned sqlglot, each applied to a clean checkout with
the whole catalog run under pytest (`SKIP_INTEGRATION=0`) to record which tests
really fail. Format matches `sample/`: `commits/<id>.patch` and `answers.json`.

**The selector never reads this folder** (it is not copied into the image). It
exists only to measure misses and cost beyond the 30 sample commits:

```sh
python tools/evaluate.py validation/mutants-1 --catalog sample/tests.json
```

| Set | Commits | How generated |
| --- | ---: | --- |
| `mutants-1` | 135 | `tools/mutants.py generate SOURCE OUT --count 240 --seed 1` (sample-like: operator/constant flips, comment lines, fixture edits, combinations); first 135 with outcomes |
| `mutants-2-extended` | 120 | `... --count 120 --seed 2 --extended` (line delete/duplicate/swap, test-file edits, docstring edits, combinations) |
| `swaps` | 109 | `is`/`==` and `is not`/`!=` swaps at comparison sites from the type profile: 89 the selector proves equivalent, 20 controls |

Outcomes come from `tools/mutants.py truth SOURCE OUT sample/tests.json --workers N`
(about 100 CPU-seconds per patch).
