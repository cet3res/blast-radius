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


def serve():
    """Answer one request per input line, each in a fresh fork of this process.

    The bytecode cache and baseline state are loaded once here; sqlglot itself is
    never imported here, so every fork imports its own patched copy from scratch.
    """
    config = json.loads(sys.stdin.readline())
    codes = load_codes(config['cache'])
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
