"""Caps Lock as a command key.

Runs inside the low-level keyboard hook, so every call must return in microseconds.  It never
touches the UI: it emits events (`emit(name, arg)`) that the UI thread picks up.

  Caps Lock goes down          -> 'caps_down'   the companion pops up; a gesture may follow
  tap                          -> 'tap'         the companion stays and listens: typing goes to it
  tap while it is listening    -> 'dismiss'     it leaves
  hold, gesture or Caps+scroll -> 'release'     on key-up; the UI decides what the hold meant
  Caps Lock + a letter         -> 'rollover'    fast typists start a command before letting go
  Esc while holding            -> 'cancel'
  Shift + Caps Lock            -> real Caps Lock

While the companion listens, keys are *captured*: swallowed here and forwarded to the UI as 'char'
and 'key' events, so the window you're in keeps keyboard focus and nothing leaks into it.  Any
shortcut (Alt+Tab, Win+D, Ctrl+S...) ends the capture and goes through untouched.  The UI
acknowledges the capture several times a second; if it stops (a hang), capture switches itself off
after CAPTURE_TIMEOUT so a hang can never eat your typing.
"""
from winutil import (VK_BACK, VK_CAPITAL, VK_CONTROL, VK_DOWN, VK_ESCAPE, VK_LEFT, VK_LWIN, VK_MENU, VK_RETURN,
                     VK_RIGHT, VK_RWIN, VK_SHIFT, VK_TAB, VK_UP, vk_char)

TAP_MAX = 0.35          # seconds: a longer still press is a hold (and opens the ring menu)
CAPTURE_TIMEOUT = 3.0   # stop capturing if the UI stops acknowledging (a hung UI must not eat typing)
MODIFIERS = {VK_SHIFT, VK_CONTROL, VK_MENU, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN}
NAMED = {VK_RETURN: 'enter', VK_ESCAPE: 'esc', VK_BACK: 'back', VK_UP: 'up', VK_DOWN: 'down', VK_TAB: 'tab',
         VK_LEFT: 'left', VK_RIGHT: 'right'}


class CapsKeys:
    def __init__(self, emit, on_expire=None):
        self.emit = emit
        self.on_expire = on_expire   # hook thread: capture gave up (e.g. turn the Caps Lock light back off now)
        self.down_at = None      # Caps Lock is held since this time
        self.used = False        # a key was pressed while it was held
        self.moved = False       # set by the UI (pointer moved) or the wheel hook (scrolled) during the hold
        self.was_listening = False
        self.swallow = set()     # keys whose key-up must be swallowed because we ate the key-down
        self.capturing = False
        self.capture_seen = 0.0  # last time the UI acknowledged (or started) the capture

    # called from the UI thread
    def start_capture(self, now):
        self.capturing, self.capture_seen = True, now

    def ack(self, now):
        self.capture_seen = now

    def stop_capture(self):
        self.capturing = False

    # called from the hook thread
    def feed(self, vk, down, now, shift=False, ctrl=False, alt_win=False):
        """Returns 'suppress' to swallow the key, otherwise None (the key proceeds normally).
        alt_win: Alt or a Windows key is held."""
        if vk == VK_CAPITAL:
            return self._caps(down, now, shift)
        if not down:
            if vk in self.swallow:
                self.swallow.discard(vk)
                return 'suppress'
            return None
        if self.down_at is not None:  # Caps Lock is being held
            if vk in MODIFIERS:
                return None
            self.used = True
            self.swallow.add(vk)
            if vk == VK_ESCAPE:
                self.emit('cancel', None)
                return 'suppress'
            ch = vk_char(vk, shift)
            if ch and ch != ' ' and not ctrl and not alt_win:
                self.emit('rollover', ch)
                self.start_capture(now)
            return 'suppress'
        if self.capturing:
            if now - self.capture_seen > CAPTURE_TIMEOUT:
                self.capturing = False
                if self.on_expire:
                    self.on_expire()
                self.emit('dismiss', 'expired')
                return None
            if vk in MODIFIERS:
                return None
            if alt_win or (ctrl and vk not in (0x56, VK_BACK)):
                self.capturing = False   # a shortcut: the companion leaves and the shortcut goes through
                self.emit('dismiss', 'shortcut')
                return None
            self.swallow.add(vk)
            if ctrl:
                self.emit('key', 'paste' if vk == 0x56 else 'backword')
                return 'suppress'
            if vk in NAMED:
                self.emit('key', NAMED[vk])
            else:
                ch = vk_char(vk, shift)
                if ch:
                    self.emit('char', ch)
            return 'suppress'
        return None

    def _caps(self, down, now, shift):
        if down:
            if self.down_at is None:
                if shift:
                    return None  # Shift+Caps Lock: the real thing
                self.down_at, self.used, self.moved = now, False, False
                self.was_listening = self.capturing
                self.emit('caps_down', None)
            return 'suppress'  # also swallows auto-repeat
        if self.down_at is None:
            return None  # key-up of a pass-through Shift+Caps Lock
        held, self.down_at = now - self.down_at, None
        if not self.used and not self.moved and held <= TAP_MAX:
            if self.was_listening:
                self.capturing = False
                self.emit('dismiss', 'caps')
            else:
                self.start_capture(now)  # catch keys typed before the UI has caught up
                self.emit('tap', None)
            return 'suppress'
        if not self.used:
            self.capturing = False  # a gesture, ring choice or scroll: nothing to type into afterwards
        self.emit('release', {'held': held, 'used': self.used})
        return 'suppress'
