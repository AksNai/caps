"""Caps: Caps Lock becomes a command key.

    python main.py            (or start_caps.bat, which runs it without a console window)
    python main.py --verbose  print what happens, including math-autocorrect corrections

Tap Caps Lock and the companion pops up at your pointer, listening: type part of a window's or
app's name to switch to it or open it, a command such as `focus 50 cs` or `jot bench 135 3x5`, a
question about what's on screen (`why is this red?`), or a task for the agent (`open spotify and
play lofi`).  Hold Caps Lock and move the mouse for gestures (flick a window to a half or corner,
circle for focus, scribble to undo); hold it still for a ring menu of them; drag a window to move
it, right-drag to resize it; scroll to flip through your windows.  Shift+Caps Lock is the normal
Caps Lock.  Math autocorrect (../math-autocorrect) runs in the same keyboard hook when installed.
"""
import argparse
import ctypes
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')

import requests  # noqa: E402
from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QCursor  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon  # noqa: E402

import agent as agent_mod  # noqa: E402
import board_api  # noqa: E402
import sandbox  # noqa: E402
import apps  # noqa: E402
import commands  # noqa: E402
import drawing  # noqa: E402
import gestures  # noqa: E402
import screen  # noqa: E402
import search  # noqa: E402
import winutil  # noqa: E402
from brain import Brain, BrainError  # noqa: E402
from commands import Suggestion  # noqa: E402
from focus import DEFAULTS as FOCUS_DEFAULTS, Focus  # noqa: E402
from keys import TAP_MAX, CapsKeys  # noqa: E402
from board_ui import BoardPanel, ScreenMarks  # noqa: E402
from ui import (Answer, Bubble, Buddy, Carousel, GestureLayer, Ghost, Pill, Prompt, Shield, TaskCard, Theme,  # noqa: E402
                buddy_icon, native_rect_to_logical, screen_rect_at)

MUTEX = r'Local\CapsMathAutocorrectKeyboardHook'  # shared with math-autocorrect/daemon.py
DEFAULT_CONFIG = {
    'buddy': {'enabled': True},
    'focus': FOCUS_DEFAULTS,
    'autocorrect': {'enabled': True, 'path': '../math-autocorrect', 'model': 'model', 'device': 'cpu'},
    'jot': {'url': 'http://127.0.0.1:8765', 'log_focus': False, 'path': '../jot'},
    'brain': {'url': 'http://127.0.0.1:11434', 'model': 'qwen3.8:27b', 'keep_alive': '15m', 'think': False},
    'caps_light': True,   # the Caps Lock light is on while your keys go to Caps
    'agent': {'permission': 'normal'},   # careful | normal | trusted: what the agent asks you before doing
}
MOVE_PX = 7             # logical px the pointer may wander before a hold counts as a gesture
RING_DELAY = TAP_MAX    # hold still this long and the ring menu opens (exactly when it stops being a tap)
LISTEN_IDLE = 10.0      # the companion stops listening after this long with nothing typed...
PROMPT_IDLE = 60.0      # ...and the prompt closes after this long without a key
FOLLOWUP = 180.0        # a question within this long of the last answer continues the conversation
TASK_LINGER = 15.0      # a finished task card stays this long (longer while the pointer is on it)
REPLAY_KEYS = {'enter': winutil.VK_RETURN, 'esc': winutil.VK_ESCAPE, 'back': winutil.VK_BACK,
               'up': winutil.VK_UP, 'down': winutil.VK_DOWN, 'tab': winutil.VK_TAB, 'left': winutil.VK_LEFT,
               'right': winutil.VK_RIGHT}
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x201, 0x202, 0x204, 0x205
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
ASK_SYSTEM = """You are Caps, a small companion on the user's Windows desktop that answers questions about their screen and teaches them how to do things. The user pointed at something and asked. You get a screenshot of the window (when available), what the mouse pointer is over, and a numbered list of the window's elements.

Answer in one to three short sentences of plain text. Be specific to what is on screen. To point at one thing, put [[N]] right after it, using that element's number from the list. Never mention the numbers or the list. If the screen doesn't show the answer, say so briefly and answer from what you know.

When a drawing explains it better, add one ```board code block of Python that draws it:
- How something works, or a graph, chart or diagram on screen: redraw it, building it up with step(caption) so each part has a one-sentence explanation. Use the labels and numbers you can see on screen.
- Anything 3D (a shape, surface, molecule or mechanism): draw a rough 3D version.
- How to do something in this program: teach one step at a time. mark() the element to use now, with a short note, and end your reply with [[NEXT]] if more steps follow. You will see the screen again after the user does it. When the task is complete, say so without [[NEXT]].
Keep the code short. Plain Python, no imports needed for drawing; math is available.

The drawing API:
""" + board_api.__doc__.split('\n\n', 1)[1]
log = logging.getLogger('caps')


def merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def save_setting(keys, value):
    """Change one setting in data/config.json, keeping everything else you wrote there."""
    path = os.path.join(DATA, 'config.json')
    try:
        user = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        user = {}
    node = user
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(user, f, indent=2)
    os.replace(tmp, path)


def load_config():
    path = os.path.join(DATA, 'config.json')
    user = {}
    if os.path.exists(path):
        try:
            user = json.load(open(path, encoding='utf-8'))
        except ValueError as e:
            log.error('config.json is not valid JSON (%s); using defaults', e)
    else:
        os.makedirs(DATA, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return merge(DEFAULT_CONFIG, user)


def clip(rect, bounds):
    return (max(rect[0], bounds[0]), max(rect[1], bounds[1]), min(rect[2], bounds[2]), min(rect[3], bounds[3]))


class Hub(QObject):
    """Carries events from the hook and worker threads to the UI thread."""
    event = Signal(str, object)


class Hold:
    """One press of Caps Lock, from key-down to key-up, as the UI thread sees it."""

    def __init__(self, pointer, target):
        self.t0 = time.monotonic()
        self.start = QPointF(pointer)
        self.points = [(pointer.x(), pointer.y())]
        self.target = target          # the window under the pointer when Caps Lock went down
        self.moving = False           # drew a stroke
        self.ring = False             # held still: the ring menu is open
        self.hover = None             # ring slot under the pointer
        self.shown = None             # the gesture the stroke currently looks like
        self.carousel = False         # scrolled: the window carousel is open
        self.pick = None              # the carousel's window when it closed
        self.typed = False            # typed a letter while holding: that went to the prompt
        self.cancelled = False        # Esc
        self.dragged = False          # clicked: dragging or resizing a window
        self.wheel = gestures.Wheel()


class Drag:
    """Caps Lock + drag (move) or right-drag (resize), in physical pixels."""

    def __init__(self, hwnd, mode, start, wrect, edges, before):
        self.hwnd, self.mode, self.start, self.wrect, self.edges, self.before = hwnd, mode, start, wrect, edges, before
        self.last, self.snap, self.work = None, None, None


class TaskBridge:
    """The agent thread's view of the task card: events go to the UI thread, approvals block."""

    def __init__(self, app, token, stop):
        self.app, self.token, self.stop = app, token, stop
        self.n, self.last_plan = 0, 0.0

    def _emit(self, *a):
        self.app.hub.event.emit('agent', (self.token,) + a)

    def plan(self, text, final=False):
        now = time.time()
        if final or now - self.last_plan > 0.05:
            self.last_plan = now
            self._emit('plan', text)

    def status(self, text):
        self._emit('plan', text)

    def step(self, text):
        i, self.n = self.n, self.n + 1
        self._emit('step', i, text)
        return i

    def update(self, i, state, detail=''):
        self._emit('update', i, state, detail)

    def approve(self, i, text, why, detail):
        box, ev = [], threading.Event()
        self._emit('ask', i, why, detail, box, ev)
        while not ev.wait(0.2):
            if self.stop.is_set():
                return 'stop'
        return box[0] if box else 'stop'

    def point(self, rect):
        self._emit('point', rect)

    def finish(self, kind, message):
        self._emit('finish', kind, message)


class App:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config()
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.theme = Theme(winutil.apps_use_light_theme())
        self.ghost = Ghost(self.theme)        # created first so the other overlays stack above it
        self.layer = GestureLayer(self.theme)
        self.carousel = Carousel(self.theme)
        self.answer = Answer(self.theme)
        self.taskcard = TaskCard(self.theme)
        self.taskcard.choice.connect(self.on_task_choice)
        self.board = BoardPanel(self.theme)
        self.board.closed.connect(self._buddy_home)
        self.marks = ScreenMarks(self.theme)
        self.teach = None                     # teaching a task step by step: {'q', 'round', 'targets', 'pending'}
        self.prompt = Prompt(self.theme)
        self.prompt.query_changed.connect(self.refresh)
        self.prompt.activated.connect(self.run)
        self.buddy = Buddy(self.theme)
        self.buddy.set_enabled(self.cfg['buddy']['enabled'])
        self.bubble = Bubble(self.theme, self.buddy)
        self.pill = Pill(self.theme)
        self.shield = Shield(self.theme)
        self.shield.chosen.connect(self.on_shield)
        self.focus = Focus(self.cfg['focus'], os.path.join(DATA, 'state.json'), os.path.join(DATA, 'focus.jsonl'))
        self.brain = Brain(self.cfg['brain'])
        self.hub = Hub()
        self.hub.event.connect(self.on_event)
        self.keys = CapsKeys(lambda name, arg: self.hub.event.emit(name, arg), on_expire=self._light_failsafe)
        self.light_owned = False              # we turned the Caps Lock light on (so we turn it off)
        self.user_caps = False                # what you last set Caps Lock to (Shift+Caps Lock)
        self.caps_pass_down, self.light_t, self.light_bad = False, 0.0, 0
        self.windows = []
        self.apps = apps.load(os.path.join(DATA, 'apps.json'))
        self.apps_at = 0.0
        self.hold = None
        self.listening, self.listen_fg, self.listen_t, self.last_key = False, 0, 0.0, 0.0
        self.button_down = False
        self.ctx = None                       # what the pointer was on when you tapped Caps Lock
        self.undo = []                        # (hwnd, WINDOWPLACEMENT) before each gesture moved a window
        self.mouse_listener = None
        self.mouse_state = {'drag': None}     # hook thread: the button-up that ends the current drag
        self.drag = None
        self.ask_token, self.ask_stop, self.convo, self.answer_until, self.answer_btn = 0, threading.Event(), None, 0, False
        self.agent_token, self.agent_stop, self.agent_thread, self.approval = 0, threading.Event(), None, None
        self.approve_btn, self.task_close_at, self.last_warm = False, 0.0, 0.0
        self.log_lock = threading.Lock()
        self.last_good, self.last_nudge = None, {}
        # foreground is a full-screen game/video (kept up to date by the watcher)
        self.fullscreen = winutil.is_fullscreen(winutil.user32.GetForegroundWindow())
        self.engine = None
        self.autocorrect_ready = False
        self._tray()
        self.watch_timer = self._timer(self.watch, 400)
        self.hold_timer = self._timer(self.track)
        self.listen_timer = self._timer(self.heartbeat)
        self.drag_timer = self._timer(self.drag_tick)
        self.answer_timer = self._timer(self.answer_tick)
        self.approve_timer = self._timer(self.approve_tick)
        self.task_timer = self._timer(self.task_tick)
        if winutil.caps_lock_on() and not self.fullscreen:
            winutil.tap(winutil.VK_CAPITAL)  # start with Caps Lock off: the key is ours now (never in a game)
            self.light_t = time.time()
        self._start_hook()
        self.refresh_apps()

    @staticmethod
    def _timer(fn, ms=None):
        t = QTimer()
        t.timeout.connect(fn)
        if ms:
            t.start(ms)
        return t

    # ================================================================== keyboard hook (hook thread)
    def route(self, msg, data):
        down = msg in (0x100, 0x104)
        vk = data.vkCode
        if vk == winutil.VK_CAPITAL and self.keys.down_at is None and \
                winutil.is_fullscreen(winutil.user32.GetForegroundWindow()):
            return None  # full-screen game or video: Caps Lock is just Caps Lock there
        p = winutil.pressed
        r = self.keys.feed(vk, down, time.monotonic(), p(winutil.VK_SHIFT), p(winutil.VK_CONTROL),
                           p(winutil.VK_MENU) or p(winutil.VK_LWIN) or p(winutil.VK_RWIN))
        if vk == winutil.VK_CAPITAL and r is None:
            self._caps_passed(down)
        if r:
            return r
        if self.fullscreen:
            return 'skip'  # games: the autocorrect neither tracks nor types anything
        if down and winutil.foreground_is_own():
            return 'skip'  # typing into one of our own windows: not the autocorrect's business
        return None

    def _start_hook(self):
        ac = self.cfg['autocorrect']
        path = os.path.normpath(os.path.join(ROOT, ac['path']))
        daemon = None
        if ac['enabled'] and not self.args.no_autocorrect and os.path.exists(os.path.join(path, 'daemon.py')):
            sys.path.append(path)
            try:
                import daemon
            except Exception:
                log.exception('could not import math autocorrect from %s', path)
                daemon = None
        if daemon is None:
            threading.Thread(target=self._standalone_hook, daemon=True).start()
            return
        from common import KEEP
        self.engine = daemon.Engine(lambda *a: KEEP, lambda app, mode, text: text, injector=None)
        self.engine.verbose = self.args.verbose
        threading.Thread(target=daemon.run_windows, args=(self.engine,), daemon=True,
                         kwargs={'quiet': True, 'route': self.route, 'on_log': self._ac_log}).start()
        threading.Thread(target=self._load_model, args=(path, ac), daemon=True).start()

    def _load_model(self, path, ac):
        try:
            from infer import Corrector
            c = Corrector(os.path.join(path, ac['model']), device=ac['device'])
            c.inline('Discord', 'warm up ', 'lamda')
            self.engine.correct, self.engine.select_fn = c.inline, c.select
            self.autocorrect_ready = True
            log.info('math autocorrect ready (%.0fM parameters on %s)', c.n_params / 1e6, c.device)
            if self.args.verbose:
                print(f'math autocorrect ready on {c.device}', flush=True)
        except Exception:
            log.exception('math autocorrect failed to load; Caps keeps running without it')

    def _ac_log(self, entry):
        if entry and entry[0] == 'error':
            log.error('autocorrect: %s', entry[1])  # exception text only, never typed text
        if self.args.verbose:
            print(*entry, flush=True)

    def _standalone_hook(self):
        from pynput import keyboard
        listener = None

        def filt(msg, data):
            if data.dwExtraInfo == winutil.MAGIC:
                return False
            if self.route(msg, data) == 'suppress':
                listener.suppress_event()
            return False

        with keyboard.Listener(win32_event_filter=filt) as listener:
            listener.join()

    # ================================================================== mouse (only while Caps Lock is held)
    def _start_mouse_hook(self):
        """A low-level mouse hook exists only while Caps Lock is down (and until a drag that began
        then is let go), so the rest of the time, and in games, where Caps Lock is never ours, mouse
        input doesn't pass through Python at all."""
        if self.mouse_listener is not None:
            return
        from pynput import mouse
        keys, hub, st = self.keys, self.hub, self.mouse_state
        listener = None

        def filt(msg, data):
            if data.flags & 3:
                return False   # injected (the agent clicking): leave it alone
            if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                if keys.down_at is not None and st['drag'] is None:
                    delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
                    keys.moved = True
                    hub.event.emit('wheel', -delta if msg == WM_MOUSEWHEEL else delta)  # down / tilt right = next
                    listener.suppress_event()   # the window under the pointer doesn't scroll
            elif msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
                if keys.down_at is not None and st['drag'] is None:
                    st['drag'] = msg + 1          # the matching button-up ends it
                    keys.moved = True
                    hub.event.emit('drag', ('start', 'move' if msg == WM_LBUTTONDOWN else 'resize',
                                            (data.pt.x, data.pt.y)))
                    listener.suppress_event()   # the click doesn't reach the window
            elif msg in (WM_LBUTTONUP, WM_RBUTTONUP):
                if st['drag'] == msg:
                    st['drag'] = None
                    hub.event.emit('drag', ('end', None, (data.pt.x, data.pt.y)))
                    listener.suppress_event()
            return False

        listener = mouse.Listener(win32_event_filter=filt)
        listener.daemon = True
        listener.start()
        self.mouse_listener = listener

    def _stop_mouse_hook(self):
        if self.mouse_state['drag'] is not None:
            return  # a drag is still going: the hook must see its button-up
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
            self.mouse_listener = None

    # ================================================================== events (UI thread)
    def on_event(self, name, arg):
        try:
            handler = {'caps_down': self.on_caps_down, 'tap': self.on_tap, 'dismiss': self.on_dismiss,
                       'release': self.on_release, 'rollover': self.on_rollover, 'cancel': self.on_cancel,
                       'char': self.on_char, 'key': self.on_key, 'wheel': self.on_wheel, 'drag': self.on_drag,
                       'ask': self.on_ask, 'agent': self.on_agent, 'jot': self.on_jot, 'call': self.on_call,
                       'apps': self.on_apps}.get(name)
            if handler:
                handler(arg)
            if name in ('caps_down', 'tap', 'dismiss', 'release', 'cancel', 'key', 'char', 'agent'):
                self.sync_light()
            if self.args.verbose and name not in ('char', 'rollover', 'call', 'ask', 'agent'):
                print('event', name, arg, flush=True)
        except Exception:
            log.exception('event %s failed', name)

    def on_call(self, arg):
        fn, box, ev = arg
        try:
            box.append(fn())
        except Exception as e:
            box.append(e)
        finally:
            ev.set()

    def call_ui(self, fn, timeout=10):
        """Run fn on the UI thread and return its result (from a worker thread)."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        box, ev = [], threading.Event()
        self.hub.event.emit('call', (fn, box, ev))
        if not ev.wait(timeout):
            raise TimeoutError('the UI did not answer')
        if isinstance(box[0], BaseException):
            raise box[0]
        return box[0]

    # ------------------------------------------------------------------ holding Caps Lock
    def on_caps_down(self, _):
        if self.prompt.isVisible():
            self.prompt.hide_prompt()        # a new press starts something new
        self.listen_timer.stop()
        self.listening = False
        self.buddy.set_listening(False)
        px, py = winutil.cursor_pos()
        target = winutil.window_at(px, py)
        if target is None:
            fg = winutil.foreground_hwnd()
            target = fg if fg and not winutil.foreground_is_own() and winutil.is_switchable(fg) else None
        self.hold = Hold(QCursor.pos(), target)
        self.buddy.follow()
        self.buddy.summon(True)
        self.layer.begin()
        self.hold_timer.start(10)
        self._start_mouse_hook()

    def track(self):
        """Every 10 ms while Caps Lock is held: follow the pointer, open the ring when it rests."""
        h = self.hold
        if h is None or h.typed or h.carousel or h.cancelled or h.dragged:
            return
        pt = QCursor.pos()
        if (pt.x(), pt.y()) != h.points[-1]:
            h.points.append((pt.x(), pt.y()))
        dx, dy = pt.x() - h.start.x(), pt.y() - h.start.y()
        if not h.moving and not h.ring:
            if math.hypot(dx, dy) > MOVE_PX:
                h.moving = self.keys.moved = True
            elif time.monotonic() - h.t0 >= RING_DELAY:
                h.ring = self.keys.moved = True
                self.layer.show_ring(h.start, self.slot_labels(h.target))
            if h.moving or h.ring:
                self.layer.refresh()
                self.buddy.raise_()  # the companion rides on top of the stroke and the ring
        if h.ring:
            d = gestures.ring_slot(dx, dy, self.layer.DEAD)
            if d != h.hover:
                h.hover = d
                self.preview(('flick', d) if d is not None else None, h.target)
            self.layer.set_hover(d, pt)
            self.layer.refresh()
        elif h.moving:
            g = gestures.classify(h.points)
            if g != h.shown:
                h.shown = g
                self.preview(g, h.target)
            self.layer.set_points(h.points)
            self.layer.refresh()

    def slot_labels(self, hwnd):
        out = dict(gestures.SLOTS)
        if hwnd and winutil.is_maximized(hwnd):
            out[2] = ('restore', 'Restore')
        return out

    def preview(self, g, hwnd):
        """Say what letting go will do, and show where the window will land."""
        if g is None:
            self.layer.set_label('')
            self.ghost.hide()
            return
        kind, arg = g
        if kind == 'flick':
            key, label = self.slot_labels(hwnd)[arg]
            app = winutil.win_info(hwnd).app if hwnd else ''
            self.layer.set_label(f'{gestures.ARROWS[arg]}  {label}', app or 'no window here')
            rect = gestures.placement(winutil.work_area(hwnd), key) if hwnd else None
            self.ghost.show_rect(native_rect_to_logical(*rect) if rect else None)
            if rect:
                self.layer.raise_()
                self.buddy.raise_()
            return
        self.ghost.hide()
        if kind == 'circle':
            st = self.focus.status()
            self.layer.set_label('○  Focus', f"{st['remaining_min']} min left" if st else
                                 f"{self.cfg['focus']['default_minutes']} min")
        elif kind == 'scribble':
            self.layer.set_label('↺  Undo', 'last window move' if self.undo else 'nothing to undo')

    def on_wheel(self, delta):
        h = self.hold
        if h is None or h.typed or h.cancelled or h.dragged:
            return
        steps = h.wheel.add(delta)
        if steps == 0:
            return
        if not h.carousel:
            wins = winutil.list_windows(exclude_pid=os.getpid())
            if not wins:
                return
            h.carousel = True
            self.layer.end()
            self.ghost.hide()
            self.carousel.open(wins, 0, h.start)
        self.carousel.step(steps)
        self.buddy.park_at(self.carousel.perch())
        self.buddy.raise_()

    def _end_hold(self):
        h, self.hold = self.hold, None
        self.hold_timer.stop()
        self._stop_mouse_hook()
        if h is not None:
            if h.carousel:
                h.pick = self.carousel.selected()
                self.carousel.dismiss()
            if self.drag is None:
                self.ghost.hide()
            self.layer.end(fade=h.moving and not h.ring and not h.dragged)
        return h

    def agent_running(self):
        return self.agent_thread is not None and self.agent_thread.is_alive()

    def on_tap(self, _):
        h = self._end_hold()
        if h is not None and (h.moving or h.carousel or h.ring or h.dragged):
            self.keys.stop_capture()  # the hook decided before it heard about the movement
            self.finish(h)
            return
        if self.agent_running():
            self.keys.stop_capture()
            self.stop_agent()          # tapping Caps Lock is the agent's stop button
            self._buddy_home()
            return
        if self.teach is not None:
            self.keys.stop_capture()
            self.teach_next()          # while teaching, a tap means "done, show me the next step"
            return
        self.listen()

    def on_release(self, info):
        h = self._end_hold()
        if h is None:
            return
        if h.cancelled:
            self.stop_listening()
        elif h.typed or info.get('used'):
            if not self.prompt.isVisible():
                self.stop_listening()
        else:
            self.finish(h)

    def on_cancel(self, _):
        if self.teach is not None:
            self.end_teach()           # Caps Lock + Esc stops a lesson
        if self.hold is not None:
            self.hold.cancelled = True
            self.layer.end()
            self.ghost.hide()
            if self.hold.carousel:
                self.carousel.dismiss()
                self.hold.carousel = False

    def finish(self, h):
        """Caps Lock came up after a gesture, a ring choice, a scroll, a drag or a long still hold."""
        try:
            self._finish(h)
        finally:
            self.stop_listening()  # even if the action failed, the companion must not stay stuck on screen

    def _finish(self, h):
        self.buddy.follow()
        if h.dragged:
            return  # the drag ends with its mouse button, not with Caps Lock
        if h.carousel:
            w = h.pick
            if w is not None and w.hwnd != winutil.foreground_hwnd() and not winutil.activate(w.hwnd):
                self.reply('Could not switch to that window', 'It may be running as administrator.')
        elif h.ring:
            if h.hover is not None:
                self.do_slot(h.hover, h.target)
        elif h.moving:
            g = gestures.classify(h.points)
            if g is None:
                self.reply("That wasn't a gesture", 'Hold Caps Lock still to see them all.', 2.5)
            elif g[0] == 'flick':
                self.do_slot(g[1], h.target)
            elif g[0] == 'circle':
                self.circle()
            elif g[0] == 'scribble':
                self.undo_move()

    # ------------------------------------------------------------------ gesture actions
    def do_slot(self, d, hwnd):
        if not hwnd or not winutil.exists(hwnd):
            self.reply('No window there', 'Point at a window, then hold Caps Lock.', 2.5)
            return
        key = self.slot_labels(hwnd)[d][0]
        before = winutil.get_placement(hwnd)
        if key == 'min':
            winutil.minimize(hwnd)
            ok = True
        elif key in ('max', 'restore'):
            ok = winutil.maximize(hwnd, key == 'max')
        else:
            ok = winutil.move_frame(hwnd, gestures.placement(winutil.work_area(hwnd), key))
        if not ok:
            self.reply("Couldn't move that window", 'It may be running as administrator.')
            return
        self.remember(hwnd, before)
        if key != 'min':
            winutil.activate(hwnd)

    def remember(self, hwnd, placement):
        if placement is not None:
            self.undo = (self.undo + [(hwnd, placement)])[-20:]

    def undo_move(self):
        while self.undo:
            hwnd, wp = self.undo.pop()
            if winutil.exists(hwnd):
                winutil.set_placement(hwnd, wp)
                if wp.showCmd != winutil.SW_MINIMIZE and wp.showCmd != 2:
                    winutil.activate(hwnd)
                self.reply('Undone', winutil.win_info(hwnd).app, 1.6)
                return
        self.reply('Nothing to undo', 'Scribble undoes the last window a gesture or drag moved.', 2.2)

    def circle(self):
        st = self.focus.status()
        if st:
            self.reply(f"Focusing{' on ' + st['label'] if st['label'] else ''}",
                       f"{st['remaining_min']} min left. Tap Caps, type 'focus off' to end.")
        else:
            self.start_focus(self.cfg['focus']['default_minutes'], '')
            self.buddy.linger(5)

    # ------------------------------------------------------------------ Caps Lock + drag
    def on_drag(self, arg):
        phase, mode, pt = arg
        if phase == 'start':
            self.drag_start(mode, pt)
        else:
            self.drag_end()

    def drag_start(self, mode, pt):
        h = self.hold
        if h is not None:
            h.dragged = True
            self.layer.end()
            self.ghost.hide()
            if h.carousel:
                self.carousel.dismiss()
                h.carousel = False
        hwnd = winutil.window_at(*pt)
        if hwnd is None:
            self.reply('No window there', 'Hold Caps Lock and drag a window to move it, right-drag to resize.', 2.5)
            return
        before = winutil.get_placement(hwnd)
        if winutil.is_maximized(hwnd):
            frame = winutil.frame_rect(hwnd)
            winutil.restore(hwnd)
            if mode == 'move':  # come out of maximized under the pointer, like a title bar drag
                l, t, r, b = winutil.window_rect(hwnd)
                winutil.set_window_rect(hwnd, *gestures.unmaximized_rect(frame, (r - l, b - t), pt))
        edges = gestures.resize_edges(winutil.frame_rect(hwnd), pt) if mode == 'resize' else None
        if winutil.foreground_hwnd() != hwnd:
            winutil.activate(hwnd)
        self.drag = Drag(hwnd, mode, pt, winutil.window_rect(hwnd), edges, before)
        self.drag_timer.start(8)

    def drag_tick(self):
        d = self.drag
        if d is None or not winutil.exists(d.hwnd):
            self.drag = None
            self.drag_timer.stop()
            self.ghost.hide()
            return
        x, y = winutil.cursor_pos()
        rect = gestures.drag_rect(d.wrect, d.edges, x - d.start[0], y - d.start[1])
        if rect != d.last:
            d.last = rect
            winutil.set_window_rect(d.hwnd, *rect, size=d.edges is not None)
        if d.edges is None:  # moving: against a screen edge it will snap there
            mon, work = winutil.monitor_at(x, y)
            z = gestures.snap_zone((x, y), mon)
            if z != d.snap:
                d.snap, d.work = z, work
                self.ghost.show_rect(native_rect_to_logical(*gestures.placement(work, z)) if z else None)
                if z:
                    self.buddy.raise_()

    def drag_end(self):
        d, self.drag = self.drag, None
        self.drag_timer.stop()
        self.ghost.hide()
        if self.hold is None:
            self._stop_mouse_hook()
        if d is None or not winutil.exists(d.hwnd):
            return
        if d.snap == 'max':
            winutil.maximize(d.hwnd, True)
        elif d.snap:
            winutil.move_frame(d.hwnd, gestures.placement(d.work, d.snap))
        if d.last is not None or d.snap:
            self.remember(d.hwnd, d.before)

    # ------------------------------------------------------------------ listening and the prompt
    def _context(self, foreground=False):
        """What you were pointing at: 'ask' looks at this window, the agent starts from it.  While
        teaching, the window you're working in instead, dialogs included."""
        px, py = winutil.cursor_pos()
        fg = winutil.foreground_hwnd()
        hwnd = None if foreground else winutil.window_at(px, py)
        if hwnd is None and fg and not winutil.foreground_is_own() and (foreground or winutil.is_switchable(fg)):
            hwnd = fg
        return SimpleNamespace(pointer=(px, py), hwnd=hwnd, fg=fg)

    def listen(self):
        """The companion stays out and listens: typing opens the prompt beside it."""
        self.keys.start_capture(time.monotonic())
        self.listening = True
        self.listen_fg = winutil.foreground_hwnd()
        self.listen_t = self.last_key = time.time()
        self.button_down = winutil.mouse_down()
        self.ctx = self._context()
        self.buddy.summon(True)
        self.buddy.set_listening(not self.prompt.isVisible())
        self.listen_timer.start(15)

    def stop_listening(self):
        self.listening = False
        if self.approval is None:
            self.sync_light(self.hold is not None)
            self.keys.stop_capture()
        self.listen_timer.stop()
        self.buddy.set_listening(False)
        if self.prompt.isVisible():
            self.prompt.hide_prompt()
        self._buddy_home()

    def sync_light(self, want=None):
        """The Caps Lock light is on while your keys go to Caps: Caps Lock held, the companion
        listening or the prompt open, or the agent waiting for your OK.  It is the real Caps Lock
        state, but those keys never reach an app (the prompt does its own capitals), and it goes
        off before typing goes back to your apps.  A Caps Lock you turned on yourself (Shift+Caps
        Lock) is left alone.

        Windows applies the toggle we send a few milliseconds later, so right after one the state
        can't be read back: Caps keeps its own account (light_owned, user_caps) and reads the real
        state only once things have settled.  watch() repairs any drift."""
        if want is None:
            want = self.keys.capturing or self.hold is not None
        if not self.cfg.get('caps_light', True) or self.fullscreen:
            want = False
        settled = time.time() - self.light_t > 0.4
        if want and not self.light_owned and not self.user_caps:
            if not (settled and winutil.caps_lock_on()):
                self._toggle_caps()
            self.light_owned = True
        elif not want and self.light_owned:
            self.light_owned = False
            if not settled or winutil.caps_lock_on():
                self._toggle_caps()

    def _toggle_caps(self):
        self.light_t = time.time()
        winutil.tap(winutil.VK_CAPITAL)   # tagged: the hook lets it through untouched

    def _caps_passed(self, down):
        """Hook thread: Shift+Caps Lock went through to Windows, a real toggle."""
        if down and not self.caps_pass_down:
            if self.light_owned:
                self.light_owned = False          # it was our light, and you just turned it off
            else:
                self.user_caps = not self.user_caps
        self.caps_pass_down = down

    def _light_failsafe(self):
        """Hook thread, when capture gives up because the UI stopped answering: the light must not
        stay on, or what you type next would come out in capitals."""
        if self.light_owned:
            self.light_owned = False
            self._toggle_caps()

    def heal_light(self):
        """Every 400 ms: when nothing is using the keys, Caps Lock should be what you last set it to
        with Shift+Caps Lock (off, usually).  Two readings in a row that disagree get fixed."""
        busy = (self.light_owned or self.keys.capturing or self.hold is not None or self.fullscreen
                or time.time() - self.light_t < 1.0 or not self.cfg.get('caps_light', True))
        if busy or winutil.caps_lock_on() == self.user_caps:
            self.light_bad = 0
            return
        self.light_bad += 1
        if self.light_bad >= 2:
            self.light_bad = 0
            self._toggle_caps()
            log.info('Caps Lock light was out of step; turned it %s', 'on' if self.user_caps else 'off')

    def _buddy_home(self):
        """Where the companion goes when nothing is using it: next to a task or an answer, or away."""
        if self.hold is not None or self.listening:
            return
        if self.taskcard.isVisible():
            self.buddy.summon(True)
            if self.buddy.mode in ('follow', 'think'):
                self.buddy.park_at(self.buddy.center())   # it stays where it is working, not at the card
        elif self.board.isVisible():
            self.buddy.summon(True)
            if self.buddy.mode in ('follow', 'think'):
                self.buddy.park_at(self.board.perch())     # it stays beside its drawing
        elif self.answer.isVisible():
            self.buddy.summon(True)
            if self.answer.target is not None:
                self.buddy.point_at(self.answer.target.center())
            elif self.answer.done:
                self.buddy.park_at(self.answer.perch())
            else:
                self.buddy.think(True, self.answer.perch())
        else:
            self.buddy.follow()
            self.buddy.summon(False)

    def heartbeat(self):
        """Every 15 ms while listening: keep the capture alive and notice when you've moved on."""
        if not self.listening:
            self.listen_timer.stop()
            return
        self.keys.ack(time.monotonic())
        if not self.keys.capturing:
            self.stop_listening()  # the hook let a shortcut through, or gave up on us
            return
        down = winutil.mouse_down()
        if down and not self.button_down:
            on_prompt = self.prompt.isVisible() and self.prompt.geometry().contains(QCursor.pos())
            if not on_prompt:
                self.stop_listening()  # clicked somewhere else
                return
        self.button_down = down
        fg = winutil.foreground_hwnd()
        if fg != self.listen_fg and not winutil.foreground_is_own():
            self.stop_listening()
            return
        idle = time.time() - self.last_key
        if idle > (PROMPT_IDLE if self.prompt.isVisible() else LISTEN_IDLE):
            self.stop_listening()

    def open_prompt(self, prefill=''):
        self.windows = winutil.list_windows(exclude_pid=os.getpid())
        if time.time() - self.apps_at > 600:
            self.refresh_apps()
        if not self.listening:
            self.listen()
        self.buddy.summon(True)
        self.buddy.set_listening(False)
        anchor = self.buddy.center() if self.buddy.isVisible() else QPointF(QCursor.pos()) + Buddy.OFFSET
        self.prompt.open_at(anchor)
        self.buddy.park_at(self.prompt.perch())
        self.buddy.raise_()
        self.refresh('')
        if prefill:
            self.prompt.type_char(prefill)

    def on_rollover(self, ch):
        if self.hold is not None:
            self.hold.typed = True
            self.layer.end()
            self.ghost.hide()
        if self.prompt.isVisible():
            self.prompt.type_char(ch)
        else:
            self.open_prompt(ch)

    def on_char(self, ch):
        self.keys.ack(time.monotonic())
        self.last_key = time.time()
        if self.approval is not None and not self.listening:
            choice = {'a': 'all', 'y': 'allow', 'n': 'stop'}.get(ch.lower())
            if choice:
                self.decide(choice)
            return
        if self.prompt.isVisible():
            self.prompt.type_char(ch)
        elif self.listening:
            self.open_prompt(ch)
        else:
            winutil.type_text(ch)  # arrived just after we stopped listening: hand it back

    def on_key(self, name):
        self.keys.ack(time.monotonic())
        self.last_key = time.time()
        if self.approval is not None and not self.listening:
            if name in ('enter', 'esc'):
                self.decide('allow' if name == 'enter' else 'stop')
            return
        if self.prompt.isVisible():
            if name == 'esc':
                self.stop_listening()
            else:
                self.prompt.handle_key(name)
        elif self.listening:
            if name in ('up', 'down', 'tab', 'paste'):
                self.open_prompt()
                self.prompt.handle_key(name)
            else:
                if name == 'esc' and self.answer.isVisible():
                    self.close_answer()
                self.stop_listening()  # Esc, Enter or Backspace with nothing typed
        elif name in REPLAY_KEYS:
            winutil.tap(REPLAY_KEYS[name])
        elif name == 'paste':
            winutil.chord(winutil.VK_CONTROL, 0x56)
        elif name == 'backword':
            winutil.chord(winutil.VK_CONTROL, winutil.VK_BACK)

    def on_dismiss(self, reason):
        if self.hold is not None and reason == 'caps':
            self._end_hold()
        if reason == 'caps':
            if self.agent_running():
                self.stop_agent()
            elif self.answer.isVisible():
                self.close_answer()
        self.stop_listening()

    def state(self):
        return {'focus': self.focus.status(),
                'autocorrect': self.engine.enabled if self.engine is not None else None,
                'buddy': self.buddy.enabled}

    def refresh(self, query):
        wins = search.rank_windows(query, self.windows)
        win_items = []
        for i, w in enumerate(wins):
            sub = w.app
            if not query.strip() and i == 0:
                sub += ' · current window'
            win_items.append(Suggestion(w.title, sub, ('switch', w.hwnd), 'window', win=w))
        q = query.strip()
        if not q:
            self.prompt.set_items(win_items, 1 if len(win_items) > 1 else 0)  # Enter = previous window, like Alt+Tab
            return
        self.prompt.set_items(self.rank(q, win_items), 0)

    def rank(self, q, win_items):
        """Order the prompt's rows for typed text q: windows, apps, commands, Ask and Do."""
        cmds = commands.suggest(q, self.state())
        kind = commands.intent(q)
        followup = bool(self.convo) and time.time() - self.convo['t'] < FOLLOWUP
        ask, do = (commands.ai_suggestions(q, followup) + [None, None])[:2]
        if kind and time.time() - self.last_warm > 30:
            self.last_warm = time.time()
            self.brain.warm()  # loading takes ~30 s the first time: start while you finish typing
        running = [w.app for w in self.windows if w.app]
        app_items = [Suggestion(a.name, 'Open app', ('open_app', a), 'app', icon_path=a.icon)
                     for a in apps.rank(q, self.apps) if not any(apps.good_match(r, a.name) for r in running)]
        low = q.lower()
        if low[:1] in '?!>' or low.startswith(('ask ', 'do ')):
            items = [ask, do] if kind == 'ask' else [do, ask]
        elif low.startswith('jot ') and cmds:
            items = cmds
        elif kind == 'ask':
            items = [ask] + cmds + win_items + app_items + [do]
        elif kind == 'do':
            verb, _, rest = q.partition(' ')
            rest = rest[3:] if rest.lower().startswith('to ') else rest
            direct = []
            if verb.lower() in ('open', 'launch', 'start', 'switch', 'go', 'bring', 'show', 'pull') and rest:
                wm = search.rank_windows(rest, self.windows)[:1]
                am = apps.rank(rest, self.apps, 1)
                if wm:
                    direct = [Suggestion(wm[0].title, wm[0].app, ('switch', wm[0].hwnd), 'window', win=wm[0])]
                elif am:
                    direct = [Suggestion(am[0].name, 'Open app', ('open_app', am[0]), 'app', icon_path=am[0].icon)]
            items = direct + [do] + cmds + ([] if direct else win_items + app_items) + [ask]
        else:
            base = (cmds + win_items) if cmds and (len(q) >= 3 or not win_items) else (win_items + cmds)
            items = base + app_items + ([ask, do] if len(q.split()) >= 2 else [])
        return [i for i in items if i is not None]

    # ================================================================== actions
    def run(self, s):
        self.stop_listening()
        a = s.action
        kind = a[0] if a else 'noop'
        if kind == 'switch':
            if not winutil.activate(a[1]):
                self.reply('Could not switch to that window', 'It may be running as administrator.')
        elif kind == 'open_app':
            try:
                apps.launch(a[1])
            except OSError as e:
                self.reply(f"Couldn't open {a[1].name}", str(e))
        elif kind == 'ask':
            self.ask(a[1])
        elif kind == 'agent':
            self.start_agent(a[1])
        elif kind == 'jot':
            self.jot(a[1])
        elif kind == 'focus_start':
            self.start_focus(a[1], a[2])
            self.buddy.linger(5)
        elif kind == 'focus_stop':
            self.end_focus(completed=False)
            self.buddy.linger(6)
        elif kind == 'focus_extend':
            self.focus.extend(a[1])
            self.reply(f'Added {a[1]} min', f"{self.focus.status()['remaining_min']} min left")
        elif kind == 'allow':
            self.focus.allow(a[1])
            self.shield.uncover()
            self.reply(f'Break: {a[1]} min', 'Blocking resumes after that. The focus timer keeps running.')
        elif kind == 'autocorrect' and self.engine is not None:
            self.engine.enabled = a[1]
            self.reply(f"Math autocorrect {'on' if a[1] else 'off'}")
            self._sync_tray()
        elif kind == 'buddy':
            self.buddy.set_enabled(a[1])
            self._sync_tray()
        elif kind == 'quit':
            self.quit()

    def reply(self, title, body='', seconds=4.0):
        """Answer a command: the companion stays next to its message until it fades."""
        self.bubble.say(title, body, seconds)
        self.buddy.linger(seconds)

    def refresh_apps(self):
        self.apps_at = time.time()

        def scan():
            try:
                found = apps.scan()
                if found:
                    apps.save(os.path.join(DATA, 'apps.json'), found)
                    self.hub.event.emit('apps', found)
            except Exception:
                log.exception('could not list installed apps')

        threading.Thread(target=scan, daemon=True).start()

    def on_apps(self, found):
        self.apps = found

    # ------------------------------------------------------------------ screenshots without our own overlays
    def _overlay_ids(self):
        ws = (self.buddy, self.bubble, self.answer, self.taskcard, self.board, self.marks, self.pill, self.ghost,
              self.layer, self.prompt,
              self.carousel, self.shield)
        return [int(w.winId()) for w in ws if w.isVisible()]

    def grab_hidden(self, rect):
        """Screenshot (physical rect) with the companion, cards and bubbles left out: they are
        excluded from capture for the moment it takes, so nothing on screen flickers."""
        ids = self.call_ui(self._overlay_ids)
        self.call_ui(lambda: [winutil.exclude_from_capture(h, True) for h in ids])
        time.sleep(0.08)  # one or two frames for the compositor
        try:
            return screen.grab(rect)
        finally:
            self.call_ui(lambda: [winutil.exclude_from_capture(h, False) for h in ids])

    # ------------------------------------------------------------------ ask about the screen
    def ask(self, question, lesson=False):
        """lesson: the next step of a lesson in progress (the screen is read again first)."""
        ctx = self._context(foreground=True) if lesson else (self.ctx or self._context())
        if not lesson:
            self.end_teach(close=False)
        self.marks.clear()
        convo = self.convo['history'] if self.convo and time.time() - self.convo['t'] < FOLLOWUP else []
        self.ask_stop.set()
        self.ask_stop = threading.Event()
        self.ask_token += 1
        anchor = self.buddy.center() if self.buddy.isVisible() else QPointF(QCursor.pos()) + Buddy.OFFSET
        self.answer.start(question, anchor)
        self.answer.raise_()
        self.buddy.summon(True)
        self.buddy.think(True, self.answer.perch())
        self.buddy.raise_()
        self.answer_timer.start(50)
        threading.Thread(target=self._ask_worker, args=(self.ask_token, question, ctx, convo, self.ask_stop, lesson),
                         daemon=True).start()

    def _ask_worker(self, token, q, ctx, convo, stop, lesson=False):
        def emit(*a):
            self.hub.event.emit('ask', (token,) + a)

        try:
            caps = self.brain.capabilities()
            if not caps:
                raise BrainError(f"Can't reach Ollama at {self.brain.url}. Is it running?")
            if not self.brain.loaded():
                emit('status', 'Waking up the model (the first question takes about 30 s)')
            px, py = ctx.pointer
            mon, _ = winutil.monitor_at(px, py)
            hwnd = ctx.hwnd if ctx.hwnd and winutil.exists(ctx.hwnd) else None
            frame = clip(winutil.frame_rect(hwnd), mon) if hwnd else mon
            els, under, seer = [], '', None
            try:
                seer = screen.Seer()
                if hwnd:
                    els = screen.select(seer.elements(hwnd), frame, (px, py), 200)
                under = seer.under(px, py)
            except Exception:
                log.exception('UI Automation failed')
            finally:
                if seer is not None:
                    seer.close()
            images, ring = [], False
            if 'vision' in caps:
                inside = frame[0] <= px < frame[2] and frame[1] <= py < frame[3]
                images = [screen.encode(self.grab_hidden(frame), (px - frame[0], py - frame[1]) if inside else None)]
                ring = inside
            w = winutil.win_info(hwnd) if hwnd else None
            parts = [f'Window: "{w.title}" ({w.app})' if w else 'The desktop (no particular window).',
                     'The mouse pointer is over: ' + (under or 'nothing in particular')
                     + (' (a red ring marks it in the screenshot)' if ring else '')]
            if els:
                parts.append('Elements in this window:\n' + screen.format_elements(els, frame))
            parts.append(f'I did that step. Show me the next one. (The task: {q})' if lesson else f'Question: {q}')
            user = {'role': 'user', 'content': '\n\n'.join(parts)}
            if images:
                user['images'] = images
            emit('status', 'Thinking')
            last = [0.0]

            def on_text(t):
                now = time.time()
                if now - last[0] > 0.04:
                    last[0] = now
                    emit('text', screen.visible_text(t))

            msgs = [{'role': 'system', 'content': ASK_SYSTEM}] + convo + [user]
            reply = self.brain.chat(msgs, on_text=on_text, stop=stop)
            if stop.is_set():
                return
            raw = reply.get('content', '')
            said, code, more = screen.split_board(raw)
            text, el = screen.parse_answer(said, els, frame)
            board = None
            if code:
                emit('status', 'Drawing it')
                r = sandbox.run(code)
                if not r.get('ok') and not stop.is_set():   # one chance to fix its own drawing code
                    fix = self.brain.chat(msgs + [{'role': 'assistant', 'content': raw}, {
                        'role': 'user', 'content': f"That board code failed: {r.get('error')}. Reply with only the "
                                                   'corrected ```board block.'}], stop=stop)
                    code2 = screen.split_board(fix.get('content', ''))[1]
                    r = sandbox.run(code2) if code2 else r
                if r.get('ok') and any(st['items'] for st in (r.get('board') or {}).get('steps', [])):
                    board = r['board']
            rects = {e.n: e.rect for e in els}
            emit('done', text or ("Here's a drawing." if board else "I don't have an answer for that."),
                 el.rect if el else None, q, board, rects, more)
        except BrainError as e:
            emit('error', str(e))
        except Exception as e:
            log.exception('ask failed')
            emit('error', f'Something went wrong: {e}')

    def on_ask(self, arg):
        token, kind, *rest = arg
        if token != self.ask_token or not self.answer.isVisible():
            return
        if kind == 'status':
            self.answer.set_status(rest[0])
        elif kind == 'text':
            self.answer.set_text(rest[0])
            if not self.listening and self.hold is None:
                self.buddy.think(True, self.answer.perch())
        elif kind == 'done':
            text, rect, q, board, rects, more = rest
            target = QRectF(native_rect_to_logical(*rect)) if rect else None
            self.convo = {'t': time.time(), 'history': ((self.convo or {}).get('history', [])
                                                        + [{'role': 'user', 'content': q},
                                                           {'role': 'assistant', 'content': text}])[-8:]}
            diagram, on_screen = drawing.kinds(board) if board else (False, False)
            marks = self.screen_marks(board, rects) if on_screen else []
            if marks:
                self.marks.show_marks(marks)
                target = target or marks[0]['rect']
            if more or (self.teach is not None and marks):
                rnd = (self.teach or {}).get('round', 0) + 1
                self.teach = {'q': q, 'round': rnd, 'targets': [m['rect'] for m in marks], 'pending': False,
                              'btn': winutil.mouse_down(), 't': time.time()}
                foot = 'Do it, or tap Caps Lock for the next step. Caps Lock + Esc stops.' if more else ''
                if not more:
                    self.teach = None   # the last step: nothing more to wait for
            else:
                foot = 'Tap Caps Lock to ask a follow-up'
                self.teach = None
            if diagram:
                pt = QCursor.pos()
                avoid = QRectF(pt.x() - 260, pt.y() - 200, 520, 400)   # what you were pointing at stays visible
                self.show_board(dict(board, intro=text, title=board.get('title') or q[:1].upper() + q[1:]),
                                QPointF(pt), avoid)
                self.answer.hide()
            else:
                self.answer.finish(text, target, foot)
            self.answer_until = time.time() + (600 if self.teach else min(60.0, 8.0 + 0.06 * len(text)))
            self.answer_btn = winutil.mouse_down()
            if not self.listening and self.hold is None:
                self.buddy.think(False)
                self._buddy_home()
            self.answer.raise_()
            self.marks.raise_()
            self.buddy.raise_()
        elif kind == 'error':
            self.answer.fail(rest[0])
            self.answer_until = time.time() + 8.0
            self.answer_btn = winutil.mouse_down()
            if not self.listening and self.hold is None:
                self.buddy.think(False)
                self._buddy_home()

    def answer_tick(self):
        """Every 50 ms while an answer shows: it goes when you click, or after a while unread.  In a
        lesson, clicking the marked element is how you say you did the step."""
        if not self.answer.isVisible():
            self.answer_timer.stop()
            return
        if not self.answer.done:
            return
        now = time.time()
        if self.teach is not None:
            down = winutil.mouse_down()
            pos = QPointF(QCursor.pos())
            if self.teach['btn'] and not down and not self.teach['pending'] and \
                    any(r.adjusted(-8, -8, 8, 8).contains(pos) for r in self.teach['targets']):
                self.teach['pending'] = True
                QTimer.singleShot(1200, self.teach_next)    # let the program react before looking again
            self.teach['btn'] = down
            if now - self.teach['t'] > 600:
                self.end_teach()
            return
        if self.answer.geometry().contains(QCursor.pos()):
            self.answer_until = max(self.answer_until, now + 2.0)  # you're reading it
        down = winutil.mouse_down()
        if down and not self.answer_btn and not self.listening:
            self.close_answer()
            return
        self.answer_btn = down
        if now > self.answer_until and not self.listening and self.hold is None:
            self.close_answer()

    def close_answer(self):
        self.ask_stop.set()
        self.answer_timer.stop()
        self.teach = None
        self.marks.clear()
        if self.answer.isVisible():
            self.answer.hide()
        if self.buddy.mode in ('point', 'think'):
            self.buddy.follow()
        self._buddy_home()

    # ------------------------------------------------------------------ drawings and lessons
    def screen_marks(self, board, rects):
        """Board marks on element numbers -> rectangles on the screen (logical), latest step first."""
        out = []
        for st in board['steps']:
            for it in st['items']:
                if it['t'] in ('mark', 'box') and it.get('el') in rects:
                    out.append({'rect': QRectF(native_rect_to_logical(*rects[it['el']])),
                                'kind': it['t'], 'num': it.get('num', ''), 'note': it.get('note', '')})
        return out

    def show_board(self, board, anchor=None, avoid=None):
        """A drawing from an answer, or from the agent's show tool."""
        anchor = anchor or QPointF(QCursor.pos())
        self.board.show_board(board, anchor, avoid)
        self.board.raise_()
        if self.hold is None and not self.listening:
            self.buddy.summon(True)
            self.buddy.park_at(self.board.perch())
            self.buddy.raise_()
        return True

    def teach_next(self):
        """You did the step (clicked it, or tapped Caps Lock): look at the screen again, show the next."""
        if self.teach is None:
            return
        if self.teach['round'] >= 15:
            self.end_teach()
            return
        self.marks.clear()
        self.ask(self.teach['q'], lesson=True)

    def end_teach(self, close=True):
        if self.teach is None:
            return
        self.teach = None
        self.marks.clear()
        if close:
            self.close_answer()

    # ------------------------------------------------------------------ agent mode
    def start_agent(self, goal):
        if self.agent_running():
            self.reply('Already working on a task', 'Tap Caps Lock to stop it first.')
            return
        self.close_answer()
        self.agent_token += 1
        self.agent_stop = stop = threading.Event()
        self.taskcard.start(goal, screen_rect_at(QCursor.pos()))
        self.task_timer.stop()
        self.buddy.summon(True)
        self.buddy.park_at(self.buddy.center())   # it waits where you asked, then goes where each step happens
        self.buddy.raise_()
        level = self.cfg['agent'].get('permission', 'normal')
        ui = TaskBridge(self, self.agent_token, stop)
        hands = agent_mod.Hands(self.apps, os.getpid(), ui_call=self.call_ui, point=ui.point,
                                grab_hidden=self.grab_hidden, jot=self.jot_for_agent,
                                focus=lambda m, label: self.start_focus(m, label), show=self.show_board)

        def go():
            try:
                caps = self.brain.capabilities()
                if not caps:
                    ui.finish('failed', f"Can't reach Ollama at {self.brain.url}. Is it running?")
                    return
                if 'tools' not in caps:
                    ui.finish('failed', f"{self.brain.model} can't use tools. Set brain.model in data/config.json "
                                        'to one that can.')
                    return
                hands.vision = 'vision' in caps
                if not self.brain.loaded():
                    ui.status('Waking up the model (about 30 s the first time)...')
                agent_mod.Agent(self.brain, hands, ui, log=self.agent_log, stop=stop, level=level).run(goal)
            except Exception as e:
                log.exception('agent failed')
                ui.finish('failed', f'Something went wrong: {e}')

        self.agent_thread = threading.Thread(target=go, daemon=True)
        self.agent_thread.start()

    def agent_log(self, line):
        with self.log_lock:
            with open(os.path.join(DATA, 'agent-log.md'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def jot_for_agent(self, text):
        self.jot(text, quiet=True)
        return True, 'Sent to jot; it files the note itself.'

    def on_agent(self, arg):
        token, kind, *rest = arg
        if token != self.agent_token:
            if kind == 'ask':
                rest[3].append('stop')
                rest[4].set()
            return
        card = self.taskcard
        if kind == 'plan':
            card.set_plan(rest[0])
        elif kind == 'step':
            card.add_step(rest[1])
        elif kind == 'update':
            card.set_step(*rest)
            if rest[1] in ('ok', 'fail') and self.buddy.mode == 'point' and self.hold is None \
                    and not self.listening:
                self.buddy.park_at(self.buddy.target + Buddy.OFFSET)   # step done: stop pulsing, stay right there
        elif kind == 'ask':
            i, why, detail, box, ev = rest
            self.approval = (box, ev)
            card.ask(i, why, detail)
            card.raise_()
            self.buddy.raise_()   # it is pointing at what it's asking about (sent just before)
            self.keys.start_capture(time.monotonic())   # Enter / A / Esc answer it; it doesn't steal focus
            self.approve_btn = winutil.mouse_down()
            self.approve_timer.start(15)
        elif kind == 'point':
            if self.hold is None and not self.listening:
                self.buddy.point_at(QRectF(native_rect_to_logical(*rest[0])).center())
                self.buddy.raise_()
        elif kind == 'finish':
            kind2, message = rest
            self._end_approval()
            card.finish(kind2, message)
            self.task_close_at = time.time() + TASK_LINGER
            self.task_timer.start(300)
            if self.buddy.mode == 'point' and self.hold is None and not self.listening:
                self.buddy.park_at(self.buddy.target + Buddy.OFFSET)

    def decide(self, choice):
        if self.approval is not None:
            box, ev = self.approval
            box.append(choice)
            ev.set()
        self._end_approval()
        if choice == 'stop':
            self.stop_agent()

    def _end_approval(self):
        self.approval = None
        self.approve_timer.stop()
        if self.taskcard.asking is not None:
            self.taskcard.clear_ask()
        if not self.listening:
            self.sync_light(self.hold is not None)
            self.keys.stop_capture()

    def approve_tick(self):
        if self.approval is None:
            self.approve_timer.stop()
            return
        self.keys.ack(time.monotonic())
        if not self.keys.capturing:
            self.approve_timer.stop()   # a shortcut went through: the card's buttons still work
            self.sync_light()
            return
        down = winutil.mouse_down()
        if down and not self.approve_btn and not self.taskcard.geometry().contains(QCursor.pos()):
            self.sync_light(False)
            self.keys.stop_capture()    # clicked elsewhere: typing goes to your apps again; the card waits
            self.approve_timer.stop()
        self.approve_btn = down

    def stop_agent(self):
        if not self.agent_running():
            return
        self.agent_stop.set()
        if self.approval is not None:
            box, ev = self.approval
            box.append('stop')
            ev.set()
            self._end_approval()
        if not self.taskcard.finished:
            self.taskcard.set_status('stopping')

    def on_task_choice(self, choice):
        if choice in ('allow', 'all') and self.approval is not None:
            self.decide(choice)
        elif choice == 'stop':
            if self.approval is not None:
                self.decide('stop')
            else:
                self.stop_agent()
        elif choice == 'close':
            self.close_task()

    def task_tick(self):
        if not self.taskcard.isVisible() or not self.taskcard.finished:
            self.task_timer.stop()
            return
        if self.taskcard.geometry().contains(QCursor.pos()):
            self.task_close_at = max(self.task_close_at, time.time() + 3)
        elif time.time() > self.task_close_at:
            self.close_task()

    def close_task(self):
        self.task_timer.stop()
        if self.agent_running():
            self.stop_agent()
        self.taskcard.hide()
        if self.buddy.mode in ('park', 'point'):
            self.buddy.follow()
        self._buddy_home()

    # ------------------------------------------------------------------ jot
    def jot(self, text, quiet=False):
        if not quiet:
            self.reply('Jotting it down', text, 2.5)
        threading.Thread(target=self._jot_worker, args=(text, quiet), daemon=True).start()

    def _jot_worker(self, text, quiet):
        def emit(*a):
            self.hub.event.emit('jot', a + (quiet,))

        base = self.cfg['jot']['url'].rstrip('/')
        try:
            nid = self._jot_post(base, text)
        except OSError as e:
            emit('error', str(e))
            return
        t0 = time.time()
        while time.time() - t0 < 120:
            time.sleep(1.0)
            try:
                note = requests.get(f'{base}/api/notes/{nid}', timeout=3).json()
            except (requests.RequestException, ValueError):
                continue
            if note.get('status') == 'done':
                emit('done', note.get('summary') or 'Filed.', note.get('answer') or '')
                return
            if note.get('status') == 'error':
                emit('error', note.get('error') or "jot couldn't file it.")
                return
        emit('slow', '')

    def _jot_post(self, base, text):
        started = False
        for _ in range(30):
            try:
                r = requests.post(base + '/api/notes', json={'text': text}, timeout=4)
                r.raise_for_status()
                return r.json()['id']
            except requests.ConnectionError:
                if not started:
                    started = True
                    if not self._start_jot():
                        raise OSError(f"jot isn't running at {base}.")
                time.sleep(1.0)
            except (requests.RequestException, ValueError, KeyError) as e:
                raise OSError(f'jot answered with an error: {e}')
        raise OSError("jot didn't start in time.")

    def _start_jot(self):
        """jot isn't running: start its server (hidden), as jot's own start.bat would."""
        path = os.path.normpath(os.path.join(ROOT, self.cfg['jot'].get('path', '../jot')))
        if not os.path.exists(os.path.join(path, 'server.py')):
            return False
        exe = sys.executable
        if os.path.basename(exe).lower() == 'pythonw.exe':
            exe = os.path.join(os.path.dirname(exe), 'python.exe')
        subprocess.Popen([exe, 'server.py'], cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        log.info('started jot from %s', path)
        return True

    def on_jot(self, arg):
        """('done', summary, answer, quiet) | ('error', message, quiet) | ('slow', '', quiet)"""
        kind, quiet = arg[0], arg[-1]
        if kind == 'done':
            summary, answer = arg[1], arg[2]
            self.reply('Jotted', summary + (f'\n{answer}' if answer else ''), 4 if quiet else 6)
        elif kind == 'error':
            self.reply("Couldn't jot that", arg[1], 6)
        elif kind == 'slow' and not quiet:
            self.reply('jot is still filing it', 'It will show up in jot when the model is done.', 4)

    # ------------------------------------------------------------------ focus
    def start_focus(self, minutes, label):
        self.focus.start(minutes, label)
        self.last_nudge.clear()
        apps_ = ', '.join(self.cfg['focus']['block_apps'][:2] + self.cfg['focus']['block_sites'][:2])
        self.bubble.say(f"Focus{' on ' + label if label else ''}: {minutes} min",
                        f'{apps_} and other distractions are blocked until then. Tap Caps, type "allow 5" '
                        'for a break.', 5)
        self.watch()

    def end_focus(self, completed):
        summary = self.focus.stop(completed=completed)
        self.pill.hide()
        self.shield.uncover()
        self.buddy.ring = None
        if not summary:
            return
        what = f" on {summary['label']}" if summary['label'] else ''
        n = summary['blocked']
        body = f"{summary['actual_min']} min{what} · {n} distraction{'s' if n != 1 else ''} blocked"
        self.bubble.say('Focus session done' if completed else 'Focus ended early', body, 8)
        if completed:
            QApplication.beep()
        if self.cfg['jot'].get('log_focus'):
            text = f"focused {summary['actual_min']} min" + (f" on {summary['label']}" if summary['label'] else '')
            self.jot(text, quiet=True)

    def on_shield(self, choice):
        hwnd = self.shield.hwnd
        if choice == 'allow':
            self.run(Suggestion('', '', ('allow', 5)))
            return
        if not hwnd or not winutil.exists(hwnd):
            self.shield.uncover()
            return
        winutil.activate(hwnd)
        if choice == 'back':
            winutil.chord(winutil.VK_MENU, winutil.VK_LEFT)   # browser Back
        elif choice == 'close':
            winutil.chord(winutil.VK_CONTROL, 0x57)           # Ctrl+W; Ctrl+Shift+T brings it back

    # ================================================================== watcher (every 400 ms)
    def watch(self):
        try:
            self._watch()
        except Exception:
            log.exception('watch failed')

    def _watch(self):
        fg = winutil.foreground()
        own = fg is not None and fg.pid == os.getpid()
        full = bool(fg) and not own and winutil.is_fullscreen(fg.hwnd)
        if self.fullscreen and not full and winutil.caps_lock_on():
            winutil.tap(winutil.VK_CAPITAL)  # Caps Lock may have been toggled in the game; it is ours again
            self.light_t, self.user_caps, self.light_owned = time.time(), False, False
        if full and not self.fullscreen and self.brain.last_used and time.time() - self.brain.last_used < 3600 \
                and not self.agent_running():
            threading.Thread(target=self.brain.unload, daemon=True).start()  # a game wants the GPU memory
        self.fullscreen = self.buddy.hidden_fullscreen = full
        self.heal_light()
        if not self.focus.active():
            if self.pill.isVisible():
                self.pill.hide()
            if self.shield.isVisible():
                self.shield.uncover()
            return
        now = time.time()
        if self.focus.expired(now):
            self.end_focus(completed=True)
            return
        s = self.focus.session
        left = s.remaining(now)
        frac = left / max(1.0, s.end - s.start)
        mm, ss = divmod(int(left), 60)
        label = f' · {s.label}' if s.label else ''
        brk = ' · break' if now < s.allow_until else ''
        if self.cfg['focus']['show_timer']:
            self.pill.set(f'{mm:02d}:{ss:02d}{label}{brk}', frac)
            if not self.pill.isVisible():
                self.pill.show()
        if own:
            return
        act = self.focus.check(fg, now)
        if act is None:
            if self.shield.isVisible():
                self.shield.uncover()
            self.last_good = fg.hwnd if fg else self.last_good
            return
        kind, name = act
        left_text = f"{mm} min left in your focus session{' on ' + s.label if s.label else ''}."
        if kind == 'minimize':
            winutil.minimize(fg.hwnd)
            self.focus.count_block()
            if now - self.last_nudge.get(name, 0) > 3:
                self.last_nudge[name] = now
                self.bubble.say(f'{name} can wait', left_text + ' Tap Caps, type "allow 5" for a break.', 4)
        else:
            if self.shield.hwnd != fg.hwnd or self.shield.site != name:
                self.focus.count_block()
            self.shield.cover(fg.hwnd, name, left_text)

    # ================================================================== tray
    def _tray(self):
        self.tray = QSystemTrayIcon(buddy_icon(self.theme))
        self.tray.setToolTip('Caps: tap Caps Lock and type, or hold it and move the mouse')
        menu = QMenu()
        a = QAction('Type a command  (tap Caps Lock)', menu)
        a.triggered.connect(lambda: self.open_prompt())
        menu.addAction(a)
        menu.addSeparator()
        self.act_ac = QAction('Math autocorrect', menu, checkable=True)
        self.act_ac.triggered.connect(lambda on: self.run(Suggestion('', '', ('autocorrect', on))))
        menu.addAction(self.act_ac)
        self.act_buddy = QAction('Companion (appears with Caps Lock)', menu, checkable=True)
        self.act_buddy.triggered.connect(lambda on: self.run(Suggestion('', '', ('buddy', on))))
        menu.addAction(self.act_buddy)
        perm = menu.addMenu('Agent asks before…')
        self.act_perm = {}
        for level, label in (('careful', 'Anything that changes something'),
                             ('normal', 'Sending, deleting, buying, closing, changing commands'),
                             ('trusted', 'Only sending, deleting, buying, risky commands')):
            act = QAction(label, perm, checkable=True)
            act.setToolTip(agent_mod.LEVEL_INFO[level])
            act.triggered.connect(lambda _=False, lv=level: self.set_permission(lv))
            perm.addAction(act)
            self.act_perm[level] = act
        lg = QAction("Agent log (everything it did)", menu)
        lg.triggered.connect(self.open_agent_log)
        menu.addAction(lg)
        menu.addSeparator()
        q = QAction('Quit Caps', menu)
        q.triggered.connect(self.quit)
        menu.addAction(q)
        menu.aboutToShow.connect(self._sync_tray)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_prompt() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self._menu = menu
        self._sync_tray()

    def open_agent_log(self):
        path = os.path.join(DATA, 'agent-log.md')
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                f.write('# Caps agent log\n\nEvery task, its plan, each step and your answers.\n')
        os.startfile(path)

    def _sync_tray(self):
        self.act_ac.setEnabled(self.engine is not None)
        self.act_ac.setChecked(bool(self.engine is not None and self.engine.enabled))
        self.act_buddy.setChecked(self.buddy.enabled)
        level = self.cfg['agent'].get('permission', 'normal')
        for lv, act in self.act_perm.items():
            act.setChecked(lv == level)

    def set_permission(self, level):
        """How much the agent may do without asking; saved in data/config.json (agent.permission)."""
        self.cfg['agent']['permission'] = level
        save_setting(('agent', 'permission'), level)
        self._sync_tray()
        self.reply(f'Agent: {level}', agent_mod.LEVEL_INFO[level] + '.', 4)

    def quit(self):
        self.sync_light(False)
        self.keys.stop_capture()
        self.agent_stop.set()
        self.ask_stop.set()
        self.mouse_state['drag'] = None
        self._stop_mouse_hook()
        self.tray.hide()
        self.qapp.quit()

    def exec(self):
        return self.qapp.exec()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--no-autocorrect', action='store_true', help='do not load math autocorrect')
    ap.add_argument('--exit-after', type=float, default=0, help=argparse.SUPPRESS)  # smoke tests
    args = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    logging.basicConfig(filename=os.path.join(DATA, 'caps.log'), level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if not winutil.single_instance(MUTEX):
        app = QApplication(sys.argv)
        QMessageBox.information(None, 'Caps', 'Caps (or the math autocorrect daemon) is already running.\n'
                                'Close it from the tray icon or Task Manager first.')
        return 1
    app = App(args)
    if args.exit_after:
        QTimer.singleShot(int(args.exit_after * 1000), app.quit)
    log.info('started')
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
