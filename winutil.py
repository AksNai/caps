"""Win32 helpers, ctypes only: windows, processes, focus, keystrokes."""
import ctypes
import os
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
dwmapi = ctypes.WinDLL('dwmapi')

HWND, DWORD, BOOL, UINT = wintypes.HWND, wintypes.DWORD, wintypes.BOOL, wintypes.UINT
LONG_PTR = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)


def _sig(fn, res, *args):
    fn.restype, fn.argtypes = res, list(args)


_sig(user32.GetForegroundWindow, HWND)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.IsWindow, BOOL, HWND)
_sig(user32.IsWindowVisible, BOOL, HWND)
_sig(user32.IsIconic, BOOL, HWND)
_sig(user32.GetWindowLongPtrW, LONG_PTR, HWND, ctypes.c_int)
_sig(user32.GetWindow, HWND, HWND, UINT)
_sig(user32.GetWindowThreadProcessId, DWORD, HWND, ctypes.POINTER(DWORD))
_sig(user32.ShowWindow, BOOL, HWND, ctypes.c_int)
_sig(user32.SetForegroundWindow, BOOL, HWND)
_sig(user32.BringWindowToTop, BOOL, HWND)
_sig(user32.AttachThreadInput, BOOL, DWORD, DWORD, BOOL)
_sig(user32.GetWindowRect, BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.MonitorFromWindow, wintypes.HMONITOR, HWND, DWORD)
_sig(user32.GetShellWindow, HWND)
_sig(user32.GetDesktopWindow, HWND)
_sig(user32.EnumWindows, BOOL, WNDENUMPROC, wintypes.LPARAM)
_sig(user32.EnumChildWindows, BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
_sig(user32.GetKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.SetWindowDisplayAffinity, BOOL, HWND, DWORD)
_sig(user32.MapVirtualKeyW, UINT, UINT, UINT)
_sig(kernel32.OpenProcess, wintypes.HANDLE, DWORD, BOOL, DWORD)
_sig(kernel32.QueryFullProcessImageNameW, BOOL, wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD))
_sig(kernel32.CloseHandle, BOOL, wintypes.HANDLE)
_sig(kernel32.GetCurrentThreadId, DWORD)
_sig(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, BOOL, wintypes.LPCWSTR)
_sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, HWND, DWORD, ctypes.c_void_p, DWORD)


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT), ('dwFlags', DWORD)]


_sig(user32.GetMonitorInfoW, BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
SW_MINIMIZE, SW_RESTORE = 6, 9
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0, 0x11

# Keystrokes we send carry this tag so the keyboard hook can skip them.  Must equal
# math-autocorrect/daemon.py MAGIC (the two share one hook).
MAGIC = 0x4D41544843

VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12
VK_CAPITAL, VK_ESCAPE, VK_SPACE = 0x14, 0x1B, 0x20
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_MEDIA_PLAY_PAUSE = 0xB3


# --------------------------------------------------------------------------- windows
@dataclass
class Win:
    hwnd: int
    title: str
    pid: int
    exe: str  # full path, '' if unknown

    @property
    def app(self):
        """Executable name without .exe, e.g. 'Discord', 'zen', 'Code'."""
        base = os.path.basename(self.exe)
        return base[:-4] if base.lower().endswith('.exe') else base


def title_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def pid_of(hwnd):
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache = {}


def exe_of_pid(pid):
    if pid in _exe_cache:
        return _exe_cache[pid]
    path = ''
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                path = buf.value
        finally:
            kernel32.CloseHandle(h)
    if len(_exe_cache) > 2000:
        _exe_cache.clear()
    _exe_cache[pid] = path
    return path


def _uwp_exe(hwnd, frame_pid):
    """Store apps live inside ApplicationFrameHost; the real process owns a child window."""
    found = []

    def cb(child, _):
        p = pid_of(child)
        if p != frame_pid:
            found.append(p)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return exe_of_pid(found[0]) if found else ''


def win_info(hwnd):
    if not hwnd:
        return None
    pid = pid_of(hwnd)
    exe = exe_of_pid(pid)
    if exe.lower().endswith('applicationframehost.exe'):
        exe = _uwp_exe(hwnd, pid) or exe
    return Win(int(hwnd), title_of(hwnd), pid, exe)


def is_cloaked(hwnd):
    v = DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return bool(v.value)


def is_switchable(hwnd):
    """Roughly the windows Alt+Tab shows."""
    if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
        return False
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if not ex & WS_EX_APPWINDOW:
        if ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) or user32.GetWindow(hwnd, GW_OWNER):
            return False
    if is_cloaked(hwnd):
        return False
    return class_of(hwnd) not in ('Progman', 'WorkerW', 'Shell_TrayWnd')


def list_windows(exclude_pid=None):
    """Switchable top-level windows, most recently used first (Z order)."""
    hwnds = []

    def cb(hwnd, _):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    out = []
    for h in hwnds:
        if is_switchable(h):
            w = win_info(h)
            if w and w.pid != exclude_pid:
                out.append(w)
    return out


def foreground():
    return win_info(user32.GetForegroundWindow())


def foreground_hwnd():
    return int(user32.GetForegroundWindow() or 0)


def foreground_is_own():
    h = user32.GetForegroundWindow()
    return bool(h) and pid_of(h) == os.getpid()


def exists(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def frame_rect(hwnd):
    """Visible bounds in physical pixels (without the invisible resize border)."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def is_fullscreen(hwnd):
    """A borderless window covering its whole monitor (games, full-screen video)."""
    if not hwnd or class_of(hwnd) in ('Progman', 'WorkerW') or hwnd == user32.GetShellWindow():
        return False
    if user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION:
        return False
    mon = user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


def minimize(hwnd):
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def activate(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    return force_foreground(hwnd)


def force_foreground(hwnd):
    """SetForegroundWindow, working around the foreground lock Windows puts on background apps."""
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    me = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if other and other != me:
        user32.AttachThreadInput(me, other, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(me, other, False)
    if user32.GetForegroundWindow() == hwnd:
        return True
    # While Alt is down Windows lets any process take the foreground; release it afterwards so the
    # key-up lands in the new window rather than opening the old window's menu bar.
    send([key_event(VK_MENU)])
    user32.SetForegroundWindow(hwnd)
    send([key_event(VK_MENU, up=True)])
    return user32.GetForegroundWindow() == hwnd


def exclude_from_capture(hwnd, on=True):
    user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE)


# --------------------------------------------------------------------------- moving windows
class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [('length', UINT), ('flags', UINT), ('showCmd', UINT), ('ptMinPosition', wintypes.POINT),
                ('ptMaxPosition', wintypes.POINT), ('rcNormalPosition', wintypes.RECT)]


_sig(user32.GetWindowPlacement, BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
_sig(user32.SetWindowPlacement, BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
_sig(user32.SetWindowPos, BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT)
_sig(user32.IsZoomed, BOOL, HWND)
_sig(user32.WindowFromPoint, HWND, wintypes.POINT)
_sig(user32.GetAncestor, HWND, HWND, UINT)
_sig(user32.GetCursorPos, BOOL, ctypes.POINTER(wintypes.POINT))
SW_MAXIMIZE = 3
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10


def cursor_pos():
    """Pointer position in physical pixels."""
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_at(x, y):
    """The switchable top-level window under a physical point, or None."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    h = user32.GetAncestor(h, 2) if h else None  # GA_ROOT
    return int(h) if h and is_switchable(h) else None


def work_area(hwnd):
    """The work area (screen minus taskbar) of the window's monitor, physical pixels."""
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    w = mi.rcWork
    return w.left, w.top, w.right, w.bottom


def is_maximized(hwnd):
    return bool(user32.IsZoomed(hwnd))


def get_placement(hwnd):
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    return wp if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)) else None


def set_placement(hwnd, wp):
    return bool(user32.SetWindowPlacement(hwnd, ctypes.byref(wp)))


def move_frame(hwnd, rect):
    """Put the window's *visible* frame exactly on rect (l, t, r, b).  Windows 10/11 windows have an
    invisible resize border around the visible frame, so the window rect is grown by it."""
    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    fl, ft, fr, fb = frame_rect(hwnd)
    bl, bt, br, bb = fl - wr.left, ft - wr.top, wr.right - fr, wr.bottom - fb
    l, t, r, b = rect
    return bool(user32.SetWindowPos(hwnd, None, l - bl, t - bt, (r - l) + bl + br, (b - t) + bt + bb,
                                    SWP_NOZORDER | SWP_NOACTIVATE))


def maximize(hwnd, on=True):
    user32.ShowWindow(hwnd, SW_MAXIMIZE if on else SW_RESTORE)
    return is_maximized(hwnd) == on


def window_rect(hwnd):
    """The full window rect (including the invisible resize border), physical pixels."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def set_window_rect(hwnd, l, t, r, b, size=True):
    flags = SWP_NOZORDER | SWP_NOACTIVATE | (0 if size else SWP_NOSIZE)
    return bool(user32.SetWindowPos(hwnd, None, int(l), int(t), int(r - l), int(b - t), flags))


def restore(hwnd):
    user32.ShowWindow(hwnd, SW_RESTORE)


_sig(user32.MonitorFromPoint, wintypes.HMONITOR, wintypes.POINT, DWORD)


def monitor_at(x, y):
    """(monitor rect, work area) of the monitor under a physical point."""
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2), ctypes.byref(mi))
    m, w = mi.rcMonitor, mi.rcWork
    return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)


_sig(user32.PostMessageW, BOOL, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)


def close_window(hwnd):
    """Ask a window to close, as its X button would (it may ask to save first)."""
    return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE


_sig(user32.SetCursorPos, BOOL, ctypes.c_int, ctypes.c_int)
MOUSE_DOWN_UP = {'left': (0x2, 0x4), 'right': (0x8, 0x10)}


def click_at(x, y, button='left', tag=MAGIC):
    """Click at a physical point, then put the pointer back where it was."""
    back = cursor_pos()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    d, u = MOUSE_DOWN_UP[button]
    send([INPUT(0, _U(mi=MOUSEINPUT(0, 0, 0, d, 0, tag))), INPUT(0, _U(mi=MOUSEINPUT(0, 0, 0, u, 0, tag)))])
    time.sleep(0.05)
    user32.SetCursorPos(*back)


KEY_NAMES = {'ctrl': VK_CONTROL, 'control': VK_CONTROL, 'shift': VK_SHIFT, 'alt': VK_MENU, 'win': VK_LWIN,
             'enter': VK_RETURN, 'return': VK_RETURN, 'esc': VK_ESCAPE, 'escape': VK_ESCAPE, 'tab': VK_TAB,
             'space': VK_SPACE, 'backspace': VK_BACK, 'delete': 0x2E, 'del': 0x2E, 'up': VK_UP, 'down': VK_DOWN,
             'left': VK_LEFT, 'right': VK_RIGHT, 'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
             'plus': 0xBB, 'minus': 0xBD, 'volumeup': 0xAF, 'volumedown': 0xAE, 'mute': 0xAD,
             'playpause': VK_MEDIA_PLAY_PAUSE, 'nexttrack': 0xB0, 'prevtrack': 0xB1,
             **{f'f{i}': 0x6F + i for i in range(1, 13)}}


def parse_keys(combo):
    """'ctrl+shift+t' -> [VK_CONTROL, VK_SHIFT, 0x54]; None if a part isn't a key."""
    out = []
    for part in combo.lower().replace(' ', '').split('+'):
        if part in KEY_NAMES:
            out.append(KEY_NAMES[part])
        elif len(part) == 1 and (part.isalnum()):
            out.append(ord(part.upper()))
        else:
            return None
    return out or None


def mouse_down():
    """Any mouse button is held right now."""
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))


def type_text(s, tag=MAGIC):
    """Type text into the focused window (used to hand back keys Caps swallowed by mistake)."""
    ev = []
    for unit in (s.encode('utf-16-le')[i:i + 2] for i in range(0, len(s.encode('utf-16-le')), 2)):
        code = int.from_bytes(unit, 'little')
        ev += [INPUT(1, _U(ki=KEYBDINPUT(0, code, 0x4, 0, tag))),
               INPUT(1, _U(ki=KEYBDINPUT(0, code, 0x4 | KEYUP, 0, tag)))]
    send(ev)


# --------------------------------------------------------------------------- keys
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', DWORD),
                ('time', DWORD), ('dwExtraInfo', ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', DWORD),
                ('dwFlags', DWORD), ('time', DWORD), ('dwExtraInfo', ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', DWORD), ('u', _U)]


_sig(user32.SendInput, UINT, UINT, ctypes.POINTER(INPUT), ctypes.c_int)
KEYUP = 0x2


def key_event(vk, up=False, tag=MAGIC):
    return INPUT(1, _U(ki=KEYBDINPUT(vk, 0, KEYUP if up else 0, 0, tag)))


def send(events):
    if events:
        arr = (INPUT * len(events))(*events)
        user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def tap(vk, tag=MAGIC):
    send([key_event(vk, tag=tag), key_event(vk, up=True, tag=tag)])


def chord(*vks, tag=MAGIC):
    send([key_event(v, tag=tag) for v in vks] + [key_event(v, up=True, tag=tag) for v in reversed(vks)])


def pressed(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def caps_lock_on():
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


_sig(user32.ToUnicodeEx, ctypes.c_int, UINT, UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR, ctypes.c_int,
     UINT, ctypes.c_void_p)
_sig(user32.GetKeyboardLayout, ctypes.c_void_p, DWORD)


def vk_char(vk, shift=False):
    """The character a key types on the current layout (letters, digits, punctuation, space),
    including shifted ones such as ? ! ( ) : so questions can be typed into the prompt."""
    if 0x41 <= vk <= 0x5A:
        c = chr(vk)
        return c if shift else c.lower()
    if vk == VK_SPACE:
        return ' '
    if 0x30 <= vk <= 0x39 and not shift:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return chr(vk - 0x30)  # numpad digits
    if not shift:
        c = user32.MapVirtualKeyW(vk, 2)  # MAPVK_VK_TO_CHAR (unshifted)
        return chr(c) if c and not c & 0x80000000 and chr(c).isprintable() else None
    state = (ctypes.c_ubyte * 256)()
    state[VK_SHIFT] = 0x80
    buf = ctypes.create_unicode_buffer(8)
    layout = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None))
    # flag 4: don't touch the keyboard's dead-key state (Windows 10 1607+)
    n = user32.ToUnicodeEx(vk, user32.MapVirtualKeyW(vk, 0), state, buf, 8, 4, layout)
    return buf.value[0] if n == 1 and buf.value[:1].isprintable() else None


def single_instance(name):
    """False if another process already holds this named mutex."""
    handle = kernel32.CreateMutexW(None, False, name)
    single_instance.handles.append(handle)  # keep it alive for the life of the process
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


single_instance.handles = []


def apps_use_light_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return bool(winreg.QueryValueEx(k, 'AppsUseLightTheme')[0])
    except OSError:
        return False


def wait_until(pred, timeout=1.0, step=0.01):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return pred()
