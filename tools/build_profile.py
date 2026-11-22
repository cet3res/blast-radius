"""Add an operand-type profile of sqlglot's identity and equality comparisons to the map.

Usage: python tools/build_profile.py SOURCE MAP_IN MAP_OUT [--workers 8]

Every single-operator `is`, `is not`, `==` and `!=` comparison in sqlglot is
rewritten (in memory, at import) into a call that records the types of its two
operands and then performs the original comparison. The unmodified test suite
runs once; the map then lists, per comparison site, the operand type pairs seen,
and for each type how it decides equality: by identity, by a builtin type, or by
Python code of its own.

This is a dynamic type profile of the pinned revision, like coverage: it holds no
patch, failure or test identities. The selector uses it to decide whether
swapping `is` and `==` at a site can change any comparison the tests evaluate.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PLUGIN = r'''
import ast, builtins, concurrent.futures, enum, importlib.abc, importlib.util, json, os, pickle, sys

SOURCE = os.environ["BLAST_SOURCE"]
SITES = []          # index -> (path, line, col)
SEEN = {}           # index -> set of (id(type a), id(type b))
TYPES = {}          # id -> type
# A comparison against None can use Expression's exact-type guard when both
# rich comparison methods are the pinned implementations, including the
# instance lookup used by Expression.__ne__. Keep only type information.
EXPR_NONE_OK = set()
EXPR_NONE_UNSAFE = set()
OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)


def __blast_cmp__(site, a, b, op):
    ta, tb = type(a), type(b)
    if (a is None) != (b is None):
        obj = b if a is None else a
        cls = type(obj)
        if cls.__module__.startswith("sqlglot.expressions."):
            core = sys.modules.get("sqlglot.expressions.core")
            base = getattr(core, "Expression", None)
            if (base is not None and cls.__eq__ is base.__eq__ and cls.__ne__ is base.__ne__
                    and cls.__getattribute__ is object.__getattribute__
                    and getattr(obj.__eq__, "__func__", None) is base.__eq__):
                EXPR_NONE_OK.add(id(cls))
            else:
                EXPR_NONE_UNSAFE.add(id(cls))
    TYPES[id(ta)] = ta
    TYPES[id(tb)] = tb
    seen = SEEN.get(site)
    if seen is None:
        seen = SEEN[site] = set()
    seen.add((id(ta), id(tb)))
    if op == 0:
        return a is b
    if op == 1:
        return a is not b
    if op == 2:
        return a == b
    return a != b


builtins.__blast_cmp__ = __blast_cmp__


class Rewrite(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], OPS):
            site = len(SITES)
            SITES.append((self.path, node.lineno, node.col_offset))
            op = [isinstance(node.ops[0], k) for k in OPS].index(True)
            call = ast.Call(
                func=ast.Name("__blast_cmp__", ast.Load()),
                args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(op)],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


class Loader(importlib.abc.SourceLoader):
    def __init__(self, path):
        self.path = path

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        with open(path, "rb") as f:
            return f.read()

    def source_to_code(self, data, path, *, _optimize=-1):
        relative = os.path.relpath(path, SOURCE).replace(os.sep, "/")
        tree = Rewrite(relative).visit(ast.parse(data, path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        base = os.path.join(SOURCE, *name.split("."))
        for candidate, package in ((os.path.join(base, "__init__.py"), True), (base + ".py", False)):
            if os.path.exists(candidate):
                return importlib.util.spec_from_file_location(
                    name, candidate, loader=Loader(candidate),
                    submodule_search_locations=[base] if package else None,
                )
        return None


sys.meta_path.insert(0, Finder())
sys.dont_write_bytecode = True


class Synchronous(concurrent.futures.Executor):
    """Pool tasks run here, so their comparisons are profiled too."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ProcessPoolExecutor = Synchronous


def describe(t):
    kinds = []
    for method in ("__eq__", "__ne__"):
        defining = next((k for k in t.__mro__ if method in k.__dict__), object)
        if defining is object:
            kinds.append("identity")
        elif defining.__module__ == "builtins":
            kinds.append("builtin")
        else:
            kinds.append("custom")
    # Equality and inequality must both be standard for a swap of either to be judged.
    kind = "custom" if "custom" in kinds else ("builtin" if "builtin" in kinds else "identity")
    if id(t) in EXPR_NONE_OK and id(t) not in EXPR_NONE_UNSAFE:
        kind = "expression_type_guard"
    unique = False
    if isinstance(t, type) and issubclass(t, enum.Enum):
        values = [m._value_ for m in t.__members__.values()]
        members = list(dict.fromkeys(t.__members__.values()))
        try:
            unique = len(members) == len(values) and len(set(values)) == len(values)
        except TypeError:
            unique = False
    return [f"{t.__module__}:{t.__qualname__}", kind, unique]


def pytest_sessionfinish(session, exitstatus):
    types = {i: describe(t) for i, t in TYPES.items()}
    out = {}
    for site, pairs in SEEN.items():
        path, line, col = SITES[site]
        entry = out.setdefault(path, {}).setdefault(f"{line}:{col}", [])
        for a, b in pairs:
            entry.append([types[a], types[b]])
    target = os.path.join(os.environ["BLAST_PROFILE_DIR"], f"profile-{os.getpid()}.json")
    with open(target, "w") as f:
        json.dump(out, f)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('map_in')
    parser.add_argument('map_out')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    with gzip.open(args.map_in, 'rt') as f:
        payload = json.load(f)
    work = Path(tempfile.mkdtemp(prefix='blast-profile-'))
    (work / 'blast_profile_plugin.py').write_text(PLUGIN)
    out_dir = work / 'out'
    out_dir.mkdir()
    env = dict(os.environ)
    env.update({
        'SKIP_INTEGRATION': '0', 'BLAST_SOURCE': str(source), 'BLAST_PROFILE_DIR': str(out_dir),
        'PYTHONPATH': os.pathsep.join([str(work), str(source)]), 'PYTHONDONTWRITEBYTECODE': '1',
        'PATH': str(Path(args.python).parent) + os.pathsep + env.get('PATH', ''),
    })
    command = [args.python, '-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider', '-p', 'blast_profile_plugin']
    if args.workers > 1:
        command += ['-n', str(args.workers)]
    done = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True)
    print(done.stdout[-1500:], file=sys.stderr)
    if done.returncode != 0 or 'passed' not in done.stdout:
        raise SystemExit('profiling run failed')

    types: dict[str, list] = {}
    sites: dict[str, dict[str, set]] = {}
    files = list(out_dir.glob('profile-*.json'))
    if not files:
        raise SystemExit('no profile written')
    for file in files:
        for path, entries in json.loads(file.read_text()).items():
            for site, pairs in entries.items():
                bucket = sites.setdefault(path, {}).setdefault(site, set())
                for a, b in pairs:
                    for described in (a, b):
                        known = types.get(described[0])
                        if known is None:
                            types[described[0]] = described
                        elif known != described:
                            types[described[0]] = [described[0], 'custom', False]  # same name, different classes
                    bucket.add((a[0], b[0]))
    names = sorted(types)
    number = {name: i for i, name in enumerate(names)}
    payload['compare_types'] = [types[n] for n in names]
    payload['compare'] = {
        path: {site: sorted([number[a], number[b]] for a, b in pairs) for site, pairs in sorted(entries.items())}
        for path, entries in sorted(sites.items())
    }
    with gzip.open(args.map_out, 'wt', compresslevel=9) as f:
        json.dump(payload, f, separators=(',', ':'))
    print(f'profile: {sum(len(e) for e in sites.values())} comparison sites, {len(names)} types, '
          f'{len(files)} processes', file=sys.stderr)


if __name__ == '__main__':
    main()
