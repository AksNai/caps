"""Mouse gestures drawn while Caps Lock is held.  Pure functions, no Windows or Qt.

  straight flick         -> ('flick', d)   d = 0..7, counter-clockwise from right (screen y points down)
  circle (either way)    -> ('circle', 'cw' | 'ccw')
  scribble back and forth-> ('scribble', None)

Each flick direction throws the window under the pointer to that part of the screen (SLOTS).
Holding Caps Lock still instead opens a ring menu with the same eight slots.
"""
import math

MIN_LEN = 36          # logical px of travel before a stroke can mean anything
FLICK_MIN = 40        # a flick covers at least this distance...
FLICK_STRAIGHT = 0.82  # ...and start-to-end distance / path length is at least this
CIRCLE_TURN = math.radians(290)
SCRIBBLE_REVERSALS = 3
RESAMPLE = 8          # px between resampled points (removes mouse jitter)

#   d: (key, label)          d counts counter-clockwise from "right", 45 degrees apart
SLOTS = {
    0: ('right', 'Right half'),
    1: ('tr', 'Top right'),
    2: ('max', 'Maximize'),
    3: ('tl', 'Top left'),
    4: ('left', 'Left half'),
    5: ('bl', 'Bottom left'),
    6: ('min', 'Minimize'),
    7: ('br', 'Bottom right'),
}
ARROWS = {0: '→', 1: '↗', 2: '↑', 3: '↖', 4: '←', 5: '↙', 6: '↓', 7: '↘'}


def direction(dx, dy, cardinal_bias=False):
    """Screen-space vector -> 0..7.  With cardinal_bias a casual flick snaps to left/right/up/down
    unless it is clearly diagonal (cardinals get +-30 degrees, diagonals +-15)."""
    ang = math.degrees(math.atan2(-dy, dx)) % 360
    if not cardinal_bias:
        return int((ang + 22.5) // 45) % 8
    for d in (0, 2, 4, 6):
        if abs((ang - d * 45 + 180) % 360 - 180) <= 30:
            return d
    return int(ang // 90) * 2 + 1


def path_length(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def resample(pts, step=RESAMPLE):
    """Points spaced `step` apart along the path."""
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    carry = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        if seg == 0:
            continue
        pos = step - carry
        while pos <= seg:
            t = pos / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            pos += step
        carry = (carry + seg) % step
    if math.dist(out[-1], pts[-1]) > step / 2:
        out.append(pts[-1])
    return out


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def turning(rs):
    """(signed, absolute) total turning in radians along a resampled path."""
    heads = [math.atan2(b[1] - a[1], b[0] - a[0]) for a, b in zip(rs, rs[1:])]
    d = [_wrap(b - a) for a, b in zip(heads, heads[1:])]
    return sum(d), sum(abs(x) for x in d)


def reversals(rs, min_run=12):
    """How often the stroke turns back along its main axis (runs shorter than min_run px are noise)."""
    xs, ys = [p[0] for p in rs], [p[1] for p in rs]
    axis = 0 if max(xs) - min(xs) >= max(ys) - min(ys) else 1
    runs = []  # [sign, travel]
    for a, b in zip(rs, rs[1:]):
        v = b[axis] - a[axis]
        if abs(v) < 1e-6:
            continue
        s = 1 if v > 0 else -1
        if runs and runs[-1][0] == s:
            runs[-1][1] += abs(v)
        else:
            runs.append([s, abs(v)])
    runs = [r for r in runs if r[1] >= min_run]
    merged = []
    for s, t in runs:  # drop noise runs, then join neighbours that now point the same way
        if merged and merged[-1][0] == s:
            merged[-1][1] += t
        else:
            merged.append([s, t])
    return max(0, len(merged) - 1)


def classify(pts):
    """pts: [(x, y)] pointer positions in logical px, oldest first.  -> (kind, arg) or None."""
    if len(pts) < 2:
        return None
    total = path_length(pts)
    if total < MIN_LEN:
        return None
    rs = resample(pts)
    dx, dy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    span = math.hypot(dx, dy)
    if span >= FLICK_MIN and span / total >= FLICK_STRAIGHT:
        return ('flick', direction(dx, dy, cardinal_bias=True))
    if len(rs) >= 4:
        signed, absolute = turning(rs)
        if abs(signed) >= CIRCLE_TURN and abs(signed) >= 0.6 * absolute:
            return ('circle', 'cw' if signed > 0 else 'ccw')  # y points down: positive turning is clockwise
        if reversals(rs) >= SCRIBBLE_REVERSALS:
            return ('scribble', None)
    return None


def ring_slot(dx, dy, dead=24):
    """Ring menu: which slot the pointer is in, or None inside the dead zone at the centre."""
    if math.hypot(dx, dy) < dead:
        return None
    return direction(dx, dy)


def placement(area, key):
    """Target rectangle (l, t, r, b) for a slot inside a monitor's work area, or None for max/min."""
    l, t, r, b = area
    mx, my = l + (r - l) // 2, t + (b - t) // 2
    return {'left': (l, t, mx, b), 'right': (mx, t, r, b), 'tl': (l, t, mx, my), 'tr': (mx, t, r, my),
            'bl': (l, my, mx, b), 'br': (mx, my, r, b), 'max': (l, t, r, b)}.get(key)


class Wheel:
    """Mouse-wheel deltas -> whole steps.  Ordinary wheels send 120 per notch, smooth wheels and
    touchpads send small pieces; this adds them up so both move one item per notch."""
    NOTCH = 120

    def __init__(self):
        self.acc = 0

    def add(self, delta):
        self.acc += delta
        steps = int(self.acc / self.NOTCH)  # truncates toward zero
        self.acc -= steps * self.NOTCH
        return steps


# --------------------------------------------------------------------------- Caps Lock + drag
# Physical pixels throughout (they come straight from Win32).
MIN_W, MIN_H = 320, 200


def resize_edges(rect, pt):
    """Which edges a Caps+right-drag moves, from where it grabbed the window: a 3x3 grid, so the
    middle of the left side moves just the left edge, a corner moves both edges, and the middle of
    the window moves the nearest corner.  Returns (ex, ey), each -1 (left/top), 0 or 1."""
    l, t, r, b = rect
    fx = (pt[0] - l) / max(1, r - l)
    fy = (pt[1] - t) / max(1, b - t)
    ex = -1 if fx < 1 / 3 else 1 if fx > 2 / 3 else 0
    ey = -1 if fy < 1 / 3 else 1 if fy > 2 / 3 else 0
    if ex == 0 and ey == 0:
        ex, ey = (-1 if fx < 0.5 else 1), (-1 if fy < 0.5 else 1)
    return ex, ey


def drag_rect(rect, edges, dx, dy, min_w=MIN_W, min_h=MIN_H):
    """The window rect after dragging by (dx, dy): moved (edges None) or resized from those edges,
    never smaller than min_w x min_h."""
    l, t, r, b = rect
    if edges is None:
        return l + dx, t + dy, r + dx, b + dy
    ex, ey = edges
    if ex < 0:
        l = min(l + dx, r - min_w)
    elif ex > 0:
        r = max(r + dx, l + min_w)
    if ey < 0:
        t = min(t + dy, b - min_h)
    elif ey > 0:
        b = max(b + dy, t + min_h)
    return l, t, r, b


def snap_zone(pt, monitor, edge=3, corner=0.2):
    """Dragging a window with the pointer against a screen edge snaps it, like dragging a title
    bar: left/right edge -> that half (the top or bottom fifth of the edge -> that quarter), top
    edge -> maximize.  Returns a SLOTS key or None."""
    x, y = pt
    l, t, r, b = monitor
    h = b - t
    left, right = x <= l + edge, x >= r - 1 - edge
    if left or right:
        if y <= t + corner * h:
            return 'tl' if left else 'tr'
        if y >= b - corner * h:
            return 'bl' if left else 'br'
        return 'left' if left else 'right'
    if y <= t + edge:
        return 'max'
    return None


def unmaximized_rect(max_frame, normal_size, pt, grip=None):
    """Dragging a maximized window restores it under the pointer, keeping the pointer at the same
    fraction of the width (as Windows does with a title bar).  Returns the new window rect."""
    fl, ft, fr, fb = max_frame
    w, h = normal_size
    frac = (pt[0] - fl) / max(1, fr - fl)
    grip = min(pt[1] - ft, 40) if grip is None else grip
    l = round(pt[0] - frac * w)
    t = pt[1] - grip
    return l, t, l + w, t + h
