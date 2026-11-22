"""Select the tests a sqlglot patch can break, and split them across workers.

Every decision comes from one dependency map built on the pinned, unmodified
sqlglot revision (tools/build_map.py) and from the patch itself:

* Python edits are compared as syntax trees. Comment, whitespace, docstring and
  other no-op edits select nothing.
* Edits inside function bodies select the tests whose run executed the changed
  statement (or reached the insertion point). If the edit changes which names
  the function binds, every test that entered the function is selected.
* Edits evaluated at import time (module and class bodies, decorators, defaults,
  functions called while importing) are judged by importing the patched sqlglot
  in a child process and fingerprinting every module global and class attribute.
  Changed class attributes select the tests that instantiated that class, a
  subclass, or a class of the same dialect; changed globals select the tests
  that ran a line naming them. An import that fails selects everything.
* Data files select the tests that opened them.

Anything not understood selects every test. Logs go to stderr.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import copy
import difflib
import gzip
import io
import json
import marshal
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
CODING = re.compile(r'^[ \t\f]*#.*?coding[:=]')
# pytest reads these from the repository root; a conftest.py is loaded from any test directory.
CONFIG_FILES = ('pyproject.toml', 'setup.cfg', 'setup.py', 'pytest.ini', 'tox.ini', 'conftest.py', '.coveragerc')
WORKER_MS = 2000
# CPU seconds a forked child may use: a check recomputes at most every TPC-DS step
# (a few seconds); a fingerprint imports everything once.
CHECK_CPU_LIMIT = 25
FINGERPRINT_CPU_LIMIT = 60
# Wall-clock guards for the whole run (the scorer allows 4.5 minutes): past the
# first, selected TPC tests are kept without checks; past the second, commits that
# need a patched import select every test. (Practice runs get 3 minutes, the final 4.5.)
CHECKS_UNTIL = 100.0
IMPORTS_UNTIL = 140.0
# Verification by running selected tests (plan step 4): a checkout of the pinned
# revision (tests and fixtures) put in the image at build; costs measured there.
VERIFY_RATIO = 0.5    # verify when the estimated CPU is at most this share of the saving
RUN_FACTOR = 1.2      # CPU seconds per recorded second of a test, in the container
VERIFY_UNTIL = 120.0  # wall-clock guard: no verification after this
CHECK_PHASES = ('transpile', 'parse', 'optimize', 'plan')
MAX_WORKERS = 8
HERE = Path(__file__).resolve().parent
TEST_SOURCE = Path(os.environ.get('BLAST_SQLGLOT_SOURCE', str(HERE / 'sqlglot-src')))


class Everything(Exception):
    """The change is not understood well enough to rule any test out."""


def log(*args):
    print(*args, file=sys.stderr)


# ---------------------------------------------------------------- patches

class FilePatch:
    def __init__(self):
        self.old = None  # path, or None for a new file
        self.new = None  # path, or None for a deleted file
        self.hunks = []  # (old_start, [(tag, text)])
        self.special = False  # rename, copy, or unknown metadata
        self.binary = False
        self.mode_only = False
        self.git_paths = None  # paths from the "diff --git" line


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    files = []
    current = None
    i = 0
    n = len(lines)

    def path_of(raw):
        raw = raw.split('\t')[0].rstrip('\r')
        if raw == '/dev/null':
            return None
        if raw.startswith('"'):
            raise Everything('quoted path')
        if raw[:2] in ('a/', 'b/'):
            return raw[2:]
        raise Everything('unexpected path prefix')

    while i < n:
        line = lines[i]
        if line.startswith('diff --git '):
            current = FilePatch()
            files.append(current)
            rest = line[len('diff --git '):]
            if rest.startswith('a/') and rest.count(' b/') == 1:
                old, new = rest[2:].split(' b/')
                current.git_paths = (old, new)
            i += 1
            continue
        if line.startswith('--- ') and i + 1 < n and lines[i + 1].startswith('+++ '):
            if current is None or current.hunks or current.old is not None or current.new is not None:
                current = FilePatch()
                files.append(current)
            current.old = path_of(line[4:])
            current.new = path_of(lines[i + 1][4:])
            i += 2
            continue
        if line.startswith('@@ '):
            match = HUNK.match(line)
            if match is None or current is None:
                raise Everything('bad hunk header')
            old_start = int(match[1])
            old_len = 1 if match[2] is None else int(match[2])
            new_len = 1 if match[4] is None else int(match[4])
            body = []
            i += 1
            while old_len > 0 or new_len > 0:
                if i >= n:
                    raise Everything('truncated hunk')
                raw = lines[i]
                tag = raw[:1]
                if tag == ' ' or raw == '':
                    old_len -= 1
                    new_len -= 1
                    body.append((' ', raw[1:]))
                elif tag == '-':
                    old_len -= 1
                    body.append(('-', raw[1:]))
                elif tag == '+':
                    new_len -= 1
                    body.append(('+', raw[1:]))
                elif raw.startswith('\\'):
                    body.append(('\\', raw))
                else:
                    raise Everything('bad hunk line')
                i += 1
                if old_len < 0 or new_len < 0:
                    raise Everything('hunk length mismatch')
            while i < n and lines[i].startswith('\\'):
                body.append(('\\', lines[i]))
                i += 1
            current.hunks.append((old_start, body))
            continue
        if current is not None:
            if line.startswith(('index ', 'similarity index', 'dissimilarity index')):
                pass
            elif line.startswith(('new file mode', 'deleted file mode')):
                pass
            elif line.startswith(('old mode', 'new mode')):
                current.mode_only = True
            elif line.startswith(('Binary files', 'GIT binary patch')):
                current.binary = True
                if line.startswith('GIT binary patch'):
                    while i + 1 < n and not lines[i + 1].startswith('diff --git '):
                        i += 1  # skip the encoded data
            elif line.startswith(('rename from', 'rename to', 'copy from', 'copy to')):
                current.special = True
            elif line.strip():
                raise Everything('unrecognized patch line')
        elif line.strip():
            raise Everything('text outside a file diff')
        i += 1
    for f in files:
        if f.old is None and f.new is None and not f.special:
            if f.binary and f.git_paths and f.git_paths[0] == f.git_paths[1]:
                f.old = f.new = f.git_paths[0]
                continue
            if f.mode_only and not f.hunks:
                continue
            raise Everything('file diff without paths')
    return files


def edited_lines(hunks) -> tuple[set[int], set[int]]:
    """Removed line numbers in the old text, added line numbers in the new text."""
    removed, added = set(), set()
    offset = 0
    for old_start, body in hunks:
        old_line = max(old_start, 1)
        new_line = old_line + offset
        for tag, _ in body:
            if tag == ' ':
                old_line += 1
                new_line += 1
            elif tag == '-':
                removed.add(old_line)
                old_line += 1
                offset -= 1
            elif tag == '+':
                added.add(new_line)
                new_line += 1
                offset += 1
    return removed, added


def touched_new_lines(hunks) -> set[int]:
    """New line numbers that an edit added, or that sit next to removed lines."""
    touched = set()
    offset = 0
    for old_start, body in hunks:
        new_line = max(old_start, 1) + offset
        for tag, _ in body:
            if tag == ' ':
                new_line += 1
            elif tag == '-':
                touched.update((new_line - 1, new_line))
                offset -= 1
            elif tag == '+':
                touched.add(new_line)
                new_line += 1
                offset += 1
    return touched


def check_compiles(tree, path, touched: set[int]):
    """Compile the statements an edit touched, inside the classes that hold them.

    Errors that only the compiler reports (return or await outside a function, a
    nonlocal without binding...) are local to the function or class statement that
    has them. A touched __future__ import, or nothing touched, compiles everything.
    """
    marks = sorted(touched)

    def is_touched(node):
        i = bisect.bisect_left(marks, first_line(node))
        return i < len(marks) and marks[i] <= node.end_lineno

    future = [s for s in tree.body if isinstance(s, ast.ImportFrom) and s.module == '__future__']
    if not marks or any(is_touched(s) for s in future):
        compile(tree, path, 'exec', dont_inherit=True)
        return

    def prune(stmt):
        if not isinstance(stmt, ast.ClassDef):
            return stmt
        members = [prune(s) for s in stmt.body if is_touched(s)] or [ast.Pass()]
        copy = ast.ClassDef(
            name=stmt.name, bases=stmt.bases, keywords=stmt.keywords, body=members,
            decorator_list=stmt.decorator_list, type_params=getattr(stmt, 'type_params', []),
        )
        return ast.fix_missing_locations(ast.copy_location(copy, stmt))

    body = [prune(s) for s in tree.body if is_touched(s)]
    if not body:
        compile(tree, path, 'exec', dont_inherit=True)
        return
    compile(ast.Module(body=future + body, type_ignores=[]), path, 'exec', dont_inherit=True)


def apply_hunks(source: str, hunks) -> str:
    """The patched text, verifying every context and removed line."""
    old = source.split('\n') if source else []
    if source.endswith('\n'):
        old.pop()
    out = []
    cursor = 0
    for old_start, body in hunks:
        start = old_start - 1 if old_start > 0 else 0
        if start < cursor or start > len(old):
            raise Everything('hunk out of order')
        out.extend(old[cursor:start])
        cursor = start
        for tag, text in body:
            if tag in (' ', '-'):
                if cursor >= len(old) or old[cursor] != text:
                    raise Everything('context does not match the pinned source')
                if tag == ' ':
                    out.append(text)
                cursor += 1
            elif tag == '+':
                out.append(text)
    out.extend(old[cursor:])
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- python structure

def dump(node) -> str:
    return ast.dump(node)


def is_noop(stmt) -> bool:
    return isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))


BLOCK_FIELDS = ('body', 'orelse', 'finalbody', 'handlers', 'cases')


def header_dump(node) -> str:
    """The node with its nested statement blocks removed."""
    fields = []
    for name, value in ast.iter_fields(node):
        if name in BLOCK_FIELDS:
            continue
        fields.append(f'{name}={dump(value) if isinstance(value, ast.AST) else _dump_value(value)}')
    return type(node).__name__ + '(' + ','.join(fields) + ')'


def _dump_value(value):
    if isinstance(value, list):
        return '[' + ','.join(dump(v) if isinstance(v, ast.AST) else repr(v) for v in value) + ']'
    return repr(value)


def first_line(node) -> int:
    decorators = getattr(node, 'decorator_list', None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


def header_lines(node) -> range:
    """Lines of a compound statement up to its first nested statement."""
    start = first_line(node)
    body = getattr(node, 'body', None)
    if isinstance(body, list) and body and isinstance(body[0], ast.AST) and hasattr(body[0], 'lineno'):
        end = max(start, body[0].lineno - 1)
    else:
        end = node.end_lineno
    return range(start, end + 1)


def full_lines(node) -> range:
    return range(first_line(node), node.end_lineno + 1)


def bindings(fn) -> tuple:
    """What decides a function's variable scoping and kind."""
    names, flags = set(), set()
    args = fn.args
    for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
        if a is not None:
            names.add(a.arg)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
                for d in child.decorator_list:
                    walk_expr(d)
                if not isinstance(child, ast.ClassDef):
                    for d in child.args.defaults + [x for x in child.args.kw_defaults if x is not None]:
                        walk_expr(d)
                else:
                    for b in child.bases + [k.value for k in child.keywords]:
                        walk_expr(b)
                continue
            if isinstance(child, ast.Lambda):
                for d in child.args.defaults + [x for x in child.args.kw_defaults if x is not None]:
                    walk_expr(d)
                continue
            visit(child)
            walk(child)

    def walk_expr(node):
        visit(node)
        walk(node)

    def visit(child):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            names.add(child.target.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(child, ast.Global):
            flags.add(('global',) + tuple(child.names))
        elif isinstance(child, ast.Nonlocal):
            flags.add(('nonlocal',) + tuple(child.names))
        elif isinstance(child, (ast.Yield, ast.YieldFrom)):
            flags.add('yield')
        elif isinstance(child, ast.Await):
            flags.add('await')
        elif isinstance(child, ast.Name) and child.id in ('super', '__class__'):
            flags.add('super')
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif type(child).__name__ in ('MatchAs', 'MatchStar') and child.name:
            names.add(child.name)
        elif type(child).__name__ == 'MatchMapping' and child.rest:
            names.add(child.rest)

    walk(fn)
    return frozenset(names), frozenset(flags), isinstance(fn, ast.AsyncFunctionDef)


MUTATORS = {
    'append', 'extend', 'insert', 'pop', 'popitem', 'remove', 'clear', 'update', 'setdefault',
    'add', 'discard', 'difference_update', 'intersection_update', 'symmetric_difference_update', 'sort', 'reverse',
}
SHARED_NAME = re.compile(r'^_?[A-Z][A-Z0-9_]*$')


def shared_writes(node, module_names) -> set[str]:
    """Class constants or module globals that code in `node` writes or mutates in place.

    Such state outlives the call: a later test that never runs this code still sees it.
    """
    found = set()

    def target_name(expr):
        if isinstance(expr, ast.Attribute):
            if SHARED_NAME.match(expr.attr):
                return expr.attr
            return None
        if isinstance(expr, ast.Name) and expr.id in module_names:
            return expr.id
        return None

    for sub in ast.walk(node):
        targets = []
        if isinstance(sub, (ast.Assign, ast.Delete)):
            targets = sub.targets
        elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
            targets = [sub.target]
        for target in targets:
            if isinstance(target, ast.Subscript):
                name = target_name(target.value)
                if name:
                    found.add(name)
            elif isinstance(target, ast.Attribute) and not (
                isinstance(target.value, ast.Name) and target.value.id == 'self'
            ):
                name = target_name(target)
                if name:
                    found.add(name)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in MUTATORS:
            name = target_name(sub.func.value)
            if name:
                found.add(name)
        if isinstance(sub, ast.Global):
            found.update(sub.names)
    return found


def module_level_names(tree) -> set[str]:
    names = set()
    for stmt in tree.body:
        for sub in ast.walk(stmt) if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else ():
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                names.add(sub.id)
    return names


SWAPS = ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq})


def swap_sites(a, b, sites, top=True) -> bool:
    """True when two trees differ only by `is`/`==` (or `is not`/`!=`) swaps, collected in sites.

    For a statement, only its own header is compared, not nested statement blocks.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
        if {type(a.ops[0]), type(b.ops[0])} not in SWAPS:
            return False
        sites.append((a.lineno, a.col_offset))
        return swap_sites(a.left, b.left, sites, False) and swap_sites(a.comparators[0], b.comparators[0], sites, False)
    if isinstance(a, ast.AST):
        for field in a._fields:
            if top and isinstance(a, ast.stmt) and field in BLOCK_FIELDS:
                continue
            if not swap_sites(getattr(a, field, None), getattr(b, field, None), sites, False):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(swap_sites(x, y, sites, False) for x, y in zip(a, b))
    return a == b


def bool_shape(node, leaves):
    """The and/or structure of a boolean tree over its leaves (collected in order)."""
    if isinstance(node, ast.BoolOp):
        return (isinstance(node.op, ast.Or), tuple(bool_shape(v, leaves) for v in node.values))
    leaves.append(node)
    return len(leaves) - 1


NEGATED = {ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In,
           ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}


def negates(x, y) -> bool:
    """y is the logical negation of x: `not x`, or a single comparison with the opposite
    operator (is/is not, in/not in, ==/!=)."""
    if isinstance(y, ast.UnaryOp) and isinstance(y.op, ast.Not) and dump(y.operand) == dump(x):
        return True
    if isinstance(x, ast.UnaryOp) and isinstance(x.op, ast.Not) and dump(x.operand) == dump(y):
        return True
    if (isinstance(x, ast.Compare) and isinstance(y, ast.Compare) and len(x.ops) == len(y.ops) == 1
            and NEGATED.get(type(x.ops[0])) is type(y.ops[0])):
        return dump(x.left) == dump(y.left) and dump(x.comparators[0]) == dump(y.comparators[0])
    return False


def bool_regroups(a, b, found, top=True) -> bool:
    """True when two trees differ only in how `and`/`or` combine the same leaves, some of
    them possibly negated. Changed boolean trees are collected as (line, col, leaf count,
    old shape, new shape, negated leaves) of the outermost operator. For a statement,
    only its own header is compared."""
    if isinstance(a, ast.BoolOp) and isinstance(b, ast.BoolOp):
        old_leaves, new_leaves = [], []
        old, new = bool_shape(a, old_leaves), bool_shape(b, new_leaves)
        if len(old_leaves) != len(new_leaves):
            return False
        negated = []
        for i, (x, y) in enumerate(zip(old_leaves, new_leaves)):
            if dump(x) != dump(y):
                if not negates(x, y):
                    return False
                negated.append(i)
        if old != new or negated:
            found.append((a.lineno, a.col_offset, len(old_leaves), old, new, tuple(negated)))
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for field in a._fields:
            if top and isinstance(a, ast.stmt) and field in BLOCK_FIELDS:
                continue
            if not bool_regroups(getattr(a, field, None), getattr(b, field, None), found, False):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(bool_regroups(x, y, found, False) for x, y in zip(a, b))
    return a == b


def negate_leaves(vector, negated):
    flip = {'T': 'F', 'F': 'T', 'E': 'E'}
    return ''.join(flip[c] if i in negated else c for i, c in enumerate(vector))


def bool_outcome(shape, vector):
    """Truth of a boolean tree for leaf truths 'T'/'F', or 'raise' on reaching an 'E' leaf."""
    if isinstance(shape, int):
        return 'raise' if vector[shape] == 'E' else vector[shape] == 'T'
    is_or, kids = shape
    result = None
    for kid in kids:
        result = bool_outcome(kid, vector)
        if result == 'raise' or result is is_or:
            return result
    return result


def default_edits(a, b, postponed) -> list[str] | None:
    """Parameters whose default value alone changed between two versions of a def, or None
    when anything else in the header changed. Annotations are ignored when the module
    postpones them (`from __future__ import annotations`): they are then stored as strings
    that sqlglot never reads (only parameter names, via getfullargspec/signature)."""
    def normalized(fn):
        args = copy.deepcopy(fn.args)
        args.defaults = [ast.Constant(None) for _ in args.defaults]
        args.kw_defaults = [None if d is None else ast.Constant(None) for d in args.kw_defaults]
        if postponed:
            for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if arg is not None:
                    arg.annotation = None
        clone = ast.FunctionDef(name=fn.name, args=args, body=[], decorator_list=fn.decorator_list,
                                returns=None if postponed else fn.returns, type_comment=None,
                                type_params=getattr(fn, 'type_params', []))
        return header_dump(clone)

    if normalized(a) != normalized(b):
        return None
    positional = [x.arg for x in a.args.posonlyargs + a.args.args]
    changed = []
    offset = len(positional) - len(a.args.defaults)
    for i, (x, y) in enumerate(zip(a.args.defaults, b.args.defaults)):
        if dump(x) != dump(y):
            changed.append(positional[offset + i])
    for arg, x, y in zip(a.args.kwonlyargs, a.args.kw_defaults, b.args.kw_defaults):
        if x is not None and dump(x) != dump(y):
            changed.append(arg.arg)
    return changed


def postpones_annotations(tree) -> bool:
    return any(isinstance(s, ast.ImportFrom) and s.module == '__future__' and any(n.name == 'annotations' for n in s.names)
               for s in tree.body)


SAFE_EXPRESSIONS = (
    ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.UnaryOp,
    ast.BinOp, ast.BoolOp, ast.Compare, ast.Subscript, ast.IfExp, ast.Slice, ast.Starred,
    ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
)


def inert_header_change(a, b) -> bool:
    """A def whose new arguments, defaults and annotations cannot change import-time state.

    Without decorators the stored object is the plain function, and the fingerprint
    ignores the defaults and annotations of functions defined in source; they only
    matter to callers, which the function's coverage selects. Expressions that could
    run code with side effects while defining (calls, lambdas...) do not qualify.
    """
    if a.name != b.name or a.decorator_list or b.decorator_list:
        return False
    if [dump(t) for t in getattr(a, 'type_params', [])] != [dump(t) for t in getattr(b, 'type_params', [])]:
        return False
    for fn in (a, b):
        parts = [fn.args, fn.returns] if fn.returns is not None else [fn.args]
        for part in parts:
            for node in ast.walk(part):
                if isinstance(node, (ast.arguments, ast.arg, ast.keyword)):
                    continue
                if not isinstance(node, SAFE_EXPRESSIONS):
                    return False
    return True


def statement_name(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        return tuple(dump(t) for t in targets)
    return None


def pair_statements(olds, news) -> list[tuple[int, int]]:
    """Order-preserving pairs of old and new statements that are versions of each other."""
    if len(olds) == len(news):
        return list(zip(range(len(olds)), range(len(news))))
    pairs = []
    start = 0
    for nj, b in enumerate(news):
        for oi in range(start, len(olds)):
            a = olds[oi]
            if type(a) is type(b) and statement_name(a) == statement_name(b):
                pairs.append((oi, nj))
                start = oi + 1
                break
    return pairs


def lambda_only_diff(a, b, out) -> bool:
    """True when two trees differ only inside lambda bodies (collected into out)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Lambda):
        if dump(a.args) != dump(b.args):
            return False
        if dump(a.body) != dump(b.body):
            out.append(a)
        return True
    if isinstance(a, ast.AST):
        return all(lambda_only_diff(getattr(a, f, None), getattr(b, f, None), out) for f in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(lambda_only_diff(x, y, out) for x, y in zip(a, b))
    return a == b


class Differ:
    """Locate a Python edit: lines whose executors are affected, and import-time edits."""

    def __init__(self, import_lines: set[int], import_lambdas: set[int], texts=None, edited=None, module_names=(),
                 agree=None, postponed=False, has_function=None, has_bool_site=None):
        # agree(line, col) -> True when identity and equality give the same answer for every
        # operand pair the tests evaluated at that comparison (from the type profile).
        self.agree = agree
        # Value profiles (tools/build_values.py): has_function(key) and has_bool_site(line, col).
        self.postponed = postponed
        self.has_function = has_function
        self.has_bool_site = has_bool_site
        self.default_changes: list[tuple[str, list[str]]] = []  # (function key, parameters)
        self.regroups: list[tuple] = []  # (line, col, leaves, old shape, new shape, negated leaves)
        self.profiled_lines: set[int] = set()  # statements judged by a profile instead of coverage
        self.lines: set[int] = set()
        self.shared: set[str] = set()  # long-lived state written by edited run-time code
        self.module_names = set(module_names)
        self.import_time = False
        self.import_lines = import_lines
        self.import_lambdas = import_lambdas
        # Source lines and edited line numbers of (old, new): statements away from the
        # edit keep their exact text, which is cheaper to compare than their syntax tree.
        self.texts = texts
        self.edited = [sorted(e) for e in edited] if edited else None

    def key(self, stmt, side):
        if self.texts is None:
            return dump(stmt)
        start, end = first_line(stmt), stmt.end_lineno
        edited = self.edited[side]
        i = bisect.bisect_left(edited, start)
        if i < len(edited) and edited[i] <= end:
            # Touched by the edit: never "equal", so it gets compared node by node.
            return f'\x01{side}:{start}'
        return '\x00' + '\n'.join(self.texts[side][start - 1:end])

    def add(self, lines):
        lines = set(lines)
        self.lines |= lines
        if lines & self.import_lines:
            self.import_time = True

    def add_function(self, fn):
        """Every test that entered fn. Its def line runs at import by definition; only its
        body running at import (a call while importing) makes this an import-time edit."""
        self.lines |= set(full_lines(fn))
        if set(range(fn.body[0].lineno, fn.end_lineno + 1)) & self.import_lines:
            self.import_time = True

    def block(self, old, new, parent, scope):
        old = [s for s in old if not is_noop(s)]
        new = [s for s in new if not is_noop(s)]
        od = [self.key(s, 0) for s in old]
        nd = [self.key(s, 1) for s in new]
        if od == nd:
            return
        matcher = difflib.SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            olds, news = old[i1:i2], new[j1:j2]
            pairs = pair_statements(olds, news)
            for oi, nj in pairs:
                self.node(olds[oi], news[nj], scope)
            paired_old = {oi for oi, _ in pairs}
            for oi, a in enumerate(olds):
                if oi not in paired_old:
                    self.changed(a, scope)
            paired_new = {nj for _, nj in pairs}
            for nj in range(len(news)):
                if nj not in paired_new:
                    if scope == 'function':
                        self.shared |= shared_writes(news[nj], self.module_names)
                    # Inserted before the first old statement paired with a later new one.
                    after = [oi for oi, pj in pairs if pj > nj]
                    self.inserted(old, i1 + (min(after) if after else len(olds)), parent, scope)

    def changed(self, stmt, scope, new=None):
        if scope == 'function':
            self.add(full_lines(stmt))
            self.shared |= shared_writes(stmt, self.module_names)
            if new is not None:
                self.shared |= shared_writes(new, self.module_names)
        else:
            self.import_time = True
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.add(full_lines(stmt))

    def inserted(self, old, index, parent, scope):
        if scope != 'function':
            self.import_time = True
            return
        if index > 0:
            self.add(full_lines(old[index - 1]))
        else:
            self.add(full_lines(parent))

    def flipped(self, a, b, lines) -> bool:
        """The edit only regroups `and`/`or` over the same leaves, at boolean trees whose
        leaf truths the tests profiled."""
        if self.has_bool_site is None:
            return False
        found = []
        if not (bool_regroups(a, b, found) and found and all(self.has_bool_site(f[0], f[1], f[2]) for f in found)):
            return False
        self.regroups.extend(found)
        self.profiled_lines |= set(lines)
        if set(lines) & self.import_lines:
            self.import_time = True
        return True

    def equivalent(self, a, b) -> bool:
        """The edit only swaps `is` and `==` where the tests never see a difference."""
        if self.agree is None:
            return False
        sites = []
        return swap_sites(a, b, sites) and bool(sites) and all(self.agree(*site) for site in sites)

    def node(self, a, b, scope):
        if type(a) is not type(b):
            self.changed(a, scope, b)
            return
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if header_dump(a) != header_dump(b) and scope != 'function' and inert_header_change(a, b):
                changed = default_edits(a, b, self.postponed)
                key = f'{first_line(a)}:{a.name}'
                if changed is not None and (not changed or (self.has_function and self.has_function(key))):
                    # Only annotations (stored as strings nobody reads) and/or default values
                    # changed: a default matters only to calls that left it out.
                    if changed:
                        self.default_changes.append((key, changed))
                        self.profiled_lines |= set(full_lines(a))
                        if set(range(a.body[0].lineno, a.end_lineno + 1)) & self.import_lines:
                            self.import_time = True
                    if bindings(a) != bindings(b):
                        self.add_function(a)
                    self.block(a.body, b.body, a, 'function')
                    return
            if header_dump(a) != header_dump(b):
                # Callers see new arguments, defaults or decorators.
                self.add_function(a)
                if scope == 'function':
                    self.add(header_lines(a))
                elif not inert_header_change(a, b):
                    # Decorators or evaluated defaults can change what the definition stores.
                    self.import_time = True
            if bindings(a) != bindings(b):
                self.add_function(a)
            self.block(a.body, b.body, a, 'function')
            return
        if isinstance(a, ast.ClassDef):
            if header_dump(a) != header_dump(b):
                self.changed(a, scope)
                if scope != 'function':
                    self.import_time = True
            self.block(a.body, b.body, a, 'class' if scope != 'function' else 'function')
            return
        if isinstance(a, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            if header_dump(a) != header_dump(b) and not self.equivalent(a, b) and not (
                    scope == 'function' and self.flipped(a, b, header_lines(a))):
                if scope == 'function':
                    self.add(header_lines(a))
                    for field in ('test', 'iter', 'target', 'items'):
                        for side in (a, b):
                            value = getattr(side, field, None)
                            for part in value if isinstance(value, list) else [value] if value else []:
                                self.shared |= shared_writes(part, self.module_names)
                else:
                    self.import_time = True
            for field in ('body', 'orelse'):
                if hasattr(a, field):
                    self.block(getattr(a, field), getattr(b, field), a, scope)
            return
        if isinstance(a, (ast.Try, getattr(ast, 'TryStar', ast.Try))):
            if len(a.handlers) != len(b.handlers) or any(
                header_dump(x) != header_dump(y) for x, y in zip(a.handlers, b.handlers)
            ):
                self.changed(a, scope)
            for field in ('body', 'orelse', 'finalbody'):
                self.block(getattr(a, field), getattr(b, field), a, scope)
            if len(a.handlers) == len(b.handlers):
                for x, y in zip(a.handlers, b.handlers):
                    self.block(x.body, y.body, a, scope)
            return
        if dump(a) == dump(b) or self.equivalent(a, b):
            return
        if scope == 'function' and self.flipped(a, b, full_lines(a)):
            return
        if scope == 'function':
            self.changed(a, scope, b)
            return
        lambdas = []
        if lambda_only_diff(a, b, lambdas):
            for lam in lambdas:
                self.lines |= set(full_lines(lam))
                if lam.lineno in self.import_lambdas:
                    self.import_time = True
            return
        self.import_time = True


def coding_lines(text: str) -> list[str]:
    return [line for line in text.split('\n')[:2] if CODING.match(line)]


# ---------------------------------------------------------------- the map

class DependencyMap:
    def __init__(self, path: Path, catalog: list[dict]):
        self.path = Path(path)
        with gzip.open(path, 'rt') as f:
            data = json.load(f)
        if data.get('version') != 2 or data.get('revision') != PIN:
            raise ValueError('Incompatible dependency map')
        self.data = data
        self.sets = data['sets']
        self.sources = data['sources']
        self.map_ids = data['tests']
        number_of = {t['id']: t['number'] for t in catalog}
        self.number_of_index = [number_of.get(test_id) for test_id in self.map_ids]
        known = set(self.map_ids)
        self.always = {t['number'] for t in catalog if t['id'] not in known}
        self.always |= {self.number_of_index[i] for i in data['unmeasured'] if self.number_of_index[i] is not None}
        self.catalog = catalog
        self.file_tests = {}
        for i, test_id in enumerate(self.map_ids):
            self.file_tests.setdefault(test_id.split('::')[0], set()).add(i)
        self._lines = {}
        self._units = {}
        self._trees = {}
        self.subclasses = {}
        for key, ancestors in data['mro'].items():
            for ancestor in ancestors:
                self.subclasses.setdefault(ancestor, set()).add(key)
        self.unit_of = {}
        for dialect, members in data['units'].items():
            for member in members:
                self.unit_of.setdefault(member, set()).add(dialect)
        # Differential checks for the expensive TPC tests (tools/build_proxies.py).
        self.proxies = {}
        proxies = data.get('proxies')
        if proxies and 'tests/helpers.py' in self.sources:
            index = {test_id: i for i, test_id in enumerate(self.map_ids)}
            for test_id, name, phases in ((proxies['tests'][0], 'tpcds', CHECK_PHASES),
                                          (proxies['tests'][1], 'tpch_opt', ('transpile',))):
                if test_id in index and test_id in proxies['run'] and name in proxies['queries']:
                    queries = proxies['queries'][name]
                    self.proxies[index[test_id]] = {
                        'name': name, 'phases': phases, 'names': None,
                        'run': {p: set(v) for p, v in proxies['run'][test_id].items()},
                        # phase -> path -> line -> bitmask of the queries whose step runs it
                        'queries': {ph: {p: dict(map(tuple, pairs)) for p, pairs in queries.get(ph, {}).items()}
                                    for ph in phases},
                    }
        self.import_only_spawners = set()
        self.other_spawners = set()
        for index, commands in data['subprocess'].items():
            (self.import_only_spawners if all(import_only_command(c) for c in commands)
             else self.other_spawners).add(int(index))

    def value_map(self):
        """tools/build_values.py output next to the map, loaded on first use (None if absent)."""
        if not hasattr(self, '_value_map'):
            self._value_map = None
            try:
                with gzip.open(self.path.parent / 'values.json.gz', 'rt') as f:
                    data = json.load(f)
                if data.get('version') == 1 and data.get('revision') == PIN:
                    self._value_map = data
            except (OSError, ValueError):
                pass
        return self._value_map

    def key_map(self):
        """tools/build_keys.py output next to the map, loaded on first use (None if absent)."""
        if not hasattr(self, '_key_map'):
            self._key_map = None
            path = self.path.parent / 'keys.json.gz'
            try:
                with gzip.open(path, 'rt') as f:
                    data = json.load(f)
                if data.get('version') == 1 and data.get('revision') == PIN:
                    self._key_map = data
            except (OSError, ValueError):
                pass
        return self._key_map

    def tree(self, path):
        if path not in self._trees:
            self._trees[path] = ast.parse(self.sources[path])
        return self._trees[path]

    def line_sets(self, path):
        if path not in self._lines:
            entry = self.data['files'].get(path, {'lines': [], 'import': []})
            self._lines[path] = ({line: sid for line, sid in entry['lines']}, set(entry['import']))
        return self._lines[path]

    def executors(self, path, lines) -> set[int]:
        sets, _ = self.line_sets(path)
        tests = set()
        for line in lines:
            sid = sets.get(line)
            if sid is not None:
                tests.update(self.sets[sid])
        return tests

    def agreement(self, path, guarded_swap=None):
        """agree(line, col) for comparisons in `path`, from the operand-type profile."""
        sites = self.data.get('compare', {}).get(path)
        types = self.data.get('compare_types')
        if sites is None or types is None:
            return None
        none = 'builtins:NoneType'

        def agree_pair(a, b):
            name_a, kind_a, unique_a = types[a]
            name_b, kind_b, unique_b = types[b]
            if name_a == none or name_b == none:
                # NoneType and builtin or identity equality: unequal to None unless identical.
                other = (name_b, kind_b) if name_a == none else (name_a, kind_a)
                if other[1] == 'expression_type_guard':
                    return guarded_swap is not None and guarded_swap()
                return other[0] == none or other[1] in ('identity', 'builtin')
            if kind_a == 'identity' and kind_b == 'identity':
                return True
            # Members of one enum with distinct values are equal only when identical.
            return name_a == name_b and unique_a and unique_b

        def agree(line, col):
            pairs = sites.get(f'{line}:{col}')
            return pairs is not None and all(agree_pair(a, b) for a, b in pairs)

        return agree

    def import_lambdas(self, path) -> set[int]:
        return {line for line, name in self.data['code_at_import'].get(path, []) if name == '<lambda>'}

    def units(self, path):
        """Per line: the smallest statement, header or lambda that contains it."""
        if path in self._units:
            return self._units[path]
        spans = self.data.get('spans', {}).get(path)
        if spans is not None:
            best = SpanIndex(spans)
            self._units[path] = best
            return best
        best = {}
        for node in ast.walk(self.tree(path)):
            if isinstance(node, ast.Lambda):
                span = range(node.lineno, node.end_lineno + 1)
            elif isinstance(node, ast.stmt):
                compound = isinstance(getattr(node, 'body', None), list)
                span = header_lines(node) if compound else full_lines(node)
            else:
                continue
            size = len(span)
            for line in span:
                if line not in best or size < len(best[line]):
                    best[line] = span
        self._units[path] = best
        return best

    def instance_tests(self, keys) -> set[int]:
        tests = set()
        instances = self.data['instances']
        for key in keys:
            for sub in self.subclasses.get(key, {key}) | {key}:
                sid = instances.get(sub)
                if sid is not None:
                    tests.update(self.sets[sid])
        return tests


class SpanIndex:
    """Multi-line units of a file, as [start, end] pairs; other lines are their own unit."""

    def __init__(self, spans):
        self.spans = sorted(spans)
        self.starts = [s for s, _ in self.spans]

    def get(self, line, default=None):
        best = None
        for i in range(bisect.bisect_right(self.starts, line) - 1, -1, -1):
            start, end = self.spans[i]
            if end >= line and (best is None or end - start < best[1] - best[0]):
                best = (start, end)
        return range(best[0], best[1] + 1) if best else default


def import_only_command(argv) -> bool:
    """`python -c CODE` where CODE only imports sqlglot and inspects it."""
    if len(argv) != 3 or argv[1] != '-c' or not os.path.basename(argv[0]).startswith('python'):
        return False
    try:
        tree = ast.parse(argv[2])
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in ('len', 'print')):
            return False
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
            if any(n != 'sqlglot' for n in names):
                return False
    return True


# ---------------------------------------------------------------- import-time state

class ImportState:
    """Fingerprints of sqlglot's import-time state, baseline and patched (child processes).

    The baseline state and compiled baseline modules are prepared while the image is
    built (`--prepare`, not charged), or else once per run into the temp directory.
    """

    def __init__(self, sources: dict[str, str], prepared: Path = HERE, check_inputs: dict | None = None,
                 order=()):
        self.sources = {k: v for k, v in sources.items() if k.startswith('sqlglot/')}
        self.order = list(order)  # sqlglot modules in the order test collection imports them
        # tests/helpers.py and the TPC fixtures, for the differential checks (fingerprint.run_checks)
        self.check_inputs = check_inputs
        self.runs = 0
        self.checks_run = 0
        self.stamp = state_stamp(self.sources, check_inputs)
        self.stamp['order'] = state_stamp({'order': '\n'.join(self.order)})['sources']
        self.temporary = []
        self.code = self.state = self.checks = self.keys_file = self.costs_file = None
        meta = prepared / 'prepared.json'
        try:
            if json.loads(meta.read_text()) == self.stamp:
                code, state = prepared / 'baseline-code.marshal', prepared / 'baseline-state.json'
                if code.is_file() and state.is_file():
                    self.code, self.state = str(code), str(state)
                    if (prepared / 'baseline-keys.marshal').is_file():
                        self.keys_file = str(prepared / 'baseline-keys.marshal')
                    if (prepared / 'test-costs.json').is_file():
                        self.costs_file = str(prepared / 'test-costs.json')
                    checks = prepared / 'baseline-checks.json'
                    if check_inputs is not None and checks.is_file():
                        self.checks = str(checks)
        except (OSError, ValueError):
            pass
        self.failed = False
        self.checks_failed = False
        self.server = None
        self.seconds = {}  # wall seconds spent waiting on the server, by request kind
        self.started = time.monotonic()

    def close(self):
        if self.server is not None:
            try:
                self.server.stdin.close()
                self.server.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.kill_server()
        for path in self.temporary:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def env():
        return {'PYTHONHASHSEED': '0', 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp')}

    def kill_server(self):
        if self.server is not None:
            try:
                os.killpg(self.server.pid, 9)  # the server and the fork it is waiting on
            except OSError:
                pass
            try:
                self.server.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.server = None

    def readline(self, timeout):
        """One line from the server, or None after `timeout` seconds."""
        import select
        fd = self.server.stdout.fileno()
        deadline = time.monotonic() + timeout
        data = b''
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                return None
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return None
            data += chunk
        return data.split(b'\n', 1)[0].decode()

    def ask(self, request: dict) -> dict | None:
        """Answer a request (fingerprint or checks) in a fork of a server holding the baselines."""
        if self.server is None:
            try:
                self.server = subprocess.Popen(
                    [sys.executable, '-s', '-B', str(HERE / 'fingerprint.py'), '--serve'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=self.env(), cwd=str(HERE), start_new_session=True, bufsize=0,
                )
                config = {'cache': self.code, 'baseline': self.state, 'checks': self.checks, 'order': self.order}
                self.server.stdin.write((json.dumps(config) + '\n').encode())
                self.server.stdin.flush()
                if self.readline(60) is None:
                    raise OSError('fingerprint server did not start')
            except OSError as error:
                log('fingerprint server unavailable:', error)
                self.kill_server()
                if 'checks' in request or 'tests' in request:
                    return None
                self.runs -= 1
                return self.run(dict(request, cache=self.code, baseline=self.state, order=self.order))
        started = time.monotonic()
        try:
            self.server.stdin.write((json.dumps(request) + '\n').encode())
            self.server.stdin.flush()
        except OSError:
            self.kill_server()
            return None
        line = self.readline(2 * int(request.get('cpu_limit') or 60) + 10)
        kind = 'checks' if 'checks' in request else 'tests' if 'tests' in request else 'fingerprint'
        self.seconds[kind] = self.seconds.get(kind, 0.0) + time.monotonic() - started
        if line is None:
            log('fingerprint timed out or died')
            self.kill_server()
            return None
        try:
            result = json.loads(line)
        except ValueError:
            return None
        if not result.get('ok'):
            log('fingerprint import failed:', result.get('error'))
            return None
        return result

    def run(self, request: dict) -> dict | None:
        env = self.env()
        self.runs += 1
        try:
            done = subprocess.run(
                [sys.executable, '-s', '-B', str(HERE / 'fingerprint.py')],
                input=json.dumps(request), capture_output=True, text=True, timeout=120, env=env, cwd=str(HERE),
            )
        except subprocess.TimeoutExpired:
            log('fingerprint timed out')
            return None
        try:
            result = json.loads(done.stdout)
        except ValueError:
            log('fingerprint crashed:', done.stderr[-500:])
            return None
        if not result.get('ok'):
            log('fingerprint import failed:', result.get('error'))
            return None
        return result

    def prepare(self, code: str, state: str, keyed=(), keys_file: str | None = None) -> bool:
        request = {'sources': self.sources, 'cache': code, 'write_cache': True, 'write_state': state,
                   'order': self.order}
        if keyed and keys_file:
            request.update(keyed=list(keyed), write_keys=keys_file)
        result = self.run(request)
        if keys_file and os.path.isfile(keys_file):
            self.keys_file = keys_file
        return bool(result) and os.path.isfile(code) and os.path.isfile(state)

    def prepare_checks(self, checks: str) -> bool:
        """The pinned step results of the TPC tests (needs the compiled baseline modules)."""
        if self.check_inputs is None or self.code is None:
            return False
        request = dict(self.check_inputs, cache=self.code, write_checks=checks,
                       checks={'tpcds': {ph: -1 for ph in CHECK_PHASES}, 'tpch_opt': {'transpile': -1}})
        result = self.run(request)
        if not result or not os.path.isfile(checks):
            return False
        found = result['checks']
        return all(isinstance(found.get(name), dict) for name in ('tpcds', 'tpch_opt'))

    def baseline(self) -> bool:
        if self.state is None and not self.failed:
            base = os.path.join(tempfile.gettempdir(), f'blast-radius-{os.getpid()}')
            code, state = base + '-code.marshal', base + '-state.json'
            self.temporary += [code, state]
            if self.prepare(code, state):
                self.code, self.state = code, state
            else:
                self.failed = True
        return self.state is not None

    def test_costs(self) -> dict | None:
        """CPU to import each test module and set up each test class (measured at build)."""
        if not hasattr(self, '_costs'):
            self._costs = None
            if self.costs_file is not None:
                try:
                    self._costs = json.loads(Path(self.costs_file).read_text())
                except (OSError, ValueError):
                    pass
        return self._costs

    def prepare_costs(self, path: str, files, sources: dict) -> bool:
        """Measure test costs, only if the checkout's test files are the map's, byte for byte."""
        if self.code is None or not (TEST_SOURCE / 'tests').is_dir():
            return False
        for rel, text in sources.items():
            if rel.startswith('tests/'):
                try:
                    if (TEST_SOURCE / rel).read_text(encoding='utf-8') != text:
                        log(f'test checkout differs from the map at {rel}')
                        return False
                except OSError:
                    return False
        result = self.run({'files': sorted(files), 'source': str(TEST_SOURCE), 'cache': self.code, 'write_costs': path})
        return bool(result) and os.path.isfile(path)

    def run_tests(self, test_ids, overrides, cpu_limit) -> dict | None:
        """{test id: passed} for tests run, in order, against the patched sources."""
        if not self.baseline():
            return None
        result = self.ask({'tests': list(test_ids), 'overrides': overrides, 'source': str(TEST_SOURCE),
                           'stop': True, 'cpu_limit': cpu_limit})
        return result.get('results') if result else None

    def check_baseline(self) -> bool:
        if self.checks is None and not self.checks_failed and self.baseline():
            path = os.path.join(tempfile.gettempdir(), f'blast-radius-{os.getpid()}-checks.json')
            self.temporary.append(path)
            if self.prepare_checks(path):
                self.checks = path
                self.kill_server()  # restart it with the check baseline loaded
            else:
                self.checks_failed = True
        return self.checks is not None

    def verify(self, checks: dict, overrides: dict, fixtures: dict) -> dict:
        """name -> True when the TPC test's recomputed step results equal the pinned ones."""
        if not checks or self.check_inputs is None or not self.check_baseline():
            return {}
        self.checks_run += 1
        if time.monotonic() - self.started > CHECKS_UNTIL:
            return {}
        request = {'checks': checks, 'overrides': overrides, 'helpers': self.check_inputs['helpers'],
                   'fixtures': dict(self.check_inputs['fixtures'], **fixtures), 'cpu_limit': CHECK_CPU_LIMIT}
        result = self.ask(request)
        if result is None:
            return {}
        found = result.get('checks', {})
        return {name: found.get(name) is True for name in checks}

    def changes(self, overrides):
        if time.monotonic() - self.started > IMPORTS_UNTIL:
            raise Everything('out of time for patched imports')
        if not self.baseline():
            raise Everything('baseline import state unavailable')
        self.runs += 1
        result = self.ask({'overrides': overrides, 'cpu_limit': FINGERPRINT_CPU_LIMIT})
        if result is None:
            raise Everything('patched sqlglot fails to import')
        if result['new_failures']:
            raise Everything(f'patched modules fail to import: {result["new_failures"][:3]}')
        return result['changed'], result.get('keys') or {}

    def baseline_keys(self) -> dict | None:
        """Per-key hashes of every tracked container at the pinned revision (built with the image)."""
        if not hasattr(self, '_baseline_keys'):
            self._baseline_keys = None
            if self.keys_file is not None:
                try:
                    with open(self.keys_file, 'rb') as f:
                        self._baseline_keys = marshal.load(f)
                except (OSError, ValueError, EOFError):
                    pass
        return self._baseline_keys


def state_stamp(sources, check_inputs=None) -> dict:
    """What prepared baseline files must have been built from: these sources, this Python."""
    import hashlib
    import importlib.util
    digest = hashlib.sha256()
    for path in sorted(sources):
        digest.update(path.encode() + b'\0' + sources[path].encode('utf-8', 'surrogatepass') + b'\0')
    if check_inputs is not None:
        digest.update(json.dumps(check_inputs, sort_keys=True).encode('utf-8', 'surrogatepass'))
    fingerprint = (HERE / 'fingerprint.py').read_bytes() if (HERE / 'fingerprint.py').is_file() else b''
    return {
        'sources': digest.hexdigest(),
        'fingerprint': hashlib.sha256(fingerprint).hexdigest(),
        'python': sys.version,
        'magic': importlib.util.MAGIC_NUMBER.hex(),
    }


def key_digest(entries: dict) -> str:
    """Digest of a container's key descriptions, as tools/build_keys.py computes it."""
    import hashlib
    text = '\n'.join(sorted(entries))
    return hashlib.blake2b(text.encode('utf-8', 'surrogatepass'), digest_size=10).hexdigest()


def check_inputs_of(data: dict) -> dict | None:
    proxies = data.get('proxies')
    helpers = data.get('sources', {}).get('tests/helpers.py')
    if not proxies or helpers is None:
        return None
    return {'helpers': helpers, 'fixtures': proxies['fixtures']}


def prepare(map_path: Path) -> int:
    """Build-time: write the baseline import state, compiled modules and TPC step results."""
    with gzip.open(map_path, 'rt') as f:
        data = json.load(f)
    state = ImportState(data['sources'], prepared=Path('/nonexistent'), check_inputs=check_inputs_of(data),
                        order=data.get('collection_order', ()))
    code, saved, checks = HERE / 'baseline-code.marshal', HERE / 'baseline-state.json', HERE / 'baseline-checks.json'
    keys_file = HERE / 'baseline-keys.marshal'
    keyed = ()
    try:
        with gzip.open(map_path.parent / 'keys.json.gz', 'rt') as f:
            keyed = sorted(json.load(f)['containers'])
    except (OSError, ValueError, KeyError):
        pass
    if not state.prepare(str(code), str(saved), keyed, str(keys_file)):
        log('prepare failed; the baseline will be computed at run time')
        return 0
    state.code = str(code)
    written = [code, saved] + ([keys_file] if keys_file.is_file() else [])
    costs = HERE / 'test-costs.json'
    files = {test_id.split('::')[0] for test_id in data['tests']}
    if state.prepare_costs(str(costs), files, data['sources']):
        written.append(costs)
    else:
        log('no test checkout or cost measurement failed: selected tests will not be run')
    if state.prepare_checks(str(checks)):
        written.append(checks)
    else:
        log('check baseline failed; it will be computed at run time')
    (HERE / 'prepared.json').write_text(json.dumps(state.stamp))
    for path in written + [HERE / 'prepared.json']:
        os.chmod(path, 0o644)
    log('prepared baseline import state')
    return 0


# ---------------------------------------------------------------- selection

class Selector:
    def __init__(self, dependency_map: DependencyMap):
        self.m = dependency_map
        self.state = ImportState(dependency_map.sources, check_inputs=check_inputs_of(dependency_map.data),
                                 order=dependency_map.data.get('collection_order', ()))
        self.py_info = {}
        self.last_changed = None
        self._texts = None
        self._references = None

    # -- textual references: lines naming something, executed by which tests

    def references(self, pattern: re.Pattern, literals) -> set[int]:
        if self._references is None:
            self._references = {}
        key = pattern.pattern
        if key not in self._references:
            self._references[key] = self._scan(pattern, literals)
        return set(self._references[key])

    def _scan(self, pattern: re.Pattern, literals) -> frozenset:
        tests = set()
        literals = sorted(set(literals))
        for path, text in self.m.sources.items():
            if not any(literal in text for literal in literals):
                continue
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            starts = self.line_starts(path)
            units = self.m.units(path) if path.endswith('.py') else {}
            lines = set()
            for match in matches:
                line = bisect.bisect_right(starts, match.start())
                lines.update(units.get(line, (line,)))
            # Import-time uses show up as changed state of their own; collection-time
            # uses in test files are already charged to every test of that file.
            tests |= self.m.executors(path, lines)
        return frozenset(tests)

    def line_starts(self, path):
        if self._texts is None:
            self._texts = {}
        if path not in self._texts:
            text = self.m.sources[path]
            starts = [0]
            pos = text.find('\n')
            while pos != -1:
                starts.append(pos + 1)
                pos = text.find('\n', pos + 1)
            self._texts[path] = starts
        return self._texts[path]

    # -- one commit

    def select(self, patch_text: str, explain=None) -> set[int] | None:
        """Catalog test numbers to run, or None for every test."""
        try:
            tests, spawners = self._select(patch_text, explain)
        except Everything as reason:
            if explain is not None:
                explain.append(f'everything: {reason}')
            return None
        numbers = {self.m.number_of_index[i] for i in tests if self.m.number_of_index[i] is not None}
        numbers |= self.m.always
        numbers |= {self.m.number_of_index[i] for i in spawners if self.m.number_of_index[i] is not None}
        return numbers

    def _select(self, patch_text, explain):
        m = self.m
        files = parse_patch(patch_text)
        self.single_file = len(files) == 1
        tests: set[int] = set()
        import_time_sources = {}
        new_modules = set()
        spawners = set()
        self.py_info = {}
        self.last_changed = None
        # Running selected tests needs a patch made only of edits to existing sqlglot modules.
        self.verifiable = bool(files) and all(
            f.old is not None and f.old == f.new and f.new.startswith('sqlglot/') and f.new.endswith('.py')
            and not f.binary and not f.special and f.new in m.sources for f in files)
        # What the differential checks need: patched sqlglot sources, data files (new text or None).
        patched, data = {}, {}
        fixtures_only = all(f.old == f.new and f.old in m.data.get('sql_fixtures', {})
                            and f.hunks and not f.special and not f.binary for f in files)
        for f in files:
            if f.special:
                raise Everything('rename or copy')
            if f.binary:
                path = f.new or f.old
                if f.old != f.new or path.endswith('.py') or path not in m.data['reads'] or path in m.data['read_at_import']:
                    raise Everything('binary change')
                tests |= self.read_tests(path)
                data[path] = None
                continue
            if f.old is None and f.new is None:
                continue  # mode change only
            path = f.new or f.old
            if f.old is not None and f.new is not None and f.old != f.new:
                raise Everything('path changes')
            name = path.rsplit('/', 1)[-1]
            at_root = '/' not in path
            if (at_root and name in CONFIG_FILES) or name == 'conftest.py' or path in m.data['read_at_import']:
                raise Everything(f'configuration file {path}')
            if f.old is None or f.new is None:
                # Created or deleted.
                if path.endswith('.py') and (path.startswith('sqlglot/') or path.startswith('tests/')):
                    if f.new is None:
                        raise Everything(f'deleted module {path}')
                    if path.startswith('tests/') and '/fixtures/' not in path:
                        raise Everything(f'new test module {path}')
                    # A new sqlglot module only matters when imported or listed.
                    import_time_sources[path] = apply_hunks('', f.hunks)
                    new_modules.add(path)
                    tests |= self.listing_tests(path)
                    continue
                data[path] = None
                tests |= self.listing_tests(path)
                tests |= self.read_tests(path)
                if explain is not None:
                    explain.append(f'{path}: created/deleted, {len(tests)} tests')
                continue
            if path.endswith('.py') and path in m.sources:
                new_text = apply_hunks(m.sources[path], f.hunks)
                found, import_time = self.python_change(
                    path, m.sources[path], new_text, explain, edited_lines(f.hunks), touched_new_lines(f.hunks))
                tests |= found
                if path.startswith('sqlglot/'):
                    patched[path] = new_text
                if import_time:
                    if path.startswith('sqlglot/'):
                        import_time_sources[path] = new_text
                    else:
                        tests |= self.test_module_dependents(path)
                continue
            if path.endswith('.py') and (path.startswith('sqlglot/') or path.startswith('tests/')):
                raise Everything(f'unknown module {path}')
            if not path.startswith('tests/') and path.endswith('.py'):
                found = self.read_tests(path)
                tests |= found
                data[path] = None
                continue
            if fixtures_only and self.equivalent_fixture(path, f.hunks):
                if explain is not None:
                    explain.append(f'{path}: identical fixture records')
                continue
            data[path] = self.patched_fixture(path, f.hunks)
            tests |= self.read_tests(path)
            if path.startswith('tests/') and path not in m.data['reads']:
                tests |= self.unread_fixture(path)
            if explain is not None:
                explain.append(f'{path}: data file, readers {len(self.read_tests(path))}')
        if import_time_sources:
            found, init_changed = self.import_state_change(import_time_sources, new_modules, explain)
            tests |= found
            if init_changed:
                spawners |= m.import_only_spawners
        if import_time_sources or tests:
            spawners |= m.other_spawners
        self.patched = patched
        if m.proxies and not new_modules:
            try:
                tests = self.prune_by_checks(tests, patched, data, explain)
            except Exception as error:  # noqa: BLE001 - a failed check only keeps the TPC tests
                log(f'check error {error!r}')
        return tests, spawners

    # -- running cheap selections (plan step 4)

    def verify(self, chosen: set[int], durations: dict[int, int], explain=None) -> set[int]:
        """Drop selected tests that pass when run against the patched sources.

        Only for patches that edit existing sqlglot modules in place, never for tests that
        start subprocesses (those import sqlglot from disk), and only when the estimated
        CPU (forking, importing the test modules, class setup, running the tests) is well
        below the simulated seconds a passing run would save. Tests run cheapest first and
        stop at the first one that does not pass; it and the ones not reached stay.
        """
        if (not chosen or not getattr(self, 'verifiable', False) or not (TEST_SOURCE / 'tests').is_dir()
                or time.monotonic() - self.state.started > VERIFY_UNTIL):
            return chosen
        costs = self.state.test_costs()
        if not costs:
            return chosen
        m = self.m
        ids = {t['number']: t['id'] for t in m.catalog}
        spawners = {m.number_of_index[i] for i in m.import_only_spawners | m.other_spawners}
        if chosen & spawners or any(n not in ids for n in chosen):
            return chosen
        files = {ids[n].split('::')[0] for n in chosen}
        classes = {ids[n].rsplit('::', 1)[0] for n in chosen}
        frontier = list(files)
        while frontier:  # with the test modules they import (each costed once)
            for dep in costs.get('deps:' + frontier.pop(), ()):
                if dep not in files:
                    files.add(dep)
                    frontier.append(dep)
        if any(f in costs and costs[f] is None for f in files):
            return chosen  # a test module that does not import here
        estimate = (costs.get('<fork>', 0.1) + sum(costs.get(f) or 0.0 for f in files)
                    + sum(costs.get(c, 0.0) for c in classes)
                    + RUN_FACTOR * sum(durations[n] for n in chosen) / 1000)
        workers = schedule({n: durations[n] for n in chosen})
        saving = (max(sum(durations[n] for n in w) for w in workers) + WORKER_MS * len(workers)) / 1000
        if estimate > VERIFY_RATIO * saving:
            if explain is not None:
                explain.append(f'verify: skipped (cost {estimate:.2f}s vs saving {saving:.2f}s)')
            return chosen
        order = sorted(chosen, key=lambda n: (durations[n], n))
        results = self.state.run_tests([ids[n] for n in order], self.patched, max(5, int(3 * estimate) + 2))
        if results is None:
            return chosen
        passed = {n for n in order if results.get(ids[n]) is True}
        if explain is not None:
            explain.append(f'verify: {len(passed)}/{len(chosen)} pass (cost {estimate:.2f}s, saving {saving:.2f}s)')
        return chosen - passed

    # -- differential checks of the expensive TPC tests

    def equivalent_fixture(self, path, hunks):
        """Re-run the exact observed fixture loaders. Keep any unknown or malformed input."""
        entry = self.m.data.get('sql_fixtures', {}).get(path)
        if not entry:
            return False
        loaders, baseline = entry
        try:
            patched = apply_hunks(baseline, hunks)
            if len(patched) > len(baseline) + 100_000:
                return False
            ns = self.__dict__.get('_fixture_helpers')
            if ns is None:
                ns = {'__name__': 'tests.helpers', '__file__': '/tests/helpers.py'}
                exec(compile(self.m.sources['tests/helpers.py'], 'tests/helpers.py', 'exec', dont_inherit=True), ns)
                self._fixture_helpers = ns

            def records(text, loader):
                ns['open'] = lambda *a, **k: io.StringIO(text)
                if loader == 'load_sql_fixtures':
                    return list(ns[loader](path))
                if loader in ('load_sql_fixture_pairs', 'check_file'):
                    pairs = list(ns['load_sql_fixture_pairs'](path))
                    if loader == 'check_file':
                        # This caller uses the title only to label the subtest when 'only' is unset.
                        for meta, _, _ in pairs:
                            meta.pop('title', None)
                    return pairs
                raise ValueError('unsupported loader')

            return bool(loaders) and all(records(baseline, mode) == records(patched, mode) for mode in loaders)
        except Exception:
            return False

    def patched_fixture(self, path, hunks):
        fixtures = (self.m.data.get('proxies') or {}).get('fixtures', {})
        if path not in fixtures:
            return None
        try:
            return apply_hunks(fixtures[path], hunks)
        except Everything:
            return None

    def run_names(self, proxy) -> set[str]:
        """Identifiers on the lines a TPC test runs outside its checked steps."""
        if proxy['names'] is None:
            names = set()
            for path, lines in proxy['run'].items():
                text = self.m.sources.get(path)
                if text is None:
                    continue
                source = text.split('\n')
                units = self.m.units(path)
                for line in lines:
                    for number in units.get(line, (line,)):
                        if 0 < number <= len(source):
                            names.update(re.findall(r'[A-Za-z_]\w*', source[number - 1]))
            proxy['names'] = names
        return proxy['names']

    def naming_lines(self, path, names) -> set[int]:
        """Lines of a source file whose statement names one of `names`."""
        key = (path, frozenset(names))
        cache = self.__dict__.setdefault('_naming', {})
        if key not in cache:
            text = self.m.sources.get(path, '')
            found = set()
            if any(name in text for name in names):
                pattern = re.compile(r'(?<![\w])(?:' + '|'.join(map(re.escape, sorted(names))) + r')(?![\w])')
                starts = self.line_starts(path)
                units = self.m.units(path)
                for match in pattern.finditer(text):
                    line = bisect.bisect_right(starts, match.start())
                    found.update(units.get(line, (line,)))
            cache[key] = found
        return cache[key]

    def check_phases(self, index, proxy, data) -> dict | None:
        """Steps to recompute for a selected TPC test ({phase: query bitmask, -1 for all,
        'inputs' when a TPC fixture changed}), or None when the test must run.

        It must run when the edit reaches code it runs outside the checked steps, writes
        long-lived state, changes import-time state its run phase could read, edits its
        test module or helpers, or changes a file it reads other than the TPC fixtures.
        """
        m = self.m
        run = proxy['run']
        phases = {}
        for path, (lines, import_time, shared) in self.py_info.items():
            if not path.startswith('sqlglot/'):
                if path in ('tests/test_executor.py', 'tests/helpers.py', 'tests/__init__.py'):
                    return None
                continue  # another test module: not imported by tests/test_executor.py
            if shared or lines & run.get(path, set()):
                return None
            known = set(run.get(path, ()))
            for ph in proxy['phases']:
                per_line = proxy['queries'][ph].get(path, {})
                known.update(per_line)
                for line in lines:
                    if line in per_line:
                        phases[ph] = phases.get(ph, 0) | per_line[line]
            sets, _ = m.line_sets(path)
            for line in lines - known:
                sid = sets.get(line)
                if sid is not None and index in m.sets[sid]:
                    return None  # the test runs it, but no recorded step does
        if self.last_changed:
            names = self.run_names(proxy)
            changed = set()
            for owner, attrs in self.last_changed.items():
                if owner in ('<init>', '<runtime>') or attrs is None:
                    return None
                module = owner.startswith('module:')
                for attr, kind in attrs.items():
                    if not attr.isidentifier() or attr.startswith('__') or attr in names:
                        return None
                    if not module and kind != 'd':
                        return None  # methods are dispatched by computed names
                    changed.add(attr)
            # Import-time data is read by code that names it (as when selecting tests for
            # it): recompute the steps of the queries that ran such a line.
            for ph in proxy['phases']:
                for path, per_line in proxy['queries'][ph].items():
                    for line in self.naming_lines(path, changed) & per_line.keys():
                        phases[ph] = phases.get(ph, 0) | per_line[line]
        fixtures = (m.data.get('proxies') or {}).get('fixtures', {})
        for path, text in data.items():
            if path in fixtures:
                if text is None:
                    return None
                phases['inputs'] = -1
            elif index in self.read_tests(path) or index in self.listing_tests(path):
                return None
        return phases

    def prune_by_checks(self, tests, patched, data, explain):
        checks, todo = {}, {}
        for index, proxy in self.m.proxies.items():
            if index not in tests:
                continue
            phases = self.check_phases(index, proxy, data)
            if phases is None:
                if explain is not None:
                    explain.append(f'check {proxy["name"]}: must run')
                continue
            todo[index] = proxy['name']
            if phases:
                checks[proxy['name']] = {ph: mask for ph, mask in phases.items() if ph != 'inputs'}
        if not todo:
            return tests
        fixtures = {path: text for path, text in data.items()
                    if path in (self.m.data.get('proxies') or {}).get('fixtures', {})}
        verdict = self.state.verify(checks, patched, fixtures) if checks else {}
        for index, name in todo.items():
            ok = verdict.get(name) if name in checks else True
            if explain is not None:
                wanted = {ph: (bin(mask).count('1') if mask >= 0 else 'all') for ph, mask in checks.get(name, {}).items()}
                explain.append(f'check {name} {wanted if name in checks else "unreached"}: '
                               f'{"same, dropped" if ok else "differs"}')
            if ok:
                tests = tests - {index}
        return tests

    def read_tests(self, path) -> set[int]:
        sid = self.m.data['reads'].get(path)
        return set(self.m.sets[sid]) if sid is not None else set()

    def listing_tests(self, path) -> set[int]:
        tests = set()
        directory = path.rsplit('/', 1)[0] if '/' in path else '.'
        for listed, sid in self.m.data['listings'].items():
            if directory == listed or directory.startswith(listed + '/'):
                tests |= set(self.m.sets[sid])
        return tests

    def unread_fixture(self, path) -> set[int]:
        """A data file no test opened through Python: tests whose source names it."""
        stem = path.rsplit('/', 1)[-1].split('.')[0]
        tests = set()
        for test_file, text in self.m.sources.items():
            if test_file.startswith('tests/') and stem and stem in text:
                tests |= self.m.file_tests.get(test_file, set())
        return tests

    def test_module_dependents(self, path) -> set[int]:
        """Import-time change in a test module: its tests, and tests of files importing it."""
        if path.endswith('__init__.py'):
            package = path[: -len('__init__.py')]
            return {i for f, ids in self.m.file_tests.items() if f.startswith(package) for i in ids}
        module = path[:-3].replace('/', '.')
        short = module.rsplit('.', 1)[-1]
        affected = {path}
        frontier = [module]
        while frontier:
            current = frontier.pop()
            short = current.rsplit('.', 1)[-1]
            pattern = re.compile(rf'^\s*(from\s+\S*{re.escape(short)}\s+import|import\s+\S*{re.escape(short)}\b|from\s+\S+\s+import\s+.*\b{re.escape(short)}\b)', re.M)
            for test_file, text in self.m.sources.items():
                if test_file.startswith('tests/') and test_file not in affected and pattern.search(text):
                    affected.add(test_file)
                    frontier.append(test_file[:-3].replace('/', '.'))
        tests = set()
        for f in affected:
            tests |= self.m.file_tests.get(f, set())
        return tests

    def python_change(self, path, old_text, new_text, explain, edited=None, touched=None):
        if old_text == new_text:
            return set(), False
        try:
            new_tree = ast.parse(new_text)
            if touched is None:
                compile(new_tree, path, 'exec', dont_inherit=True)
            else:
                check_compiles(new_tree, path, touched)
        except (SyntaxError, ValueError) as error:
            raise Everything(f'{path} does not compile: {error}')
        if coding_lines(old_text) != coding_lines(new_text):
            raise Everything('source encoding changed')
        old_tree = self.m.tree(path)
        _, import_lines = self.m.line_sets(path)
        texts = (old_text.split('\n'), new_text.split('\n'))
        # The refined expression proof uses the pinned comparison methods. A second
        # edit might change those methods or the operands reaching the comparison.
        guarded = None
        def guarded_swap():
            nonlocal guarded
            if guarded is None:
                sites = []
                guarded = (self.single_file and swap_sites(old_tree, new_tree, sites)
                           and len(sites) == 1 and not any(
                               isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                               and fn.name in ('__eq__', '__ne__', '__getattribute__')
                               and fn.lineno <= sites[0][0] <= fn.end_lineno
                               for fn in ast.walk(old_tree)))
            return guarded

        def has_function(key):  # the profile is loaded only when an edit could use it
            values = self.m.value_map()
            return values is not None and f'{path}:{key}' in values['defaults']

        def has_bool_site(line, col, leaves):
            values = self.m.value_map()
            entry = values['boolops'].get(f'{path}:{line}:{col}') if values is not None else None
            return entry is not None and all(len(v) == leaves for v in entry['vectors'])
        differ = Differ(import_lines, self.m.import_lambdas(path), texts, edited,
                        module_level_names(old_tree) | module_level_names(new_tree), self.m.agreement(path, guarded_swap),
                        postpones_annotations(old_tree) and postpones_annotations(new_tree), has_function, has_bool_site)
        differ.block(old_tree.body, new_tree.body, old_tree, 'module')
        self.py_info[path] = (set(differ.lines) | differ.profiled_lines, differ.import_time, set(differ.shared))
        profiled = self.value_tests(path, differ)
        if profiled and explain is not None:
            explain.append(f'{path}: profiles {len(differ.default_changes)} defaults, {len(differ.regroups)} and/or '
                           f'-> {len(profiled)} tests')
        if not differ.lines and not differ.import_time and not differ.shared:
            if profiled:
                return profiled, False
            if explain is not None:
                explain.append(f'{path}: no syntax change')
            return set(), False
        tests = self.m.executors(path, differ.lines) | profiled
        if differ.shared:
            names = sorted(differ.shared)
            tests |= self.references(
                re.compile(r'(?<![\w])(?:' + '|'.join(map(re.escape, names)) + r')(?![\w])'), names)
            if explain is not None:
                explain.append(f'{path}: writes shared {names}')
        if explain is not None:
            explain.append(f'{path}: {len(differ.lines)} lines -> {len(tests)} tests, import time: {differ.import_time}')
        return tests, differ.import_time

    def value_tests(self, path, differ) -> set[int]:
        """Tests that used a changed default or saw mixed operands at a flipped and/or site."""
        if not differ.default_changes and not differ.regroups:
            return set()
        values = self.m.value_map()
        sets = values['sets']
        tests = set(values['broken'])
        for key, params in differ.default_changes:
            entry = values['defaults'][f'{path}:{key}']
            for param in params:
                if entry.get(param) is not None:
                    tests.update(sets[entry[param]])
        for line, col, _, old, new, negated in differ.regroups:
            entry = values['boolops'][f'{path}:{line}:{col}']
            if entry['opaque'] is not None:
                tests.update(sets[entry['opaque']])
            for vector, sid in entry['vectors'].items():
                if bool_outcome(old, vector) != bool_outcome(new, negate_leaves(vector, negated)):
                    tests.update(sets[sid])
        return tests

    def import_state_change(self, sources, new_modules, explain):
        changed, keyed = self.state.changes(sources)
        self.last_changed = changed
        tests_from_keys = self.key_tests = set()
        m = self.m
        new_names = {p[:-12].replace('/', '.') if p.endswith('/__init__.py') else p[:-3].replace('/', '.') for p in new_modules}
        tests = set()
        init_changed = False
        if explain is not None:
            explain.append(f'import state: {len(changed)} owners changed: ' + ', '.join(
                f'{o}{sorted(a)[:4] if a else ""}' for o, a in sorted(changed.items())[:12]))
        if '<runtime>' in changed:
            raise Everything('process-wide state changed at import')
        changed = self.narrow_containers(changed, keyed, explain)
        if changed is None:
            return tests_from_keys, False
        global_names = set()      # module globals: any line naming them
        qualified = {}            # attribute -> class names it may be read through
        broad_names = set()       # classes used without instances: any line naming them
        broad_attrs = set()       # and any attribute access of the changed attribute
        for owner, attrs in changed.items():
            if owner == '<init>':
                init_changed = True
                continue
            module = owner[len('module:'):] if owner.startswith('module:') else owner.split(':', 1)[0]
            if module in new_names:
                continue
            if owner.startswith('module:'):
                if attrs is None:
                    raise Everything(f'module {module} appears or disappears')
                path = module.replace('.', '/')
                path = path + '/__init__.py' if path + '/__init__.py' in m.sources else path + '.py'
                for name in attrs:
                    global_names.add(name)
                    tests |= self.definition_tests(path, name)
                continue
            found, names, attr_names, broad = self.class_change(owner, attrs)
            if not broad and found and all(
                kind == 'd' and not attr.startswith('__') for attr, kind in attrs.items()
            ):
                # Plain data is read by code that names it (derived values changed on
                # their own): keep the users of the class that ran such a line.
                pattern = re.compile(r'(?<![\w])(?:' + '|'.join(map(re.escape, sorted(attrs))) + r')(?![\w])')
                found &= self.references(pattern, attrs)
            tests |= found
            for attr in attr_names:
                qualified.setdefault(attr, set()).update(names)
            if broad:
                broad_names.update(names)
                broad_attrs.update(attr_names)
        patterns = []
        if global_names:
            patterns.append(r'(?<![\w])(?:' + '|'.join(map(re.escape, sorted(global_names))) + r')(?![\w])')
        for attr, names in sorted(qualified.items()):
            if names:
                patterns.append(r'(?<![\w])(?:' + '|'.join(map(re.escape, sorted(names))) + r')\s*\.\s*'
                                + re.escape(attr) + r'(?![\w])')
        if broad_names:
            patterns.append(r'(?<![\w])(?:' + '|'.join(map(re.escape, sorted(broad_names))) + r')(?![\w])')
        if broad_attrs:
            patterns.append(r'\.\s*(?:' + '|'.join(map(re.escape, sorted(broad_attrs))) + r')(?![\w])')
        if patterns:
            literals = global_names | set(qualified) | broad_names | broad_attrs
            tests |= self.references(re.compile('|'.join(f'(?:{p})' for p in patterns)), literals)
        return tests | tests_from_keys, init_changed

    def narrow_containers(self, changed, keyed, explain):
        """Take changed dicts and sets out of `changed` when the key map says which tests
        read the changed entries (tools/build_keys.py); those tests go to self.key_tests.

        An attribute qualifies when the map tracked it, its baseline and patched per-key
        hashes are known, and it is still a dict or set. Its tests are those that looked
        up a changed key (added, removed or with a new value), read it whole, or behaved
        differently under tracking. Everything else keeps the usual handling.
        """
        keys_map = self.m.key_map()
        baseline = self.state.baseline_keys() if keys_map and keyed else None
        if not keys_map or not baseline:
            return changed
        containers = keys_map['containers']
        sets = keys_map['sets']
        remaining = {}
        narrowed = []
        for owner, attrs in changed.items():
            if attrs is None or owner.startswith('<'):
                remaining[owner] = attrs
                continue
            left = {}
            for attr, kind in attrs.items():
                name = owner + '\0' + attr
                entry = containers.get(name)
                if entry is None or name not in keyed or name not in baseline:
                    left[attr] = kind
                    continue
                old, new = baseline[name], keyed[name]
                if entry.get('digest') != key_digest(old):
                    left[attr] = kind  # keys described differently than when mapped
                    continue
                touched = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
                found = set(keys_map['broken'])
                if entry['full'] is not None:
                    found.update(sets[entry['full']])
                for k in touched:
                    sid = entry['keys'].get(k)
                    if sid is not None:
                        found.update(sets[sid])
                self.key_tests |= found
                narrowed.append(f'{owner}.{attr}: {len(touched)} keys -> {len(found)} tests')
            if left:
                remaining[owner] = left
        if explain is not None and narrowed:
            explain.append('keys: ' + '; '.join(narrowed[:8]))
        return remaining

    def definition_tests(self, path, name) -> set[int]:
        """Tests that entered a module-level function or class named `name`."""
        if path not in self.m.sources:
            return set()
        for node in self.m.tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                return self.m.executors(path, full_lines(node))
        return set()

    def class_change(self, owner, attrs):
        """Tests instantiating the class, a subclass, or its dialect; names to search for."""
        m = self.m
        related = {owner}
        for dialect in m.unit_of.get(owner, ()):
            related.add(dialect)
            related.update(m.data['units'].get(dialect, ()))
        if owner in m.data['units']:
            related.update(m.data['units'][owner])
        tests = m.instance_tests(related)
        names = set()
        for key in {owner} | m.subclasses.get(owner, set()):
            names.add(key.split('#', 1)[0].rsplit(':', 1)[-1].split('.')[-1])
        names = {n for n in names if n.isidentifier()}
        attr_names = {a for a in (attrs or ()) if a.isidentifier()}
        # Used as a namespace, enum or metaclass, or its shape changed: anything naming it.
        broad = not tests or attrs is None or any(a.startswith('<') for a in attrs)
        return tests, names, attr_names, broad


# ---------------------------------------------------------------- scheduling

def schedule(durations: dict[int, int]) -> list[list[int]]:
    """Fewest simulated seconds: busiest worker plus 2 s per worker."""
    if not durations:
        return []
    items = sorted(durations.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(d for _, d in items)
    best, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        bound = max(items[0][1], -(-total // k)) + WORKER_MS * k
        if best_cost is not None and bound >= best_cost:
            continue
        workers, loads = lpt(items, k)
        if max(loads) + WORKER_MS * k > bound:
            workers, loads = improve(workers, loads, durations)
        cost = max(loads) + WORKER_MS * len([w for w in workers if w])
        if best_cost is None or cost < best_cost:
            best, best_cost = [w for w in workers if w], cost
    return best


def lpt(items, k):
    workers = [[] for _ in range(k)]
    loads = [0] * k
    for number, duration in items:
        i = min(range(k), key=lambda j: (loads[j], j))
        workers[i].append(number)
        loads[i] += duration
    return workers, loads


def improve(workers, loads, durations):
    """Move or swap single tests off the busiest worker while that lowers its load."""
    k = len(workers)
    for _ in range(200):
        top = max(range(k), key=lambda j: loads[j])
        best = None
        for t in workers[top]:
            dt = durations[t]
            for j in range(k):
                if j == top:
                    continue
                if loads[j] + dt < loads[top]:
                    peak = max(loads[top] - dt, loads[j] + dt)
                    if best is None or peak < best[0]:
                        best = (peak, t, j, None)
                for u in workers[j]:
                    du = durations[u]
                    if du < dt:
                        peak = max(loads[top] - dt + du, loads[j] + dt - du)
                        if peak < loads[top] and (best is None or peak < best[0]):
                            best = (peak, t, j, u)
            if len(workers[top]) * k > 4000:
                break
        if best is None:
            break
        _, t, j, u = best
        workers[top].remove(t)
        workers[j].append(t)
        loads[top] -= durations[t]
        loads[j] += durations[t]
        if u is not None:
            workers[j].remove(u)
            workers[top].append(u)
            loads[j] -= durations[u]
            loads[top] += durations[u]
    return workers, loads


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--map', type=Path, default=HERE / 'dependencies.json.gz')
    parser.add_argument('--all-tests', action='store_true')
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--prepare', action='store_true', help='build time: precompute the baseline import state')
    args = parser.parse_args()
    if args.prepare:
        raise SystemExit(prepare(args.map))
    started = time.process_time()
    catalog = json.loads((args.data / 'tests.json').read_text())['tests']
    durations = {t['number']: round(t['seconds'] * 1000) for t in catalog}
    every = set(durations)
    selector = None
    if not args.all_tests:
        try:
            selector = Selector(DependencyMap(args.map, catalog))
        except Exception as error:  # noqa: BLE001 - an unusable map means run everything
            log('dependency map unusable:', repr(error))
    patches = sorted((args.data / 'commits').glob('*.patch'))
    lines = []
    for patch in patches:
        explain = [] if args.explain else None
        chosen = None
        if selector is not None:
            try:
                chosen = selector.select(patch.read_text(encoding='utf-8', errors='surrogateescape'), explain)
            except Exception as error:  # noqa: BLE001 - never crash, never miss
                log(f'{patch.stem}: selector error {error!r}')
                chosen = None
        if chosen is None:
            chosen = every
        chosen &= every
        if selector is not None and chosen is not every:
            try:
                chosen = selector.verify(chosen, durations, explain)
            except Exception as error:  # noqa: BLE001 - verification only ever removes passing tests
                log(f'{patch.stem}: verify error {error!r}')
        workers = schedule({n: durations[n] for n in chosen})
        lines.append(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))
        if explain is not None:
            log(f'{patch.stem}: {len(chosen)} tests; ' + ' | '.join(explain))
    if selector is not None:
        selector.state.close()
    sys.stdout.write('\n'.join(lines) + ('\n' if lines else ''))
    sys.stdout.flush()
    runs = selector.state.runs if selector else 0
    waits = ', '.join(f'{k} {v:.2f}s' for k, v in sorted(selector.state.seconds.items())) if selector else ''
    checks = selector.state.checks_run if selector else 0
    log(f'{len(patches)} commits, {runs} import fingerprints, {checks} checks ({waits}), '
        f'{time.process_time() - started:.2f}s CPU in selector')


if __name__ == '__main__':
    main()
