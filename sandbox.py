"""Runs Python the model writes, for calculations and drawings.

The code runs in a separate Python process with a time limit.  It may import only math-style
modules (math, itertools, numpy, sympy...), and it can't open files, start processes or reach the
network: a syntax check rejects the ways in (open, exec, eval, __import__, attributes starting
with an underscore, file functions in numpy) before anything runs, and the child process runs
with a cut-down set of builtins.  The drawing API in board_api.py is available without importing.
"""
import ast
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED_IMPORTS = {'math', 'cmath', 'random', 'statistics', 'itertools', 'functools', 'collections', 'fractions',
                   'decimal', 'datetime', 're', 'json', 'heapq', 'bisect', 'string', 'operator', 'numpy', 'sympy'}
BANNED_NAMES = {'open', 'exec', 'eval', 'compile', '__import__', 'globals', 'locals', 'vars', 'getattr', 'setattr',
                'delattr', 'input', 'breakpoint', 'help', 'exit', 'quit', 'memoryview', '__builtins__', 'dir',
                'object', 'type', 'super', 'classmethod', 'staticmethod', 'property'}
BANNED_ATTRS = {'system', 'popen', 'remove', 'unlink', 'rmdir', 'rmtree', 'load', 'save', 'savez',
                'savez_compressed', 'savetxt', 'loadtxt', 'fromfile', 'tofile', 'memmap', 'genfromtxt', 'open',
                'write_text', 'write_bytes', 'read_text', 'read_bytes', 'preview', 'DataSource', 'ctypeslib', 'ctypes',
                'lib', 'f2py', 'testing', 'distutils', 'utilities', 'printing', 'parsing', 'external', 'interactive',
                'lambdify', 'sympify', 'parse_expr', 'S', 'environ', 'getenv', 'chdir', 'walk', 'listdir', 'compile'}
SAFE_BUILTINS = ['abs', 'all', 'any', 'bin', 'bool', 'chr', 'complex', 'dict', 'divmod', 'enumerate', 'filter', 'float',
                 'format', 'frozenset', 'hash', 'hex', 'int', 'isinstance', 'issubclass', 'iter', 'len', 'list', 'map',
                 'max', 'min', 'next', 'oct', 'ord', 'pow', 'print', 'range', 'repr', 'reversed', 'round', 'set',
                 'slice', 'sorted', 'str', 'sum', 'tuple', 'zip', 'callable', 'True', 'False', 'None', 'Exception',
                 'ValueError', 'TypeError', 'ZeroDivisionError', 'ArithmeticError', 'IndexError', 'KeyError',
                 'StopIteration', 'RuntimeError', 'AssertionError', 'NotImplementedError', 'OverflowError',
                 '__build_class__']


class Rejected(Exception):
    pass


def check(code):
    """Raises Rejected with a reason the model can act on, or returns the parsed tree."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise Rejected(f'syntax error on line {e.lineno}: {e.msg}')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split('.')[0] not in ALLOWED_IMPORTS:
                    raise Rejected(f'import of {a.name!r} is not allowed (allowed: {", ".join(sorted(ALLOWED_IMPORTS))})')
        elif isinstance(node, ast.ImportFrom):
            if not node.module or node.module.split('.')[0] not in ALLOWED_IMPORTS or node.level:
                raise Rejected(f'import from {node.module!r} is not allowed')
            for a in node.names:
                if a.name == '*' or a.name.startswith('_') or a.name in BANNED_ATTRS:
                    raise Rejected(f'importing {a.name!r} is not allowed')
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise Rejected(f'{node.id!r} is not allowed here')
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith('_'):
                raise Rejected(f'attributes starting with "_" are not allowed ({node.attr})')
            if node.attr in BANNED_ATTRS:
                raise Rejected(f'.{node.attr} is not allowed here')
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise Rejected('global and nonlocal are not allowed')
    return tree


CHILD = r'''
import builtins, contextlib, io, json, sys, traceback
sys.path.insert(0, HERE)
import board_api
req = json.loads(sys.stdin.read())
allowed = set(req["allowed"])
real_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in allowed:
        raise ImportError("import of %r is not allowed" % name)
    return real_import(name, globals, locals, fromlist, level)
safe = {k: getattr(builtins, k) for k in req["builtins"] if hasattr(builtins, k)}
safe.update({"True": True, "False": False, "None": None, "__import__": guarded_import})
g = {"__builtins__": safe, "__name__": "__main__"}
import math
g["math"] = math
g.update({k: getattr(math, k) for k in dir(math) if not k.startswith("_")})   # pi, sin, sqrt... as models write them
for n in board_api.__all__:
    g[n] = getattr(board_api, n)
out = io.StringIO()
res = {"ok": True}
try:
    with contextlib.redirect_stdout(out):
        exec(compile(req["code"], "<code>", "exec"), g)
except BaseException as e:
    tb = traceback.extract_tb(e.__traceback__)
    line = next((f.lineno for f in reversed(tb) if f.filename == "<code>"), None)
    res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e) + (" (line %s)" % line if line else "")}
res["stdout"] = out.getvalue()[-6000:]
try:
    res["board"] = board_api.result()
except Exception as e:
    res["board"] = None
sys.stdout.write(json.dumps(res))
'''


def python_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == 'pythonw.exe':
        exe = os.path.join(os.path.dirname(exe), 'python.exe')
    return exe


def run(code, timeout=20):
    """-> {'ok', 'stdout', 'error'?, 'board'?}.  Never raises for the model's mistakes."""
    try:
        check(code)
    except Rejected as e:
        return {'ok': False, 'error': f'rejected: {e}', 'stdout': '', 'board': None}
    req = json.dumps({'code': code, 'allowed': sorted(ALLOWED_IMPORTS), 'builtins': SAFE_BUILTINS})
    try:
        r = subprocess.run([python_exe(), '-I', '-c', 'HERE = %r\n' % HERE + CHILD], input=req, capture_output=True,
                           text=True, timeout=timeout, encoding='utf-8',
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'the code ran longer than {timeout} s and was stopped', 'stdout': '', 'board': None}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {'ok': False, 'error': (r.stderr or 'the code crashed').strip()[-600:], 'stdout': '', 'board': None}
