"""Synthetic changes with real outcomes, to measure the selector beyond 30 samples.

    python tools/mutants.py generate SOURCE OUT_DIR --count 200 --seed 1
    python tools/mutants.py truth SOURCE OUT_DIR tests.json --workers 9

`generate` writes unified diffs against the pinned checkout: single-token code
mutations, comment lines, fixture edits, and combinations. `truth` applies each
one to a private copy of the checkout, runs the whole catalog with pytest, and
writes OUT_DIR/answers.json in the sample format. Development only: nothing here
is used by the submitted selector.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import difflib
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import tokenize

COMMENTS = [
    '# Cheap check first', '# Kept for backwards compatibility', '# Intentionally not cached',
    '# Mirrors the behavior of the reference implementation', '# Keep in sync with the generator',
    '# TODO: revisit', '# Order matters here', '# See the dialect docs', '# Fast path',
]
SWAPS = {
    '==': '!=', '!=': '==', '<': '<=', '<=': '<', '>': '>=', '>=': '>', 'and': 'or', 'or': 'and',
    'True': 'False', 'False': 'True', 'is': '==', '+': '-', '-': '+', 'in': 'not in',
    'None': '0', 'if': 'if not', 'not': '', 'any': 'all', 'all': 'any', 'min': 'max', 'max': 'min',
}


def py_files(source: Path):
    return sorted(p for p in (source / 'sqlglot').rglob('*.py') if p.name != '__main__.py')


def fixture_files(source: Path):
    return sorted(p for p in (source / 'tests' / 'fixtures').rglob('*') if p.is_file() and p.suffix in ('.sql', '.json', '.py', '.txt'))


def code_mutation(rng, source: Path, text: str):
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    lines = text.split('\n')
    candidates = []
    for tok in tokens:
        if tok.type == tokenize.OP and tok.string in SWAPS:
            candidates.append(tok)
        elif tok.type == tokenize.NAME and tok.string in SWAPS:
            candidates.append(tok)
        elif tok.type == tokenize.NUMBER and tok.string.isdigit():
            candidates.append(tok)
        elif tok.type == tokenize.STRING and len(tok.string) < 40 and tok.string[-1] in '"\'' and not tok.string.startswith(('"""', "'''", 'f', 'r', 'b')):
            candidates.append(tok)
    for _ in range(50):
        tok = rng.choice(candidates)
        (row, col), (erow, ecol) = tok.start, tok.end
        if row != erow:
            continue
        line = lines[row - 1]
        if tok.type == tokenize.NUMBER:
            new = str(int(tok.string) + rng.choice([1, -1])) if tok.string != '0' else '1'
        elif tok.type == tokenize.STRING:
            q = tok.string[-1]
            body = tok.string[1:-1]
            new = q + (body + 'x' if rng.random() < 0.5 or not body else body[:-1]) + q
        else:
            new = SWAPS[tok.string]
            if tok.string == 'not':
                new = ''
                if line[ecol:ecol + 1] == ' ':
                    ecol += 1
        mutated = line[:col] + new + line[ecol:]
        if mutated == line:
            continue
        candidate = '\n'.join(lines[: row - 1] + [mutated] + lines[row:])
        try:
            compile(candidate, 'x', 'exec')
        except SyntaxError:
            continue
        return candidate
    return None


def comment_mutation(rng, text: str):
    lines = text.split('\n')
    for _ in range(50):
        i = rng.randrange(len(lines))
        nxt = lines[i] if i < len(lines) else ''
        indent = nxt[: len(nxt) - len(nxt.lstrip())]
        candidate = '\n'.join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        try:
            compile(candidate, 'x', 'exec')
        except SyntaxError:
            continue
        return candidate
    return None


def fixture_mutation(rng, text: str):
    lines = text.split('\n')
    for _ in range(50):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip():
            continue
        choice = rng.random()
        if choice < 0.4:
            j = rng.randrange(len(line) + 1)
            line = line[:j] + rng.choice(['x', ' 1', '(', ',', 'X']) + line[j:]
        elif choice < 0.7 and len(line) > 1:
            j = rng.randrange(len(line))
            line = line[:j] + line[j + 1:]
        else:
            line = line.replace(' ', '  ', 1) if ' ' in line else line + ' '
        if line != lines[i]:
            lines[i] = line
            return '\n'.join(lines)
    return None


def line_mutation(rng, text: str):
    """Delete, duplicate or swap whole lines (kinds the samples do not show)."""
    lines = text.split('\n')
    for _ in range(80):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.strip().startswith('#'):
            continue
        op = rng.choice(['delete', 'duplicate', 'swap'])
        if op == 'delete':
            candidate = lines[:i] + lines[i + 1:]
        elif op == 'duplicate':
            candidate = lines[:i + 1] + [line] + lines[i + 1:]
        else:
            if i + 1 >= len(lines) or not lines[i + 1].strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent != len(lines[i + 1]) - len(lines[i + 1].lstrip()):
                continue
            candidate = lines[:i] + [lines[i + 1], line] + lines[i + 2:]
        candidate = '\n'.join(candidate)
        try:
            compile(candidate, 'x', 'exec')
        except SyntaxError:
            continue
        if candidate != text:
            return candidate
    return None


def docstring_mutation(rng, text: str):
    """Insert a line inside a triple-quoted string."""
    lines = text.split('\n')
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
              if t.type == tokenize.STRING and t.start[0] != t.end[0]]
    if not tokens:
        return None
    tok = rng.choice(tokens)
    row = rng.randrange(tok.start[0], tok.end[0])
    indent = lines[row][: len(lines[row]) - len(lines[row].lstrip())]
    candidate = '\n'.join(lines[:row] + [indent + rng.choice(COMMENTS)] + lines[row:])
    try:
        compile(candidate, 'x', 'exec')
    except SyntaxError:
        return None
    return candidate


def literal_entries(text: str):
    """Single-line entries of dict and set literals assigned at module or class level:
    (lineno, kind, node) where kind is 'dict' (node: (key, value)) or 'set' (node: element)."""
    found = []
    tree = ast.parse(text)

    def literals(value):
        for node in ast.walk(value):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if k is not None and k.lineno == v.end_lineno:
                        found.append((k.lineno, 'dict', (k, v)))
            elif isinstance(node, ast.Set):
                for e in node.elts:
                    if e.lineno == e.end_lineno:
                        found.append((e.lineno, 'set', e))

    def scan(body):
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                literals(stmt.value)
            elif isinstance(stmt, ast.ClassDef):
                scan(stmt.body)

    scan(tree.body)
    return found


def entry_mutation(rng, text: str):
    """Delete one entry line, alter one string in an entry, or swap two dict values."""
    entries = literal_entries(text)
    if not entries:
        return None
    lines = text.split('\n')
    for _ in range(20):
        lineno, kind, node = rng.choice(entries)
        line = lines[lineno - 1]
        how = rng.choice(['delete', 'string', 'swap'] if kind == 'dict' else ['delete', 'string'])
        new_lines = list(lines)
        if how == 'delete':
            first = node[0] if kind == 'dict' else node
            last = node[1] if kind == 'dict' else node
            segment = line[first.col_offset:last.end_col_offset]
            if line.strip().rstrip(',') != segment.strip():
                continue  # not alone on its line
            del new_lines[lineno - 1]
        elif how == 'string':
            strings = [n for part in (node if kind == 'dict' else (node,)) for n in ast.walk(part)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.lineno == lineno and n.value]
            if not strings:
                continue
            target = rng.choice(strings)
            quoted = line[target.col_offset:target.end_col_offset]
            if not quoted or quoted[-1] not in '"\'' or quoted.startswith(('f', 'b', 'r')):
                continue
            new_lines[lineno - 1] = line[:target.end_col_offset - 1] + 'X' + line[target.end_col_offset - 1:]
        else:
            others = [e for e in entries if e[1] == 'dict' and e[0] != lineno and e[2][1].lineno == e[0]]
            if not others:
                continue
            other_line, _, (ok, ov) = rng.choice(others)
            k, v = node
            if v.lineno != lineno:
                continue
            a = line[v.col_offset:v.end_col_offset]
            b = lines[other_line - 1][ov.col_offset:ov.end_col_offset]
            if a == b:
                continue
            new_lines[lineno - 1] = line[:v.col_offset] + b + line[v.end_col_offset:]
            o = lines[other_line - 1]
            new_lines[other_line - 1] = o[:ov.col_offset] + a + o[ov.end_col_offset:]
        new_text = '\n'.join(new_lines)
        try:
            ast.parse(new_text)
        except SyntaxError:
            continue
        if new_text != text:
            return new_text
    return None


def generate_entries(args):
    """Entry-level changes to dict and set literals (validation of key-level narrowing)."""
    rng = random.Random(args.seed)
    source = Path(args.source)
    out = Path(args.out)
    (out / 'commits').mkdir(parents=True, exist_ok=True)
    candidates = []
    for path in py_files(source):
        count = len(literal_entries(path.read_text()))
        if count:
            candidates.append((path, count))
    kinds = {}
    made = 0
    while made < args.count:
        path = rng.choices([p for p, _ in candidates], weights=[c for _, c in candidates])[0]
        text = path.read_text()
        new_text = entry_mutation(rng, text)
        if new_text is None:
            continue
        name = f'e{args.seed:02d}{made:04d}'
        (out / 'commits' / f'{name}.patch').write_text(diff(path.relative_to(source).as_posix(), text, new_text))
        kinds[name] = 'entry'
        made += 1
    (out / 'kinds.json').write_text(json.dumps(kinds, indent=1))


def diff(rel: str, old: str, new: str) -> str:
    body = ''.join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f'a/{rel}', f'b/{rel}', n=3))
    return f'diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n' + body


def generate(args):
    rng = random.Random(args.seed)
    source = Path(args.source)
    out = Path(args.out)
    (out / 'commits').mkdir(parents=True, exist_ok=True)
    pys = py_files(source)
    weights = [len(p.read_text().split('\n')) for p in pys]
    test_pys = sorted(p for p in (source / 'tests').rglob('test_*.py'))
    test_weights = [len(p.read_text().split('\n')) for p in test_pys]
    fixtures = fixture_files(source)
    kinds = {}
    made = 0
    while made < args.count:
        if args.extended:
            kind = rng.choices(['lines', 'testcode', 'docstring', 'code', 'multi'], weights=[40, 20, 15, 10, 15])[0]
        else:
            kind = rng.choices(['code', 'comment', 'fixture', 'multi'], weights=[50, 12, 25, 13])[0]
        parts = []
        used = set()

        def one(k):
            if k == 'fixture':
                path = rng.choice(fixtures)
                text = path.read_text()
                new = fixture_mutation(rng, text)
            elif k == 'lines':
                path = rng.choices(pys, weights=weights)[0]
                text = path.read_text()
                new = line_mutation(rng, text)
            elif k == 'testcode':
                path = rng.choices(test_pys, weights=test_weights)[0]
                text = path.read_text()
                new = code_mutation(rng, source, text) if rng.random() < 0.6 else line_mutation(rng, text)
            elif k == 'docstring':
                path = rng.choices(pys, weights=weights)[0]
                text = path.read_text()
                new = docstring_mutation(rng, text)
            else:
                path = rng.choices(pys, weights=weights)[0]
                text = path.read_text()
                new = code_mutation(rng, source, text) if k == 'code' else comment_mutation(rng, text)
            rel = path.relative_to(source).as_posix()
            if new is None or rel in used:
                return None
            used.add(rel)
            return diff(rel, text, new)

        if kind == 'multi':
            pool = ['lines', 'testcode', 'docstring', 'fixture'] if args.extended else ['code', 'comment', 'fixture']
            for k in rng.sample(pool, 2):
                part = one(k)
                if part:
                    parts.append(part)
            if len(parts) < 2:
                continue
        else:
            part = one(kind)
            if not part:
                continue
            parts.append(part)
        name = f'm{args.seed:02d}{made:04d}'
        (out / 'commits' / f'{name}.patch').write_text(''.join(parts))
        kinds[name] = kind
        made += 1
    (out / 'kinds.json').write_text(json.dumps(kinds, indent=1))


PLUGIN = '''
import json, os
_failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _failed.add('<collect>' + report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ['BLAST_OUT'], 'w') as f:
        json.dump(sorted(_failed), f)
'''


def run_one(task):
    worker_dir, patch_path, out_file, python, timeout = task
    worker_dir = Path(worker_dir)
    patch = Path(patch_path).read_text()
    applied = subprocess.run(['git', 'apply', '--whitespace=nowarn', str(patch_path)], cwd=worker_dir, capture_output=True, text=True)
    if applied.returncode:
        return patch_path, None, 'apply failed: ' + applied.stderr
    env = dict(os.environ)
    env.update({
        'SKIP_INTEGRATION': '0', 'PYTHONPATH': str(worker_dir) + os.pathsep + str(Path(out_file).parent),
        'BLAST_OUT': str(out_file), 'PATH': str(Path(python).parent) + os.pathsep + env.get('PATH', ''),
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    try:
        done = subprocess.run(
            [python, '-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider', '-p', 'blast_plugin',
             '--timeout', str(timeout)],
            cwd=worker_dir, env=env, capture_output=True, text=True, timeout=1800)
        status = done.returncode
    except subprocess.TimeoutExpired:
        status = 'timeout'
    finally:
        subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=worker_dir)
        subprocess.run(['git', 'clean', '-qfd', '--', 'sqlglot', 'tests'], cwd=worker_dir)
    try:
        failed = json.loads(Path(out_file).read_text())
    except (OSError, ValueError):
        return patch_path, None, f'no result (status {status})'
    return patch_path, failed, status


def truth(args):
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    catalog = json.loads(Path(args.catalog).read_text())['tests']
    number = {t['id']: t['number'] for t in catalog}
    files = {}
    for t in catalog:
        files.setdefault(t['file'], []).append(t['number'])
    answers_path = out / 'answers.json'
    answers = json.loads(answers_path.read_text()) if answers_path.exists() else {'commits': {}}
    kinds = json.loads((out / 'kinds.json').read_text())
    patches = [p for p in sorted((out / 'commits').glob('*.patch')) if p.stem not in answers['commits']]
    work = Path(tempfile.mkdtemp(prefix='blast-truth-'))
    (work / 'blast_plugin.py').write_text(PLUGIN)
    workers = []
    for i in range(args.workers):
        d = work / f'w{i}'
        subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(d), 'HEAD'], cwd=source, check=True)
        workers.append(d)
    free = list(workers)
    try:
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            pending = {}
            queue = list(patches)
            while queue or pending:
                while queue and free:
                    d = free.pop()
                    p = queue.pop(0)
                    task = (str(d), str(p), str(work / f'{p.stem}.json'), args.python, args.timeout)
                    pending[pool.submit(run_one, task)] = d
                done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    free.append(pending.pop(fut))
                    patch_path, failed, status = fut.result()
                    stem = Path(patch_path).stem
                    if failed is None:
                        print(f'{stem}: {status}', file=sys.stderr)
                        continue
                    nums = set()
                    for nodeid in failed:
                        if nodeid.startswith('<collect>'):
                            nums.update(files.get(nodeid[len('<collect>'):], []))
                        elif nodeid in number:
                            nums.add(number[nodeid])
                    answers['commits'][stem] = {'failing': sorted(nums), 'kind': kinds.get(stem, '?')}
                    answers_path.write_text(json.dumps(answers, indent=1))
                    print(f'{stem}: {len(nums)} failing (status {status})', file=sys.stderr)
    finally:
        for d in workers:
            subprocess.run(['git', 'worktree', 'remove', '--force', str(d)], cwd=source)
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('generate')
    g.add_argument('source')
    g.add_argument('out')
    g.add_argument('--count', type=int, default=100)
    g.add_argument('--seed', type=int, default=1)
    g.add_argument('--extended', action='store_true')
    g.add_argument('--entries', action='store_true', help='entry-level changes to dict and set literals')
    t = sub.add_parser('truth')
    t.add_argument('source')
    t.add_argument('out')
    t.add_argument('catalog')
    t.add_argument('--workers', type=int, default=8)
    t.add_argument('--python', default=sys.executable)
    t.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.cmd == 'generate':
        generate_entries(args) if args.entries else generate(args)
    else:
        truth(args)


if __name__ == '__main__':
    main()
