"""Fingerprint sqlglot's import-time state, for one version of its sources.

Run as a child process: `python -I -B fingerprint.py < request.json > state.json`.
The request holds the pinned sources plus the patched files. Every sqlglot module
is imported from memory (nothing is written), then every module global and every
class attribute is reduced to a short hash. Two runs, baseline and patched, are
compared by the selector: an attribute whose hash differs changed at import time.

Function bodies are not part of a function's hash: the selector handles code in
function bodies with coverage. Everything evaluated at import is: defaults,
closure cells, decorators, wrapper types, dict/set/list contents, derived tries.
"""
from __future__ import annotations

import enum
import functools
import hashlib
import importlib.abc
import importlib.util
import json
import marshal
import os
import re
import resource
import sys
import types

ROOT = '/__blast__/'
UNIT_ATTRS = ('tokenizer_class', 'jsonpath_tokenizer_class', 'parser_class', 'generator_class')
SKIP_CLASS_ATTRS = {
    '__dict__', '__weakref__', '__module__', '__qualname__', '__doc__', '__annotations__',
    '__firstlineno__', '__static_attributes__', '__annotate__', '__annotate_func__',
    '__annotations_cache__', '__orig_bases__', '__parameters__',
}


class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources, codes, fresh, available):
        self.sources = sources
        self.codes = codes  # compiled baseline modules, reused for files the patch leaves alone
        self.fresh = fresh  # paths that must be compiled from source
        self.available = available
        self.compiled = {}

    def find_spec(self, name, path, target=None):
        if name != 'sqlglot' and not name.startswith('sqlglot.'):
            return None
        rel = name.replace('.', '/')
        if rel + '/__init__.py' in self.available:
            return importlib.util.spec_from_loader(name, self, origin=ROOT + rel + '/__init__.py', is_package=True)
        if rel + '.py' in self.available:
            return importlib.util.spec_from_loader(name, self, origin=ROOT + rel + '.py', is_package=False)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        origin = module.__spec__.origin
        rel = origin[len(ROOT):]
        module.__file__ = origin
        if module.__spec__.submodule_search_locations is not None:
            module.__path__ = [ROOT + rel.rsplit('/', 1)[0]]
        code = None if rel in self.fresh else self.codes.get(rel)
        if code is None:
            code = compile(self.sources[rel], origin, 'exec', dont_inherit=True)
            self.compiled[rel] = code
        exec(code, module.__dict__)


class Fingerprinter:
    def __init__(self, keys):
        self.keys = keys  # id(class) -> stable key
        self.memo = {}
        self.keep = []
        self.active = set()

    def class_key(self, cls):
        key = self.keys.get(id(cls))
        if key is None:
            key = f'{getattr(cls, "__module__", "?")}:{getattr(cls, "__qualname__", "?")}'
        return key

    def __call__(self, o):
        t = type(o)
        if t is str:
            text = 'str:' + repr(o)
            return text if len(text) < 80 else 'h:' + _hash(text)
        i = id(o)
        known = self.memo.get(i)
        if known is not None:
            return known
        if o is None or t in (bool, int, float, complex, bytes):
            text = f'{t.__name__}:{o!r}'
            return text if len(text) < 80 else 'h:' + _hash(text)
        if i in self.active:
            return '<cycle>'
        self.active.add(i)
        try:
            text = self.describe(o)
        finally:
            self.active.discard(i)
        if len(text) > 80:
            text = 'h:' + _hash(text)
        self.memo[i] = text
        self.keep.append(o)
        return text

    def describe(self, o):
        f = self
        if isinstance(o, type):
            return 'class:' + self.class_key(o)
        if isinstance(o, types.ModuleType):
            return 'module:' + o.__name__
        if isinstance(o, enum.Enum):
            return f'enum:{self.class_key(type(o))}.{o.name}={f(o._value_)}'
        if isinstance(o, (tuple, list)):
            items = list(map(f, o))
            if items and isinstance(o[0], type) and all(isinstance(x, type) for x in o):
                items.sort()  # class tuples feed isinstance; built from sets, their order varies by run
            return type(o).__name__ + ':' + self.class_key(type(o)) + '(' + ','.join(items) + ')'
        if isinstance(o, dict):
            extra = ''
            if hasattr(o, 'default_factory'):
                extra = f(o.default_factory)
            # Sorted: insertion order often follows set or address order, which varies by run.
            return 'dict:' + self.class_key(type(o)) + extra + '{' + ','.join(sorted(f(k) + ':' + f(v) for k, v in o.items())) + '}'
        if isinstance(o, (set, frozenset)):
            return 'set:' + self.class_key(type(o)) + '{' + ','.join(sorted(map(f, o))) + '}'
        if isinstance(o, types.FunctionType):
            cells = []
            for cell in o.__closure__ or ():
                try:
                    cells.append(f(cell.cell_contents))
                except ValueError:
                    cells.append('<empty>')
            attrs = {k: v for k, v in vars(o).items() if k != '__wrapped__'}
            wrapped = getattr(o, '__wrapped__', None)
            # Defaults of a def written at module or class level come from its own source,
            # which the selector covers with the function's callers. Functions made at
            # run time of their factory (closures, lambdas) carry defaults from elsewhere.
            static = '<locals>' not in o.__qualname__ and '<lambda>' not in o.__qualname__
            defaults = '' if static else f'{f(o.__defaults__)}|{f(o.__kwdefaults__)}'
            return f'fn:{o.__module__}:{o.__qualname__}|{defaults}|{",".join(cells)}|{f(attrs)}|{f(wrapped)}'
        if isinstance(o, (classmethod, staticmethod)):
            return type(o).__name__ + ':' + f(o.__func__)
        if isinstance(o, property):
            return 'property:' + f(o.fget) + f(o.fset) + f(o.fdel)
        if isinstance(o, functools.cached_property):
            return 'cached_property:' + f(o.func)
        if isinstance(o, functools.partial):
            return 'partial:' + f(o.func) + f(o.args) + f(o.keywords)
        if isinstance(o, types.MethodType):
            owner = o.__self__
            bound = 'class:' + self.class_key(owner) if isinstance(owner, type) else 'instance:' + f(owner)
            return 'method:' + f(o.__func__) + '@' + bound
        if isinstance(o, (types.BuiltinFunctionType, types.WrapperDescriptorType, types.MethodDescriptorType,
                          types.MethodWrapperType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                          types.MemberDescriptorType)):
            owner = getattr(o, '__self__', None)
            bound = ''
            if owner is not None and not isinstance(owner, types.ModuleType):
                bound = '@' + (self.class_key(owner) if isinstance(owner, type) else self.class_key(type(owner)))
            return f'builtin:{getattr(o, "__module__", None)}:{getattr(o, "__qualname__", repr(o))}{bound}'
        if isinstance(o, re.Pattern):
            return f're:{o.pattern!r}:{o.flags}'
        if isinstance(o, types.CodeType):
            return 'code:' + o.co_qualname
        # Generic object: its type and its attributes, without parent links.
        attrs = {}
        if hasattr(o, '__dict__') and isinstance(vars(o), dict):
            attrs.update(vars(o))
        for klass in type(o).__mro__:
            for slot in getattr(klass, '__slots__', ()) if isinstance(getattr(klass, '__slots__', ()), (tuple, list)) else ():
                if slot not in ('__dict__', '__weakref__') and hasattr(o, slot):
                    attrs.setdefault(slot, getattr(o, slot))
        attrs.pop('parent', None)
        if attrs:
            return 'object:' + self.class_key(type(o)) + '{' + ','.join(
                k + '=' + f(attrs[k]) for k in sorted(attrs)
            ) + '}'
        text = repr(o)
        if ' at 0x' in text or ' object at ' in text:
            text = ''
        return 'object:' + self.class_key(type(o)) + ':' + text


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode('utf-8', 'surrogatepass'), digest_size=10).hexdigest()


CALLABLE = (
    types.FunctionType, types.MethodType, classmethod, staticmethod, property, functools.partial,
    functools.cached_property, types.BuiltinFunctionType, types.WrapperDescriptorType,
    types.MethodDescriptorType, types.ClassMethodDescriptorType, type,
)


def attr_hash(fp, value) -> str:
    """'d' for plain data, which code reads by name; 'c' for code and descriptors."""
    kind = 'c' if isinstance(value, CALLABLE) or callable(value) else 'd'
    return kind + _hash(fp(value))


def resolve_static(cls):
    """The class reached by module + qualname, if that is this class."""
    module = sys.modules.get(getattr(cls, '__module__', ''))
    target = module
    for part in getattr(cls, '__qualname__', '').split('.'):
        target = getattr(target, part, None)
        if target is None:
            return False
    return target is cls


def class_keys(classes, dialects):
    """Stable keys: module:qualname, or dialect key + unit attribute for generated classes."""
    keys = {}
    for cls in classes:
        if resolve_static(cls):
            keys[id(cls)] = f'{cls.__module__}:{cls.__qualname__}'
    for dialect in sorted(dialects, key=lambda d: keys.get(id(d), '')):
        if id(dialect) not in keys:
            continue
        for attr in UNIT_ATTRS:
            member = dialect.__dict__.get(attr)
            if isinstance(member, type) and id(member) not in keys:
                keys[id(member)] = f'{keys[id(dialect)]}.{attr}'
    counts = {}
    for cls in sorted((c for c in classes if id(c) not in keys), key=lambda c: (c.__module__, c.__qualname__)):
        base = f'{cls.__module__}:{cls.__qualname__}'
        counts[base] = counts.get(base, 0) + 1
        keys[id(cls)] = f'{base}#{counts[base]}'
    return keys


def collect_classes():
    classes = {}

    def add(cls):
        if id(cls) in classes or not str(getattr(cls, '__module__', '')).startswith('sqlglot'):
            return
        classes[id(cls)] = cls
        for value in list(vars(cls).values()):
            if isinstance(value, type):
                add(value)
        try:
            for sub in type.__subclasses__(cls):
                add(sub)
        except TypeError:
            pass

    for name in sorted(sys.modules):
        if name == 'sqlglot' or name.startswith('sqlglot.'):
            for value in list(vars(sys.modules[name]).values()):
                if isinstance(value, type):
                    add(value)
    return list(classes.values())


def load_codes(path):
    try:
        with open(path, 'rb') as f:
            codes = marshal.load(f)
        return codes if isinstance(codes, dict) else {}
    except (OSError, ValueError, EOFError, TypeError):
        return {}


def diff(base, patched):
    """Owners whose state differs: owner -> {attribute: kind} (None when added or removed)."""
    changed = {}
    for owner in set(base) | set(patched):
        old, new = base.get(owner), patched.get(owner)
        if old == new:
            continue
        if old is None or new is None:
            changed[owner] = None
        else:
            changed[owner] = {
                a: (new.get(a) or old.get(a) or '?')[:1] for a in set(old) | set(new) if old.get(a) != new.get(a)
            }
    return changed


def main():
    """Request (stdin JSON):

    sources      every sqlglot source, needed only when no code cache is given
    overrides    patched or new files, always compiled from source
    cache        marshal file of compiled baseline modules (read; written when write_cache)
    write_state  write this run's full state there (baseline runs)
    baseline     a written state: answer only with the differences from it
    """
    request = json.loads(sys.stdin.read())
    cache = request.get('cache')
    codes = load_codes(cache) if cache and not request.get('write_cache') else {}
    if 'checks' in request:
        result = run_checks(request, codes)
        if request.get('write_checks'):
            write_atomic(request['write_checks'], lambda f: json.dump(result['checks'], f))
        print(json.dumps(result))
        return
    base = None
    if request.get('baseline'):
        with open(request['baseline']) as f:
            base = json.load(f)
    print(json.dumps(compute(request, codes, base)))


def compute(request, codes, base):
    """Import sqlglot with the request's overrides and fingerprint it (see main)."""
    overrides = request.get('overrides', {})
    cache = request.get('cache')
    sources = dict(request.get('sources') or {})
    sources.update(overrides)
    available = set(sources) | set(codes)
    finder = Finder(sources, codes, set(overrides), available)
    sys.meta_path.insert(0, finder)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    failures = {}
    try:
        import sqlglot  # noqa: F401
        dialect = sys.modules['sqlglot.dialects.dialect'].Dialect
        init = {
            'modules': _hash(','.join(sorted(n for n in sys.modules if n == 'sqlglot' or n.startswith('sqlglot.')))),
            'dialects': _hash(','.join(sorted(map(str, type(dialect)._classes)))) if hasattr(type(dialect), '_classes') else '',
        }
    except BaseException as error:  # noqa: BLE001
        return {'ok': False, 'error': f'import sqlglot: {error!r}'}
    modules = sorted(
        (p[:-12] if p.endswith('/__init__.py') else p[:-3]).replace('/', '.')
        for p in available if p.startswith('sqlglot/') and p.endswith('.py')
    )
    for name in modules:
        if name == 'sqlglot.__main__':
            continue
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001
            failures[name] = f'{type(error).__name__}: {error}'
    try:
        dialect.classes  # every dialect, registered by name
    except BaseException as error:  # noqa: BLE001
        failures['<dialects>'] = repr(error)

    # Generated classes nobody references get '#' keys below and are skipped, so
    # whether the collector has run yet does not matter.
    classes = collect_classes()
    dialects = [c for c in classes if isinstance(c, type) and issubclass(c, dialect)]
    keys = class_keys(classes, dialects)
    fp = Fingerprinter(keys)
    owners = {}
    for name in sorted(sys.modules):
        if not (name == 'sqlglot' or name.startswith('sqlglot.')):
            continue
        module = sys.modules[name]
        attrs = {}
        for attr, value in sorted(vars(module).items()):
            if attr.startswith('__') and attr not in ('__all__', '__getattr__', '__dir__'):
                continue
            attrs[attr] = attr_hash(fp, value)
        owners['module:' + name] = attrs
    for cls in classes:
        key = keys[id(cls)]
        if '#' in key:
            continue  # generated, unreachable by name or dialect: numbering is not stable
        attrs = {}
        for attr, value in sorted(vars(cls).items(), key=lambda kv: str(kv[0])):
            if attr in SKIP_CLASS_ATTRS:
                continue
            attrs[str(attr)] = attr_hash(fp, value)
        attrs['<bases>'] = _hash(','.join(keys.get(id(b), fp(b)) for b in cls.__bases__))
        attrs['<metaclass>'] = _hash(fp(type(cls)))
        owners[key] = attrs
    owners['<init>'] = init
    owners['<runtime>'] = {
        'recursionlimit': str(sys.getrecursionlimit()),
        'loggers': _hash(repr(sorted(
            (n, getattr(l, 'level', None), getattr(l, 'disabled', None))
            for n, l in __import__('logging').root.manager.loggerDict.items()
        ))),
        'warnings': _hash(repr(__import__('warnings').filters)),
    }
    if cache and request.get('write_cache') and finder.compiled:
        write_atomic(cache, lambda f: marshal.dump(finder.compiled, f), binary=True)
    state = {'failures': failures, 'owners': owners}
    if request.get('write_state'):
        write_atomic(request['write_state'], lambda f: json.dump(state, f))
    if base is not None:
        return {
            'ok': True,
            'new_failures': sorted(set(failures) - set(base['failures'])),
            'changed': diff(base['owners'], owners),
        }
    return {'ok': True}


# ---------------------------------------------------------------- differential checks
#
# The TPC tests in tests/test_executor.py depend on sqlglot only through a few step
# results that are cheap to recompute (see tools/build_proxies.py): transpiling each
# query to duckdb, parsing it and pickling it to the pool worker, optimizing it inside
# execute(), and planning it. The selector compares these, computed with the patched
# sources, against the same computed at image build time for the pinned sources.

TPCDS_FIXTURE = 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
TPCH_FIXTURE = 'tests/fixtures/optimizer/tpc-h/tpc-h.sql'
CHECK_PHASES = ('transpile', 'parse', 'optimize', 'plan')


class Dumper:
    """Deterministic text of everything an expression tree or plan holds.

    Expressions: class, args in order, arg keys, indexes, comments, types, meta, and
    whether every child points back at its parent. Plans: every step reachable from
    the root with all its fields; dependents by name, sets sorted.
    """

    PLAIN = (bool, int, float, str, bytes, complex, type(None))

    def __init__(self, expr_class, step_class):
        self.expr_class = expr_class
        self.step_class = step_class
        self.steps = {}
        self.slots = {}

    def __call__(self, o):
        t = type(o)
        if t is str:
            return 's' + repr(o)
        if t in self.PLAIN:
            return f'{t.__name__}:{o!r}'
        if isinstance(o, self.expr_class):
            out = []
            self.expr(o, out)
            return ''.join(out)
        if isinstance(o, self.step_class):
            return self.step(o)
        if isinstance(o, enum.Enum):
            return f'enum:{t.__module__}:{t.__qualname__}.{o.name}={self(o._value_)}'
        if isinstance(o, type):
            return f'class:{o.__module__}:{o.__qualname__}'
        if isinstance(o, (list, tuple)):
            return f'{t.__name__}[' + ','.join(map(self, o)) + ']'
        if isinstance(o, dict):
            return f'{t.__name__}{{' + ','.join(self(k) + ':' + self(v) for k, v in o.items()) + '}'
        if isinstance(o, (set, frozenset)):
            return f'{t.__name__}{{' + ','.join(sorted(map(self, o))) + '}'
        if hasattr(o, '__dict__'):
            return f'object:{t.__module__}:{t.__qualname__}{{' + ','.join(
                k + '=' + self(v) for k, v in sorted(vars(o).items())) + '}'
        text = repr(o)
        return f'object:{t.__module__}:{t.__qualname__}:' + ('' if ' at 0x' in text else text)

    def class_slots(self, cls):
        slots = self.slots.get(cls)
        if slots is None:
            slots = []
            for klass in cls.__mro__:
                declared = getattr(klass, '__slots__', ())
                for slot in (declared,) if isinstance(declared, str) else declared:
                    if slot not in slots and slot not in ('args', 'parent', '_hash', '__weakref__', '__dict__'):
                        slots.append(slot)
            slots = self.slots[cls] = (f'E:{cls.__module__}:{cls.__qualname__}(', tuple(slots))
        return slots

    def expr(self, node, out):
        head, slots = self.class_slots(type(node))
        out.append(head)
        for slot in slots:
            value = getattr(node, slot, '<unset>')
            out.append(slot)
            out.append('=')
            if value is None:
                out.append('-')
            elif isinstance(value, self.expr_class):
                self.expr(value, out)
            else:
                out.append(self(value))
            out.append(';')
        extra = getattr(node, '__dict__', None)
        if extra:
            out.append('dict=' + self(extra) + ';')
        expr_class = self.expr_class
        for key, value in node.args.items():
            out.append(key)
            if isinstance(value, expr_class):
                if value.parent is not node:
                    out.append('!unlinked')
                out.append('=')
                self.expr(value, out)
            elif type(value) is list:
                out.append('=[')
                for item in value:
                    if isinstance(item, expr_class):
                        if item.parent is not node:
                            out.append('!unlinked')
                        self.expr(item, out)
                    else:
                        out.append(self(item))
                    out.append(',')
                out.append(']')
            else:
                out.append('=')
                out.append(self(value))
            out.append(';')
        out.append(')')

    def step(self, step):
        known = self.steps.get(id(step))
        if known is not None:
            return known
        self.steps[id(step)] = f'<cycle {step.name}>'
        fields = []
        for key, value in sorted(vars(step).items()):
            if key == 'dependents':
                fields.append(key + '=' + ','.join(sorted(repr(getattr(d, 'name', None)) for d in value)))
            else:
                fields.append(key + '=' + self(value))
        text = f'S:{type(step).__qualname__}{{' + ';'.join(fields) + '}'
        self.steps[id(step)] = text
        return text


def fixture_pairs(helpers_source, fixtures):
    """load_sql_fixture_pairs of the given tests/helpers.py, reading fixture texts from memory."""
    import io
    ns = {'__name__': 'tests.helpers', '__file__': '/__blast__/tests/helpers.py'}
    exec(compile(helpers_source, ns['__file__'], 'exec', dont_inherit=True), ns)

    def fake_open(path, *args, **kwargs):
        rel = os.path.relpath(os.fspath(path), '/__blast__')
        return io.StringIO(fixtures[rel])

    ns['open'] = fake_open
    load = ns['load_sql_fixture_pairs']
    tpcds = list(load(TPCDS_FIXTURE[len('tests/fixtures/'):]))
    tpch = list(load(TPCH_FIXTURE[len('tests/fixtures/'):]))
    return ns, tpcds, tpch


class Differs(Exception):
    """A recomputed step result is not the pinned one."""


def run_checks(request, codes, baseline=None):
    """Step results of the TPC tests for the request's sources.

    request['checks']: {'tpcds': {phase: queries}, 'tpch_opt': {phase: queries}} where
    queries is a bitmask of query numbers (-1: all) in the order the test makes the
    calls (see tools/build_proxies.py). Without a baseline (image build) every result is
    computed and returned as digest lists. With one, each test's answer is True when
    the requested results equal the pinned ones, else False, stopping at the first
    difference. Inputs the requested steps need are recomputed without comparing. A
    step that raises answers False: the test would error. Fixture inputs are always
    compared.
    """
    import pickle
    overrides = request.get('overrides', {})
    finder = Finder(dict(overrides), codes, set(overrides), set(overrides) | set(codes))
    sys.meta_path.insert(0, finder)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    checks = request.get('checks', {})
    out = {}
    try:
        ns, tpcds, tpch = fixture_pairs(request['helpers'], request['fixtures'])
    except BaseException as error:  # noqa: BLE001 - setUpClass would fail
        return {'ok': True, 'checks': {name: False for name in checks}, 'error': repr(error)[:300]}
    inputs = {
        'tpcds': _hash(repr([(meta, sql) for meta, sql, _ in tpcds])),
        'tpch_opt': _hash(repr([(sql, expected) for _, sql, expected in tpch])),
    }
    dumper = None

    def setup():
        nonlocal dumper
        if dumper is None:
            import sqlglot  # noqa: F401
            from sqlglot import planner
            dumper = Dumper(sys.modules['sqlglot.expressions.core'].Expr, planner.Step)
        return dumper

    for name in ('tpcds', 'tpch_opt'):
        if name not in checks:
            continue
        wanted = {phase: int(mask) for phase, mask in checks[name].items() if int(mask)}
        pinned = baseline.get(name) if baseline is not None else None
        results = {}

        def chosen(phase, n):
            return baseline is None or (wanted.get(phase, 0) >> n) & 1 == 1

        def record(phase, n, text):
            digest = _hash(text)
            if pinned is None:
                results.setdefault(phase, []).append(digest)
                return
            expected = pinned.get(phase) or []
            if n >= len(expected) or expected[n] != digest:
                raise Differs(f'{phase} #{n}')

        try:
            if pinned is not None and pinned.get('inputs') != inputs[name]:
                raise Differs('inputs')
            if wanted or baseline is None:
                d = setup()
                from sqlglot import parse_one, transpile
            if name == 'tpch_opt':
                for n, (_, sql, optimized) in enumerate(tpch):
                    if chosen('transpile', 2 * n):
                        record('transpile', 2 * n, transpile(sql, write='duckdb')[0])
                    if chosen('transpile', 2 * n + 1):
                        record('transpile', 2 * n + 1, transpile(optimized, write='duckdb')[0])
            elif wanted or baseline is None:
                executed = [n for n, (meta, _, _) in enumerate(tpcds) if meta.get('execute')]
                for j, n in enumerate(executed):
                    if chosen('transpile', j):
                        record('transpile', j, transpile(tpcds[n][1], write='duckdb')[0])
                # Executed queries whose optimize or plan step is wanted, and the parses they need.
                planned = [j for j in range(len(executed)) if chosen('optimize', j) or chosen('plan', j)]
                needed = {executed[j] for j in planned}
                parsed = {}
                for n, (meta, sql, _) in enumerate(tpcds):
                    if n in needed or chosen('parse', n):
                        expression, sent = pickle.loads(pickle.dumps((parse_one(sql), meta)))
                        parsed[n] = (sent, expression)
                        if chosen('parse', n):
                            record('parse', n, d(expression))
                if planned:
                    executor = importlib.import_module('sqlglot.executor')
                    schema_dict = pickle.loads(pickle.dumps(ns['TPCDS_SCHEMA']))
                    for j in planned:
                        sent, expression = parsed[executed[j]]
                        schema = executor.ensure_schema(schema_dict, dialect=None)
                        optimized = executor.optimize(expression, schema, leave_tables_isolated=True, dialect=None)
                        text = optimized.sql(pretty=True)
                        if chosen('optimize', j):
                            d.steps = {}
                            record('optimize', j, d(optimized) + '\n' + text)
                        if chosen('plan', j):
                            d.steps = {}
                            record('plan', j, d(executor.Plan(optimized)))
            if baseline is None:
                results['inputs'] = inputs[name]
                out[name] = results
            else:
                out[name] = True
        except Differs as reason:
            out[name] = False
            out[name + '_reason'] = str(reason)
        except BaseException as error:  # noqa: BLE001
            out[name] = False if baseline is not None else None
            out[name + '_reason'] = repr(error)[:300]
    return {'ok': True, 'checks': out}


def serve():
    """Answer one request per input line, each in a fresh fork of this process.

    The bytecode cache and baseline state are loaded once here; sqlglot itself is
    never imported here, so every fork imports its own patched copy from scratch.
    """
    config = json.loads(sys.stdin.readline())
    codes = load_codes(config['cache'])
    checks_baseline = None
    if config.get('checks'):
        try:
            with open(config['checks']) as f:
                checks_baseline = json.load(f)
        except (OSError, ValueError):
            checks_baseline = None
    with open(config['baseline']) as f:
        base = json.load(f)
    # Standard modules sqlglot imports, loaded before forking so each fork reuses them.
    for name in ('abc', 'collections', 'contextlib', 'copy', 'dataclasses', 'datetime', 'decimal',
                 'enum', 'functools', 'heapq', 'inspect', 'itertools', 'logging', 'math', 'numbers',
                 'operator', 'string', 'textwrap', 'threading', 'time', 'typing', 'uuid', 'warnings'):
        try:
            importlib.import_module(name)
        except ImportError:
            pass
    sys.stdout.write('{"ready": true}\n')
    sys.stdout.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        request['cache'] = config['cache']
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            try:
                # Patched code can loop forever; CPU time is charged, so cap it (the
                # child then dies and the selector keeps what it could not judge).
                limit = int(request.get('cpu_limit') or 60)
                resource.setrlimit(resource.RLIMIT_CPU, (limit, limit + 1))
            except (ValueError, OSError):
                pass
            try:
                if 'checks' in request:
                    result = run_checks(request, codes, checks_baseline) if checks_baseline else {'ok': False, 'error': 'no check baseline'}
                else:
                    result = compute(request, codes, base)
            except BaseException as error:  # noqa: BLE001
                result = {'ok': False, 'error': repr(error)}
            with os.fdopen(write, 'w') as out:
                out.write(json.dumps(result))
            os._exit(0)
        os.close(write)
        with os.fdopen(read) as answer:
            text = answer.read()
        os.waitpid(pid, 0)
        if not text:
            text = json.dumps({'ok': False, 'error': 'fingerprint process died'})
        sys.stdout.write(text.replace('\n', ' ') + '\n')
        sys.stdout.flush()


def write_atomic(path, write, binary=False):
    temporary = f'{path}.{os.getpid()}'
    try:
        with open(temporary, 'wb' if binary else 'w') as f:
            write(f)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass


if __name__ == '__main__':
    serve() if sys.argv[1:] == ['--serve'] else main()
