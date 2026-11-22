"""Which keys of sqlglot's class-level and module-level dicts and sets each test reads.

Usage: python tools/build_keys.py SOURCE MAP.gz OUT.gz

Run with the environment of tools/build_map.py (Python 3.13, requirements.lock).
SOURCE must be an unmodified checkout at the pinned revision; MAP.gz supplies the
test list. Nothing here reads patches or answers.

After every sqlglot module is imported, each dict, set and frozenset stored as a class
attribute or module global is replaced by a tracking copy (same type family, same
contents, same order). While the catalog runs, every lookup (`k in x`, `x[k]`,
`x.get(k)`, hits and misses alike) is recorded with its key, and any other use
(iteration, length, copies, comparisons, set algebra, pickling) as a full read.
Generator dispatch tables are built from `TRANSFORMS`; lookups in them are recorded
as lookups of the `TRANSFORMS` object they came from.

A container is narrowable only when nothing but its owners' namespaces refers to it
(checked with gc), the tests never mutate it, and, for sets, no run-time code hands
it to C routines that read a set's table directly (`set(x)`, `frozenset(x)`,
`a.union(x)`, `a.issubset(x)`...; found by a static scan, through local aliases and
one level of parameters). Lookups made while collecting a test file are charged to
the file's tests, during a setup phase (setUpClass) to the class's tests, as in
tools/build_map.py. Tests whose outcome differs under tracking are listed as
"broken" and selected for any narrowed change.
"""
from __future__ import annotations

import ast
import concurrent.futures
import gc
import gzip
import importlib
import json
import os
from pathlib import Path
import pkgutil
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
UPPER = re.compile(r'^_?[A-Z][A-Z0-9_]*$')
BYPASS_METHODS = {
    'union', 'intersection', 'difference', 'symmetric_difference', 'issubset', 'issuperset', 'isdisjoint',
    'update', 'intersection_update', 'difference_update', 'symmetric_difference_update',
}

context = ['<import>']
suppressed: set[int] = set()
lookups: dict[int, dict] = {}   # id(container) -> key -> contexts
fulls: dict[int, set] = {}      # id(container) -> contexts
mutated: set[int] = set()


def record(container, key):
    keys = lookups.get(id(container))  # copies (pickle, copy) are not registered: ignored
    if keys is None or id(container) in suppressed:
        return
    try:
        keys.setdefault(key, set()).add(context[0])
    except TypeError:  # unhashable: the lookup itself raises; count it as a full read
        fulls[id(container)].add(context[0])


def record_full(container):
    if id(container) in fulls and id(container) not in suppressed:
        fulls[id(container)].add(context[0])


def full(method):
    def wrapper(self, *args, **kwargs):
        record_full(self._source if hasattr(type(self), '_derived') else self)
        return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    return wrapper


def mutating(method):
    def wrapper(self, *args, **kwargs):
        target = self._source if hasattr(type(self), '_derived') else self
        mutated.add(id(target))
        record_full(target)
        return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    return wrapper


def tracked_class(base, name, lookup_methods, read_methods, write_methods):
    namespace = {'__slots__': ()}
    for method in lookup_methods:
        original = getattr(base, method)
        if method == 'get':
            def get(self, key, default=None, _original=original):
                record(self, key)
                return _original(self, key, default)
            namespace['get'] = get
        else:
            def lookup(self, key, _original=original):
                record(self, key)
                return _original(self, key)
            lookup.__name__ = method
            namespace[method] = lookup
    for method in read_methods:
        if hasattr(base, method):
            namespace[method] = full(getattr(base, method))
    for method in write_methods:
        if hasattr(base, method):
            namespace[method] = mutating(getattr(base, method))
    if base is frozenset:
        namespace['__hash__'] = full(frozenset.__hash__)
    return type(name, (base,), namespace)


DICT_READS = ('__iter__', 'keys', 'values', 'items', '__len__', '__eq__', '__ne__', '__repr__', 'copy', '__or__',
              '__ror__', '__reversed__', '__reduce__', '__reduce_ex__', '__sizeof__', '__format__', '__str__')
DICT_WRITES = ('__setitem__', '__delitem__', 'pop', 'popitem', 'setdefault', 'update', 'clear', '__ior__')
SET_READS = ('__iter__', '__len__', '__eq__', '__ne__', '__lt__', '__le__', '__gt__', '__ge__', '__or__', '__ror__',
             '__and__', '__rand__', '__sub__', '__rsub__', '__xor__', '__rxor__', 'union', 'intersection',
             'difference', 'symmetric_difference', 'issubset', 'issuperset', 'isdisjoint', 'copy', '__repr__',
             '__reduce__', '__reduce_ex__', '__sizeof__', '__format__', '__str__')
SET_WRITES = ('add', 'discard', 'remove', 'pop', 'clear', 'update', 'intersection_update', 'difference_update',
              'symmetric_difference_update', '__ior__', '__iand__', '__isub__', '__ixor__')

TrackedDict = tracked_class(dict, 'TrackedDict', ('__getitem__', '__contains__', 'get'), DICT_READS, DICT_WRITES)
TrackedSet = tracked_class(set, 'TrackedSet', ('__contains__',), SET_READS, SET_WRITES)
TrackedFrozenSet = tracked_class(frozenset, 'TrackedFrozenSet', ('__contains__',), SET_READS, ())


class DerivedDict(TrackedDict):
    """A dispatch table built from a tracked TRANSFORMS: its uses count as uses of that."""
    __slots__ = ('_source',)
    _derived = True

    def __getitem__(self, key):
        record(self._source, key)
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        record(self._source, key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        record(self._source, key)
        return dict.get(self, key, default)


def static_unsafe_names(sources: dict[str, str]) -> set[str]:
    """Upper-case names whose value run-time code may hand to set routines that read a set
    directly (set(x), frozenset(x), a.union(x)...): their sets cannot be narrowed. Test code included."""
    unsafe: set[str] = set()
    passes: dict[str, set] = {}  # function name -> parameter positions/names reaching a bypass

    def names_of(expr, aliases):
        if isinstance(expr, ast.Attribute) and UPPER.match(expr.attr):
            return {expr.attr}
        if isinstance(expr, ast.Name):
            if UPPER.match(expr.id):
                return {expr.id}
            return set(aliases.get(expr.id, ()))
        if isinstance(expr, ast.Starred):
            return names_of(expr.value, aliases)
        if isinstance(expr, (ast.IfExp,)):
            return names_of(expr.body, aliases) | names_of(expr.orelse, aliases)
        if isinstance(expr, ast.BoolOp):
            return set().union(*(names_of(v, aliases) for v in expr.values))
        return set()

    def call_name(func):
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    functions = []
    for path, text in sources.items():
        if not path.startswith(('sqlglot/', 'tests/')) or not path.endswith('.py'):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                functions.append(node)

    for fn in functions:
        aliases: dict[str, set] = {}
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
        for p in params:
            aliases[p] = {'<param:' + p + '>'}
        body = fn.body if isinstance(fn.body, list) else [fn.body]
        nodes = [n for stmt in body for n in ast.walk(stmt)]
        for node in nodes:
            if isinstance(node, ast.Assign):
                found = names_of(node.value, aliases)
                for target in node.targets:
                    if isinstance(target, ast.Name) and found:
                        aliases.setdefault(target.id, set()).update(found)
            elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
                aliases.setdefault(node.target.id, set()).update(names_of(node.value, aliases))
        name = getattr(fn, 'name', '<lambda>')
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            callee = call_name(node.func)
            if callee in ('set', 'frozenset') or (isinstance(node.func, ast.Attribute) and callee in BYPASS_METHODS):
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    for found in names_of(arg, aliases):
                        if found.startswith('<param:'):
                            passes.setdefault(name, set()).add(found[7:-1])
                        else:
                            unsafe.add(found)
    # One level of calls: an upper-case value passed to a parameter that reaches a bypass.
    positions: dict[str, set] = {}
    for fn in functions:
        name = getattr(fn, 'name', '<lambda>')
        if name in passes:
            params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
            offset = 1 if params and params[0] in ('self', 'cls') else 0
            for p in passes[name]:
                if p in params:
                    positions.setdefault(name, set()).add(params.index(p) - offset)
                positions.setdefault(name, set()).add(p)
    for fn in functions:
        aliases = {}
        body = fn.body if isinstance(fn.body, list) else [fn.body]
        for node in (n for stmt in body for n in ast.walk(stmt)):
            if isinstance(node, ast.Call) and call_name(node.func) in positions:
                wanted = positions[call_name(node.func)]
                for i, arg in enumerate(node.args):
                    if i in wanted or (i - 1) in wanted:
                        unsafe |= {n for n in names_of(arg, aliases) if not n.startswith('<')}
                for keyword in node.keywords:
                    if keyword.arg in wanted:
                        unsafe |= {n for n in names_of(keyword.value, aliases) if not n.startswith('<')}
    return unsafe


def main():
    source, map_path, out_path = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3])
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        raise SystemExit('Wrong source revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'sqlglot', 'tests']):
        raise SystemExit('Source must be unmodified')
    with gzip.open(map_path, 'rt') as f:
        dependency_map = json.load(f)
    ids = dependency_map['tests']
    index = {test_id: i for i, test_id in enumerate(ids)}
    wanted = set(ids)
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ['SKIP_INTEGRATION'] = '0'

    from build_map import TracedExecutor
    import fingerprint as fp_module

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

    classes = fp_module.collect_classes()
    dialects = [c for c in classes if isinstance(c, type) and issubclass(c, Dialect)]
    class_key = fp_module.class_keys(classes, dialects)

    # Containers and their owners: (namespace dict, owner key, attribute).
    owners: dict[int, list] = {}
    objects: dict[int, object] = {}
    allowed_holders: dict[int, set[int]] = {}
    for name in sorted(sys.modules):
        if name == 'sqlglot' or name.startswith('sqlglot.'):
            namespace = vars(sys.modules[name])
            for attr, value in list(namespace.items()):
                if type(value) in (dict, set, frozenset) and not attr.startswith('__'):
                    owners.setdefault(id(value), []).append((sys.modules[name], 'module:' + name, attr))
                    objects[id(value)] = value
                    allowed_holders.setdefault(id(value), set()).add(id(namespace))
    for cls in classes:
        key = class_key[id(cls)]
        if '#' in key:
            continue
        real = next((d for d in gc.get_referents(cls) if type(d) is dict and d.get('__module__') == cls.__module__), None)
        if real is None:
            continue
        for attr, value in list(real.items()):
            if type(value) in (dict, set, frozenset) and isinstance(attr, str) and not attr.startswith('__'):
                owners.setdefault(id(value), []).append((cls, key, attr))
                objects[id(value)] = value
                allowed_holders.setdefault(id(value), set()).add(id(real))

    # Anything else holding one of them (closures, defaults, other containers) makes it untrackable.
    candidate_ids = set(objects)
    bookkeeping = {id(owners), id(objects), id(allowed_holders)} | {id(v) for v in owners.values()}
    held_elsewhere = set()
    gc.collect()
    for holder in gc.get_objects():
        if id(holder) in bookkeeping:
            continue
        for referent in gc.get_referents(holder):
            rid = id(referent)
            if rid in candidate_ids and id(holder) not in allowed_holders[rid]:
                held_elsewhere.add(rid)
    del holder

    unsafe_names = static_unsafe_names(dependency_map['sources'])
    tracked: dict[int, tuple] = {}  # id(tracked object) -> owner list
    for oid, value in objects.items():
        if oid in held_elsewhere:
            continue
        if type(value) is not dict and any(attr in unsafe_names for _, _, attr in owners[oid]):
            continue
        replacement = {dict: TrackedDict, set: TrackedSet, frozenset: TrackedFrozenSet}[type(value)](value)
        for owner, _, attr in owners[oid]:
            if isinstance(owner, type):
                type.__setattr__(owner, attr, replacement)
            else:
                vars(owner)[attr] = replacement
        lookups[id(replacement)] = {}
        fulls[id(replacement)] = set()
        tracked[id(replacement)] = (replacement, [(key, attr) for _, key, attr in owners[oid]])
    print(f'{len(objects)} containers, {len(held_elsewhere)} held elsewhere, '
          f'{len(unsafe_names)} unsafe names, {len(tracked)} tracked', flush=True)

    import sqlglot.generator as generator_module
    import sqlglot.dialects as dialects_package
    import sqlglot.optimizer as optimizer_package
    from sqlglot.dialects import dialect as dialect_module
    original_build = generator_module._build_dispatch

    def build_dispatch(cls):
        source_table = cls.TRANSFORMS
        if id(source_table) not in tracked:
            return original_build(cls)
        suppressed.add(id(source_table))
        try:
            table = original_build(cls)
        finally:
            suppressed.discard(id(source_table))
        derived = DerivedDict(table)
        derived._source = source_table
        return derived

    generator_module._build_dispatch = build_dispatch
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
        cache = getattr(generator_module, '_DISPATCH_CACHE', None)
        if cache is not None:
            suppressed.add(id(cache))
            cache.clear()
            suppressed.discard(id(cache))
            mutated.discard(id(cache))

    outcomes = {}

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            items[:] = [i for i in items if i.nodeid in wanted]

        @pytest.hookimpl(hookwrapper=True)
        def pytest_make_collect_report(self, collector):
            if isinstance(collector, pytest.Module):
                context[0] = '<collect>' + collector.nodeid
                yield
                context[0] = '<between>'
            else:
                yield

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            reset_lazy_state()
            if item.cls is not None and isinstance(getattr(item.cls, 'cache', None), dict):
                item.cls.cache.clear()
            context[0] = item.nodeid + '|s'
            yield
            context[0] = '<between>'

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            context[0] = item.nodeid
            yield
            context[0] = '<between>'

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_teardown(self, item, nextitem):
            context[0] = item.nodeid + '|t'
            yield
            context[0] = '<between>'

        def pytest_runtest_logreport(self, report):
            if report.failed:
                outcomes[report.nodeid] = 'failed'
            elif report.when == 'call':
                outcomes.setdefault(report.nodeid, 'passed')

    pytest.main(['tests', '-q', '-p', 'no:cacheprovider'], plugins=[Plugin()])
    context[0] = '<end>'
    generator_module._build_dispatch = original_build

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

    describe = fp_module.Fingerprinter(class_key)
    result = {}
    for oid, (container, owned) in tracked.items():
        if oid in mutated:
            continue
        keys = {}
        for key, contexts in lookups[oid].items():
            sid = set_id(contexts)
            if sid is not None:
                keys[describe(key)] = sid
        # The selector narrows only when the image's own description of the keys matches.
        digest = fp_module._hash('\n'.join(sorted(describe(k) for k in container)))
        entry = {'keys': keys, 'full': set_id(fulls[oid]), 'digest': digest}
        for owner_key, attr in owned:
            result[owner_key + '\0' + attr] = entry
    broken = [index[t] for t in ids if outcomes.get(t) != 'passed']
    with gzip.open(out_path, 'wt', compresslevel=9) as f:
        json.dump({'version': 1, 'revision': PIN, 'sets': sets, 'containers': result, 'broken': broken},
                  f, separators=(',', ':'))
    print(f'{len(result)} narrowable attributes, {len(sets)} test sets, {len(broken)} tests not passing: '
          + ', '.join(ids[i] for i in broken[:10]))


if __name__ == '__main__':
    main()
