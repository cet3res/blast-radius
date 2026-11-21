import ast
import unittest

from selector import (
    Differ, Everything, SpanIndex, apply_hunks, bindings, check_compiles, edited_lines, import_only_command,
    pair_statements, parse_patch, schedule, swap_sites, touched_new_lines,
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


class CommandTests(unittest.TestCase):
    def test_import_only(self):
        code = "import sqlglot; assert len(sqlglot.Dialect._classes) == 1; print('Success')"
        self.assertTrue(import_only_command(['python', '-c', code]))
        self.assertFalse(import_only_command(['python', '-c', 'import sqlglot; sqlglot.transpile("x")']))
        self.assertFalse(import_only_command(['sh', '-c', 'ls']))


if __name__ == '__main__':
    unittest.main()
