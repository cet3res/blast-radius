"""Phase coverage of the expensive TPC tests, for the selector's differential checks.

Usage: python tools/build_proxies.py SOURCE MAP.gz [OUT.gz]

Run with the same environment as tools/build_map.py (Python 3.13, requirements.lock,
coverage). SOURCE must be an unmodified checkout at the pinned revision.

`test_execute_tpcds` runs every TPC-DS query through four sqlglot steps whose
results can be recomputed cheaply, and one that cannot:

  transpile   transpile(sql, write="duckdb") for the duckdb reference result
  parse       parse_one(sql), pickled to the pool worker and back
  optimize    ensure_schema + optimize + .sql(pretty=True) inside execute()
  plan        Plan(optimized)
  run         everything else: setUpClass (CSV loading), find_tables, the Python
              executor, pickling the tables, comparing the frames

`test_optimized_tpch` only depends on sqlglot through transpile(..., write="duckdb")
of each TPC-H query and its optimized form (phase "transpile"); the rest is "run".

This records, per test and phase, the sqlglot lines executed. A patched run whose
changed lines miss every "run" line, and whose recomputed step results equal the
pinned ones, behaves exactly like the pinned run. Caches that would hide cold-path
lines are reset before each run-phase call. Nothing here reads patches or answers.
"""
from __future__ import annotations

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
import unittest

import coverage

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
TPCDS = 'tests/test_executor.py::TestExecutor::test_execute_tpcds'
TPCH_OPT = 'tests/test_executor.py::TestExecutor::test_optimized_tpch'
FIXTURES = ('tests/fixtures/optimizer/tpc-ds/tpc-ds.sql', 'tests/fixtures/optimizer/tpc-h/tpc-h.sql')


def main():
    source = Path(sys.argv[1]).resolve()
    map_in = Path(sys.argv[2])
    map_out = Path(sys.argv[3]) if len(sys.argv) > 3 else map_in
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        raise SystemExit('Wrong source revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'sqlglot', 'tests']):
        raise SystemExit('Source must be unmodified')
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ['SKIP_INTEGRATION'] = '0'

    cov = coverage.Coverage(source=[str(source / 'sqlglot')], data_file=None)
    phase = ['<import>']

    def switch(name):
        previous = phase[0]
        phase[0] = name
        cov.switch_context(name)
        return previous

    cov.start()
    switch('<import>')
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):
        if info.name != 'sqlglot.__main__':
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - optional dependencies
                pass
    from sqlglot.dialects.dialect import Dialect
    Dialect.classes
    import sqlglot.dialects as dialects_package
    import sqlglot.executor as executor_module
    import sqlglot.generator as generator_module
    import sqlglot.optimizer as optimizer_package
    from sqlglot.dialects import dialect as dialect_module
    builtin = set(getattr(dialect_module, 'DIALECT_MODULE_NAMES', ()))
    registry = getattr(type(Dialect), '_classes', {})

    def reset():
        """Cold caches, as in a fresh pool worker (see tools/build_map.py)."""
        for key in list(registry):
            if key in builtin:
                del registry[key]
        for name in list(getattr(dialects_package, 'MODULE_BY_ATTRIBUTE', {})):
            dialects_package.__dict__.pop(name, None)
        for name in list(getattr(optimizer_package, '_LAZY_ATTRS', {})):
            optimizer_package.__dict__.pop(name, None)
        getattr(generator_module, '_DISPATCH_CACHE', {}).clear()

    run = ['run']  # the run phase of the test in progress
    # Per-query contexts: "<phase>|<test>#<n>", n counting the calls in the order the
    # selector's check records them (fingerprint.run_checks). Caches are reset before
    # each call so every query's lines include the cold paths it depends on.
    counters = {}
    test = ['']

    def numbered(phase, key=None):
        key = key or phase
        n = counters.get(key, 0)
        counters[key] = n + 1
        return f'{phase}|{test[0]}#{n}'

    def within(phase, fn, then_run=False, key=None, current=False, cold=True):
        def wrapper(*args, **kwargs):
            if cold:
                reset()
            name = f'{phase}|{test[0]}#{counters[key] - 1}' if current else numbered(phase, key)
            previous = switch(name)
            try:
                return fn(*args, **kwargs)
            finally:
                switch(run[0] if then_run else previous)
        return wrapper

    class Pool(concurrent.futures.Executor):
        """The test's spawn pool, in process: tables to the worker, query and result back."""

        def __init__(self, *args, initializer=None, initargs=(), **kwargs):
            if initializer:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, /, *args, **kwargs):
            future = concurrent.futures.Future()
            try:
                previous = switch(numbered('parse', 'sent'))  # query n goes to its worker
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                switch(previous)
                if args[1].get('execute'):
                    counters['executed'] = counters.get('executed', 0) + 1
                reset()  # a fresh worker process
                result = fn(*args, **kwargs)
                future.set_result(pickle.loads(pickle.dumps(result)))
            except BaseException as error:  # noqa: BLE001
                future.set_exception(error)
            return future

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    switch('<import>')
    import tests.test_executor as module
    module.ProcessPoolExecutor = Pool
    module.transpile = within('transpile', module.transpile)
    module.parse_one = within('parse', module.parse_one)
    # execute(): ensure_tables (run), ensure_schema, table-arg check (run), optimize, sql, Plan,
    # executor (run). Numbered by executed query; caches were reset when the query was sent.
    executor_module.ensure_schema = within('optimize', executor_module.ensure_schema,
                                           key='executed', current=True, cold=False)
    optimize = executor_module.optimize

    def optimize_then_stay(*args, **kwargs):
        # stays on through `expression.sql(pretty=True)` up to Plan
        switch(f'optimize|{test[0]}#{counters["executed"] - 1}')
        return optimize(*args, **kwargs)

    executor_module.optimize = optimize_then_stay
    executor_module.Plan = within('plan', executor_module.Plan, then_run=True, key='executed', current=True,
                                  cold=False)

    for name, short in (('test_execute_tpcds', 'tpcds'), ('test_optimized_tpch', 'tpch_opt')):
        test[0] = short
        counters.clear()
        run[0] = 'run|' + name
        reset()
        switch(run[0])
        module.TestExecutor.setUpClass()
        module.TestExecutor.cache.clear()
        outcome = unittest.TestResult()
        module.TestExecutor(name).run(outcome)
        module.TestExecutor.tearDownClass()
        switch('<between>')
        if outcome.errors or outcome.failures or not outcome.testsRun:
            raise SystemExit(f'{name} did not pass: {outcome.errors or outcome.failures}')
    switch('<end>')
    cov.stop()

    data = cov.get_data()
    phases: dict[str, dict[str, set[int]]] = {}
    for file in data.measured_files():
        rel = Path(file).resolve().relative_to(source).as_posix()
        for line, contexts in data.contexts_by_lineno(file).items():
            for ctx in contexts:
                phases.setdefault(ctx, {}).setdefault(rel, set()).add(line)

    def lines(name):
        return {path: sorted(found) for path, found in sorted(phases.get(name, {}).items())}

    def masks(prefix):
        """{path: [[line, bitmask of query numbers], ...]} over contexts '<prefix>#<n>'."""
        found: dict[str, dict[int, int]] = {}
        for ctx, files in phases.items():
            if ctx.startswith(prefix + '#'):
                bit = 1 << int(ctx[len(prefix) + 1:])
                for path, numbers in files.items():
                    per_line = found.setdefault(path, {})
                    for line in numbers:
                        per_line[line] = per_line.get(line, 0) | bit
        return {path: sorted(map(list, per_line.items())) for path, per_line in sorted(found.items())}

    with gzip.open(map_in, 'rt') as f:
        out = json.load(f)
    out['proxies'] = {
        'tests': [TPCDS, TPCH_OPT],
        'queries': {short: {phase: masks(f'{phase}|{short}') for phase in ('transpile', 'parse', 'optimize', 'plan')}
                    for short in ('tpcds', 'tpch_opt')},
        'run': {TPCDS: lines('run|test_execute_tpcds'), TPCH_OPT: lines('run|test_optimized_tpch')},
        'fixtures': {path: (source / path).read_text(encoding='utf-8') for path in FIXTURES},
    }
    with gzip.open(map_out, 'wt', compresslevel=9) as f:
        json.dump(out, f, separators=(',', ':'))
    for name in sorted({ctx.split('#')[0] for ctx in phases}):
        merged = {}
        for ctx, files in phases.items():
            if ctx.split('#')[0] == name:
                for path, numbers in files.items():
                    merged.setdefault(path, set()).update(numbers)
        print(name, sum(map(len, merged.values())), 'lines', len([c for c in phases if c.split('#')[0] == name]), 'contexts')


if __name__ == '__main__':
    main()
