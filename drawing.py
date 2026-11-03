"""Geometry for drawing boards: fitting 2D axes, tick marks, drawing strokes part way (so a
diagram draws itself), and a small 3D camera with flat shading.  Pure functions, no Qt."""
import math


# ------------------------------------------------------------------ 2D
def nice_ticks(lo, hi, n=5):
    """Round tick values covering [lo, hi]: 0, 2, 4... or 0, 0.5, 1..."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    out, v = [], first
    while v <= hi + 1e-9 and len(out) < 50:
        out.append(round(v, 10))
        v += step
    return out


def bounds2d(board):
    """The data rectangle to show: the board's axes if it set them, else everything drawn, padded."""
    if board.get('axes'):
        a = board['axes']
        return a['x0'], a['x1'], a['y0'], a['y1']
    xs, ys = [], []
    for s in board['steps']:
        for it in s['items']:
            for p in item_points(it):
                xs.append(p[0])
                ys.append(p[1])
    if not xs:
        return 0.0, 1.0, 0.0, 1.0
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    return x0 - dx * 0.08, x1 + dx * 0.08, y0 - dy * 0.08, y1 + dy * 0.08


def item_points(it):
    t = it['t']
    if t in ('poly', 'fill'):
        return it['pts']
    if t == 'arrow':
        return [it['a'], it['b']]
    if t in ('pt', 'text'):
        return [it['p']]
    return []


class View2D:
    """Data coordinates -> pixels inside a rectangle (y up)."""

    def __init__(self, bounds, rect):
        self.x0, self.x1, self.y0, self.y1 = bounds
        self.l, self.t, self.w, self.h = rect

    def __call__(self, x, y):
        return (self.l + (x - self.x0) / (self.x1 - self.x0) * self.w,
                self.t + self.h - (y - self.y0) / (self.y1 - self.y0) * self.h)


def poly_length(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def partial(pts, frac):
    """The first `frac` of a polyline by length, so a stroke can be drawn growing."""
    if frac >= 1 or len(pts) < 2:
        return list(pts)
    if frac <= 0:
        return pts[:1]
    want = poly_length(pts) * frac
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        if d >= want:
            k = want / d if d else 0
            out.append((a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
            return out
        out.append(b)
        want -= d
    return out


def step_timing(board, draw=0.9, min_hold=2.2, chars_per_s=16):
    """Start time of each step: draw its strokes, then leave time to read its caption."""
    times, t = [], 0.0
    for s in board['steps']:
        times.append(t)
        t += draw + max(min_hold, len(s.get('caption', '')) / chars_per_s)
    return times, t


# ------------------------------------------------------------------ 3D
def scene_frame(board, max_ratio=1.2):
    """Centre, radius and per-axis scale of everything 3D, so any scale fits the view.  On a graph
    (board['graph3d']), an axis far longer than the others (z = x^2 - y^2 over a small square) is
    squeezed to at most max_ratio times the shortest, the way plotting tools do.  Objects (a car,
    a cube) keep their true proportions."""
    pts = []
    for s in board['steps']:
        for it in s['items']:
            if it['t'] == 'tri':
                pts += it['p']
            elif it['t'] == 'seg3':
                pts += [it['a'], it['b']]
            elif it['t'] in ('pt3', 'lab3'):
                pts.append(it['p'])
    if not pts:
        return (0.0, 0.0, 0.0), 1.0, (1.0, 1.0, 1.0)
    c = tuple((min(p[i] for p in pts) + max(p[i] for p in pts)) / 2 for i in range(3))
    ext = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
    flat = [e for e in ext if e > 1e-9]
    least = min(flat) if flat else 1.0
    k = tuple(min(1.0, max_ratio * least / e) if e > 1e-9 and board.get('graph3d') else 1.0 for e in ext)
    r = max(math.sqrt(sum(((p[i] - c[i]) * k[i]) ** 2 for i in range(3))) for p in pts) or 1.0
    return c, r, k


def rotation(yaw, pitch):
    """Turn around the vertical (z) axis, then tilt toward the viewer."""
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    return ((cy, -sy, 0.0), (sy * cp, cy * cp, -sp), (sy * sp, cy * sp, cp))


class Camera:
    """Scene point -> (screen x, screen y, depth).  z is up on screen; depth grows away from you."""

    def __init__(self, center, radius, yaw, pitch, rect, dist=3.2, axis_scale=(1.0, 1.0, 1.0)):
        self.c, self.r, self.k = center, radius, axis_scale
        self.m = rotation(yaw, pitch)
        self.l, self.t, self.w, self.h = rect
        self.dist = dist
        self.scale = min(self.w, self.h) * 0.42

    def view(self, p):
        q = [(p[i] - self.c[i]) * self.k[i] / self.r for i in range(3)]
        m = self.m
        x = m[0][0] * q[0] + m[0][1] * q[1] + m[0][2] * q[2]
        y = m[1][0] * q[0] + m[1][1] * q[1] + m[1][2] * q[2]    # toward/away from the viewer
        z = m[2][0] * q[0] + m[2][1] * q[1] + m[2][2] * q[2]    # up
        return x, y, z

    def project(self, p):
        x, y, z = self.view(p)
        k = self.dist / (self.dist + y)
        return (self.l + self.w / 2 + x * k * self.scale, self.t + self.h / 2 - z * k * self.scale, y)


def shade(tri_view, light=(-0.4, -0.7, 0.6)):
    """Brightness 0.35..1 from the angle between the face and a light from the upper left."""
    a, b, c = tri_view
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    ln = math.sqrt(sum(x * x for x in n)) or 1.0
    ll = math.sqrt(sum(x * x for x in light))
    d = abs(sum(n[i] * light[i] for i in range(3))) / ln / ll   # two-sided: shapes may be open
    return 0.35 + 0.65 * d


def draw_order(board, cam, upto_step=None):
    """Everything 3D, projected and sorted far to near (painter's algorithm).
    -> [(depth, kind, data, step)] with kind 'tri' | 'seg' | 'pt' | 'lab'."""
    out = []
    for si, s in enumerate(board['steps']):
        if upto_step is not None and si > upto_step:
            break
        for it in s['items']:
            t = it['t']
            if t == 'tri':
                pv = [cam.view(p) for p in it['p']]
                pr = [cam.project(p) for p in it['p']]
                depth = sum(p[2] for p in pr) / 3
                out.append((depth, 'tri', (pr, shade(pv), it['color']), si))
            elif t == 'seg3':
                a, b = cam.project(it['a']), cam.project(it['b'])
                out.append(((a[2] + b[2]) / 2 - 0.02, 'seg', (a, b, it), si))
            elif t in ('pt3', 'lab3'):
                p = cam.project(it['p'])
                out.append((p[2] - 0.05, 'pt' if t == 'pt3' else 'lab', (p, it), si))
    out.sort(key=lambda o: -o[0])
    return out


def kinds(board):
    """What a board contains: ('diagram' if 2D/3D items, 'screen' if marks on the screen)."""
    has_diag = any(it['t'] not in ('mark', 'box') for s in board['steps'] for it in s['items'])
    has_marks = any(it['t'] in ('mark', 'box') for s in board['steps'] for it in s['items'])
    return has_diag, has_marks
