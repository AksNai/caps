"""The drawing API that model-written code runs against, inside the sandbox.  Calls only record
primitives; Caps draws them: diagrams and 3D shapes in a panel, marks on the real screen.

2D, in data coordinates (axes are optional; they're fitted to what you draw):
    axes(x0, x1, y0, y1, xlabel='', ylabel='')
    plot(f, x0, x1, label='', color=None, dash=False)     f(x) -> y; or plot(xs, ys, ...)
    line(x1, y1, x2, y2, color=None, dash=False, label='')
    arrow(x1, y1, x2, y2, label='', color=None)
    point(x, y, label='', color=None)        text(x, y, s, color=None)
    hline(y, label='')    vline(x, label='')
    shade(f, g, x0, x1, color=None)          area between two curves
    polygon(points, color=None, label='')    rect(x0, y0, x1, y1, color=None, label='')
    circle(x, y, r, color=None)
    bar(xs, heights, labels=None, color=None)   a bar chart
Numbers may be expressions such as 'pi/2'.  Extra matplotlib-style arguments are ignored.
3D, in scene coordinates (any scale; the view fits itself; z is up, x points forward):
    surface(f, x0, x1, y0, y1, n=16, color=None)    z = f(x, y); the axes are evened out to fit
    sphere(center, r)        r may be (rx, ry, rz) for an ellipsoid
    box(center, size)        size is (dx, dy, dz) or one number
    cylinder(center, r, h, axis='z')   cone(center, r, h, axis='z')   axis is 'x', 'y', 'z' or a
                             direction; a cone's tip points along it
    torus(center, R, r)   mesh(vertices, faces)
    Objects keep their true proportions, so build a thing from parts at real sizes: a car is a
    long box for the body, a smaller box for the cabin and four cylinder(..., axis='y') wheels.
    line3d(p, q)   arrow3d(p, q, label='')   point3d(p, label='')   curve3d(f, t0, t1)   label3d(p, s)
On the user's screen, by element number from the list you were given (for teaching):
    mark(n, note='')        a numbered circle on the element, with a note
    highlight(n, note='')   a box around the element
Order:
    title(s)
    step(caption)   starts the next step: what follows is drawn after the earlier steps, and
                    the caption explains it.  Items before the first step() are drawn first.
"""
import math

__all__ = ['axes', 'plot', 'line', 'arrow', 'point', 'text', 'hline', 'vline', 'shade', 'polygon', 'rect', 'circle', 'bar',
           'surface', 'sphere', 'box', 'cylinder', 'cone', 'torus', 'mesh', 'line3d', 'arrow3d', 'point3d', 'curve3d',
           'label3d', 'mark', 'highlight', 'title', 'step']

PALETTE = ['#4f86ff', '#ff6b6b', '#2fbf71', '#ffa94d', '#9a8cff', '#20c5c5', '#f06595', '#94a3b8']
NAMED = {'blue': '#4f86ff', 'red': '#ff6b6b', 'green': '#2fbf71', 'orange': '#ffa94d', 'purple': '#9a8cff',
         'teal': '#20c5c5', 'cyan': '#20c5c5', 'pink': '#f06595', 'gray': '#94a3b8', 'grey': '#94a3b8',
         'black': '#9aa3b2', 'white': '#e8eaf0', 'yellow': '#f2c94c', 'brown': '#b08968'}
MAX_ITEMS = 6000


class Limit(Exception):
    pass


class _Board:
    def __init__(self):
        self.reset()

    def reset(self):
        self.title, self.axes, self.color_i, self.count, self.graph3d = '', None, 0, 0, False
        self.steps = [{'caption': '', 'items': []}]


B = _Board()


def result():
    steps = [s for s in B.steps if s['items'] or s['caption']]
    has3d = any(it['t'] in ('tri', 'seg3', 'pt3', 'lab3') for s in steps for it in s['items'])
    return {'title': B.title, 'axes': B.axes, 'three_d': has3d, 'graph3d': B.graph3d, 'steps': steps}


def _add(item):
    B.count += 1
    if B.count > MAX_ITEMS:
        raise Limit(f'too many things drawn (more than {MAX_ITEMS})')
    B.steps[-1]['items'].append(item)


_NAMES = {'pi': math.pi, 'e': math.e, 'tau': math.tau}


def _expr(s):
    """'pi/2', '2*pi', '3/4' -> a number; nothing but arithmetic is evaluated."""
    import ast
    tree = ast.parse(str(s).replace('π', 'pi').replace('^', '**'), mode='eval')

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in _NAMES:
            return _NAMES[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            a, b = ev(n.left), ev(n.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}.get(type(n.op)) if not isinstance(
                n.op, (ast.Div, ast.Pow)) else (a / b if isinstance(n.op, ast.Div) else a ** b)
        raise ValueError(f'could not read {s!r} as a number')
    return ev(tree)


def _num(v):
    v = _expr(v) if isinstance(v, str) else float(v)
    v = float(v)
    if not math.isfinite(v):
        raise ValueError('not a finite number')
    return round(v, 5)


def _opts(kw):
    """matplotlib habits -> ours: c/colour -> color, linestyle '--' -> dash; anything else is ignored."""
    color = kw.get('color', kw.get('c', kw.get('colour')))
    ls = str(kw.get('linestyle', kw.get('ls', '')))
    dash = bool(kw.get('dash')) or ls in ('--', ':', '-.', 'dashed', 'dotted')
    return color, dash, str(kw.get('label', ''))


def _pt(p, n=2):
    p = list(p)
    if len(p) != n:
        raise ValueError(f'expected a point with {n} numbers, got {p!r}')
    return [_num(v) for v in p]


def _color(c):
    if c is None:
        c = PALETTE[B.color_i % len(PALETTE)]
        B.color_i += 1
        return c
    s = str(c).strip().lower()
    if s in NAMED:
        return NAMED[s]
    if s.startswith('#') and len(s) in (4, 7) and all(ch in '0123456789abcdef' for ch in s[1:]):
        return s
    if s.isalpha() and len(s) < 24:
        return s            # any other colour name (steelblue, crimson...): the renderer knows them
    return PALETTE[0]


def _s(v, n=160):
    return str(v)[:n]


# ------------------------------------------------------------------ order
def title(s, **kw):
    B.title = _s(s, 120)


def step(caption='', **kw):
    cur = B.steps[-1]
    if not cur['items'] and not cur['caption']:
        cur['caption'] = _s(caption, 400)
    else:
        B.steps.append({'caption': _s(caption, 400), 'items': []})


# ------------------------------------------------------------------ 2D
def axes(x0=None, x1=None, y0=None, y1=None, xlabel='', ylabel='', **kw):
    x0 = kw.get('xmin', x0) if x0 is None else x0
    x1 = kw.get('xmax', x1) if x1 is None else x1
    y0 = kw.get('ymin', y0) if y0 is None else y0
    y1 = kw.get('ymax', y1) if y1 is None else y1
    if None in (x0, x1, y0, y1):
        raise ValueError('axes needs x0, x1, y0, y1 (or xmin, xmax, ymin, ymax)')
    x0, x1, y0, y1 = _num(x0), _num(x1), _num(y0), _num(y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError('axes need x0 < x1 and y0 < y1')
    B.axes = {'x0': x0, 'x1': x1, 'y0': y0, 'y1': y1, 'xlabel': _s(xlabel, 60), 'ylabel': _s(ylabel, 60)}


def _sample(f, x0, x1, n):
    segs, cur = [], []
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        try:
            y = f(x)
            if isinstance(y, complex):
                raise ValueError
            y = float(y)
            ok = math.isfinite(y)
        except (ValueError, ZeroDivisionError, OverflowError, TypeError, ArithmeticError):
            ok = False
        if ok:
            cur.append([round(x, 5), round(y, 5)])
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s) > 1]


def plot(f, x0=None, x1=None, label='', color=None, dash=False, n=240, **kw):
    c2, d2, _ = _opts(kw)
    color, dash = _color(color or c2), dash or d2
    if callable(f):
        if x0 is None or x1 is None:
            if B.axes is None:
                raise ValueError('plot(f) needs x0 and x1, or call axes() first')
            x0, x1 = B.axes['x0'], B.axes['x1']
        segs = _sample(f, _num(x0), _num(x1), max(8, min(int(n), 600)))
    else:
        xs, ys = list(f), list(x0)
        if len(xs) != len(ys):
            raise ValueError('plot(xs, ys): xs and ys must be the same length')
        segs = [[[_num(a), _num(b)] for a, b in zip(xs, ys)]]
    if not segs:
        raise ValueError('nothing to plot: the function gave no real values there')
    longest = max(range(len(segs)), key=lambda i: len(segs[i]))
    for i, s in enumerate(segs):
        _add({'t': 'poly', 'pts': s, 'color': color, 'dash': bool(dash), 'label': _s(label, 60) if i == longest else ''})


def line(x1, y1, x2=None, y2=None, color=None, dash=False, label='', **kw):
    if x2 is None and y2 is None:            # line((x1, y1), (x2, y2))
        (x1, y1), (x2, y2) = x1, y1
    c2, d2, _ = _opts(kw)
    color, dash = color or c2, dash or d2
    _add({'t': 'poly', 'pts': [_pt((x1, y1)), _pt((x2, y2))], 'color': _color(color), 'dash': bool(dash),
          'label': _s(label, 60)})


def arrow(x1, y1, x2=None, y2=None, label='', color=None, **kw):
    if x2 is None and y2 is None:            # arrow((x1, y1), (x2, y2))
        (x1, y1), (x2, y2) = x1, y1
    color = color or _opts(kw)[0]
    _add({'t': 'arrow', 'a': _pt((x1, y1)), 'b': _pt((x2, y2)), 'color': _color(color or 'gray'), 'label': _s(label, 60)})


def point(x, y, label='', color=None, **kw):
    _add({'t': 'pt', 'p': _pt((x, y)), 'color': _color(color or 'white'), 'label': _s(label, 60)})


def text(x, y, s, color=None, **kw):
    _add({'t': 'text', 'p': _pt((x, y)), 's': _s(s, 120), 'color': _color(color or 'white')})


def hline(y, label='', color=None, **kw):
    _add({'t': 'hline', 'y': _num(y), 'label': _s(label, 60), 'color': _color(color or 'gray')})


def vline(x, label='', color=None, **kw):
    _add({'t': 'vline', 'x': _num(x), 'label': _s(label, 60), 'color': _color(color or 'gray')})


def shade(f, g, x0, x1, color=None, **kw):
    a, b = _sample(f, _num(x0), _num(x1), 120), _sample(g, _num(x0), _num(x1), 120)
    if not a or not b:
        raise ValueError('shade(f, g, ...) needs both curves to have values there')
    _add({'t': 'fill', 'pts': a[0] + list(reversed(b[0])), 'color': _color(color), 'label': ''})


def polygon(points, color=None, label='', **kw):
    pts = [_pt(p) for p in points]
    if len(pts) < 3:
        raise ValueError('a polygon needs at least 3 points')
    _add({'t': 'fill', 'pts': pts, 'color': _color(color), 'label': _s(label, 60)})


def rect(x0, y0, x1, y1, color=None, label='', **kw):
    x0, y0, x1, y1 = _num(x0), _num(y0), _num(x1), _num(y1)
    _add({'t': 'fill', 'pts': [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], 'color': _color(color), 'label': _s(label, 60)})


def circle(x, y, r, color=None, **kw):
    x, y, r = _num(x), _num(y), abs(_num(r))
    pts = [[round(x + r * math.cos(2 * math.pi * i / 64), 5), round(y + r * math.sin(2 * math.pi * i / 64), 5)]
           for i in range(65)]
    _add({'t': 'poly', 'pts': pts, 'color': _color(color), 'dash': False, 'label': ''})


def bar(xs, heights, labels=None, color=None, width=0.8, **kw):
    """Bars centred on xs; labels (if given) are written above them."""
    xs, hs = [_num(x) for x in xs], [_num(h) for h in heights]
    if len(xs) != len(hs):
        raise ValueError('bar(xs, heights): the lists must be the same length')
    color = _color(color or _opts(kw)[0])
    w = _num(width) / 2
    for i, (x, h) in enumerate(zip(xs, hs)):
        lab = str(labels[i]) if labels is not None and i < len(labels) else ''
        _add({'t': 'fill', 'pts': [[x - w, 0], [x + w, 0], [x + w, h], [x - w, h]], 'color': color, 'label': '',
              'solid': True})
        if lab:
            _add({'t': 'text', 'p': [x - w, h], 's': _s(lab, 40), 'color': _color('white')})


# ------------------------------------------------------------------ 3D
def _tri(a, b, c, color):
    _add({'t': 'tri', 'p': [a, b, c], 'color': color})


def _quad(a, b, c, d, color):
    _tri(a, b, c, color)
    _tri(a, c, d, color)


def surface(f, x0, x1, y0, y1, n=16, color=None, **kw):
    B.graph3d = True
    color = _color(color)
    n = max(4, min(int(n), 40))
    x0, x1, y0, y1 = _num(x0), _num(x1), _num(y0), _num(y1)
    grid = []
    for i in range(n + 1):
        row = []
        for j in range(n + 1):
            x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * j / n
            try:
                z = float(f(x, y))
                row.append([round(x, 5), round(y, 5), round(z, 5)] if math.isfinite(z) else None)
            except (ValueError, ZeroDivisionError, OverflowError, TypeError, ArithmeticError):
                row.append(None)
        grid.append(row)
    for i in range(n):
        for j in range(n):
            q = [grid[i][j], grid[i + 1][j], grid[i + 1][j + 1], grid[i][j + 1]]
            if all(q):
                _quad(*q, color)


def _ring(c, r, z, k):
    return [[round(c[0] + r * math.cos(2 * math.pi * i / k), 5), round(c[1] + r * math.sin(2 * math.pi * i / k), 5),
             round(z, 5)] for i in range(k)]


def _frame(axis):
    """A unit direction and two unit vectors across it."""
    named = {'x': (1, 0, 0), 'y': (0, 1, 0), 'z': (0, 0, 1)}
    a = named.get(str(axis).lower().strip('+-')) if isinstance(axis, str) else None
    if a is None:
        if isinstance(axis, str):
            raise ValueError(f"axis should be 'x', 'y', 'z' or a direction like (1, 0, 0), not {axis!r}")
        a = _pt(axis, 3)
    elif str(axis).startswith('-'):
        a = tuple(-v for v in a)
    n = math.sqrt(sum(v * v for v in a))
    if n < 1e-12:
        raise ValueError('axis cannot be (0, 0, 0)')
    a = [v / n for v in a]
    helper = (1, 0, 0) if abs(a[0]) < 0.9 else (0, 1, 0)
    u = [a[1] * helper[2] - a[2] * helper[1], a[2] * helper[0] - a[0] * helper[2], a[0] * helper[1] - a[1] * helper[0]]
    un = math.sqrt(sum(v * v for v in u))
    u = [v / un for v in u]
    v = [a[1] * u[2] - a[2] * u[1], a[2] * u[0] - a[0] * u[2], a[0] * u[1] - a[1] * u[0]]
    return a, u, v


def _ring_along(c, r, t, k, frame):
    """k points on a circle of radius r, t along the axis from c."""
    a, u, v = frame
    out = []
    for i in range(k):
        cs, sn = math.cos(2 * math.pi * i / k), math.sin(2 * math.pi * i / k)
        out.append([round(c[j] + t * a[j] + r * (cs * u[j] + sn * v[j]), 5) for j in range(3)])
    return out


def sphere(center, r, color=None, **kw):
    c, color = _pt(center, 3), _color(color)
    rs = [abs(_num(x)) for x in r] if isinstance(r, (list, tuple)) else [abs(_num(r))] * 3
    if len(rs) != 3:
        raise ValueError('sphere r is one number or (rx, ry, rz)')
    lat, lon = 10, 16
    rings = []
    for i in range(lat + 1):
        ring = _ring((0, 0, 0), math.sin(math.pi * i / lat), -math.cos(math.pi * i / lat), lon)
        rings.append([[round(c[j] + rs[j] * q[j], 5) for j in range(3)] for q in ring])
    for i in range(lat):
        for j in range(lon):
            a, b = rings[i][j], rings[i][(j + 1) % lon]
            cc, d = rings[i + 1][(j + 1) % lon], rings[i + 1][j]
            _quad(a, b, cc, d, color)


def box(center, size, color=None, **kw):
    c, color = _pt(center, 3), _color(color)
    s = [abs(_num(v)) / 2 for v in (size if isinstance(size, (list, tuple)) else (size, size, size))]
    v = [[c[0] + sx * s[0], c[1] + sy * s[1], c[2] + sz * s[2]] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    for f in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
        _quad(*[v[i] for i in f], color)


def cylinder(center, r, h, color=None, axis='z', **kw):
    c, r, h, color = _pt(center, 3), abs(_num(r)), _num(h), _color(color)
    fr, k = _frame(axis), 20
    lo, hi = _ring_along(c, r, -h / 2, k, fr), _ring_along(c, r, h / 2, k, fr)
    end0 = [round(c[j] - h / 2 * fr[0][j], 5) for j in range(3)]
    end1 = [round(c[j] + h / 2 * fr[0][j], 5) for j in range(3)]
    for j in range(k):
        _quad(lo[j], lo[(j + 1) % k], hi[(j + 1) % k], hi[j], color)
        _tri(end0, lo[(j + 1) % k], lo[j], color)
        _tri(end1, hi[j], hi[(j + 1) % k], color)


def cone(center, r, h, color=None, axis='z', **kw):
    c, r, h, color = _pt(center, 3), abs(_num(r)), _num(h), _color(color)
    fr, k = _frame(axis), 20
    base = _ring_along(c, r, -h / 2, k, fr)
    end0 = [round(c[j] - h / 2 * fr[0][j], 5) for j in range(3)]
    tip = [round(c[j] + h / 2 * fr[0][j], 5) for j in range(3)]
    for j in range(k):
        _tri(base[j], base[(j + 1) % k], tip, color)
        _tri(end0, base[(j + 1) % k], base[j], color)


def torus(center, R, r, color=None, **kw):
    c, R, r, color = _pt(center, 3), abs(_num(R)), abs(_num(r)), _color(color)
    m, k = 20, 10

    def p(i, j):
        u, v = 2 * math.pi * i / m, 2 * math.pi * j / k
        return [round(c[0] + (R + r * math.cos(v)) * math.cos(u), 5), round(c[1] + (R + r * math.cos(v)) * math.sin(u), 5),
                round(c[2] + r * math.sin(v), 5)]
    for i in range(m):
        for j in range(k):
            _quad(p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1), color)


def mesh(vertices, faces, color=None, **kw):
    color = _color(color)
    vs = [_pt(v, 3) for v in vertices]
    for f in faces:
        f = [int(i) for i in f]
        for k in range(1, len(f) - 1):
            _tri(vs[f[0]], vs[f[k]], vs[f[k + 1]], color)


def line3d(p, q, color=None):
    _add({'t': 'seg3', 'a': _pt(p, 3), 'b': _pt(q, 3), 'color': _color(color or 'white'), 'arrow': False, 'label': ''})


def arrow3d(p, q, label='', color=None):
    _add({'t': 'seg3', 'a': _pt(p, 3), 'b': _pt(q, 3), 'color': _color(color or 'white'), 'arrow': True,
          'label': _s(label, 60)})


def point3d(p, label='', color=None):
    _add({'t': 'pt3', 'p': _pt(p, 3), 'color': _color(color or 'white'), 'label': _s(label, 60)})


def label3d(p, s, color=None):
    _add({'t': 'lab3', 'p': _pt(p, 3), 's': _s(s, 80), 'color': _color(color or 'white')})


def curve3d(f, t0, t1, n=120, color=None):
    color = _color(color)
    t0, t1 = _num(t0), _num(t1)
    pts = []
    for i in range(int(n) + 1):
        t = t0 + (t1 - t0) * i / n
        pts.append(_pt(f(t), 3))
    for a, b in zip(pts, pts[1:]):
        _add({'t': 'seg3', 'a': a, 'b': b, 'color': color, 'arrow': False, 'label': ''})


# ------------------------------------------------------------------ on the real screen
def mark(n, note=''):
    k = sum(1 for s in B.steps for it in s['items'] if it['t'] == 'mark') + 1
    _add({'t': 'mark', 'el': int(n), 'note': _s(note, 120), 'num': k})


def highlight(n, note=''):
    _add({'t': 'box', 'el': int(n), 'note': _s(note, 120)})
