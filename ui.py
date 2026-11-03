"""Everything drawn on screen: the companion cursor, speech bubbles, the focus timer pill, the
shield over blocked sites, the prompt, the gesture ring and trail, the snap preview, the window
carousel, answers about the screen and the agent's task card."""
import math
import time
from types import SimpleNamespace

from PySide6.QtCore import QFileInfo, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QFileIconProvider, QWidget

import winutil

OVERLAY_FLAGS = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
                 | Qt.WindowDoesNotAcceptFocus)


class Theme:
    def __init__(self, light):
        self.light = light
        if light:
            self.panel, self.text, self.sub = QColor(255, 255, 255, 246), QColor('#17181c'), QColor('#6b6e78')
            self.sel, self.line, self.chip = QColor('#eceefa'), QColor(0, 0, 0, 28), QColor('#f1f2f6')
            self.accent = QColor('#5b4beb')
        else:
            self.panel, self.text, self.sub = QColor(30, 31, 37, 246), QColor('#f2f2f5'), QColor('#9a9ca6')
            self.sel, self.line, self.chip = QColor('#33364a'), QColor(255, 255, 255, 30), QColor('#2a2c35')
            self.accent = QColor('#8c7dff')
        self.buddy_a, self.buddy_b, self.eye = QColor('#9a8cff'), QColor('#4f86ff'), QColor('#14152a')
        self.warn = QColor('#ff8a4c')


def font(px, weight=QFont.Weight.Normal):
    f = QFont('Segoe UI Variable Text')
    if not f.exactMatch():
        f = QFont('Segoe UI')
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def native_to_logical(x, y):
    """Physical screen pixels (Win32) -> Qt's per-monitor logical coordinates."""
    for s in QGuiApplication.screens():
        g, r = s.geometry(), s.devicePixelRatio()
        if g.x() <= x < g.x() + g.width() * r and g.y() <= y < g.y() + g.height() * r:
            return QPointF(g.x() + (x - g.x()) / r, g.y() + (y - g.y()) / r)
    return QPointF(x, y)


def native_rect_to_logical(l, t, r, b):
    a, z = native_to_logical(l, t), native_to_logical(r - 1, b - 1)
    return QRect(QPoint(round(a.x()), round(a.y())), QPoint(round(z.x()), round(z.y())))


def draw_shadow_panel(p, rect, radius, theme, shadow=14):
    """Rounded panel with a soft drop shadow drawn inside the widget's margin."""
    for i in range(shadow, 0, -2):
        a = int(26 * (1 - i / shadow) ** 2) + 1
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, a))
        p.drawRoundedRect(QRectF(rect).adjusted(-i, -i + 4, i, i + 4), radius + i, radius + i)
    p.setBrush(theme.panel)
    p.setPen(QPen(theme.line, 1))
    p.drawRoundedRect(QRectF(rect), radius, radius)


# =========================================================================== companion cursor
class Buddy(QWidget):
    """A small creature that appears at your pointer when you press Caps Lock and leaves when you're
    done (Caps Lock again, Esc, a click elsewhere, or a command finishing).  Click-through; never
    takes focus.

    modes: 'follow' (trails the pointer), 'park' (sits still next to the prompt or the window
    switcher), 'point' (flies to a target and circles it), 'think' (spinner).  Never shown over
    full-screen apps."""
    SIZE = 96
    OFFSET = QPointF(17, 19)

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.resize(self.SIZE, self.SIZE)
        self.pos_f = QPointF(QCursor.pos()) + self.OFFSET
        self.vel = QPointF(0, 0)
        self.look = QPointF(0, 0)
        self.mode, self.target = 'follow', None
        self.opacity, self.enabled, self.summoned = 0.0, True, False
        self.linger_until = 0.0   # stays a moment after you're done if it is answering you
        self.scale, self.scale_v = 0.0, 0.0   # pop-in / pop-out size
        self.last_cursor = QCursor.pos()
        self.hidden_fullscreen = False
        self.blink_at, self.next_blink = 0.0, time.time() + 2.5
        self.ring = None          # focus progress 0..1, or None
        self.listening, self.listen_t0 = False, 0.0
        self.t0 = self.t = time.time()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)

    # -------------------------------------------------------------- control
    def linger(self, seconds):
        self.linger_until = time.time() + seconds
        self.t = time.time()  # the timer may have been idling: don't take one giant step
        self.timer.setInterval(8)

    def summon(self, on):
        if on and not self.summoned and self.scale < 0.05:
            self.pos_f, self.vel = QPointF(QCursor.pos()) + self.OFFSET, QPointF(0, 0)  # appear right at the pointer
            self.blink_at = time.time()
        self.summoned = on
        if not on:
            self.follow()
        self.t = time.time()
        self.timer.setInterval(8)

    def point_at(self, logical_pt):
        """Fly next to something on screen (like a pointer touching it) and pulse a ring around it."""
        self.mode, self.target = 'point', QPointF(logical_pt)
        self.t = time.time()
        self.timer.setInterval(8)

    def park_at(self, logical_pt):
        """Sit still at a spot (next to the prompt, on top of the window switcher)."""
        if self.mode != 'park' or self.target != QPointF(logical_pt):
            self.mode, self.target = 'park', QPointF(logical_pt)
            self.t = time.time()
            self.timer.setInterval(8)

    def set_listening(self, on):
        """Listening for typing: a blinking caret beside it says 'type here'."""
        if on != self.listening:
            self.listening, self.listen_t0 = on, time.time()
            self.update()

    def follow(self):
        self.mode, self.target = 'follow', None

    def think(self, on=True, at=None):
        """A spinner while the model works; it waits at `at` (logical) if given, else at the pointer."""
        if on:
            self.mode, self.target = 'think', (QPointF(at) if at is not None else None)
            self.t = time.time()
            self.timer.setInterval(8)
        elif self.mode == 'think':
            self.follow()

    def set_enabled(self, on):
        self.enabled = on

    def center(self):
        return QPointF(self.pos_f)

    # -------------------------------------------------------------- animation
    def tick(self):
        now = time.time()
        dt = min(0.05, now - self.t)
        self.t = now
        cur = QCursor.pos()
        moved = (cur - self.last_cursor).manhattanLength() > 2
        if moved:
            self.last_cursor = cur
        want_visible = self.enabled and (self.summoned or now < self.linger_until) and not self.hidden_fullscreen
        target_op = 1.0 if want_visible else 0.0
        self.opacity += (target_op - self.opacity) * min(1.0, dt * (18 if want_visible else 10))
        # size spring: a little overshoot when it pops in, none when it leaves
        k, damp = (260.0, 19.0) if want_visible else (400.0, 40.0)
        self.scale_v += (k * (target_op - self.scale) - damp * self.scale_v) * dt
        self.scale = max(0.0, min(1.4, self.scale + self.scale_v * dt))

        if self.mode == 'point' and self.target is not None:
            target, omega = self.target + self.OFFSET, 9.0
        elif self.mode in ('park', 'think') and self.target is not None:
            target, omega = self.target, 16.0
        else:
            target, omega = QPointF(cur) + self.OFFSET, 15.0
        # critically damped spring: smooth, no overshoot
        x = self.pos_f - target
        acc = -omega * omega * x - 2 * omega * self.vel
        self.vel += acc * dt
        self.pos_f += self.vel * dt
        if (self.pos_f - target).manhattanLength() > 1500:  # pointer jumped screens: snap
            self.pos_f, self.vel = QPointF(target), QPointF(0, 0)

        d = QPointF(cur) - self.pos_f
        n = math.hypot(d.x(), d.y()) or 1.0
        look = QPointF(d.x() / n, d.y() / n) * min(1.0, n / 30)
        self.look += (look - self.look) * min(1.0, dt * 12)
        if now >= self.next_blink:
            self.blink_at, self.next_blink = now, now + 2.5 + 3.5 * ((now * 7.31) % 1)

        if not want_visible and (self.opacity < 0.01 or self.scale < 0.03):
            if self.isVisible():
                self.hide()
            self.opacity = self.scale = self.scale_v = 0.0
            if self.timer.interval() != 100:
                self.timer.setInterval(100)
            return
        if not self.isVisible():
            self.show()
            self._applied_op = -1
        if abs(self.opacity - getattr(self, '_applied_op', -1)) > 0.004:
            self._applied_op = self.opacity
            self.setWindowOpacity(self.opacity)
        p = QPoint(round(self.pos_f.x() - self.SIZE / 2), round(self.pos_f.y() - self.SIZE / 2))
        if p != self.pos():
            self.move(p)
        self.update()
        # 120 Hz while moving, 30 Hz while resting (just the idle bob and blinks)
        busy = (self.vel.manhattanLength() > 2 or moved or self.mode in ('point', 'think')
                or abs(self.opacity - target_op) > 0.01
                or abs(self.scale - target_op) > 0.01 or abs(self.scale_v) > 0.05)
        iv = 8 if busy else 33
        if self.timer.interval() != iv:
            self.timer.setInterval(iv)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.SIZE / 2, self.SIZE / 2)
        speed = math.hypot(self.vel.x(), self.vel.y())
        stretch = min(0.22, speed / 4000)
        ang = math.degrees(math.atan2(self.vel.y(), self.vel.x())) if speed > 1 else 0
        now = time.time()
        bob = math.sin((now - self.t0) * 2.2) * 0.8 if speed < 5 else 0
        r = 9.5

        # point mode: a pulsing ring around the target
        if self.mode == 'point' and self.target is not None:
            tl = self.target - self.pos_f + c
            pulse = (now * 1.6) % 1
            p.setPen(QPen(QColor(self.theme.accent.red(), self.theme.accent.green(), self.theme.accent.blue(),
                                 int(220 * (1 - pulse))), 2.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(tl, 10 + 14 * pulse, 10 + 14 * pulse)

        p.save()
        p.translate(c.x(), c.y() + bob)
        p.scale(max(0.01, self.scale), max(0.01, self.scale))
        p.setPen(Qt.NoPen)
        for grow, a in ((2.6, 14), (1.2, 26), (0.2, 30)):  # soft contact shadow
            p.setBrush(QColor(0, 0, 0, a))
            p.drawEllipse(QPointF(0, 3.0), r + grow, r + grow * 0.6)
        p.rotate(ang)
        p.scale(1 + stretch, 1 - stretch * 0.6)
        p.rotate(-ang)
        g = QRadialGradient(QPointF(-3.5, -4.5), r * 2.1)
        g.setColorAt(0, self.theme.buddy_a.lighter(112))
        g.setColorAt(1, self.theme.buddy_b)
        p.setBrush(g)
        p.setPen(QPen(QColor(255, 255, 255, 235), 1.6))
        p.drawEllipse(QPointF(0, 0), r, r)
        # eyes look toward the pointer; blink now and then
        blink = now - self.blink_at < 0.12
        eh = 0.7 if blink else 3.4
        lx, ly = self.look.x() * 2.2, self.look.y() * 1.8
        p.setPen(Qt.NoPen)
        p.setBrush(self.theme.eye)
        for ex in (-3.3, 3.3):
            p.drawRoundedRect(QRectF(ex + lx - 1.25, -1.2 + ly - eh / 2, 2.5, eh), 1.25, 1.25)
        if self.listening and ((now - self.listen_t0) % 1.06) < 0.62:
            p.setBrush(self.theme.accent)
            p.drawRoundedRect(QRectF(r + 6, -8, 2.2, 16), 1.1, 1.1)
        p.restore()

        if self.mode == 'think':
            p.setPen(QPen(self.theme.accent, 2.2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            start = int(-(now * 360 * 1.4) % 360 * 16)
            p.drawArc(QRectF(c.x() - 15, c.y() - 15 + bob, 30, 30), start, 100 * 16)
        elif self.ring is not None:
            p.setPen(QPen(QColor(self.theme.accent.red(), self.theme.accent.green(), self.theme.accent.blue(), 200),
                          2.0, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(c.x() - 14, c.y() - 14 + bob, 28, 28), 90 * 16, int(-360 * 16 * self.ring))


# =========================================================================== speech bubble
class Bubble(QWidget):
    """A short message that sits next to the companion (or near the pointer) and fades away."""

    def __init__(self, theme, buddy):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme, self.buddy = theme, buddy
        self.title, self.body = '', ''
        self.until = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def say(self, title, body='', seconds=4.0):
        self.title, self.body = title, body
        self.until = time.time() + seconds
        fm_t, fm_b = QFontMetrics(font(14, QFont.Weight.DemiBold)), QFontMetrics(font(13))
        w = min(380, max(fm_t.horizontalAdvance(title), fm_b.horizontalAdvance(body) if body else 0) + 32)
        bh = fm_b.boundingRect(QRect(0, 0, w - 32, 400), Qt.TextWordWrap, body).height() if body else 0
        self.resize(w + 28, 20 + fm_t.height() + (bh + 4 if body else 0) + 28)
        self.setWindowOpacity(1.0)
        self.place()
        self.show()
        self.timer.start(16)
        self.update()

    def place(self):
        c = self.buddy.center() if self.buddy.isVisible() else QPointF(QCursor.pos())
        scr = QGuiApplication.screenAt(c.toPoint()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x, y = c.x() + 18, c.y() - self.height() / 2 + 8
        if x + self.width() > g.right():
            x = c.x() - 18 - self.width()
        y = max(g.top() + 4, min(y, g.bottom() - self.height() - 4))
        self.move(round(x), round(y))

    def tick(self):
        left = self.until - time.time()
        if left <= 0:
            self.timer.stop()
            self.hide()
            return
        self.setWindowOpacity(min(1.0, left / 0.4))
        self.place()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(14, 10, -14, -18)
        draw_shadow_panel(p, rect, 12, self.theme, 10)
        p.setPen(self.theme.text)
        p.setFont(font(14, QFont.Weight.DemiBold))
        fm = QFontMetrics(p.font())
        p.drawText(QRect(rect.x() + 16, rect.y() + 10, rect.width() - 32, fm.height()), Qt.AlignLeft, self.title)
        if self.body:
            p.setPen(self.theme.sub)
            p.setFont(font(13))
            p.drawText(QRect(rect.x() + 16, rect.y() + 14 + fm.height(), rect.width() - 32, rect.height()),
                       Qt.TextWordWrap, self.body)


# =========================================================================== focus timer pill
class Pill(QWidget):
    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.text, self.frac = '', 0.0
        self.resize(220, 56)

    def set(self, text, frac):
        self.text, self.frac = text, frac
        fm = QFontMetrics(font(13, QFont.Weight.DemiBold))
        w = fm.horizontalAdvance(text) + 66
        if w != self.width():
            self.resize(w, 56)
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 6)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(12, 8, -12, -16)
        draw_shadow_panel(p, rect, rect.height() / 2, self.theme, 8)
        cx, cy = rect.x() + 18, rect.center().y() + 1
        p.setPen(QPen(self.theme.line, 2.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 7, 7)
        p.setPen(QPen(self.theme.accent, 2.4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(cx - 7, cy - 7, 14, 14), 90 * 16, int(-360 * 16 * self.frac))
        p.setPen(self.theme.text)
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.drawText(QRect(rect.x() + 32, rect.y(), rect.width() - 40, rect.height()), Qt.AlignVCenter, self.text)


# =========================================================================== shield over blocked sites
class Shield(QWidget):
    """Covers a browser window showing a blocked site.  It never takes keyboard focus (the browser
    keeps it, so Ctrl+Tab and Ctrl+W still work) but it does catch mouse clicks."""
    chosen = Signal(str)  # 'back' | 'close' | 'allow'

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.hwnd, self.site, self.left = None, '', ''
        self.buttons, self.hover = [], None

    def cover(self, hwnd, site, left_text):
        self.hwnd, self.site, self.left = hwnd, site, left_text
        self.follow_window()
        if not self.isVisible():
            self.show()
        self.update()

    def follow_window(self):
        if self.hwnd:
            self.setGeometry(native_rect_to_logical(*winutil.frame_rect(self.hwnd)))

    def uncover(self):
        self.hwnd = None
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(self.theme.panel)
        bg.setAlpha(250)
        p.fillRect(self.rect(), bg)
        card = QRect(0, 0, min(460, self.width() - 40), 210)
        card.moveCenter(self.rect().center())
        draw_shadow_panel(p, card, 16, self.theme, 12)
        p.setPen(self.theme.accent)
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.drawText(QRect(card.x() + 28, card.y() + 24, card.width() - 56, 20), Qt.AlignLeft, 'FOCUS MODE')
        p.setPen(self.theme.text)
        p.setFont(font(22, QFont.Weight.DemiBold))
        p.drawText(QRect(card.x() + 28, card.y() + 46, card.width() - 56, 32), Qt.AlignLeft, f'{self.site} can wait.')
        p.setPen(self.theme.sub)
        p.setFont(font(14))
        p.drawText(QRect(card.x() + 28, card.y() + 84, card.width() - 56, 22), Qt.AlignLeft, self.left)
        labels = [('back', 'Go back'), ('close', 'Close tab'), ('allow', 'Allow 5 min')]
        self.buttons = []
        x = card.x() + 28
        for key, text in labels:
            w = QFontMetrics(font(14, QFont.Weight.DemiBold)).horizontalAdvance(text) + 32
            r = QRect(x, card.bottom() - 62, w, 38)
            self.buttons.append((r, key))
            primary = key == 'back'
            p.setPen(Qt.NoPen)
            col = QColor(self.theme.accent) if primary else QColor(self.theme.chip)
            if self.hover == key:
                col = col.lighter(112) if not self.theme.light or primary else col.darker(105)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(r), 10, 10)
            p.setPen(QColor('white') if primary else self.theme.text)
            p.setFont(font(14, QFont.Weight.DemiBold))
            p.drawText(r, Qt.AlignCenter, text)
            x += w + 10

    def mouseMoveEvent(self, e):
        h = next((k for r, k in self.buttons if r.contains(e.position().toPoint())), None)
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        for r, k in self.buttons:
            if r.contains(e.position().toPoint()):
                self.chosen.emit(k)
                return


# =========================================================================== app icons
_icons, _provider = {}, []


def exe_icon(exe, px, dpr=1.0):
    """The program's icon as a crisp pixmap (cached), or None."""
    key = (exe.lower(), px, dpr)
    if key not in _icons:
        if not _provider:
            _provider.append(QFileIconProvider())
        pm = None
        if exe.lower().endswith('.lnk'):  # a Start menu shortcut: use its program's icon, without the arrow
            target = QFileInfo(exe).symLinkTarget()
            if target and QFileInfo(target).exists() and target.lower().endswith('.exe'):
                exe = target
        if exe:
            ic = _provider[0].icon(QFileInfo(exe))
            if not ic.isNull():
                pm = ic.pixmap(QSize(px, px), dpr)
        _icons[key] = pm
    return _icons[key]


def paint_glyph(p, s, r, theme):
    """Fallback icon for rows without a program icon: focus clock, window, or command prompt."""
    p.setPen(Qt.NoPen)
    p.setBrush(theme.chip)
    p.drawRoundedRect(QRectF(r), 7, 7)
    c = QPointF(r.center()) + QPointF(0.5, 0.5)
    k = r.width() / 28
    if s.icon == 'focus':
        p.setPen(QPen(theme.accent, 2.0, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 7.5 * k, 7.5 * k)
        p.drawLine(c, c + QPointF(0, -4.5 * k))
        p.drawLine(c, c + QPointF(3.2 * k, 2 * k))
    elif s.icon == 'ask':
        p.setPen(QPen(theme.accent, 1.8))
        p.setBrush(Qt.NoBrush)
        b = QRectF(c.x() - 7.5 * k, c.y() - 6.5 * k, 15 * k, 11 * k)
        p.drawRoundedRect(b, 3.5 * k, 3.5 * k)
        p.drawLine(QPointF(c.x() - 3 * k, b.bottom()), QPointF(c.x() - 5 * k, b.bottom() + 3.5 * k))
        p.setPen(Qt.NoPen)
        p.setBrush(theme.accent)
        for dx in (-3.5, 0, 3.5):
            p.drawEllipse(QPointF(b.center().x() + dx * k, b.center().y()), 1.25 * k, 1.25 * k)
    elif s.icon == 'agent':
        p.setPen(Qt.NoPen)
        p.setBrush(theme.accent)
        star = QPainterPath()
        for i in range(8):
            rr = (7.5 if i % 2 == 0 else 2.4) * k
            a = math.pi / 4 * i - math.pi / 2
            pt = c + QPointF(math.cos(a) * rr, math.sin(a) * rr)
            star.moveTo(pt) if i == 0 else star.lineTo(pt)
        star.closeSubpath()
        p.drawPath(star)
    elif s.icon == 'jot':
        p.setPen(QPen(theme.accent, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(c.x() - 6 * k, c.y() - 7 * k, 12 * k, 14 * k), 2, 2)
        for dy in (-3, 0, 3):
            p.drawLine(c + QPointF(-3 * k, dy * k), c + QPointF(3 * k, dy * k))
    elif s.icon == 'schedule':
        p.setPen(QPen(theme.accent, 1.6))
        p.setBrush(Qt.NoBrush)
        cal = QRectF(c.x() - 7 * k, c.y() - 6 * k, 14 * k, 13 * k)
        p.drawRoundedRect(cal, 2, 2)
        p.drawLine(QPointF(cal.left(), cal.top() + 3.5 * k), QPointF(cal.right(), cal.top() + 3.5 * k))
        p.setPen(Qt.NoPen)
        p.setBrush(theme.accent)
        for dx, dy in ((-3.5, 1.5), (0, 1.5), (3.5, 1.5), (-3.5, 4.5), (0, 4.5)):
            p.drawRect(QRectF(c.x() + dx * k - 1, c.y() + dy * k - 1, 2, 2))
    elif s.icon == 'app':
        p.setPen(Qt.NoPen)
        p.setBrush(theme.sub)
        for dx in (-3.5, 3.5):
            for dy in (-3.5, 3.5):
                p.drawRoundedRect(QRectF(c.x() + dx * k - 2.6 * k, c.y() + dy * k - 2.6 * k, 5.2 * k, 5.2 * k), 1.2, 1.2)
    elif s.win is not None:
        p.setPen(QPen(theme.sub, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(r).adjusted(6 * k, 7 * k, -6 * k, -7 * k), 2, 2)
    else:
        p.setPen(QPen(theme.accent, 2.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c + QPointF(-4.5 * k, -4.5 * k), c + QPointF(1 * k, 0))
        p.drawLine(c + QPointF(1 * k, 0), c + QPointF(-4.5 * k, 4.5 * k))
        p.drawLine(c + QPointF(2.5 * k, 5.5 * k), c + QPointF(6.5 * k, 5.5 * k))


def screen_rect_at(pt):
    scr = QGuiApplication.screenAt(QPointF(pt).toPoint()) or QGuiApplication.primaryScreen()
    return scr.availableGeometry()


def pill(p, rect, theme, text, sub=None, accent=False):
    """A small rounded label (used under the ring menu and next to a gesture in progress)."""
    draw_shadow_panel(p, rect, rect.height() / 2, theme, 6)
    p.setFont(font(13, QFont.Weight.DemiBold))
    p.setPen(theme.accent if accent else theme.text)
    if sub:
        fm = QFontMetrics(p.font())
        w = fm.horizontalAdvance(text)
        p.drawText(QRect(rect.x() + 14, rect.y(), w + 2, rect.height()), Qt.AlignVCenter, text)
        p.setFont(font(13))
        p.setPen(theme.sub)
        p.drawText(QRect(rect.x() + 14 + w, rect.y(), rect.width() - w - 24, rect.height()), Qt.AlignVCenter,
                   '  ·  ' + sub)
    else:
        p.drawText(rect, Qt.AlignCenter, text)


def pill_width(text, sub=None):
    w = QFontMetrics(font(13, QFont.Weight.DemiBold)).horizontalAdvance(text)
    if sub:
        w += QFontMetrics(font(13)).horizontalAdvance('  ·  ' + sub)
    return w + 30


# =========================================================================== prompt
ROW_VERBS = {'switch': 'Switch', 'open_app': 'Open', 'ask': 'Ask', 'agent': 'Do it', 'jot': 'Jot', 'schedule': 'Plan',
             'noop': ''}


class Prompt(QWidget):
    """The command line that grows out of the companion when you type after tapping Caps Lock.

    It never takes keyboard focus.  Keys reach it through the keyboard hook, so the window you
    were in stays active, nothing needs to be handed back when it closes, and a shortcut such as
    Alt+Tab simply closes it and works.  Rows can be clicked."""
    activated = Signal(object)   # the chosen Suggestion
    query_changed = Signal(str)

    W, HEAD, ROW, MAX_ROWS, FOOT, M = 470, 52, 48, 6, 30, 22

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.text = ''
        self.items, self.sel, self.top, self.hover = [], 0, 0, None
        self.anchor = QPointF()
        self.side = 1                 # 1: card to the right of the companion, -1: to the left
        self.blink_t0 = time.time()
        self.blink = QTimer(self)
        self.blink.timeout.connect(self.update)
        self.resize(self.W + 2 * self.M, self.HEAD + 2 * self.M)

    # -------------------------------------------------------------- placement
    def full_height(self):
        return self.HEAD + 10 + self.MAX_ROWS * self.ROW + self.FOOT + 2 * self.M

    def open_at(self, anchor):
        """anchor: the companion's centre (logical px).  The input line sits level with it; the card
        keeps room below for a full list so the input never jumps while you type."""
        self.text, self.items, self.sel, self.top, self.hover = '', [], 0, 0, None
        self.anchor = QPointF(anchor)
        g = screen_rect_at(anchor)
        w = self.W + 2 * self.M
        x = anchor.x() + 18 - self.M
        self.side = 1
        if x + w > g.right():
            x, self.side = anchor.x() - 18 - self.W - self.M, -1
        x = max(g.left() - self.M, x)
        y = anchor.y() - self.M - self.HEAD / 2
        y = max(g.top() - self.M + 4, min(y, g.bottom() - self.full_height() + self.M))
        self.move(round(x), round(y))
        self.relayout()
        self.blink_t0 = time.time()
        self.show()
        self.blink.start(265)

    def perch(self):
        """Where the companion sits: just outside the card, level with the input line."""
        g = self.geometry()
        y = g.y() + self.M + self.HEAD / 2
        return QPointF(g.x() + self.M - 15, y) if self.side == 1 else QPointF(g.right() - self.M + 16, y)

    def hide_prompt(self):
        self.blink.stop()
        self.hide()

    # -------------------------------------------------------------- content
    def rows(self):
        return min(self.MAX_ROWS, len(self.items))

    def relayout(self):
        h = self.HEAD + (10 + self.rows() * self.ROW if self.items else 0) + self.FOOT
        if self.height() != h + 2 * self.M:
            self.resize(self.W + 2 * self.M, h + 2 * self.M)

    def set_items(self, items, sel=0):
        self.items = items
        self.sel = max(0, min(sel, len(items) - 1)) if items else 0
        self.top = 0
        self._scroll()
        self.relayout()
        self.update()

    def set_text(self, text):
        self.text = text
        self.blink_t0 = time.time()
        self.query_changed.emit(text)
        self.update()

    def type_char(self, ch):
        self.set_text(self.text + ch)

    def handle_key(self, name):
        if name in ('down', 'tab'):
            self.move_sel(1)
        elif name == 'up':
            self.move_sel(-1)
        elif name == 'enter':
            self.choose()
        elif name == 'back':
            self.set_text(self.text[:-1])
        elif name == 'backword':
            t = self.text.rstrip()
            self.set_text(t[:t.rfind(' ') + 1] if ' ' in t else '')
        elif name == 'paste':
            clip = QGuiApplication.clipboard().text().strip().splitlines()
            if clip:
                self.set_text(self.text + clip[0][:200])

    def move_sel(self, d):
        if self.items:
            self.sel = (self.sel + d) % len(self.items)
            self._scroll()
            self.update()

    def _scroll(self):
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + self.MAX_ROWS:
            self.top = self.sel - self.MAX_ROWS + 1

    def choose(self, idx=None):
        idx = self.sel if idx is None else idx
        if 0 <= idx < len(self.items):
            self.activated.emit(self.items[idx])

    # -------------------------------------------------------------- mouse
    def _row_at(self, pt):
        m = self.M
        y0 = m + self.HEAD + 5
        if not (m <= pt.x() <= m + self.W) or pt.y() < y0:
            return None
        i = int((pt.y() - y0) // self.ROW)
        return self.top + i if i < self.rows() and self.top + i < len(self.items) else None

    def mouseMoveEvent(self, e):
        r = self._row_at(e.position())
        if r != self.hover:
            self.hover = r
            self.update()

    def leaveEvent(self, _):
        if self.hover is not None:
            self.hover = None
            self.update()

    def mousePressEvent(self, e):
        r = self._row_at(e.position())
        if r is not None:
            self.choose(r)

    # -------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, m = self.theme, self.M
        panel = QRect(m, m, self.W, self.height() - 2 * m)
        draw_shadow_panel(p, panel, 14, t, 16)
        # input line
        p.setFont(font(18))
        fm = QFontMetrics(p.font())
        tx, room = m + 20, self.W - 40
        line = QRect(tx, m, room, self.HEAD)
        if self.text:
            shown = fm.elidedText(self.text, Qt.ElideLeft, room - 6)
            p.setPen(t.text)
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, shown)
            cx = tx + fm.horizontalAdvance(shown) + 1
        else:
            p.setPen(t.sub)
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, 'Window, app, command or question')
            cx = tx
        if ((time.time() - self.blink_t0) % 1.06) < 0.62:
            p.setPen(Qt.NoPen)
            p.setBrush(t.accent)
            p.drawRoundedRect(QRectF(cx, m + self.HEAD / 2 - 11, 2.2, 22), 1.1, 1.1)
        # results
        if self.items:
            p.setPen(QPen(t.line, 1))
            p.drawLine(m + 1, m + self.HEAD, m + self.W - 1, m + self.HEAD)
        y = m + self.HEAD + 5
        dpr = self.devicePixelRatioF()
        for i in range(self.top, self.top + self.rows()):
            s = self.items[i]
            r = QRect(m + 8, y, self.W - 16, self.ROW)
            if i == self.sel or i == self.hover:
                c = QColor(t.sel)
                if i != self.sel:
                    c.setAlpha(110)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(QRectF(r).adjusted(0, 2, 0, -2), 9, 9)
            ir = QRect(r.x() + 11, r.y() + (self.ROW - 26) // 2, 26, 26)
            pm = exe_icon(s.win.exe, 26, dpr) if s.win is not None else                 exe_icon(s.icon_path, 26, dpr) if getattr(s, 'icon_path', '') else None
            if pm is not None:
                p.drawPixmap(ir, pm)
            else:
                paint_glyph(p, s, ir, t)
            tw = r.width() - 50 - (64 if i == self.sel else 12)
            p.setFont(font(14, QFont.Weight.Medium))
            p.setPen(t.text)
            title = QFontMetrics(p.font()).elidedText(s.title, Qt.ElideRight, tw)
            if s.subtitle:
                p.drawText(QRect(r.x() + 48, r.y() + 6, tw, 20), Qt.AlignLeft | Qt.AlignVCenter, title)
                p.setFont(font(12))
                p.setPen(t.sub)
                sub = QFontMetrics(p.font()).elidedText(s.subtitle, Qt.ElideRight, tw)
                p.drawText(QRect(r.x() + 48, r.y() + 25, tw, 17), Qt.AlignLeft | Qt.AlignVCenter, sub)
            else:
                p.drawText(QRect(r.x() + 48, r.y(), tw, self.ROW), Qt.AlignLeft | Qt.AlignVCenter, title)
            label = ROW_VERBS.get(s.action[0] if s.action else 'noop', 'Run')
            if i == self.sel and label:
                p.setPen(t.sub)
                p.setFont(font(12))
                p.drawText(QRect(r.right() - 74, r.y(), 64, self.ROW), Qt.AlignRight | Qt.AlignVCenter, f'{label} ↵')
            y += self.ROW
        p.setPen(t.sub)
        p.setFont(font(12))
        foot = QRect(m + 20, self.height() - m - self.FOOT, self.W - 40, self.FOOT - 4)
        p.drawText(foot, Qt.AlignLeft | Qt.AlignVCenter, '↑↓ choose   ↵ go   esc close')
        p.drawText(foot, Qt.AlignRight | Qt.AlignVCenter, '? ask  ·  ! do  ·  jot …  ·  focus 50')


# =========================================================================== gestures
def paint_slot_glyph(p, center, key, color, frame, w=30, h=20):
    """A tiny screen with the part the window will take filled in."""
    r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    p.setPen(QPen(frame, 1.5))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r, 3, 3)
    inner = r.adjusted(2.5, 2.5, -2.5, -2.5)
    hw, hh = inner.width() / 2, inner.height() / 2
    fill = {'left': QRectF(inner.x(), inner.y(), hw, inner.height()),
            'right': QRectF(inner.x() + hw, inner.y(), hw, inner.height()),
            'tl': QRectF(inner.x(), inner.y(), hw, hh), 'tr': QRectF(inner.x() + hw, inner.y(), hw, hh),
            'bl': QRectF(inner.x(), inner.y() + hh, hw, hh), 'br': QRectF(inner.x() + hw, inner.y() + hh, hw, hh),
            'max': inner, 'restore': QRectF(inner.x() + 3, inner.y() + 2.5, inner.width() - 6, inner.height() - 5),
            'min': QRectF(inner.center().x() - 5, inner.bottom() - 2.5, 10, 2.5)}[key]
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(fill, 1.5, 1.5)


class GestureLayer(QWidget):
    """Draws a gesture while Caps Lock is held: the stroke, the ring menu and a label saying what
    will happen when you let go.  Click-through, never focused, and only as big as what it draws."""
    RING_R, DEAD, GLYPH_R = 112, 24, 74
    HINT = ('circle', 'focus'), ('scribble', 'undo'), ('scroll', 'windows'), ('drag', 'move')

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.reset()
        self.fade = QTimer(self)
        self.fade.timeout.connect(self._fade_step)

    def reset(self):
        self.points = []            # (x, y) global logical
        self.ring_c, self.ring_t0 = None, 0.0
        self.slots = {}             # d -> (key, label)
        self.hover, self.pointer = None, None
        self.label, self.sub = '', ''
        self.fading = 0.0

    def begin(self):
        self.fade.stop()
        self.reset()
        self.setWindowOpacity(1.0)

    def set_points(self, pts):
        self.points = pts

    def show_ring(self, center, slots):
        self.ring_c, self.ring_t0, self.slots = QPointF(center), time.time(), slots

    def set_hover(self, d, pointer):
        self.hover, self.pointer = d, QPointF(pointer)

    def set_label(self, text, sub=''):
        self.label, self.sub = text, sub

    def end(self, fade=False):
        if fade and self.isVisible() and self.points:
            self.fading = time.time()
            self.label = ''
            self.fade.start(16)
            self.update()
        else:
            self.fade.stop()
            self.hide()
            self.reset()

    def _fade_step(self):
        k = (time.time() - self.fading) / 0.22
        if k >= 1:
            self.end()
            return
        self.setWindowOpacity(1 - k)

    # -------------------------------------------------------------- geometry
    def _label_rect(self):
        if not self.label or not self.points:
            return None
        x, y = self.points[-1]
        return QRectF(x + 22, y + 16, pill_width(self.label, self.sub), 32)

    def _ring_rect(self):
        if self.ring_c is None:
            return None
        R = self.RING_R + 18
        return QRectF(self.ring_c.x() - max(R, 250), self.ring_c.y() - R, 2 * max(R, 250), 2 * R + 58)

    def content_rect(self):
        r = QRectF()
        if self.points:
            xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
            r = QRectF(min(xs) - 12, min(ys) - 12, max(xs) - min(xs) + 24, max(ys) - min(ys) + 24)
        for extra in (self._label_rect(), self._ring_rect()):
            if extra is not None:
                r = extra if r.isNull() else r.united(extra)
        return r

    def refresh(self):
        want = self.content_rect()
        if want.isNull():
            return
        if not self.isVisible() or not QRectF(self.geometry()).contains(want):
            desk = QGuiApplication.primaryScreen().virtualGeometry()
            self.setGeometry(want.adjusted(-140, -140, 140, 140).toAlignedRect() & desk)
        if not self.isVisible():
            self.show()
        self.update()

    # -------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(-self.geometry().x(), -self.geometry().y())
        t = self.theme
        if self.ring_c is not None:
            self._paint_ring(p, t)
        elif len(self.points) > 1:
            # opaque segments (overlapping translucent ones would leave beads at every joint),
            # tapering from a thin pale tail to the tip
            n = len(self.points)
            tail, tip = t.accent.lighter(165) if not t.light else QColor(t.accent).lighter(140), t.accent
            for i in range(1, n):
                k = i / (n - 1)
                c = QColor(round(tail.red() + (tip.red() - tail.red()) * k),
                           round(tail.green() + (tip.green() - tail.green()) * k),
                           round(tail.blue() + (tip.blue() - tail.blue()) * k))
                p.setPen(QPen(c, 1.4 + 3.6 * k, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                a, b = self.points[i - 1], self.points[i]
                p.drawLine(QPointF(*a), QPointF(*b))
            lr = self._label_rect()
            if lr is not None:
                pill(p, lr, t, self.label, self.sub)

    def _paint_ring(self, p, t):
        c = self.ring_c
        k = min(1.0, (time.time() - self.ring_t0) / 0.14)
        ease = 1 - (1 - k) ** 3
        p.setOpacity(ease)
        p.save()
        p.translate(c)
        p.scale(0.88 + 0.12 * ease, 0.88 + 0.12 * ease)
        R = self.RING_R
        for i in range(12, 0, -2):  # shadow
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, int(22 * (1 - i / 12) ** 2) + 1))
            p.drawEllipse(QPointF(0, 4), R + i, R + i)
        p.setBrush(t.panel)
        p.setPen(QPen(t.line, 1))
        p.drawEllipse(QPointF(0, 0), R, R)
        if self.hover is not None:  # highlighted wedge
            a0 = self.hover * 45 - 22.5
            wedge = QPainterPath()
            wedge.arcMoveTo(QRectF(-R + 6, -R + 6, 2 * R - 12, 2 * R - 12), a0)
            wedge.arcTo(QRectF(-R + 6, -R + 6, 2 * R - 12, 2 * R - 12), a0, 45)
            wedge.arcTo(QRectF(-self.DEAD - 4, -self.DEAD - 4, 2 * self.DEAD + 8, 2 * self.DEAD + 8), a0 + 45, -45)
            wedge.closeSubpath()
            sel = QColor(t.accent)
            sel.setAlpha(46)
            p.setPen(Qt.NoPen)
            p.setBrush(sel)
            p.drawPath(wedge)
        for d, (key, _) in self.slots.items():
            ang = math.radians(d * 45)
            at = QPointF(math.cos(ang) * self.GLYPH_R, -math.sin(ang) * self.GLYPH_R)
            on = d == self.hover
            fill = QColor(t.accent) if on else QColor(t.sub)
            if not on:
                fill.setAlpha(150)
            paint_slot_glyph(p, at, key, fill, t.accent if on else t.sub)
        p.setPen(QPen(t.line, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0, 0), self.DEAD, self.DEAD)
        p.restore()
        if self.pointer is not None:  # ink from the centre to the pointer
            ink = QColor(t.accent)
            ink.setAlpha(150)
            p.setPen(QPen(ink, 2, Qt.SolidLine, Qt.RoundCap))
            v = self.pointer - c
            n = math.hypot(v.x(), v.y())
            if n > self.DEAD:
                p.drawLine(c + v * (self.DEAD / n), self.pointer)
        p.setPen(Qt.NoPen)
        p.setBrush(t.accent)
        p.drawEllipse(c, 3, 3)
        # under the ring: what letting go does, or the other gestures
        y = c.y() + self.RING_R + 14
        if self.hover is not None and self.hover in self.slots:
            text, sub = self.label or self.slots[self.hover][1], self.sub
            w = pill_width(text, sub)
            pill(p, QRectF(c.x() - w / 2, y, w, 34), t, text, sub)
        else:
            text = '   '.join(f'{a} → {b}' for a, b in self.HINT)
            w = QFontMetrics(font(13)).horizontalAdvance(text) + 32
            r = QRectF(c.x() - w / 2, y, w, 34)
            draw_shadow_panel(p, r, 17, t, 6)
            p.setFont(font(13))
            p.setPen(t.sub)
            p.drawText(r, Qt.AlignCenter, text)
        p.setOpacity(1)


class Ghost(QWidget):
    """A translucent preview of where a window will land."""

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme

    def show_rect(self, rect):
        if rect is None or rect.isEmpty():
            self.hide()
            return
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill, edge = QColor(self.theme.accent), QColor(self.theme.accent)
        fill.setAlpha(42)
        edge.setAlpha(190)
        p.setBrush(fill)
        p.setPen(QPen(edge, 2))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(6, 6, -6, -6), 10, 10)


# =========================================================================== window carousel
class Carousel(QWidget):
    """Caps Lock + scroll wheel: your open windows in a row.  Each notch moves one window along; let
    go of Caps Lock to switch to the highlighted one.  Click-through, never focused."""
    TILE, ICON, VISIBLE, PAD, M = 76, 40, 9, 14, 20

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.windows, self.sel, self.first = [], 0, 0
        self.slide = 0.0              # animated highlight position (in tiles)
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._animate)

    def open(self, windows, sel, anchor):
        self.windows, self.sel = windows, sel
        n = min(self.VISIBLE, len(windows))
        w = n * self.TILE + 2 * self.PAD + 2 * self.M
        h = self.PAD + self.TILE + 50 + self.PAD + 2 * self.M
        self.resize(w, h)
        g = screen_rect_at(anchor)
        x = anchor.x() - w / 2
        y = anchor.y() - h - 30
        if y < g.top():
            y = anchor.y() + 40
        x = max(g.left(), min(x, g.right() - w))
        y = max(g.top(), min(y, g.bottom() - h))
        self.move(round(x), round(y))
        self._scroll()
        self.slide = self.sel - self.first
        self.show()
        self.update()

    def step(self, d):
        if self.windows:
            self.sel = (self.sel + d) % len(self.windows)
            self._scroll()
            self.anim.start(8)

    def selected(self):
        return self.windows[self.sel] if self.windows else None

    def _scroll(self):
        n = len(self.windows)
        self.first = max(0, min(self.sel - self.VISIBLE // 2, n - self.VISIBLE)) if n > self.VISIBLE else 0

    def _animate(self):
        target = self.sel - self.first
        self.slide += (target - self.slide) * 0.35
        if abs(target - self.slide) < 0.01:
            self.slide = target
            self.anim.stop()
        self.update()

    def perch(self):
        """Where the companion sits: on the panel's top edge above the chosen window."""
        g = self.geometry()
        x = g.x() + self.M + self.PAD + (self.sel - self.first + 0.5) * self.TILE
        return QPointF(x, g.y() + self.M - 4)

    def dismiss(self):
        self.anim.stop()
        self.hide()
        self.windows = []

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, m = self.theme, self.M
        panel = QRect(m, m, self.width() - 2 * m, self.height() - 2 * m)
        draw_shadow_panel(p, panel, 18, t, 14)
        x0, y0 = m + self.PAD, m + self.PAD
        hl = QRectF(x0 + self.slide * self.TILE + 3, y0, self.TILE - 6, self.TILE)
        p.setPen(Qt.NoPen)
        p.setBrush(t.sel)
        p.drawRoundedRect(hl, 12, 12)
        dpr = self.devicePixelRatioF()
        for j, w in enumerate(self.windows[self.first:self.first + self.VISIBLE]):
            i = self.first + j
            size = self.ICON + (6 if i == self.sel else 0)
            r = QRect(round(x0 + j * self.TILE + (self.TILE - size) / 2), round(y0 + (self.TILE - size) / 2), size, size)
            pm = exe_icon(w.exe, size, dpr)
            if i != self.sel:
                p.setOpacity(0.78)
            if pm is not None:
                p.drawPixmap(r, pm)
            else:
                paint_glyph(p, SimpleNamespace(icon='window', win=w), r, t)
            p.setOpacity(1)
        if self.first > 0 or self.first + self.VISIBLE < len(self.windows):  # more beyond the edges
            p.setPen(t.sub)
            p.setFont(font(16))
            if self.first > 0:
                p.drawText(QRect(m + 2, y0, self.PAD, self.TILE), Qt.AlignCenter, '‹')
            if self.first + self.VISIBLE < len(self.windows):
                p.drawText(QRect(panel.right() - self.PAD - 1, y0, self.PAD, self.TILE), Qt.AlignCenter, '›')
        w = self.selected()
        if w is not None:
            tr = QRect(x0, y0 + self.TILE + 8, panel.width() - 2 * self.PAD, 22)
            p.setFont(font(14, QFont.Weight.DemiBold))
            p.setPen(t.text)
            p.drawText(tr, Qt.AlignHCenter | Qt.AlignVCenter, QFontMetrics(p.font()).elidedText(w.title, Qt.ElideRight, tr.width()))
            p.setFont(font(12))
            p.setPen(t.sub)
            note = f'{w.app} · {self.sel + 1} of {len(self.windows)}' + (' · current' if self.sel == 0 else '')
            p.drawText(tr.translated(0, 22), Qt.AlignHCenter | Qt.AlignVCenter, note)


# =========================================================================== answers and tasks
def wrap_height(text, fnt, width, max_lines=None):
    fm = QFontMetrics(fnt)
    h = fm.boundingRect(QRect(0, 0, int(width), 100000), Qt.TextWordWrap, text).height() if text else 0
    return min(h, fm.lineSpacing() * max_lines) if max_lines else h


def draw_wrapped(p, rect, text, max_lines=None):
    """Word-wrapped text; if it doesn't fit in max_lines the last visible line ends with '…'."""
    fm = QFontMetrics(p.font())
    if not max_lines or wrap_height(text, p.font(), rect.width()) <= fm.lineSpacing() * max_lines:
        p.drawText(rect, Qt.TextWordWrap, text)
        return
    words, lines, cur = text.split(), [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if fm.horizontalAdvance(trial) <= rect.width() or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines:
        lines.append(cur)
    lines = lines[:max_lines]
    lines[-1] = fm.elidedText(lines[-1] + ' …', Qt.ElideRight, rect.width())
    for i, ln in enumerate(lines):
        p.drawText(QRect(rect.x(), rect.y() + i * fm.lineSpacing(), rect.width(), fm.lineSpacing()),
                   Qt.AlignLeft | Qt.AlignVCenter, ln)


class Answer(QWidget):
    """The companion's answer to a question about the screen.  It sits beside the companion, or
    beside the thing the companion is pointing at, and the text streams in.  Click-through and
    never focused; the app decides when it goes away."""
    W, M, PAD, MAX_LINES = 400, 16, 16, 18

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.theme = theme
        self.question, self.text, self.status, self.foot = '', '', '', ''
        self.done, self.failed = False, False
        self.anchor, self.target = QPointF(), None
        self.t0 = time.time()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)

    def start(self, question, anchor, status='Looking at the screen'):
        self.question, self.text, self.status, self.foot = question, '', status, ''
        self.done = self.failed = False
        self.anchor, self.target, self.t0 = QPointF(anchor), None, time.time()
        self.setWindowOpacity(1.0)
        self.relayout()
        self.show()
        self.timer.start(150)

    def set_status(self, s):
        self.status = s
        self.relayout()

    def set_text(self, t):
        if t != self.text:
            self.text = t
            self.relayout()

    def finish(self, text, target=None, foot='Tap Caps Lock to ask a follow-up'):
        self.text, self.target, self.done, self.foot = text, target, True, foot
        self.timer.stop()
        self.relayout()

    def fail(self, text):
        self.failed = True
        self.finish(text, None, '')

    def _body(self):
        return self.text if self.text else self.status + '...'

    def _heights(self):
        inner = self.W - 2 * self.PAD
        body = wrap_height(self._body(), font(14), inner, self.MAX_LINES)
        return 18, body, (20 if self.foot else 0)

    def relayout(self):
        q, body, foot = self._heights()
        h = self.PAD + q + 6 + body + (10 + foot if foot else 0) + self.PAD
        if self.size() != QSize(self.W + 2 * self.M, h + 2 * self.M):
            self.resize(self.W + 2 * self.M, h + 2 * self.M)
        self.place()
        self.update()

    def place(self):
        w, h = self.width(), self.height()
        if self.target is not None:
            t = QRectF(self.target)
            g = screen_rect_at(t.center())
            x, y = t.right() + 30 - self.M, t.center().y() - h / 2
            if x + w > g.right():
                x = t.left() - 30 - w + self.M
            if x < g.left():
                x, y = t.center().x() - w / 2, t.bottom() + 30 - self.M
                if y + h > g.bottom():
                    y = t.top() - 30 - h + self.M
        else:
            c = self.anchor
            g = screen_rect_at(c)
            x, y = c.x() + 24 - self.M, c.y() - self.M - 28
            if x + w > g.right():
                x = c.x() - 24 - w + self.M
        x = max(g.left() - self.M, min(x, g.right() - w + self.M))
        y = max(g.top() - self.M + 4, min(y, g.bottom() - h + self.M))
        self.move(round(x), round(y))

    def perch(self):
        """Where the companion waits when it isn't pointing: on the card's edge nearest the anchor."""
        g = self.geometry()
        right = self.anchor.x() <= g.center().x()
        y = g.y() + self.M + 28
        return QPointF(g.x() + self.M - 13, y) if right else QPointF(g.right() - self.M + 14, y)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self.theme
        rect = QRect(self.M, self.M, self.W, self.height() - 2 * self.M)
        draw_shadow_panel(p, rect, 14, t, 14)
        q, body, foot = self._heights()
        x, y, inner = rect.x() + self.PAD, rect.y() + self.PAD, self.W - 2 * self.PAD
        p.setFont(font(12))
        p.setPen(t.sub)
        p.drawText(QRect(x, y, inner, q), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(self.question, Qt.ElideRight, inner))
        y += q + 6
        p.setFont(font(14))
        if self.text:
            p.setPen(t.warn if self.failed else t.text)
            draw_wrapped(p, QRect(x, y, inner, body), self.text, self.MAX_LINES)
        else:
            dots = '.' * (1 + int((time.time() - self.t0) * 2.5) % 3)
            p.setPen(t.sub)
            p.drawText(QRect(x, y, inner, body), Qt.AlignLeft | Qt.AlignTop, self.status + dots)
        if self.foot:
            p.setFont(font(12))
            p.setPen(t.sub)
            p.drawText(QRect(x, y + body + 10, inner, foot), Qt.AlignLeft | Qt.AlignVCenter, self.foot)


class TaskCard(QWidget):
    """Agent mode: the task, the model's plan, each step as it happens (✓, ✗, spinner), and a plain
    question when a step needs your OK, with the exact command or text.  Sits at the bottom right
    of the screen; its buttons can be clicked, it never takes keyboard focus."""
    choice = Signal(str)   # 'allow' | 'all' | 'stop' | 'close'
    W, M, PAD, SHOW_STEPS = 410, 18, 16, 7
    TITLES = {'planning': 'PLANNING', 'running': 'WORKING ON IT', 'asking': 'NEEDS YOUR OK', 'stopping': 'STOPPING',
              'done': 'DONE', 'stopped': 'STOPPED', 'failed': "COULDN'T FINISH"}

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.area = QRect(0, 0, 1280, 720)
        self.reset('')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)

    def reset(self, goal):
        self.goal, self.plan, self.final = goal, '', ''
        self.steps = []           # [text, state, detail]; state: run ok fail ask denied
        self.asking = None        # (step index, why, detail)
        self.status = 'planning'
        self.buttons, self.hover = [], None

    # -------------------------------------------------------------- content
    def start(self, goal, area):
        self.reset(goal)
        self.area = QRect(area)
        self.setWindowOpacity(1.0)
        self.relayout()
        self.show()
        self.timer.start(60)

    def set_plan(self, text):
        self.plan = text.strip()
        self.relayout()

    def add_step(self, text):
        self.steps.append([text, 'run', ''])
        if self.status == 'planning':
            self.status = 'running'
        self.relayout()
        return len(self.steps) - 1

    def set_step(self, i, state, detail=''):
        if 0 <= i < len(self.steps):
            self.steps[i][1], self.steps[i][2] = state, detail
            self.relayout()

    def ask(self, i, why, detail):
        self.asking, self.status = (i, why, detail), 'asking'
        self.relayout()

    def clear_ask(self):
        self.asking = None
        if self.status == 'asking':
            self.status = 'running'
        self.relayout()

    def set_status(self, status):
        self.status = status
        self.relayout()

    def finish(self, kind, message):
        self.status, self.final, self.asking = kind, message, None
        for s in self.steps:
            if s[1] in ('run', 'ask'):
                s[1] = 'denied' if kind == 'stopped' else 'fail'
        self.timer.stop()
        self.relayout()

    @property
    def finished(self):
        return self.status in ('done', 'stopped', 'failed')

    # -------------------------------------------------------------- layout
    def _rows(self):
        """[(kind, payload, height)] top to bottom; painting and sizing share it."""
        inner = self.W - 2 * self.PAD
        rows = [('head', None, 18), ('goal', self.goal, wrap_height(self.goal, font(15, QFont.Weight.DemiBold),
                                                                    inner, 3))]
        if self.plan:
            rows.append(('plan', self.plan, wrap_height(self.plan, font(12), inner - 4, 6)))
        steps = list(enumerate(self.steps))
        if len(steps) > self.SHOW_STEPS:
            rows.append(('more', f'{len(steps) - self.SHOW_STEPS} earlier steps', 18))
            steps = steps[-self.SHOW_STEPS:]
        for i, (text, state, detail) in steps:
            h = wrap_height(text, font(13), inner - 26, 2)
            if detail and state == 'fail':
                h += 2 + wrap_height(detail, font(12), inner - 26, 2)
            rows.append(('step', i, max(20, h)))
        if self.asking is not None:
            i, why, detail = self.asking
            h = 26 + wrap_height(self.steps[i][0], font(13, QFont.Weight.DemiBold), inner - 24, 3)
            if detail:
                h += 10 + wrap_height(detail, mono(12), inner - 44, 8) + 12
            rows.append(('ask', self.asking, h + 12 + 34 + 12))
        if self.final:
            rows.append(('final', self.final, wrap_height(self.final, font(14), inner, 6)))
        if self.asking is None:
            rows.append(('foot', None, 30))
        return rows

    def relayout(self):
        h = self.PAD * 2 + sum(r[2] + 8 for r in self._rows()) - 8
        size = QSize(self.W + 2 * self.M, h + 2 * self.M)
        if self.size() != size:
            self.resize(size)
        a = self.area
        self.move(a.right() - self.W - 14 - self.M, a.bottom() - h - 14 - self.M)
        self.update()

    def perch(self):
        g = self.geometry()
        return QPointF(g.x() + self.M - 14, g.y() + self.M + 26)

    # -------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self.theme
        panel = QRect(self.M, self.M, self.W, self.height() - 2 * self.M)
        draw_shadow_panel(p, panel, 16, t, 16)
        x, y, inner = panel.x() + self.PAD, panel.y() + self.PAD, self.W - 2 * self.PAD
        self.buttons = []
        now = time.time()
        for kind, data, h in self._rows():
            if kind == 'head':
                p.setFont(font(11, QFont.Weight.Bold))
                p.setPen(t.warn if self.status in ('asking', 'failed') else t.accent)
                p.drawText(QRect(x, y, inner, h), Qt.AlignLeft | Qt.AlignVCenter, self.TITLES.get(self.status, ''))
                p.setFont(font(11))
                p.setPen(t.sub)
                hint = 'tap Caps Lock to stop' if not self.finished and self.asking is None else ''
                p.drawText(QRect(x, y, inner, h), Qt.AlignRight | Qt.AlignVCenter, hint)
            elif kind == 'goal':
                p.setFont(font(15, QFont.Weight.DemiBold))
                p.setPen(t.text)
                draw_wrapped(p, QRect(x, y, inner, h), data, 3)
            elif kind == 'plan':
                p.setPen(QPen(t.line, 2))
                p.drawLine(QPointF(x + 1, y + 2), QPointF(x + 1, y + h - 2))
                p.setFont(font(12))
                p.setPen(t.sub)
                draw_wrapped(p, QRect(x + 10, y, inner - 10, h), data, 6)
            elif kind == 'more':
                p.setFont(font(12))
                p.setPen(t.sub)
                p.drawText(QRect(x + 26, y, inner - 26, h), Qt.AlignLeft | Qt.AlignVCenter, data)
            elif kind == 'step':
                text, state, detail = self.steps[data]
                self._step_icon(p, QPointF(x + 9, y + 10), state, now)
                p.setFont(font(13))
                p.setPen(t.sub if state in ('denied',) else t.text)
                th = wrap_height(text, font(13), inner - 26, 2)
                draw_wrapped(p, QRect(x + 26, y, inner - 26, th), text, 2)
                if detail and state == 'fail':
                    p.setFont(font(12))
                    p.setPen(t.warn)
                    draw_wrapped(p, QRect(x + 26, y + th + 2, inner - 26, h - th - 2), detail, 2)
            elif kind == 'ask':
                self._paint_ask(p, QRect(x - 6, y, inner + 12, h), data)
            elif kind == 'final':
                p.setFont(font(14))
                p.setPen(t.warn if self.status == 'failed' else t.text)
                draw_wrapped(p, QRect(x, y, inner, h), data, 6)
            elif kind == 'foot':
                if self.finished:
                    self._button(p, QRect(x + inner - 76, y + 2, 76, 28), 'Close', 'close', False)
                    p.setFont(font(12))
                    p.setPen(t.sub)
                    p.drawText(QRect(x, y, inner - 84, h), Qt.AlignLeft | Qt.AlignVCenter, 'Every step is in data/agent-log.md')
                else:
                    self._button(p, QRect(x + inner - 76, y + 2, 76, 28), 'Stop', 'stop', False)
            y += h + 8

    def _step_icon(self, p, c, state, now):
        t = self.theme
        if state == 'run':
            p.setPen(QPen(t.accent, 2.0, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(c.x() - 6, c.y() - 6, 12, 12), int(-(now * 360 * 1.5) % 360 * 16), 110 * 16)
        elif state == 'ok':
            p.setPen(QPen(t.accent, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([c + QPointF(-5, 0), c + QPointF(-1.5, 3.5), c + QPointF(5, -4)])
        elif state == 'fail':
            p.setPen(QPen(t.warn, 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(c + QPointF(-4, -4), c + QPointF(4, 4))
            p.drawLine(c + QPointF(4, -4), c + QPointF(-4, 4))
        elif state == 'ask':
            p.setPen(Qt.NoPen)
            p.setBrush(t.warn)
            p.drawEllipse(c, 6.5, 6.5)
            p.setPen(QPen(QColor('white'), 1.8, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(c + QPointF(0, -3), c + QPointF(0, 0.8))
            p.drawPoint(c + QPointF(0, 3.2))
        else:  # denied
            p.setPen(QPen(t.sub, 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(c + QPointF(-4, 0), c + QPointF(4, 0))

    def _paint_ask(self, p, box, data):
        t = self.theme
        i, why, detail = data
        warn_bg = QColor(t.warn)
        warn_bg.setAlpha(26)
        p.setPen(QPen(QColor(t.warn.red(), t.warn.green(), t.warn.blue(), 150), 1.2))
        p.setBrush(warn_bg)
        p.drawRoundedRect(QRectF(box), 11, 11)
        x, y, inner = box.x() + 12, box.y() + 10, box.width() - 24
        p.setFont(font(12, QFont.Weight.DemiBold))
        p.setPen(t.warn)
        p.drawText(QRect(x, y, inner, 16), Qt.AlignLeft | Qt.AlignVCenter, why)
        y += 22
        text = self.steps[i][0]
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.setPen(t.text)
        th = wrap_height(text, p.font(), inner, 3)
        draw_wrapped(p, QRect(x, y, inner, th), text, 3)
        y += th
        if detail:
            y += 10
            dh = wrap_height(detail, mono(12), inner - 20, 8)
            p.setPen(Qt.NoPen)
            p.setBrush(t.chip)
            p.drawRoundedRect(QRectF(x, y, inner, dh + 12), 7, 7)
            p.setFont(mono(12))
            p.setPen(t.text)
            draw_wrapped(p, QRect(x + 10, y + 6, inner - 20, dh), detail, 8)
            y += dh + 12
        y += 12
        bx = x
        for label, key, primary in (('Allow  ↵', 'allow', True), ('Allow all  A', 'all', False),
                                    ('Stop  Esc', 'stop', False)):
            w = QFontMetrics(font(13, QFont.Weight.DemiBold)).horizontalAdvance(label) + 28
            self._button(p, QRect(bx, y, w, 34), label, key, primary)
            bx += w + 8

    def _button(self, p, r, label, key, primary):
        t = self.theme
        self.buttons.append((r, key))
        col = QColor(t.accent) if primary else QColor(t.chip)
        if self.hover == key:
            col = col.lighter(112) if not t.light or primary else col.darker(106)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(r), 9, 9)
        p.setPen(QColor('white') if primary else t.text)
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.drawText(r, Qt.AlignCenter, label)

    # -------------------------------------------------------------- mouse
    def mouseMoveEvent(self, e):
        h = next((k for r, k in self.buttons if r.contains(e.position().toPoint())), None)
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, _):
        if self.hover is not None:
            self.hover = None
            self.update()

    def mousePressEvent(self, e):
        for r, k in self.buttons:
            if r.contains(e.position().toPoint()):
                self.choice.emit(k)
                return


# =========================================================================== class schedules
COURSE_HUES = [258, 205, 152, 28, 330, 46, 185, 290, 0, 100]


class ScheduleCard(QWidget):
    """A week view of one schedule, with the others a key away: ← → flip through them (best
    first), Enter saves, Esc closes.  Its buttons can be clicked; it never takes focus."""
    choice = Signal(str)   # 'prev' | 'next' | 'save' | 'close'
    W, H, M, PAD = 1000, 720, 20, 22

    def __init__(self, theme):
        super().__init__(None, OVERLAY_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.result, self.index, self.saved = None, 0, ''
        self.buttons, self.hover = [], None
        self.resize(self.W + 2 * self.M, self.H + 2 * self.M)

    def show_result(self, result, area):
        self.result, self.index, self.saved = result, 0, ''
        g = QRect(area)
        self.move(g.x() + (g.width() - self.width()) // 2, g.y() + max(0, (g.height() - self.height()) // 2))
        self.show()
        self.update()

    def step(self, d):
        if self.result and self.result.options:
            self.index = (self.index + d) % len(self.result.options)
            self.saved = ''
            self.update()

    @property
    def current(self):
        return self.result.options[self.index] if self.result and self.result.options else None

    def colors(self):
        """course -> (fill, text) so the same course keeps its colour across options."""
        out = {}
        courses = []
        for c in self.result.options[0].picks if self.result.options else []:
            if c[1].course not in courses:
                courses.append(c[1].course)
        for o in self.result.options:
            for _, s in o.picks:
                if s.course not in courses:
                    courses.append(s.course)
        for i, c in enumerate(courses):
            h = COURSE_HUES[i % len(COURSE_HUES)]
            fill = QColor.fromHsl(h, 150, 200) if self.theme.light else QColor.fromHsl(h, 120, 92)
            text = QColor.fromHsl(h, 120, 45) if self.theme.light else QColor.fromHsl(h, 150, 225)
            out[c] = (fill, text)
        return out

    # -------------------------------------------------------------- paint
    def paintEvent(self, _):
        import schedule as sched
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, m, pad = self.theme, self.M, self.PAD
        panel = QRect(m, m, self.W, self.H)
        draw_shadow_panel(p, panel, 18, t, 18)
        self.buttons = []
        res, cur = self.result, self.current
        if res is None:
            return
        x0, y = panel.x() + pad, panel.y() + pad
        inner = self.W - 2 * pad
        # header
        p.setFont(font(11, QFont.Weight.Bold))
        p.setPen(t.accent)
        p.drawText(QRect(x0, y, inner, 16), Qt.AlignLeft | Qt.AlignVCenter, (res.term or 'NEXT TERM').upper())
        p.setFont(font(20, QFont.Weight.DemiBold))
        p.setPen(t.text)
        n = len(res.options)
        title = f'Option {self.index + 1} of {n}' if n else 'No schedule fits'
        p.drawText(QRect(x0, y + 18, inner, 30), Qt.AlignLeft | Qt.AlignVCenter, title)
        if cur is not None:
            meets = [mt for _, s in cur.picks for mt in s.meetings]
            days = sorted({d for mt in meets for d in mt.days}, key=sched.DAYS.index)
            credits = sum(it.credits for it in res.items) or sum(s.credits for _, s in cur.picks)
            span = (f'{sched.fmt_time(min(mt.start for mt in meets))}–{sched.fmt_time(max(mt.end for mt in meets))}'
                    if meets else '')
            facts = [f'{credits:g} credits' if credits else '', f'{len(days)} days on campus' if days else '', span]
            p.setFont(font(13))
            p.setPen(t.sub)
            p.drawText(QRect(x0 + 260, y + 18, inner - 260, 30), Qt.AlignRight | Qt.AlignVCenter,
                       '  ·  '.join(f for f in facts if f))
            if cur.notes:
                p.setPen(t.warn)
                p.drawText(QRect(x0, y + 50, inner, 18), Qt.AlignLeft | Qt.AlignVCenter, 'Not quite: ' + '; '.join(cur.notes))
            else:
                p.setPen(t.accent)
                p.drawText(QRect(x0, y + 50, inner, 18), Qt.AlignLeft | Qt.AlignVCenter, 'Fits everything you asked for')
        y += 78
        # week grid
        foot_h = 104
        grid = QRect(x0, y, inner, panel.bottom() - foot_h - y)
        if cur is not None:
            self._grid(p, grid, cur, sched)
        else:
            p.setFont(font(14))
            p.setPen(t.warn)
            p.drawText(grid, Qt.AlignCenter | Qt.TextWordWrap, res.error or 'Nothing to show.')
        # below the grid: what has no time, what's missing, saved
        y = grid.bottom() + 10
        p.setFont(font(12))
        lines = []
        if cur is not None:
            untimed = [s.label() for _, s in cur.picks if not s.meetings]
            if untimed:
                lines.append((t.sub, 'No set meeting time: ' + ', '.join(untimed)))
        if res.missing:
            lines.append((t.warn, 'Not scheduled (no sections on screen): ' + ', '.join(res.missing)
                          + ". Say which courses fill a slot, e.g. 'schedule, tech elective MA 410 or MA 441'."))
        if self.saved:
            lines.append((t.accent, self.saved))
        for col, text in lines[:3]:
            p.setPen(col)
            p.drawText(QRect(x0, y, inner, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(text, Qt.ElideRight, inner))
            y += 20
        # buttons
        by = panel.bottom() - pad - 36
        bx = x0
        for label, key, primary in (('◀', 'prev', False), ('▶', 'next', False)):
            self._button(p, QRect(bx, by, 44, 36), label, key, primary)
            bx += 52
        p.setFont(font(12))
        p.setPen(t.sub)
        p.drawText(QRect(bx + 6, by, 360, 36), Qt.AlignLeft | Qt.AlignVCenter, '← → other options   ↵ save   esc close')
        rx = panel.right() - pad
        for label, key, primary in (('Close', 'close', False), ('Save + copy', 'save', True)):
            w = QFontMetrics(font(13, QFont.Weight.DemiBold)).horizontalAdvance(label) + 32
            rx -= w
            self._button(p, QRect(rx, by, w, 36), label, key, primary)
            rx -= 8

    def _grid(self, p, r, cur, sched):
        t = self.theme
        meets = [(s, mt) for _, s in cur.picks for mt in s.meetings]
        days = [d for d in 'MTWRF']
        if any(d in mt.days for _, mt in meets for d in 'SU'):
            days += [d for d in 'SU' if any(d in mt.days for _, mt in meets)]
        lo = min([mt.start for _, mt in meets] + [9 * 60]) // 60 * 60
        hi = -(-max([mt.end for _, mt in meets] + [17 * 60]) // 60) * 60
        axis = 54
        col_w = (r.width() - axis) / len(days)
        head = 26
        ph = (r.height() - head) / max(1, (hi - lo))
        p.setFont(font(12, QFont.Weight.DemiBold))
        for i, d in enumerate(days):
            p.setPen(t.sub)
            p.drawText(QRectF(r.x() + axis + i * col_w, r.y(), col_w, head), Qt.AlignCenter, sched.DAY_NAMES[d])
        p.setFont(font(11))
        for hmin in range(lo, hi + 1, 60):
            yy = r.y() + head + (hmin - lo) * ph
            p.setPen(QPen(t.line, 1))
            p.drawLine(QPointF(r.x() + axis, yy), QPointF(r.right(), yy))
            p.setPen(t.sub)
            p.drawText(QRectF(r.x(), yy - 8, axis - 8, 16), Qt.AlignRight | Qt.AlignVCenter, sched.fmt_time(hmin).replace(':00', ''))
        colors = self.colors()
        for i, d in enumerate(days):
            here = [(s, mt) for s, mt in meets if d in mt.days]
            for s, mt in here:
                # half-semester sections that share a slot sit side by side
                twins = [(s2, m2) for s2, m2 in here if m2.start < mt.end and mt.start < m2.end]
                k, n = twins.index((s, mt)), len(twins)
                bx = r.x() + axis + i * col_w + 3 + k * (col_w - 6) / n
                bw = (col_w - 6) / n - (2 if n > 1 else 0)
                top = r.y() + head + (mt.start - lo) * ph + 1
                bh = (mt.end - mt.start) * ph - 2
                fill, text = colors.get(s.course, (t.chip, t.text))
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(QRectF(bx, top, bw, bh), 6, 6)
                p.setBrush(text)
                p.drawRoundedRect(QRectF(bx, top, 3, bh), 1.5, 1.5)
                p.setPen(text)
                p.setFont(font(12, QFont.Weight.DemiBold))
                p.drawText(QRectF(bx + 8, top + 3, bw - 10, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(p.font()).elidedText(s.label(), Qt.ElideRight, int(bw - 12)))
                if bh > 34:
                    p.setFont(font(11))
                    half = ''
                    if mt.start_date and mt.end_date and n > 1:
                        half = ' · ' + ('1st half' if k == 0 else '2nd half')
                    sub = f'{s.kind} · {sched.fmt_time(mt.start)}–{sched.fmt_time(mt.end)}{half}'
                    p.drawText(QRectF(bx + 8, top + 19, bw - 10, 15), Qt.AlignLeft | Qt.AlignVCenter,
                               QFontMetrics(p.font()).elidedText(sub, Qt.ElideRight, int(bw - 12)))
                if bh > 52 and s.title:
                    p.drawText(QRectF(bx + 8, top + 35, bw - 10, 15), Qt.AlignLeft | Qt.AlignVCenter,
                               QFontMetrics(p.font()).elidedText(s.title, Qt.ElideRight, int(bw - 12)))

    def _button(self, p, r, label, key, primary):
        t = self.theme
        self.buttons.append((r, key))
        col = QColor(t.accent) if primary else QColor(t.chip)
        if self.hover == key:
            col = col.lighter(112) if not t.light or primary else col.darker(106)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(r), 9, 9)
        p.setPen(QColor('white') if primary else t.text)
        p.setFont(font(13, QFont.Weight.DemiBold))
        p.drawText(r, Qt.AlignCenter, label)

    def mouseMoveEvent(self, e):
        h = next((k for r, k in self.buttons if r.contains(e.position().toPoint())), None)
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        for r, k in self.buttons:
            if r.contains(e.position().toPoint()):
                self.choice.emit(k)
                return


def mono(px):
    f = QFont('Cascadia Mono')
    if not f.exactMatch():
        f = QFont('Consolas')
    f.setPixelSize(px)
    return f


def buddy_icon(theme, size=64):
    """The companion drawn as an app/tray icon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 24
    g = QRadialGradient(QPointF(9 * s, 8 * s), 22 * s)
    g.setColorAt(0, theme.buddy_a.lighter(112))
    g.setColorAt(1, theme.buddy_b)
    p.setBrush(g)
    p.setPen(QPen(QColor('white'), 1.4 * s))
    p.drawEllipse(QPointF(12 * s, 12 * s), 10.5 * s, 10.5 * s)
    p.setPen(Qt.NoPen)
    p.setBrush(theme.eye)
    for ex in (8.6, 15.4):
        p.drawRoundedRect(QRectF(ex * s - 1.2 * s, 9.2 * s, 2.4 * s, 4.6 * s), 1.2 * s, 1.2 * s)
    p.end()
    return QIcon(pm)
