"""What the companion draws to explain things: a panel that draws a diagram (or a rough 3D shape)
step by step with a caption for each step, and marks on the real screen for teaching how to use
a program.  The geometry is in drawing.py; the data comes from board_api.py via the sandbox."""
import math
import time

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

import drawing
from ui import OVERLAY_FLAGS, draw_shadow_panel, font, screen_rect_at


def qcolor(hexs, alpha=255):
    c = QColor(hexs)
    if not c.isValid():
        c = QColor('#4f86ff')
    c.setAlpha(alpha)
    return c


def tidy(text):
    """Model replies come with runs of blank lines; keep one paragraph break."""
    text = text.replace('\r', '').replace('**', '').replace('__', '')
    paras = [' '.join(par.split()) for par in text.split('\n\n')]
    return '\n'.join(par for par in paras if par)


def draw_fitted(p, rect, text, sizes=(14, 13, 12, 11)):
    """Word-wrapped text in rect: smaller type if it doesn't fit, then cut at a word with '…'."""
    text = tidy(text)
    flags = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
    for size in sizes:
        fm = QFontMetrics(font(size))
        if fm.boundingRect(QRect(0, 0, rect.width(), 10000), flags, text).height() <= rect.height():
            break
    else:
        words = text.split(' ')
        lo, hi = 0, len(words)
        while lo < hi:                       # the most words that still fit, with the ellipsis
            mid = (lo + hi + 1) // 2
            cut = ' '.join(words[:mid]) + '…'
            if fm.boundingRect(QRect(0, 0, rect.width(), 10000), flags, cut).height() <= rect.height():
                lo = mid
            else:
                hi = mid - 1
        text = ' '.join(words[:lo]) + '…'
    p.setFont(font(size))
    p.drawText(rect, flags, text)


def arrow_head(p, a, b, color, size=9):
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    pts = [QPointF(b[0], b[1]),
           QPointF(b[0] - size * math.cos(ang - 0.45), b[1] - size * math.sin(ang - 0.45)),
           QPointF(b[0] - size * math.cos(ang + 0.45), b[1] - size * math.sin(ang + 0.45))]
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPolygon(QPolygonF(pts))


class BoardPanel(QWidget):
    """A floating panel that draws a board step by step.  ◀ ▶ step through it, ↻ replays, ✕
    closes; drag a 3D shape to turn it.  Clickable but never takes keyboard focus."""
    closed = Signal()
    W, H, M, PAD = 620, 600, 18, 18
    HEAD, CAP, FOOT = 44, 120, 40

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.board, self.times, self.total = None, [], 0.0
        self.t0, self.paused_at = 0.0, None
        self.yaw, self.pitch, self.spin = 0.7, 0.45, True
        self.drag, self.buttons, self.hover = None, [], None
        self.resize(self.W + 2 * self.M, self.H + 2 * self.M)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)

    # -------------------------------------------------------------- control
    def show_board(self, board, anchor, avoid=None):
        """anchor: logical point to sit beside (the pointer); avoid: a logical rect not to cover."""
        self.board = board
        self.times, self.total = drawing.step_timing(board)
        self.t0, self.paused_at = time.time(), None
        self.yaw, self.pitch, self.spin = 0.7, 0.45, True
        if board.get('three_d'):
            self.frame3d = drawing.scene_frame(board)
        self.place(anchor, avoid)
        self.show()
        self.raise_()
        self.timer.start(16)

    def place(self, anchor, avoid=None):
        g = screen_rect_at(anchor)
        w, h = self.width(), self.height()
        x = anchor.x() + 40
        if avoid is not None and QRectF(x, anchor.y() - h / 2, w, h).intersects(QRectF(avoid)):
            x = avoid.right() + 20
        if x + w > g.right():
            x = anchor.x() - 40 - w
            if avoid is not None and x + w > avoid.left():
                x = avoid.left() - 20 - w
        x = max(g.left(), min(x, g.right() - w))
        y = max(g.top(), min(anchor.y() - h / 2, g.bottom() - h))
        self.move(round(x), round(y))

    def perch(self):
        """Where the companion sits: on the panel's top-left edge."""
        g = self.geometry()
        return QPointF(g.x() + self.M - 14, g.y() + self.M + 30)

    def elapsed(self):
        return (self.paused_at if self.paused_at is not None else time.time()) - self.t0

    def current(self):
        """(step index, drawing progress 0..1 of that step)."""
        t = self.elapsed()
        i = 0
        for k, st in enumerate(self.times):
            if t >= st:
                i = k
        return i, min(1.0, (t - self.times[i]) / 0.9) if self.times else 1.0

    def go(self, i):
        i = max(0, min(i, len(self.times) - 1))
        self.t0 = time.time() - self.times[i] - (0.9 if i < len(self.times) else 0)
        self.paused_at = None
        self.update()

    def close_board(self):
        self.timer.stop()
        self.hide()
        self.board = None
        self.closed.emit()

    # -------------------------------------------------------------- paint
    def paintEvent(self, _):
        if not self.board:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, m, pad = self.theme, self.M, self.PAD
        panel = QRect(m, m, self.W, self.H)
        draw_shadow_panel(p, panel, 16, t, 16)
        x0, y = panel.x() + pad, panel.y() + pad
        inner = self.W - 2 * pad
        si, prog = self.current()
        n = len(self.board['steps'])
        p.setFont(font(15, QFont.Weight.DemiBold))
        p.setPen(t.text)
        p.drawText(QRect(x0, y, inner - 120, 24), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(self.board.get('title') or 'Explanation', Qt.ElideRight, inner - 120))
        p.setFont(font(12))
        p.setPen(t.sub)
        if n > 1:
            p.drawText(QRect(x0, y, inner - 30, 24), Qt.AlignRight | Qt.AlignVCenter, f'step {si + 1} of {n}')
        self.buttons = []
        self._button(p, QRect(panel.right() - pad - 26, y, 26, 24), '✕', 'close')
        canvas = QRect(x0, panel.y() + self.HEAD + 8, inner, self.H - self.HEAD - self.CAP - self.FOOT - 24)
        p.save()
        p.setClipRect(canvas)
        if self.board.get('three_d'):
            self._paint3d(p, canvas, si, prog)
        else:
            self._paint2d(p, canvas, si, prog)
        p.restore()
        # caption of the current step
        cap = self.board['steps'][si].get('caption', '') if n else ''
        if si == 0 and self.board.get('intro'):     # the answer itself comes with the first step
            cap = self.board['intro'] + ('\n\n' + cap if cap else '')
        cap_rect = QRect(x0, canvas.bottom() + 12, inner, self.CAP)
        p.setPen(t.text)
        draw_fitted(p, cap_rect, cap)
        # footer
        fy = panel.bottom() - pad - 30
        bx = x0
        for label, key in (('◀', 'prev'), ('▶', 'next'), ('↻', 'replay')):
            self._button(p, QRect(bx, fy, 40, 30), label, key)
            bx += 46
        p.setFont(font(12))
        p.setPen(t.sub)
        hint = 'drag to turn it' if self.board.get('three_d') else ''
        p.drawText(QRect(bx + 8, fy, inner - (bx - x0) - 8, 30), Qt.AlignRight | Qt.AlignVCenter, hint)
        # stop repainting once a 2D board has finished drawing
        if not self.board.get('three_d') and self.elapsed() > self.total + 0.5 and self.drag is None:
            self.timer.setInterval(250)

    def _button(self, p, r, label, key):
        t = self.theme
        self.buttons.append((r, key))
        p.setPen(Qt.NoPen)
        c = QColor(t.chip)
        if self.hover == key:
            c = c.lighter(115) if not t.light else c.darker(106)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(r), 8, 8)
        p.setPen(t.text)
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.drawText(r, Qt.AlignCenter, label)

    def _paint2d(self, p, canvas, si, prog):
        t = self.theme
        board = self.board
        bounds = drawing.bounds2d(board)
        plot = canvas.adjusted(52, 8, -12, -40)
        v = drawing.View2D(bounds, (plot.x(), plot.y(), plot.width(), plot.height()))
        x0, x1, y0, y1 = bounds
        # axes, ticks, labels
        p.setPen(QPen(t.line, 1))
        p.setFont(font(11))
        for xt in drawing.nice_ticks(x0, x1, 6):
            px, _ = v(xt, y0)
            p.drawLine(QPointF(px, plot.top()), QPointF(px, plot.bottom()))
            p.setPen(t.sub)
            p.drawText(QRectF(px - 30, plot.bottom() + 4, 60, 16), Qt.AlignCenter, f'{xt:g}')
            p.setPen(QPen(t.line, 1))
        for yt in drawing.nice_ticks(y0, y1, 5):
            _, py = v(x0, yt)
            p.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            p.setPen(t.sub)
            p.drawText(QRectF(plot.left() - 50, py - 8, 44, 16), Qt.AlignRight | Qt.AlignVCenter, f'{yt:g}')
            p.setPen(QPen(t.line, 1))
        p.setPen(QPen(t.sub, 1.5))
        ax_x = v(0, 0)[0] if x0 <= 0 <= x1 else plot.left()
        ax_y = v(0, 0)[1] if y0 <= 0 <= y1 else plot.bottom()
        p.drawLine(QPointF(plot.left(), ax_y), QPointF(plot.right(), ax_y))
        p.drawLine(QPointF(ax_x, plot.top()), QPointF(ax_x, plot.bottom()))
        a = board.get('axes') or {}
        p.setFont(font(12, QFont.Weight.Medium))
        p.setPen(t.sub)
        if a.get('xlabel'):
            p.drawText(QRectF(plot.left(), plot.bottom() + 20, plot.width(), 18), Qt.AlignCenter, a['xlabel'])
        if a.get('ylabel'):
            p.save()
            p.translate(canvas.left() + 10, plot.center().y())
            p.rotate(-90)
            p.drawText(QRectF(-plot.height() / 2, -9, plot.height(), 18), Qt.AlignCenter, a['ylabel'])
            p.restore()
        p.save()
        p.setClipRect(plot.adjusted(-2, -2, 2, 2))
        labels = []
        for k, s in enumerate(board['steps']):
            if k > si:
                break
            f = prog if k == si else 1.0
            for it in s['items']:
                self._item2d(p, v, plot, it, f, labels)
        p.restore()
        p.setFont(font(12, QFont.Weight.DemiBold))
        for x, y, text, color in labels:
            fm = QFontMetrics(p.font())
            w = fm.horizontalAdvance(text) + 10
            r = QRectF(min(x + 6, plot.right() - w), y - 20, w, 18)
            bg = QColor(t.panel)
            bg.setAlpha(210)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 5, 5)
            p.setPen(color)
            p.drawText(r, Qt.AlignCenter, text)

    def _item2d(self, p, v, plot, it, f, labels):
        typ = it['t']
        col = qcolor(it.get('color', '#4f86ff'))
        if typ == 'poly':
            pts = [v(*q) for q in it['pts']]
            part = drawing.partial(pts, f)
            pen = QPen(col, 2.6, Qt.DashLine if it.get('dash') else Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath(QPointF(*part[0]))
            for q in part[1:]:
                path.lineTo(QPointF(*q))
            p.drawPath(path)
            if it.get('label') and f >= 1:
                labels.append((pts[-1][0], pts[-1][1], it['label'], col))
        elif typ == 'fill':
            p.setPen(Qt.NoPen)
            p.setBrush(qcolor(it['color'], int((215 if it.get('solid') else 70) * f)))
            p.drawPolygon(QPolygonF([QPointF(*v(*q)) for q in it['pts']]))
            if it.get('label') and f >= 1:
                cx = sum(q[0] for q in it['pts']) / len(it['pts'])
                cy = sum(q[1] for q in it['pts']) / len(it['pts'])
                labels.append((*v(cx, cy), it['label'], col))
        elif typ == 'arrow':
            a, b = v(*it['a']), v(*it['b'])
            part = drawing.partial([a, b], f)
            p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*part[0]), QPointF(*part[-1]))
            if f >= 1:
                arrow_head(p, a, b, col)
                if it.get('label'):
                    labels.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, it['label'], col))
        elif typ == 'pt':
            q = v(*it['p'])
            p.setPen(QPen(QColor(self.theme.panel), 2))
            p.setBrush(col if f > 0.3 else Qt.NoBrush)
            p.drawEllipse(QPointF(*q), 5 * min(1, f * 2), 5 * min(1, f * 2))
            if it.get('label') and f >= 0.5:
                labels.append((q[0], q[1], it['label'], self.theme.text))
        elif typ == 'text':
            q = v(*it['p'])
            p.setPen(qcolor(it['color'], int(255 * f)))
            p.setFont(font(13))
            p.drawText(QPointF(*q), it['s'])
        elif typ in ('hline', 'vline'):
            if typ == 'hline':
                a, b = (plot.left(), v(0, it['y'])[1]), (plot.right(), v(0, it['y'])[1])
            else:
                a, b = (v(it['x'], 0)[0], plot.bottom()), (v(it['x'], 0)[0], plot.top())
            part = drawing.partial([a, b], f)
            p.setPen(QPen(col, 1.6, Qt.DashLine))
            p.drawLine(QPointF(*part[0]), QPointF(*part[-1]))
            if it.get('label') and f >= 1:
                labels.append((b[0] - (80 if typ == 'hline' else -4), b[1] + (0 if typ == 'hline' else 22), it['label'], col))

    def _paint3d(self, p, canvas, si, prog):
        t = self.theme
        if self.spin and self.drag is None:
            self.yaw = 0.7 + (time.time() - self.t0) * 0.35
        c, r, k = self.frame3d
        cam = drawing.Camera(c, r, self.yaw, self.pitch, (canvas.x(), canvas.y(), canvas.width(), canvas.height()),
                             axis_scale=k)
        labels = []   # drawn last, so a surface never hides them
        for depth, kind, data, s in drawing.draw_order(self.board, cam, si):
            alpha = 1.0 if s < si else max(0.0, min(1.0, prog))
            if alpha <= 0:
                continue
            if kind == 'tri':
                pr, light, color = data
                base = qcolor(color)
                fill = QColor.fromHsvF(base.hsvHueF() if base.hsvHueF() >= 0 else 0, base.hsvSaturationF(),
                                       max(0.0, min(1.0, base.valueF() * light)), 0.93 * alpha)
                p.setPen(QPen(QColor(0, 0, 0, int(40 * alpha)), 0.6))
                p.setBrush(fill)
                p.drawPolygon(QPolygonF([QPointF(q[0], q[1]) for q in pr]))
            elif kind == 'seg':
                a, b, it = data
                col = qcolor(it['color'], int(255 * alpha))
                p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(a[0], a[1]), QPointF(b[0], b[1]))
                if it.get('arrow'):
                    arrow_head(p, a, b, col)
                if it.get('label'):
                    labels.append((b, it['label'], col))
            elif kind == 'pt':
                q, it = data
                p.setPen(QPen(QColor(t.panel), 2))
                p.setBrush(qcolor(it['color'], int(255 * alpha)))
                p.drawEllipse(QPointF(q[0], q[1]), 5, 5)
                if it.get('label'):
                    labels.append((q, it['label'], t.text))
            elif kind == 'lab':
                q, it = data
                labels.append((q, it['s'], qcolor(it['color'])))
        for q, text, color in labels:
            self._label3d(p, q, text, color)

    def _label3d(self, p, q, text, color):
        t = self.theme
        p.setFont(font(12, QFont.Weight.DemiBold))
        w = QFontMetrics(p.font()).horizontalAdvance(text) + 10
        r = QRectF(q[0] + 8, q[1] - 22, w, 18)
        bg = QColor(t.panel)
        bg.setAlpha(210)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 5, 5)
        p.setPen(color)
        p.drawText(r, Qt.AlignCenter, text)

    # -------------------------------------------------------------- mouse
    def _hit(self, pos):
        return next((k for r, k in self.buttons if r.contains(pos.toPoint())), None)

    def mousePressEvent(self, e):
        k = self._hit(e.position())
        if k == 'close':
            self.close_board()
        elif k == 'prev':
            self.go(self.current()[0] - 1)
        elif k == 'next':
            self.go(self.current()[0] + 1)
        elif k == 'replay':
            self.t0, self.paused_at = time.time(), None
            self.timer.setInterval(16)
        elif self.board and self.board.get('three_d'):
            self.drag = (e.position(), self.yaw, self.pitch)
            self.spin = False
        self.timer.setInterval(16)

    def mouseMoveEvent(self, e):
        if self.drag is not None:
            start, yaw, pitch = self.drag
            d = e.position() - start
            self.yaw = yaw + d.x() * 0.01
            self.pitch = max(-1.4, min(1.4, pitch + d.y() * 0.01))
            self.update()
            return
        h = self._hit(e.position())
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else (Qt.OpenHandCursor if self.board and self.board.get('three_d')
                                                            else Qt.ArrowCursor))
            self.update()

    def mouseReleaseEvent(self, e):
        self.drag = None


class ScreenMarks(QWidget):
    """Numbered marks, boxes and notes drawn over the real screen, on the elements the model named.
    Click-through: you click the real thing underneath."""

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.marks = []        # [{'rect': QRectF (logical), 'kind': 'mark'|'box', 'num', 'note'}]
        self.t0 = time.time()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)

    def show_marks(self, marks):
        self.marks = marks
        if not marks:
            self.clear()
            return
        desk = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(desk)
        self.t0 = time.time()
        self.show()
        self.raise_()
        self.timer.start(33)

    def clear(self):
        self.marks = []
        self.timer.stop()
        self.hide()

    def paintEvent(self, _):
        if not self.marks:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(-self.geometry().x(), -self.geometry().y())
        t = self.theme
        acc = t.accent
        pulse = (time.time() - self.t0) * 1.4 % 1
        appear = min(1.0, (time.time() - self.t0) / 0.25)
        for mk in self.marks:
            r = QRectF(mk['rect'])
            if mk['kind'] == 'box':
                p.setPen(QPen(acc, 3))
                p.setBrush(QColor(acc.red(), acc.green(), acc.blue(), 28))
                p.drawRoundedRect(r.adjusted(-6, -6, 6, 6), 8, 8)
                anchor = QPointF(r.right() + 6, r.top())
            else:
                c = QPointF(r.left() + min(r.width(), 40) / 2, r.center().y())
                rad = 15 * appear
                ring = QColor(acc)
                ring.setAlpha(int(200 * (1 - pulse)))
                p.setPen(QPen(ring, 3))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, rad + 16 * pulse, rad + 16 * pulse)
                p.setPen(QPen(QColor('white'), 2))
                p.setBrush(acc)
                p.drawEllipse(c, rad, rad)
                p.setPen(QColor('white'))
                p.setFont(font(15, QFont.Weight.Bold))
                p.drawText(QRectF(c.x() - 15, c.y() - 15, 30, 30), Qt.AlignCenter, str(mk.get('num', '')))
                anchor = QPointF(c.x() + 18, c.y() - 14)
            if mk.get('note'):
                self._note(p, anchor, mk['note'], r)

    def _note(self, p, anchor, text, target):
        t = self.theme
        p.setFont(font(14, QFont.Weight.Medium))
        fm = QFontMetrics(p.font())
        w = min(360, fm.horizontalAdvance(text) + 24)
        h = fm.boundingRect(QRect(0, 0, w - 24, 400), Qt.TextWordWrap, text).height() + 16
        g = screen_rect_at(anchor)
        x, y = anchor.x() + 10, anchor.y() - h - 6
        if x + w > g.right():
            x = target.left() - w - 14
        if y < g.top():
            y = target.bottom() + 10
        box = QRect(round(x), round(y), round(w), round(h))
        draw_shadow_panel(p, box, 10, t, 8)
        p.setPen(t.text)
        p.drawText(box.adjusted(12, 8, -12, -8), Qt.TextWordWrap, text)
