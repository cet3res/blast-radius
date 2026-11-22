"""Per-test value profiles for edits to default arguments and to `and`/`or` operators.

Usage: python tools/build_values.py SOURCE MAP.gz OUT.gz

Run with the environment of tools/build_map.py (Python 3.13, requirements.lock).
SOURCE must be an unmodified checkout at the pinned revision; MAP.gz supplies the
test list. Nothing here reads patches or answers.

Default arguments. For every function stored in a sqlglot module or class (one
function object per code object), each call is watched at entry (sys.monitoring
PY_START): a parameter whose value is the very object stored as its default was
not supplied by the caller (or was supplied as that same object). A changed
default can only change calls like these, so only tests that made one can see it.

`and` / `or`. Inside function bodies, every tree of boolean operators used for its
truth value (an `if`/`while`/`assert`/conditional test, a comprehension filter, under
`not`) whose leaf operands are free of side effects (names, attributes, subscripts,
constants, `is` comparisons or comparisons with a constant, `not`, and calls to
isinstance/len/... or methods like upper and get) is rewritten, in memory, into a
call that computes exactly Python's value and then, for the record only, the truth of
every leaf, including those short-circuiting skipped ('E' when one raises). An edit
that only regroups or flips the operators over the same leaves changes a test only
if one of the truth vectors that test produced evaluates differently under the new
tree (or reaches a raising leaf); the selector checks exactly that.

Lookups made while collecting a test file are charged to its tests, during a setup
phase (setUpClass) to the class's tests, as in tools/build_map.py. Caches that hide
first-use work are reset before every test.
"""
from __future__ import annotations

import ast
import builtins
import concurrent.futures
import gzip
import importlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import pkgutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
PURE_CALLS = {'isinstance', 'issubclass', 'len', 'callable', 'type', 'bool', 'id'}
PURE_METHODS = {'upper', 'lower', 'startswith', 'endswith', 'strip', 'isdigit', 'isupper', 'islower', 'isidentifier',
                'get'}

context = ['<import>']
SITES: list[tuple[str, int, int]] = []
VECTORS: dict[int, dict[str, set]] = {}  # site -> truth vector of the leaves ('T'/'F'/'E') -> contexts
OPAQUE: dict[int, set] = {}              # site -> contexts where a leaf after the first raised
MISSING = object()


def constant_like(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(isinstance(e, ast.Constant) for e in node.elts)
    return False


def pure(node) -> bool:
    """No side effects when evaluated (beyond raising, which is recorded)."""
    for n in ast.walk(node):
        if isinstance(n, (ast.Name, ast.Attribute, ast.Subscript, ast.Constant, ast.UnaryOp, ast.BoolOp, ast.Slice,
                          ast.Tuple, ast.expr_context, ast.boolop, ast.unaryop, ast.cmpop)):
            continue
        if isinstance(n, ast.Compare):
            identity = all(isinstance(op, (ast.Is, ast.IsNot)) for op in n.ops)
            if identity or (len(n.ops) == 1 and (constant_like(n.left) or constant_like(n.comparators[0]))):
                continue
            return False
        if isinstance(n, ast.Call) and not n.keywords and not any(isinstance(a, ast.Starred) for a in n.args):
            f = n.func
            if isinstance(f, ast.Name) and f.id in PURE_CALLS:
                continue
            if isinstance(f, ast.Attribute) and f.attr in PURE_METHODS:
                continue
        return False
    return True


def __blast_tree__(site, shape, *thunks):
    """The value of a tree of `and`/`or` over leaf thunks, exactly as Python computes it,
    then the truth of every leaf (those short-circuiting skipped too) for the record."""
    values = [MISSING] * len(thunks)

    def leaf(i):
        if values[i] is MISSING:
            values[i] = thunks[i]()
        return values[i]

    def value(node):
        if isinstance(node, int):
            return leaf(node)
        is_or, kids = node
        result = None
        for kid in kids:
            result = value(kid)
            if bool(result) is is_or:
                return result
        return result

    try:
        result = value(shape)
    except BaseException:
        if values[0] is not MISSING:  # another shape could have stopped before the raising leaf
            OPAQUE.setdefault(site, set()).add(context[0])
        raise
    vector = []
    for i, thunk in enumerate(thunks):
        if values[i] is MISSING:
            try:
                vector.append('T' if thunk() else 'F')
            except BaseException:  # noqa: BLE001 - another shape would raise here
                vector.append('E')
        else:
            vector.append('T' if values[i] else 'F')
    VECTORS.setdefault(site, {}).setdefault(''.join(vector), set()).add(context[0])
    return result


builtins.__blast_tree__ = __blast_tree__


class Rewrite(ast.NodeTransformer):
    """Boolean operators in truth-value positions inside function bodies."""

    def __init__(self, path):
        self.path = path
        self.depth = 0  # function nesting

    def visit_FunctionDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self.depth += 1
        node.body = [self.visit(s) for s in node.body]
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit(node.args)
        self.depth += 1
        node.body = self.visit(node.body)
        self.depth -= 1
        return node

    def truth(self, node):
        """Rewrite an expression whose truth value alone is used."""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            node.operand = self.truth(node.operand)
            return node
        if isinstance(node, ast.BoolOp):
            leaves = []

            def shape(n):
                if isinstance(n, ast.BoolOp):
                    return (isinstance(n.op, ast.Or), tuple(shape(v) for v in n.values))
                leaves.append(n)
                return len(leaves) - 1

            tree = shape(node)
            if self.depth and all(pure(leaf) for leaf in leaves):
                thunks = [ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
                                                        defaults=[]), body=self.truth(leaf)) for leaf in leaves]
                site = len(SITES)
                SITES.append((self.path, node.lineno, node.col_offset))
                call = ast.Call(func=ast.Name('__blast_tree__', ast.Load()),
                                args=[ast.Constant(site), ast.Constant(tree), *thunks], keywords=[])
                return ast.copy_location(call, node)
            node.values = [self.truth(v) for v in node.values]  # operands still count for their truth
            return node
        return self.visit(node)

    def visit_If(self, node):
        node.test = self.truth(node.test)
        node.body = [self.visit(s) for s in node.body]
        node.orelse = [self.visit(s) for s in node.orelse]
        return node

    visit_While = visit_If

    def visit_Assert(self, node):
        node.test = self.truth(node.test)
        if node.msg is not None:
            node.msg = self.visit(node.msg)
        return node

    def visit_IfExp(self, node):
        node.test = self.truth(node.test)
        node.body = self.visit(node.body)
        node.orelse = self.visit(node.orelse)
        return node

    def visit_comprehension(self, node):
        node.target = self.visit(node.target)
        node.iter = self.visit(node.iter)
        node.ifs = [self.truth(i) for i in node.ifs]
        return node

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            node.operand = self.truth(node.operand)
            return node
        return self.generic_visit(node)


SOURCE = None


class Loader(importlib.abc.SourceLoader):
    def __init__(self, path):
        self.path = path

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        with open(path, 'rb') as f:
            return f.read()

    def source_to_code(self, data, path, *, _optimize=-1):
        relative = os.path.relpath(path, SOURCE).replace(os.sep, '/')
        tree = Rewrite(relative).visit(ast.parse(data, path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, 'exec', dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != 'sqlglot' and not name.startswith('sqlglot.'):
            return None
        base = os.path.join(SOURCE, *name.split('.'))
        for candidate, package in ((os.path.join(base, '__init__.py'), True), (base + '.py', False)):
            if os.path.exists(candidate):
                return importlib.util.spec_from_file_location(
                    name, candidate, loader=Loader(candidate), submodule_search_locations=[base] if package else None)
        return None


def main():
    global SOURCE
    source, map_path, out_path = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3])
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        raise SystemExit('Wrong source revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'sqlglot', 'tests']):
        raise SystemExit('Source must be unmodified')
    SOURCE = str(source)
    with gzip.open(map_path, 'rt') as f:
        ids = json.load(f)['tests']
    index = {test_id: i for i, test_id in enumerate(ids)}
    wanted = set(ids)
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, Finder())

    from build_map import TracedExecutor
    import unittest  # noqa: F401 - sqlglot's test-mode validation, as under pytest
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):
        if info.name != 'sqlglot.__main__':
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - optional dependencies
                pass
    from sqlglot.dialects.dialect import Dialect
    Dialect.classes
    concurrent.futures.ProcessPoolExecutor = TracedExecutor

    # Functions with defaults, one function object per code object.
    functions: dict[object, list] = {}

    def add(fn):
        while hasattr(fn, '__wrapped__') and getattr(fn, '__code__', None) is None:
            fn = fn.__wrapped__
        code = getattr(fn, '__code__', None)
        if code is None or not code.co_filename.startswith(SOURCE):
            return
        if fn.__defaults__ or fn.__kwdefaults__:
            bucket = functions.setdefault(code, [])
            if all(f is not fn for f in bucket):
                bucket.append(fn)

    def scan(value):
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, property):
            for part in (value.fget, value.fset, value.fdel):
                if part is not None:
                    add(part)
            return
        if callable(value) and hasattr(value, '__code__'):
            add(value)
            inner = getattr(value, '__wrapped__', None)
            if inner is not None and hasattr(inner, '__code__'):
                add(inner)

    seen_classes = set()

    def scan_class(cls):
        if id(cls) in seen_classes or not str(getattr(cls, '__module__', '')).startswith('sqlglot'):
            return
        seen_classes.add(id(cls))
        for value in list(vars(cls).values()):
            if isinstance(value, type):
                scan_class(value)
            else:
                scan(value)
        for sub in type.__subclasses__(cls) if isinstance(cls, type) else ():
            scan_class(sub)

    for name in sorted(sys.modules):
        if name == 'sqlglot' or name.startswith('sqlglot.'):
            for value in list(vars(sys.modules[name]).values()):
                if isinstance(value, type):
                    scan_class(value)
                else:
                    scan(value)

    watched = {}  # code -> (key, [(param, default object)])
    for code, fns in functions.items():
        if len(fns) != 1:
            continue  # same code, several function objects: defaults may differ
        fn = fns[0]
        params = []
        positional = code.co_varnames[:code.co_argcount]
        for name, default in zip(positional[len(positional) - len(fn.__defaults__ or ()):], fn.__defaults__ or ()):
            params.append((name, default))
        for name, default in (fn.__kwdefaults__ or {}).items():
            params.append((name, default))
        relative = os.path.relpath(code.co_filename, SOURCE).replace(os.sep, '/')
        watched[code] = (f'{relative}:{code.co_firstlineno}:{code.co_name}', params)

    used: dict[tuple, set] = {}
    done_here: set = set()
    mon = sys.monitoring
    tool = 4

    missing = object()
    recorded: dict[object, set] = {}  # code -> parameters recorded in the current context

    def on_start(code, offset):
        entry = watched.get(code)
        if entry is None:
            return mon.DISABLE
        key, params = entry
        local = sys._getframe(1).f_locals
        done = recorded.setdefault(code, set())
        for name, default in params:
            if name not in done and local.get(name, missing) is default:
                used.setdefault((key, name), set()).add(context[0])
                done.add(name)
        if len(done) == len(params):
            return mon.DISABLE  # nothing more to learn here until the context changes
        return None

    def switch(name):
        context[0] = name
        recorded.clear()
        mon.restart_events()

    mon.use_tool_id(tool, 'blast-radius-defaults')
    mon.register_callback(tool, mon.events.PY_START, on_start)
    for code in watched:
        mon.set_local_events(tool, code, mon.events.PY_START)
    print(f'{len(watched)} functions with defaults watched, {len(SITES)} boolean sites rewritten', flush=True)

    import sqlglot.generator as generator_module
    import sqlglot.dialects as dialects_package
    import sqlglot.optimizer as optimizer_package
    from sqlglot.dialects import dialect as dialect_module
    builtin_dialects = set(getattr(dialect_module, 'DIALECT_MODULE_NAMES', ()))
    registry = getattr(type(Dialect), '_classes', {})

    def reset_lazy_state():
        for key in list(registry):
            if key in builtin_dialects:
                del registry[key]
        for name in list(getattr(dialects_package, 'MODULE_BY_ATTRIBUTE', {})):
            dialects_package.__dict__.pop(name, None)
        for name in list(getattr(optimizer_package, '_LAZY_ATTRS', {})):
            optimizer_package.__dict__.pop(name, None)
        getattr(generator_module, '_DISPATCH_CACHE', {}).clear()

    outcomes = {}

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            items[:] = [i for i in items if i.nodeid in wanted]

        @pytest.hookimpl(hookwrapper=True)
        def pytest_make_collect_report(self, collector):
            if isinstance(collector, pytest.Module):
                switch('<collect>' + collector.nodeid)
                yield
                switch('<between>')
            else:
                yield

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            reset_lazy_state()
            if item.cls is not None and isinstance(getattr(item.cls, 'cache', None), dict):
                item.cls.cache.clear()
            switch(item.nodeid + '|s')
            yield
            switch('<between>')

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            switch(item.nodeid)
            yield
            switch('<between>')

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_teardown(self, item, nextitem):
            switch(item.nodeid + '|t')
            yield
            switch('<between>')

        def pytest_runtest_logreport(self, report):
            if report.failed:
                outcomes[report.nodeid] = 'failed'
            elif report.when == 'call':
                outcomes.setdefault(report.nodeid, 'passed')

    pytest.main(['tests', '-q', '-p', 'no:cacheprovider'], plugins=[Plugin()])
    context[0] = '<end>'
    for code in watched:
        mon.set_local_events(tool, code, 0)
    mon.register_callback(tool, mon.events.PY_START, None)
    mon.free_tool_id(tool)

    by_file: dict[str, set[int]] = {}
    by_class: dict[str, set[int]] = {}
    for test_id in ids:
        by_file.setdefault(test_id.split('::')[0], set()).add(index[test_id])
        by_class.setdefault(test_id.rsplit('::', 1)[0], set()).add(index[test_id])

    def resolve(ctx):
        if ctx.startswith('<collect>'):
            return by_file.get(ctx[len('<collect>'):], set())
        if ctx.endswith('|s'):
            return by_class.get(ctx[:-2].rsplit('::', 1)[0], set())
        if ctx.endswith('|t'):
            ctx = ctx[:-2]
        return {index[ctx]} if ctx in index else set()

    sets: list[list[int]] = []
    set_ids: dict[frozenset, int] = {}

    def set_id(contexts) -> int | None:
        tests = frozenset(t for ctx in contexts for t in resolve(ctx))
        if not tests:
            return None
        if tests not in set_ids:
            set_ids[tests] = len(sets)
            sets.append(sorted(tests))
        return set_ids[tests]

    defaults = {}
    for key, params in watched.values():
        entry = {}
        for name, _ in params:
            sid = set_id(used.get((key, name), ()))
            if sid is not None:
                entry[name] = sid
        defaults[key] = entry
    boolops = {}
    for site, (path, line, col) in enumerate(SITES):
        vectors = {}
        for vector, contexts in VECTORS.get(site, {}).items():
            sid = set_id(contexts)
            if sid is not None:
                vectors[vector] = sid
        boolops[f'{path}:{line}:{col}'] = {'vectors': vectors, 'opaque': set_id(OPAQUE.get(site, ()))}
    broken = [index[t] for t in ids if outcomes.get(t) != 'passed']
    with gzip.open(out_path, 'wt', compresslevel=9) as f:
        json.dump({'version': 1, 'revision': PIN, 'sets': sets, 'defaults': defaults, 'boolops': boolops,
                   'broken': broken}, f, separators=(',', ':'))
    print(f'{len(defaults)} functions, {sum(len(e) for e in defaults.values())} default-bound parameters; '
          f'{len(SITES)} boolean expressions ({len(VECTORS)} reached); {len(sets)} test sets; '
          f'{len(broken)} tests not passing: ' + ', '.join(ids[i] for i in broken[:10]))


if __name__ == '__main__':
    main()
