"""Score the selector on a set of patches with known outcomes (sample or validation/).

    python tools/evaluate.py validation/mutants-1 --catalog sample/tests.json
    python tools/evaluate.py sample --catalog sample/tests.json --top 10

The set directory holds commits/*.patch and answers.json in the sample format
({"commits": {id: {"failing": [...], "kind": ...}}}). Prints simulated seconds,
the oracle (exactly the failing tests, best split), misses, the selector's CPU
(including its child processes), a per-kind breakdown and the most expensive
excess. Development only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def best_cost(durations: list[int]) -> int:
    """The scorer's cost of running these tests in the best split (LPT over 1..8 workers)."""
    if not durations:
        return 0
    best = None
    for k in range(1, 9):
        loads = [0] * k
        for d in sorted(durations, reverse=True):
            loads[loads.index(min(loads))] += d
        cost = max(loads) + 2000 * k
        best = cost if best is None else min(best, cost)
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('set_dir', type=Path)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'sample' / 'tests.json')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--top', type=int, default=0, help='list the N commits with the most excess')
    parser.add_argument('--explain', action='store_true', help='keep the selector explanation (stderr) per commit')
    args = parser.parse_args()

    answers = json.loads((args.set_dir / 'answers.json').read_text())['commits']
    catalog = json.loads(args.catalog.read_text())['tests']
    duration = {t['number']: round(t['seconds'] * 1000) for t in catalog}
    work = Path(tempfile.mkdtemp(prefix='blast-eval-'))
    try:
        (work / 'commits').mkdir()
        shutil.copy(args.catalog, work / 'tests.json')
        for commit in answers:
            shutil.copy(args.set_dir / 'commits' / f'{commit}.patch', work / 'commits' / f'{commit}.patch')
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        command = [args.python, '-B', str(ROOT / 'selector.py'), '--data', str(work)]
        if args.explain:
            command.append('--explain')
        done = subprocess.run(command, capture_output=True, text=True)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if done.returncode:
        raise SystemExit(done.stderr[-2000:])
    selected = {}
    for line in done.stdout.splitlines():
        row = json.loads(line)
        selected[row['commit']] = row['workers']
    explanations = {}
    for line in done.stderr.splitlines():
        head = line.split(':', 1)[0]
        if head in answers:
            explanations[head] = line

    total = oracle = 0
    misses = []
    kinds: dict[str, list[float]] = {}
    rows = []
    for commit, answer in answers.items():
        workers = [w for w in selected.get(commit, []) if w]
        chosen = {n for w in workers for n in w}
        cost = (max(sum(duration[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
        floor = best_cost([duration[n] for n in answer['failing']])
        missed = set(answer['failing']) - chosen
        total += cost + 300000 * bool(missed)
        oracle += floor
        if missed:
            misses.append((commit, sorted(missed)[:5]))
        bucket = kinds.setdefault(answer.get('kind', '?'), [0, 0, 0, 0])
        bucket[0] += 1
        bucket[1] += cost
        bucket[2] += floor
        bucket[3] += bool(missed)
        rows.append(((cost - floor) / 1000, commit, answer.get('kind', '?'), len(answer['failing']), len(chosen)))

    print(f'{len(answers)} commits: simulated {total / 1000:.1f} s, oracle {oracle / 1000:.1f} s, '
          f'misses {len(misses)}, selector CPU {cpu:.2f} s')
    for kind, (n, cost, floor, missed) in sorted(kinds.items()):
        print(f'  {kind:14} n={n:4}  cost={cost / 1000:8.1f}  oracle={floor / 1000:7.1f}  misses={missed}')
    for commit, tests in misses:
        print(f'MISS {commit}: {tests}')
    for excess, commit, kind, failing, chosen in sorted(rows, reverse=True)[:args.top]:
        note = explanations.get(commit, '')[:200]
        print(f'{excess:7.1f} s excess  {commit} {kind} failing={failing} selected={chosen}  {note}')


if __name__ == '__main__':
    main()
