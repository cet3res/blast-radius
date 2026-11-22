"""Generate dependencies using only the pinned, unmodified source and test catalog.

Usage: python tools/build_map.py SOURCE tests.json OUTPUT.gz

Requires coverage, pytest, and the upstream test dependencies (requirements.lock).
Nothing here reads patches or answers: every fact comes from running the
unmodified test suite of the pinned revision once.

Recorded facts
- line coverage, per test, at line granularity. Coverage contexts separate
  setup, call and teardown, and collection of each test file. Lines run while a
  test file is collected are charged to every test in that file; lines run in a
  test's setup phase (setUpClass included) are charged to every test in its class.
- lines and code objects executed while sqlglot is imported ("import time").
  Every sqlglot module is imported before collection, so import-time work is
  never hidden inside whichever test happened to import a module first.
- per test, the classes it instantiated (every sqlglot __init__ is wrapped), the
  sqlglot ancestry of every class, and each dialect's tokenizer, parser and
  generator classes.
- files opened (fixtures) and directories listed, per test.
- static import graph between sqlglot modules, and modules `import sqlglot` loads.

Caches that would hide work from later tests are cleared before every test:
the generator dispatch cache and unittest classes' `cache` dicts.
"""
from __future__ import annotations

import ast
import concurrent.futures
import gzip
import importlib
import json
import os
import pickle
import pkgutil
from pathlib import Path
import subprocess
import sys

import coverage
import pytest

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
IMPORT = '<import>'


class TracedExecutor(concurrent.futures.Executor):
    """Run pool tasks under the owning test's trace, preserving pickle boundaries."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = fn(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def module_path(source: Path, module) -> str | None:
    file = getattr(module, '__file__', None)
    if not file:
        return None
    try:
        return Path(file).resolve().relative_to(source).as_posix()
    except ValueError:
        return None


def static_imports(source: Path, relative: str) -> list[str]:
    """sqlglot modules a file imports outside function bodies (executed at import)."""
    path = source / relative
    tree = ast.parse(path.read_text())
    package = relative[:-3].replace('/', '.')
    if package.endswith('.__init__'):
        package = package[: -len('.__init__')]
    else:
        package = package.rsplit('.', 1)[0]
    found = set()

    def resolve(name):
        parts = name.split('.')
        for i in range(len(parts), 0, -1):
            base = source.joinpath(*parts[:i])
            for candidate in (base.with_suffix('.py'), base / '__init__.py'):
                if candidate.exists():
                    found.add(candidate.relative_to(source).as_posix())
                    break
            else:
                continue
            break

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith('sqlglot'):
                        resolve(alias.name)
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = package.split('.')
                    base = base[: len(base) - child.level + 1]
                    mod = '.'.join(base + ([child.module] if child.module else []))
                else:
                    mod = child.module or ''
                if mod.startswith('sqlglot'):
                    resolve(mod)
                    for alias in child.names:
                        resolve(f'{mod}.{alias.name}')
            visit(child)

    visit(tree)
    found.discard(relative)
    return sorted(found)


def unit_spans(text: str) -> list[list[int]]:
    """Multi-line units: smallest statement, compound header, or lambda around each line."""
    spans = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Lambda):
            start, end = node.lineno, node.end_lineno
        elif isinstance(node, ast.stmt):
            decorators = getattr(node, 'decorator_list', None) or []
            start = min([node.lineno] + [d.lineno for d in decorators])
            body = getattr(node, 'body', None)
            if isinstance(body, list) and body and hasattr(body[0], 'lineno'):
                end = max(start, body[0].lineno - 1)
            else:
                end = node.end_lineno
        else:
            continue
        if end > start:
            spans.add((start, end))
    return sorted(map(list, spans))


def main():
    source, catalog, output = map(lambda p: Path(p).resolve(), sys.argv[1:])
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        raise SystemExit('Wrong source revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'sqlglot', 'tests']):
        raise SystemExit('Source must be unmodified')
    ids = [t['id'] for t in json.loads(catalog.read_text())['tests']]
    index = {test_id: i for i, test_id in enumerate(ids)}
    wanted = set(ids)
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ['SKIP_INTEGRATION'] = '0'
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')

    # Modules that a bare `import sqlglot` loads (what a subprocess importing sqlglot executes).
    init_modules = json.loads(subprocess.check_output([sys.executable, '-c', (
        'import json, sys, sqlglot; '
        'print(json.dumps(sorted(getattr(m, "__file__", None) or "" for n, m in list(sys.modules.items()) '
        'if n == "sqlglot" or n.startswith("sqlglot."))))')], text=True, cwd=source))
    init_modules = sorted(Path(f).resolve().relative_to(source).as_posix() for f in init_modules if f)

    cov = coverage.Coverage(source=[str(source / 'sqlglot'), str(source / 'tests')], data_file=None)
    context = [IMPORT]
    reads: dict[str, set[str]] = {}
    listings: dict[str, set[str]] = {}
    subprocesses: dict[str, list] = {}

    fixture_loaders: dict[str, set[str]] = {}

    def audit(event, args):
        ctx = context[0]
        if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'):
            argv = args[1] if event == 'subprocess.Popen' else args[0]
            try:
                argv = [os.fsdecode(a) for a in argv] if isinstance(argv, (list, tuple)) else [os.fsdecode(argv)]
            except TypeError:
                argv = [repr(argv)]
            subprocesses.setdefault(ctx, []).append(argv)
            return
        if event == 'os.fork':
            subprocesses.setdefault(ctx, []).append(['<fork>'])
            return
        if event in ('open', 'os.listdir', 'os.scandir', 'glob.glob'):
            target = args[0] if args else None
            if target is None and event in ('os.listdir', 'os.scandir'):
                target = '.'
            if not isinstance(target, (str, bytes, os.PathLike)):
                return
            try:
                path = Path(os.fsdecode(target)).absolute()
                relative = path.relative_to(source).as_posix()
            except (ValueError, TypeError):
                return
            if event == 'open':
                if (relative.endswith(('.py', '.pyc')) and relative.startswith(('sqlglot/', 'tests/'))
                        and not relative.startswith('tests/fixtures/')):
                    return  # imports of measured code; coverage covers it
                if relative.startswith('tests/fixtures/') and relative.endswith('.sql'):
                    caller = sys._getframe(1)
                    name = caller.f_code.co_name
                    if caller.f_code.co_filename != str(source / 'tests/helpers.py') or name not in (
                        'load_sql_fixture_pairs', 'load_sql_fixtures',
                    ):
                        name = 'raw'
                    elif name == 'load_sql_fixture_pairs':
                        consumer = caller.f_back
                        if (consumer and consumer.f_code.co_filename == str(source / 'tests/test_optimizer.py')
                                and consumer.f_code.co_name == 'check_file' and consumer.f_locals.get('only') is None):
                            name = 'check_file'
                    fixture_loaders.setdefault(relative, set()).add(name)
                reads.setdefault(relative, set()).add(ctx)
            else:
                listings.setdefault(relative, set()).add(ctx)

    sys.addaudithook(audit)
    concurrent.futures.ProcessPoolExecutor = TracedExecutor

    # Record every code object that starts while sqlglot is imported.
    mon = sys.monitoring
    tool = mon.PROFILER_ID if mon.get_tool(mon.PROFILER_ID) is None else 5
    mon.use_tool_id(tool, 'blast-radius-import-tracker')
    import_code: set[tuple[str, int, str]] = set()

    def on_start(code, offset):
        import_code.add((code.co_filename, code.co_firstlineno, code.co_name))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.PY_START)

    cov.start()
    cov.switch_context(IMPORT)
    import sqlglot  # noqa: E402
    broken = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):
        if info.name == 'sqlglot.__main__':
            continue
        try:
            importlib.import_module(info.name)
        except Exception as error:  # optional dependencies
            broken.append(f'{info.name}: {error!r}')
    from sqlglot.dialects.dialect import Dialect
    Dialect.classes  # load every dialect
    mon.set_events(tool, 0)
    mon.register_callback(tool, mon.events.PY_START, None)
    mon.free_tool_id(tool)

    # Class inventory and instantiation tracking.
    classes: dict[int, type] = {}

    def add_class(cls):
        if id(cls) in classes or not getattr(cls, '__module__', '').startswith('sqlglot'):
            return
        classes[id(cls)] = cls
        for value in list(vars(cls).values()):
            if isinstance(value, type):
                add_class(value)
        try:
            for sub in type.__subclasses__(cls) if isinstance(cls, type) else []:
                add_class(sub)
        except TypeError:
            pass

    for name, module in list(sys.modules.items()):
        if name == 'sqlglot' or name.startswith('sqlglot.'):
            for value in list(vars(module).values()):
                if isinstance(value, type):
                    add_class(value)

    instantiated: dict[str, set[int]] = {}
    current = [set()]

    def wrap(cls):
        original = cls.__dict__['__init__']

        def __init__(self, *args, **kwargs):
            current[0].add(id(type(self)))
            return original(self, *args, **kwargs)

        __init__.__name__ = original.__name__
        __init__.__qualname__ = original.__qualname__
        __init__.__doc__ = original.__doc__
        __init__.__wrapped__ = original
        type.__setattr__(cls, '__init__', __init__)

    for cls in list(classes.values()):
        init = cls.__dict__.get('__init__')
        if init is not None and type(init).__name__ == 'function':
            wrap(cls)

    # Also catch dialect classes handed out without instantiation.
    def wrap_classmethod_result(owner, name):
        original = owner.__dict__[name]
        func = original.__func__ if isinstance(original, classmethod) else original

        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if isinstance(result, type):
                current[0].add(id(result))
            elif result is not None:
                current[0].add(id(type(result)))
            return result

        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = func.__qualname__
        wrapper.__doc__ = func.__doc__
        type.__setattr__(owner, name, classmethod(wrapper) if isinstance(original, classmethod) else wrapper)

    meta = type(Dialect)
    for name in ('get', '__getitem__'):
        if name in meta.__dict__:
            wrap_classmethod_result(meta, name)

    import sqlglot.generator as generator_module
    import sqlglot.dialects as dialects_package
    import sqlglot.optimizer as optimizer_package
    from sqlglot.dialects import dialect as dialect_module

    builtin_dialects = set(getattr(dialect_module, 'DIALECT_MODULE_NAMES', ()))
    registry = getattr(type(Dialect), '_classes', {})

    def reset_lazy_state():
        """Undo per-process lazy loading, so every test that relies on it runs it.

        Dialect lookups by name, and the lazy attributes of sqlglot.dialects and
        sqlglot.optimizer, run their loading code only on first use in a process.
        Which test is first depends on order and worker split; resetting before each
        test charges that code to every test that depends on it.
        """
        for key in list(registry):
            if key in builtin_dialects:
                del registry[key]
        for name in list(getattr(dialects_package, 'MODULE_BY_ATTRIBUTE', {})):
            dialects_package.__dict__.pop(name, None)
        for name in list(getattr(optimizer_package, '_LAZY_ATTRS', {})):
            optimizer_package.__dict__.pop(name, None)

    def switch(name):
        # Phase boundaries: store what the previous phase instantiated.
        if current[0]:
            instantiated.setdefault(context[0], set()).update(current[0])
        current[0] = set()
        context[0] = name
        cov.switch_context(name)

    completed, failed = set(), set()
    order = []

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            items[:] = [i for i in items if i.nodeid in wanted]

        @pytest.hookimpl(hookwrapper=True)
        def pytest_make_collect_report(self, collector):
            if isinstance(collector, pytest.Module):
                switch('<collect>' + collector.nodeid)
                yield
                switch(IMPORT)
            else:
                yield

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            order.append(item.nodeid)
            reset_lazy_state()
            getattr(generator_module, '_DISPATCH_CACHE', {}).clear()
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
            if report.when == 'call' and report.passed:
                completed.add(report.nodeid)
            if report.failed or report.skipped:
                failed.add(report.nodeid)

    switch(IMPORT)
    result = pytest.main(['tests', '-q', '-p', 'no:cacheprovider'], plugins=[Plugin()])
    switch('<end>')
    cov.stop()

    # Resolve contexts to sets of test indices.
    by_file: dict[str, list[int]] = {}
    by_class: dict[str, list[int]] = {}
    for test_id in ids:
        by_file.setdefault(test_id.split('::')[0], []).append(index[test_id])
        by_class.setdefault(test_id.rsplit('::', 1)[0], []).append(index[test_id])

    def resolve(ctx: str) -> tuple[frozenset, bool]:
        """Tests a context's execution is charged to, and whether it is import time."""
        if ctx == IMPORT:
            return frozenset(), True
        if ctx.startswith('<collect>'):
            return frozenset(by_file.get(ctx[len('<collect>'):], ())), False
        if ctx.endswith('|s'):
            return frozenset(by_class.get(ctx[:-2].rsplit('::', 1)[0], ())), False
        if ctx.endswith('|t'):
            ctx = ctx[:-2]
        if ctx in index:
            return frozenset((index[ctx],)), False
        return frozenset(), False  # between tests / end: charged to nobody

    resolved: dict[str, tuple[frozenset, bool]] = {}
    sets: list[list[int]] = []
    set_ids: dict[frozenset, int] = {}

    def set_id(tests: frozenset) -> int:
        if tests not in set_ids:
            set_ids[tests] = len(sets)
            sets.append(sorted(tests))
        return set_ids[tests]

    data = cov.get_data()
    files = {}
    for filename in sorted(data.measured_files()):
        relative = Path(filename).resolve().relative_to(source).as_posix()
        line_sets = []
        import_lines = []
        for line, contexts in sorted(data.contexts_by_lineno(filename).items()):
            tests = set()
            at_import = False
            for ctx in contexts:
                if ctx not in resolved:
                    resolved[ctx] = resolve(ctx)
                charged, is_import = resolved[ctx]
                tests |= charged
                at_import |= is_import
            if tests:
                line_sets.append([line, set_id(frozenset(tests))])
            if at_import:
                import_lines.append(line)
        files[relative] = {'lines': line_sets, 'import': import_lines}

    def charge(contexts) -> list[int]:
        tests = set()
        for ctx in contexts:
            tests |= resolve(ctx)[0]
        return sorted(tests)

    def key_of(cls) -> str:
        return keys[id(cls)]

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fingerprint import class_keys
    all_dialects = [c for c in classes.values() if isinstance(c, type) and issubclass(c, Dialect)]
    keys = class_keys(list(classes.values()), all_dialects)

    # Classes created after the inventory (tests' own subclasses) map to their sqlglot ancestors.
    instances: dict[str, set[int]] = {}
    import_instances: set[str] = set()
    for ctx, class_ids in instantiated.items():
        tests, is_import = resolve(ctx)
        for class_id in class_ids:
            if class_id in classes:
                names = [key_of(classes[class_id])]
            else:
                names = []
            for name in names:
                instances.setdefault(name, set()).update(tests)
                if is_import:
                    import_instances.add(name)

    # Non-inventory classes (defined in tests) are reached through their sqlglot ancestors:
    # record them under every sqlglot ancestor key directly.
    extra = {}
    for ctx, class_ids in instantiated.items():
        tests = resolve(ctx)[0]
        for class_id in class_ids:
            if class_id in classes:
                continue
            extra.setdefault(class_id, set()).update(tests)
    import gc
    by_id = {id(o): o for o in gc.get_objects() if isinstance(o, type) and id(o) in extra}
    for class_id, tests in extra.items():
        cls = by_id.get(class_id)
        if cls is None:
            continue
        for ancestor in cls.__mro__:
            if id(ancestor) in classes:
                instances.setdefault(key_of(ancestor), set()).update(tests)

    mro = {}
    for cls in classes.values():
        mro[key_of(cls)] = [key_of(a) for a in cls.__mro__ if id(a) in classes]
    defined_in = {}
    for cls in classes.values():
        path = module_path(source, sys.modules.get(cls.__module__))
        if path:
            defined_in[key_of(cls)] = path

    units = {}
    for dialect in set(Dialect.classes.values()) | {Dialect}:
        members = []
        for attr in ('tokenizer_class', 'jsonpath_tokenizer_class', 'parser_class', 'generator_class'):
            member = getattr(dialect, attr, None)
            if isinstance(member, type) and id(member) in classes:
                members.append(key_of(member))
        if id(dialect) in classes:
            units[key_of(dialect)] = members

    from sqlglot import expressions as exp_module
    function_by_name = {}
    for name, func in getattr(exp_module, 'FUNCTION_BY_NAME', {}).items():
        if id(func) in classes:
            function_by_name[name] = key_of(func)

    code_at_import = {}
    for filename, firstlineno, name in import_code:
        try:
            relative = Path(filename).resolve().relative_to(source).as_posix()
        except ValueError:
            continue
        code_at_import.setdefault(relative, []).append([firstlineno, name])

    python_files = sorted(
        p.relative_to(source).as_posix()
        for root in ('sqlglot', 'tests') for p in (source / root).rglob('*.py')
    )
    imports = {f: static_imports(source, f) for f in python_files if f.startswith('sqlglot/')}

    # Subprocess-launching tests: keep their command so the selector can judge them.
    spawned = {}
    for ctx, commands in subprocesses.items():
        for test in charge([ctx]):
            spawned.setdefault(test, []).extend(commands)

    unmeasured = sorted(index[t] for t in wanted - (completed - failed))
    payload = {
        'version': 2,
        'revision': PIN,
        'tests': ids,
        'unmeasured': unmeasured,
        'subprocess': {str(k): v for k, v in sorted(spawned.items())},
        'sets': sets,
        'sql_fixtures': {
            path: [sorted(loaders), (source / path).read_text(encoding='utf-8')]
            for path, loaders in sorted(fixture_loaders.items())
            if 'raw' not in loaders
        },
        'files': files,
        'code_at_import': {k: sorted(v) for k, v in sorted(code_at_import.items())},
        'reads': {k: set_id(frozenset(charge(v))) for k, v in sorted(reads.items())},
        'read_at_import': sorted(k for k, v in reads.items() if IMPORT in v),
        'listings': {k: set_id(frozenset(charge(v))) for k, v in sorted(listings.items())},
        'instances': {k: set_id(frozenset(v)) for k, v in sorted(instances.items())},
        'import_instances': sorted(import_instances),
        'mro': dict(sorted(mro.items())),
        'defined_in': dict(sorted(defined_in.items())),
        'units': dict(sorted(units.items())),
        'function_by_name': dict(sorted(function_by_name.items())),
        'imports': imports,
        'init_modules': init_modules,
        'sources': {name: (source / name).read_text() for name in python_files},
        'spans': {name: unit_spans((source / name).read_text()) for name in python_files},
        'environment': {
            'python': sys.version, 'pytest': pytest.__version__, 'coverage': coverage.__version__,
            'pool': 'synchronous, with pickle round trips', 'platform': sys.platform,
            'import_errors': broken,
        },
    }
    with gzip.open(output, 'wt', compresslevel=9) as f:
        json.dump(payload, f, separators=(',', ':'))
    print(
        f'Map: {len(files)} files, {len(sets)} test sets, {len(reads)} reads, '
        f'{len(instances)} instantiated classes, {len(unmeasured)} unmeasured tests, '
        f'{len(spawned)} subprocess tests; pytest exit {result}; import errors {broken}',
        file=sys.stderr,
    )


if __name__ == '__main__':
    main()
