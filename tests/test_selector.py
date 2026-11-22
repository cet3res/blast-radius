import ast
from pathlib import Path
import unittest

from selector import (
    Differ, Everything, SpanIndex, apply_hunks, bindings, bool_outcome, bool_regroups, check_compiles,
    default_edits, edited_lines, import_only_command, pair_statements, parse_patch, schedule, swap_sites,
    touched_new_lines,
)


def patch_for(path, old_start, removed, added, context_before=(), context_after=()):
    lines = [f'diff --git a/{path} b/{path}', f'--- a/{path}', f'+++ b/{path}']
    old_len = len(context_before) + len(removed) + len(context_after)
    new_len = len(context_before) + len(added) + len(context_after)
    lines.append(f'@@ -{old_start},{old_len} +{old_start},{new_len} @@')
    lines += [' ' + c for c in context_before] + ['-' + r for r in removed] + ['+' + a for a in added]
    lines += [' ' + c for c in context_after]
    return '\n'.join(lines) + '\n'


def locate(old, new, import_lines=()):
    differ = Differ(set(import_lines), set())
    differ.block(ast.parse(old).body, ast.parse(new).body, ast.parse(old), 'module')
    fast = locate_with_text(old, new, import_lines)
    assert fast == (differ.lines, differ.import_time), (fast, differ.lines, differ.import_time)
    return differ.lines, differ.import_time


def locate_with_text(old, new, import_lines=()):
    """The same, comparing untouched statements by text, as the selector does."""
    matcher = __import__('difflib').SequenceMatcher(None, old.split('\n'), new.split('\n'), autojunk=False)
    removed, added = set(), set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            removed.update(range(i1 + 1, i2 + 1))
            added.update(range(j1 + 1, j2 + 1))
    differ = Differ(set(import_lines), set(), (old.split('\n'), new.split('\n')), (removed, added))
    differ.block(ast.parse(old).body, ast.parse(new).body, ast.parse(old), 'module')
    return differ.lines, differ.import_time


class PatchTests(unittest.TestCase):
    def test_parse_and_apply(self):
        source = 'a\nb\nc\nd\n'
        text = patch_for('x.py', 2, ['b'], ['B', 'B2'], ['a'], ['c'])
        # context before starts one line earlier
        text = text.replace('@@ -2,', '@@ -1,').replace(' +2,', ' +1,')
        files = parse_patch(text)
        self.assertEqual(len(files), 1)
        self.assertEqual((files[0].old, files[0].new), ('x.py', 'x.py'))
        self.assertEqual(apply_hunks(source, files[0].hunks), 'a\nB\nB2\nc\nd\n')

    def test_sql_comment_lines_are_not_headers(self):
        # A removed "-- comment" line shows as "--- comment" inside the hunk.
        text = patch_for('tests/fixtures/x.sql', 1, ['-- comment'], ['++ x'])
        files = parse_patch(text)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].hunks[0][1], [('-', '-- comment'), ('+', '++ x')])

    def test_wrong_context_and_odd_metadata(self):
        files = parse_patch(patch_for('x.py', 1, ['zzz'], ['y']))
        with self.assertRaises(Everything):
            apply_hunks('a\n', files[0].hunks)
        with self.assertRaises(Everything):
            parse_patch('something else\n')
        renamed = parse_patch('diff --git a/a b/b\nsimilarity index 90%\nrename from a\nrename to b\n')
        self.assertTrue(renamed[0].special)

    def test_new_file(self):
        files = parse_patch('diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n')
        self.assertIsNone(files[0].old)
        self.assertEqual(apply_hunks('', files[0].hunks), 'x = 1\ny = 2\n')


class DifferTests(unittest.TestCase):
    def test_comments_and_docstrings_change_nothing(self):
        old = 'def f(x):\n    """Doc."""\n    return x\n'
        new = '# note\ndef f(x):\n    """Other doc."""\n    # why\n    return x\n'
        self.assertEqual(locate(old, new), (set(), False))

    def test_statement_in_function(self):
        old = 'def f(x):\n    y = 1\n    if x:\n        y = 2\n    return y\n'
        new = old.replace('y = 2', 'y = 3')
        self.assertEqual(locate(old, new), ({4}, False))

    def test_condition_change_uses_header_lines(self):
        old = 'def f(x):\n    if (x and\n            x > 1):\n        return 1\n    return 2\n'
        new = old.replace('x and', 'x or')
        self.assertEqual(locate(old, new), ({2, 3}, False))

    def test_insertion_uses_previous_statement(self):
        old = 'def f(x):\n    a = 1\n    b = 2\n    return a + b\n'
        new = 'def f(x):\n    a = 1\n    if x:\n        return 0\n    b = 2\n    return a + b\n'
        self.assertEqual(locate(old, new), ({2}, False))

    def test_insertion_at_block_start_uses_parent(self):
        old = 'def f(x):\n    """Doc."""\n    return x\n'
        new = 'def f(x):\n    """Doc."""\n    x += 1\n    return x\n'
        self.assertEqual(locate(old, new), ({1, 2, 3}, False))

    def test_new_local_name_covers_whole_function(self):
        old = 'def f(x):\n    y = x\n    return y\n'
        new = 'def f(x):\n    z = x\n    return y\n'
        lines, _ = locate(old, new)
        self.assertEqual(lines, {1, 2, 3})

    def test_class_attribute_is_import_time(self):
        old = 'class A:\n    X = 1\n    def f(self):\n        return self.X\n'
        new = old.replace('X = 1', 'X = 2')
        self.assertEqual(locate(old, new), (set(), True))

    def test_lambda_body_in_class_dict_is_run_time(self):
        old = 'class A:\n    F = {\n        "a": lambda s: s.f(\n            0),\n    }\n'
        new = old.replace('0)', '1)')
        self.assertEqual(locate(old, new), ({3, 4}, False))

    def test_function_run_at_import(self):
        old = 'def f():\n    return 1\n\nX = f()\n'
        new = old.replace('return 1', 'return 2')
        self.assertEqual(locate(old, new, import_lines={2, 4}), ({2}, True))

    def test_default_change(self):
        old = 'class A:\n    def f(self, x=1):\n        return x\n'
        new = old.replace('x=1', 'x=2')
        # Plain def, constant default: callers only, no import-time state can change.
        self.assertEqual(locate(old, new), ({2, 3}, False))
        # Unless the function also runs while importing.
        self.assertEqual(locate(old, new, import_lines={2, 3}), ({2, 3}, True))
        # Decorators or defaults that call code may store something different.
        decorated = 'class A:\n    @cache\n    def f(self, x=1):\n        return x\n'
        self.assertEqual(locate(decorated, decorated.replace('x=1', 'x=2'))[1], True)
        called = old.replace('x=1', 'x=make()')
        self.assertEqual(locate(called, called.replace('make()', 'other()'))[1], True)

    def test_shared_state_writes(self):
        old = 'class P:\n    D = {}\n    def f(self):\n        self.D["k"] = 1\n        x = 2\n        self.D.pop("k")\n'
        new = old.replace('        self.D.pop("k")\n', '')
        tree_old, tree_new = ast.parse(old), ast.parse(new)
        differ = Differ(set(), set())
        differ.block(tree_old.body, tree_new.body, tree_old, 'module')
        self.assertEqual(differ.shared, {'D'})
        changed_local = old.replace('x = 2', 'x = 3')
        differ = Differ(set(), set())
        differ.block(tree_old.body, ast.parse(changed_local).body, tree_old, 'module')
        self.assertEqual(differ.shared, set())

    def test_identity_equality_swap(self):
        old = 'def f(x):\n    if x is None:\n        return 1\n    return x == 2\n'
        new = old.replace('x is None', 'x == None')
        tree_old, tree_new = ast.parse(old), ast.parse(new)
        sites = []
        self.assertTrue(swap_sites(tree_old.body[0].body[0], tree_new.body[0].body[0], sites))
        self.assertEqual(sites, [(2, 7)])
        for agree, expected in ((lambda line, col: True, set()), (lambda line, col: False, {2})):
            differ = Differ(set(), set(), agree=agree)
            differ.block(tree_old.body, tree_new.body, tree_old, 'module')
            self.assertEqual(differ.lines, expected)
        other = old.replace('x is None', 'x is not None')
        self.assertFalse(swap_sites(tree_old.body[0].body[0], ast.parse(other).body[0].body[0], []))

    def test_bindings(self):
        f = ast.parse('def f(a):\n    b = 1\n    yield b\n').body[0]
        names, flags, _ = bindings(f)
        self.assertEqual(names, {'a', 'b'})
        self.assertIn('yield', flags)


class CompileCheckTests(unittest.TestCase):
    def check(self, old, new):
        hunks = parse_patch(''.join(__import__('difflib').unified_diff(
            old.splitlines(True), new.splitlines(True), 'a/x.py', 'b/x.py')))[0].hunks
        check_compiles(ast.parse(new), 'x.py', touched_new_lines(hunks))

    def test_errors_inside_touched_statements(self):
        old = 'class A:\n    def f(self):\n        for x in y:\n            pass\n        return 1\n\ndef g():\n    return 2\n'
        self.check(old, old.replace('return 2', 'return 3'))
        with self.assertRaises(SyntaxError):
            self.check(old, old.replace('            pass\n', '            pass\n        break\n'))
        with self.assertRaises(SyntaxError):
            self.check(old, old.replace('        return 1', '        await z'))

    def test_deleted_binding_for_nonlocal(self):
        old = 'def f():\n    x = 1\n    def g():\n        nonlocal x\n        x = 2\n    return g\n'
        with self.assertRaises(SyntaxError):
            self.check(old, old.replace('    x = 1\n', ''))


class AlignmentTests(unittest.TestCase):
    def test_edited_lines(self):
        hunks = parse_patch(patch_for('x.py', 2, ['b'], ['B', 'B2'], ['a'], ['c']).replace('@@ -2,', '@@ -1,'))[0].hunks
        self.assertEqual(edited_lines(hunks), ({2}, {2, 3}))

    def test_pairs_by_name(self):
        olds = ast.parse('def f(): pass\ndef g(): pass\n').body
        news = ast.parse('def f(): pass\nx = 1\ndef g(): return 1\n').body
        self.assertEqual(pair_statements(olds, news), [(0, 0), (1, 2)])

    def test_insert_between_changed_functions(self):
        old = 'def f(x):\n    return x\n\ndef g(x):\n    return x\n'
        new = 'def f(x):\n    return x + 1\n\nY = 2\n\ndef g(x):\n    return x - 1\n'
        self.assertEqual(locate(old, new), ({2, 5}, True))

    def test_span_index(self):
        index = SpanIndex([[1, 5], [2, 3]])
        self.assertEqual(list(index.get(2)), [2, 3])
        self.assertEqual(list(index.get(4)), [1, 2, 3, 4, 5])
        self.assertEqual(index.get(9, (9,)), (9,))


class ScheduleTests(unittest.TestCase):
    def test_best_worker_count(self):
        workers = schedule({n: 1000 for n in range(8)})
        self.assertEqual(len(workers), 2)
        self.assertEqual(sorted(n for w in workers for n in w), list(range(8)))
        self.assertEqual(schedule({}), [])

    def test_one_worker_for_small_tests(self):
        self.assertEqual(len(schedule({n: 1 for n in range(20)})), 1)

    def test_big_test_alone(self):
        workers = schedule({0: 28817, 1: 21454, 2: 3000, 3: 3000})
        cost = max(sum({0: 28817, 1: 21454, 2: 3000, 3: 3000}[n] for n in w) for w in workers) + 2000 * len(workers)
        self.assertEqual(cost, 28817 + 4000)


class DumperTests(unittest.TestCase):
    """The structural text the TPC checks compare (fingerprint.Dumper)."""

    def setUp(self):
        from fingerprint import Dumper

        class Node:
            __slots__ = ('args', 'parent', 'comments', '_hash')

            def __init__(self, **args):
                self.args, self.parent, self.comments, self._hash = args, None, None, None
                for value in args.values():
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, Node):
                            item.parent = self

        class Step:
            def __init__(self, name):
                self.name, self.dependencies, self.dependents = name, set(), set()

        self.Node, self.Step, self.dumper = Node, Step, Dumper(Node, Step)

    def test_everything_but_parent_links_and_hash_counts(self):
        Node, d = self.Node, self.dumper
        a, b = Node(this='x', expressions=[Node(this='y')]), Node(this='x', expressions=[Node(this='y')])
        b._hash = 123
        self.assertEqual(d(a), d(b))
        b.comments = ['c']
        self.assertNotEqual(d(a), d(b))
        c = Node(this='x', expressions=[Node(this='y')])
        c.args['expressions'][0].parent = None  # a child that does not point back
        self.assertNotEqual(d(a), d(c))
        self.assertNotEqual(d(Node(this='x', y='z')), d(Node(y='z', this='x')))  # arg order is kept

    def test_plan_steps(self):
        Step, d = self.Step, self.dumper
        root, leaf = Step('root'), Step('leaf')
        root.dependencies.add(leaf)
        leaf.dependents.add(root)
        text = d(root)
        self.assertIn("dependents='root'", text)
        d.steps = {}
        leaf.limit = 3
        self.assertNotEqual(d(root), text)


class FixtureTests(unittest.TestCase):
    def test_pairs_from_memory(self):
        from fingerprint import TPCDS_FIXTURE, TPCH_FIXTURE, fixture_pairs
        source = (
            'import os\nFILE_DIR = os.path.dirname(__file__)\nFIXTURES_DIR = os.path.join(FILE_DIR, "fixtures")\n'
            'def load_sql_fixture_pairs(filename):\n'
            '    with open(os.path.join(FIXTURES_DIR, filename), encoding="utf-8") as f:\n'
            '        parts = f.read().split(";")\n'
            '        for i in range(0, len(parts) - 1, 2):\n'
            '            yield {}, parts[i].strip(), parts[i + 1].strip()\n'
            'TPCDS_SCHEMA = {}\n'
        )
        _, tpcds, tpch = fixture_pairs(source, {TPCDS_FIXTURE: 'a;b;c;d;', TPCH_FIXTURE: 'x;y;'})
        self.assertEqual(tpcds, [({}, 'a', 'b'), ({}, 'c', 'd')])
        self.assertEqual(tpch, [({}, 'x', 'y')])


class CheckPhaseTests(unittest.TestCase):
    """When a selected TPC test can be judged by recomputing its sqlglot steps."""

    def setUp(self):
        from types import SimpleNamespace
        from selector import Selector
        fixture = 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
        self.m = SimpleNamespace(
            sources={'sqlglot/a.py': 'def f():\n    RUN_NAME = 1\n    x = 2\n    y = 3\n'},
            sets=[[0, 1], [1]],
            data={'proxies': {'fixtures': {fixture: ''}}, 'reads': {'tests/fixtures/x.csv': 1}, 'listings': {}},
            line_sets=lambda path: ({2: 0, 3: 0, 4: 0}, set()) if path == 'sqlglot/a.py' else ({}, set()),
            units=lambda path: {},
        )
        self.proxy = {'name': 'tpcds', 'phases': ('optimize',), 'names': None, 'run': {'sqlglot/a.py': {2}},
                      'queries': {'optimize': {'sqlglot/a.py': {3: 0b101}}}}
        self.selector = Selector.__new__(Selector)
        self.selector.m, self.selector.last_changed, self.selector._texts = self.m, None, None
        self.fixture = fixture

    def phases(self, info, data=None, changed=None):
        self.selector.py_info, self.selector.last_changed = info, changed
        return self.selector.check_phases(1, self.proxy, data or {})

    def test_steps_and_queries(self):
        self.assertEqual(self.phases({'sqlglot/a.py': ({3}, False, set())}), {'optimize': 0b101})
        self.assertIsNone(self.phases({'sqlglot/a.py': ({2}, False, set())}))  # run phase
        self.assertIsNone(self.phases({'sqlglot/a.py': ({4}, False, set())}))  # run by the test, by no step
        self.assertIsNone(self.phases({'sqlglot/a.py': ({3}, False, {'X'})}))  # long-lived state
        self.assertIsNone(self.phases({'tests/helpers.py': ({1}, False, set())}))
        self.assertEqual(self.phases({'tests/test_other.py': ({1}, False, set())}), {})

    def test_import_time_state(self):
        self.assertEqual(self.phases({}, changed={'sqlglot.a:K': {'DATA': 'd'}}), {})  # no step names it
        self.assertEqual(self.phases({}, changed={'sqlglot.a:K': {'x': 'd'}}), {'optimize': 0b101})
        self.assertIsNone(self.phases({}, changed={'sqlglot.a:K': {'method': 'c'}}))
        self.assertIsNone(self.phases({}, changed={'sqlglot.a:K': {'RUN_NAME': 'd'}}))  # read by the run phase
        self.assertIsNone(self.phases({}, changed={'<init>': {'modules': 'd'}}))

    def test_data_files(self):
        self.assertEqual(self.phases({}, {self.fixture: 'new text'}), {'inputs': -1})
        self.assertIsNone(self.phases({}, {self.fixture: None}))
        self.selector.read_tests = lambda path: {1} if path == 'tests/fixtures/x.csv' else set()
        self.selector.listing_tests = lambda path: set()
        self.assertIsNone(self.phases({}, {'tests/fixtures/x.csv': None}))
        self.assertEqual(self.phases({}, {'tests/fixtures/y.csv': None}), {})


class KeyNarrowingTests(unittest.TestCase):
    """Changed dict/set entries select the tests that read those entries (tools/build_keys.py)."""

    def setUp(self):
        from types import SimpleNamespace
        from selector import Selector, key_digest
        baseline = {'m:C\0D': {'k1': 'a', 'k2': 'b'}, 'm:C\0S': {'x': '1'}}
        key_map = {
            'sets': [[1], [2], [3], [7]],
            'broken': [9],
            'containers': {
                'm:C\0D': {'keys': {'k1': 0, 'k3': 1}, 'full': 2, 'digest': key_digest(baseline['m:C\0D'])},
                'm:C\0S': {'keys': {'x': 3}, 'full': None, 'digest': 'stale'},
            },
        }
        self.selector = Selector.__new__(Selector)
        self.selector.m = SimpleNamespace(key_map=lambda: key_map)
        self.selector.state = SimpleNamespace(baseline_keys=lambda: baseline)
        self.selector.key_tests = set()

    def test_changed_and_added_keys(self):
        changed = {'m:C': {'D': 'd', 'other': 'd'}}
        keyed = {'m:C\0D': {'k1': 'A', 'k2': 'b', 'k3': 'c'}}  # k1 changed, k3 added
        left = self.selector.narrow_containers(changed, keyed, None)
        self.assertEqual(left, {'m:C': {'other': 'd'}})
        self.assertEqual(self.selector.key_tests, {1, 2, 3, 9})  # k1 and k3 readers, full reader, broken

    def test_unmapped_or_stale_containers_keep_the_usual_handling(self):
        changed = {'m:C': {'S': 'd'}, '<init>': {'modules': 'x'}}
        left = self.selector.narrow_containers(changed, {'m:C\0S': {}}, None)
        self.assertEqual(left, changed)  # digest mismatch
        self.assertEqual(self.selector.key_tests, set())
        self.assertEqual(self.selector.narrow_containers({'m:C': {'D': 'd'}}, {}, None), {'m:C': {'D': 'd'}})


class ValueProfileTests(unittest.TestCase):
    """Default-argument, annotation-only and and/or edits judged by per-test profiles."""

    def fn(self, text):
        return ast.parse(text).body[0]

    def test_default_edits(self):
        a = self.fn('def f(x, y=None, *, z=1) -> int: pass')
        self.assertEqual(default_edits(a, self.fn('def f(x, y=0, *, z=1) -> int: pass'), False), ['y'])
        self.assertEqual(default_edits(a, self.fn('def f(x, y=None, *, z=2) -> int: pass'), False), ['z'])
        self.assertEqual(default_edits(a, self.fn('def f(x, y=None, *, z=1) -> str: pass'), True), [])
        self.assertIsNone(default_edits(a, self.fn('def f(x, y=None, *, z=1) -> str: pass'), False))
        self.assertIsNone(default_edits(a, self.fn('def f(x, w=None, *, z=1) -> int: pass'), True))
        self.assertIsNone(default_edits(a, self.fn('def f(x=1, y=None, *, z=1) -> int: pass'), True))

    def test_regroups_and_outcomes(self):
        old = ast.parse('if a and b and c and d: pass').body[0]
        new = ast.parse('if a and b or c and d: pass').body[0]
        found = []
        self.assertTrue(bool_regroups(old, new, found))
        (line, col, leaves, old_shape, new_shape, negated), = found
        self.assertEqual(leaves, 4)
        self.assertNotEqual(bool_outcome(old_shape, 'TTFF'), bool_outcome(new_shape, 'TTFF'))  # new: a and b
        self.assertEqual(bool_outcome(old_shape, 'TFTF'), bool_outcome(new_shape, 'TFTF'))  # both false
        self.assertNotEqual(bool_outcome(old_shape, 'TTFT'), bool_outcome(new_shape, 'TTFT'))
        self.assertEqual(bool_outcome(old_shape, 'FFFF'), bool_outcome(new_shape, 'FFFF'))
        self.assertEqual(bool_outcome(new_shape, 'FETT'), True)  # `a and b` stops at a
        self.assertEqual(bool_outcome(old_shape, 'TETT'), 'raise')
        found = []
        self.assertTrue(bool_regroups(old, ast.parse('if a and not b and c and d: pass').body[0], found))
        self.assertEqual(found[0][5], (1,))
        found = []
        self.assertTrue(bool_regroups(ast.parse('if a and b is None: pass').body[0],
                                      ast.parse('if a and b is not None: pass').body[0], found))
        self.assertEqual(found[0][5], (1,))
        self.assertFalse(bool_regroups(old, ast.parse('if a and c and b and d: pass').body[0], []))

    def test_differ_hooks(self):
        old = 'from __future__ import annotations\ndef f(x, y=None) -> int:\n    return x\n'
        differ = Differ(set(), set(), postponed=True, has_function=lambda key: key == '2:f')
        differ.block(ast.parse(old).body, ast.parse(old.replace('-> int', '-> str')).body, ast.parse(old), 'module')
        self.assertEqual((differ.lines, differ.default_changes), (set(), []))
        differ = Differ(set(), set(), postponed=True, has_function=lambda key: key == '2:f')
        differ.block(ast.parse(old).body, ast.parse(old.replace('y=None', 'y=0')).body, ast.parse(old), 'module')
        self.assertEqual((differ.lines, differ.default_changes), (set(), [('2:f', ['y'])]))
        body = 'def f(a, b):\n    if a and b:\n        return 1\n'
        differ = Differ(set(), set(), has_bool_site=lambda line, col, leaves: (line, leaves) == (2, 2))
        differ.block(ast.parse(body).body, ast.parse(body.replace('a and b', 'a or b')).body, ast.parse(body), 'module')
        self.assertEqual(differ.lines, set())
        self.assertEqual([r[:3] for r in differ.regroups], [(2, 7, 2)])
        self.assertEqual(differ.regroups[0][5], ())


class VerifyTests(unittest.TestCase):
    """Selected tests run against the patch when that is cheap next to what it saves."""

    def setUp(self):
        import time
        from types import SimpleNamespace
        import selector
        self.selector_module = selector
        self.ran = []
        catalog = [{'number': 0, 'id': 'tests/a.py::A::t0'}, {'number': 1, 'id': 'tests/a.py::A::t1'},
                   {'number': 2, 'id': 'tests/b.py::B::t2'}]
        costs = {'<fork>': 0.05, 'tests/a.py': 0.1, 'deps:tests/a.py': ['tests/b.py'], 'tests/b.py': 0.1,
                 'tests/b.py::B': 5.0}

        def run_tests(ids, overrides, cpu_limit):
            self.ran.append(list(ids))
            return {ids[0]: True, ids[1]: False} if len(ids) > 1 else {ids[0]: True}

        state = SimpleNamespace(started=time.monotonic(), test_costs=lambda: costs, run_tests=run_tests)
        m = SimpleNamespace(catalog=catalog, import_only_spawners=set(), other_spawners=set(), number_of_index=[0, 1, 2])
        self.sel = selector.Selector.__new__(selector.Selector)
        self.sel.m, self.sel.state, self.sel.verifiable, self.sel.patched = m, state, True, {}
        self.old_source = selector.TEST_SOURCE
        selector.TEST_SOURCE = Path(__file__).resolve().parents[1]  # has a tests/ directory

    def tearDown(self):
        self.selector_module.TEST_SOURCE = self.old_source

    def test_cheap_selection_runs_cheapest_first(self):
        self.assertEqual(self.sel.verify({0, 1}, {0: 100, 1: 50, 2: 10}), {0})
        self.assertEqual(self.ran, [['tests/a.py::A::t1', 'tests/a.py::A::t0']])

    def test_expensive_class_setup_is_skipped(self):
        self.assertEqual(self.sel.verify({2}, {0: 100, 1: 50, 2: 10}), {2})
        self.assertEqual(self.ran, [])

    def test_not_for_other_edits(self):
        self.sel.verifiable = False
        self.assertEqual(self.sel.verify({0}, {0: 100, 1: 50, 2: 10}), {0})
        self.assertEqual(self.ran, [])


class CommandTests(unittest.TestCase):
    def test_import_only(self):
        code = "import sqlglot; assert len(sqlglot.Dialect._classes) == 1; print('Success')"
        self.assertTrue(import_only_command(['python', '-c', code]))
        self.assertFalse(import_only_command(['python', '-c', 'import sqlglot; sqlglot.transpile("x")']))
        self.assertFalse(import_only_command(['sh', '-c', 'ls']))


if __name__ == '__main__':
    unittest.main()
