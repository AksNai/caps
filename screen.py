"""What's on screen, for 'ask about the screen' and the agent: a screenshot of the window plus its
UI Automation elements (buttons, links, text...), numbered so the model can point at one by
number.  The pure helpers (select, format, parse) are tested; Seer and grab touch Windows.

Pixel coordinates from a vision model turned out to be unreliable, element numbers are not: the
companion points at the element's real on-screen rectangle."""
import base64
import io
import re
from dataclasses import dataclass, field

ROLES = {50000: 'button', 50002: 'checkbox', 50003: 'combobox', 50004: 'edit', 50005: 'link', 50006: 'image',
         50007: 'list item', 50011: 'menu item', 50013: 'radio', 50015: 'slider', 50019: 'tab', 50020: 'text',
         50024: 'tree item', 50029: 'data item', 50031: 'split button', 50034: 'header', 50035: 'header item',
         50030: 'document', 50033: 'pane', 50026: 'group', 50021: 'toolbar', 50037: 'title bar'}
LISTED = set(ROLES.values()) - {'pane', 'group', 'toolbar', 'title bar'}
CLICKABLE = {'button', 'checkbox', 'combobox', 'edit', 'link', 'list item', 'menu item', 'radio', 'tab', 'tree item',
             'split button', 'header item', 'data item', 'slider'}


@dataclass
class Element:
    n: int
    role: str
    name: str
    rect: tuple          # physical pixels (l, t, r, b)
    value: str = ''
    ref: object = field(default=None, repr=False, compare=False)   # the UI Automation element, same thread only

    @property
    def center(self):
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


def _area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def select(raw, frame, pointer=None, limit=200):
    """raw: [(role, name, rect, value)] from UI Automation.  Keeps visible, named elements inside the
    window, drops duplicates, keeps the `limit` nearest the pointer, numbers them in reading order."""
    fl, ft, fr, fb = frame
    seen, keep = set(), []
    for row in raw:
        role, name, rect, value = row[:4]
        ref = row[4] if len(row) > 4 else None
        name = ' '.join((name or '').split())[:100]
        value = ' '.join((value or '').split())[:80]
        if role not in LISTED or not (name or value):
            continue
        l, t, r, b = rect
        if r - l < 2 or b - t < 2:
            continue
        cx, cy = (l + r) / 2, (t + b) / 2
        if not (fl <= cx < fr and ft <= cy < fb):
            continue
        key = (role, name, rect)
        if key in seen:
            continue
        seen.add(key)
        keep.append((role, name, rect, value, ref))
    if len(keep) > limit:
        px, py = pointer if pointer else ((fl + fr) / 2, (ft + fb) / 2)
        keep.sort(key=lambda e: ((e[2][0] + e[2][2]) / 2 - px) ** 2 + ((e[2][1] + e[2][3]) / 2 - py) ** 2)
        keep = keep[:limit]
    keep.sort(key=lambda e: (e[2][1] // 24, e[2][0]))
    return [Element(i + 1, role, name, rect, value, ref) for i, (role, name, rect, value, ref) in enumerate(keep)]


def where(rect, frame):
    """'top left', 'middle', 'bottom right'... of the element inside the window."""
    fl, ft, fr, fb = frame
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    fx, fy = (cx - fl) / max(1, fr - fl), (cy - ft) / max(1, fb - ft)
    v = 'top' if fy < 1 / 3 else 'bottom' if fy > 2 / 3 else 'middle'
    h = 'left' if fx < 1 / 3 else 'right' if fx > 2 / 3 else ''
    return f'{v} {h}'.strip() if h or v != 'middle' else 'center'


def format_elements(els, frame, max_chars=9000):
    lines, n = [], 0
    for e in els:
        s = f'{e.n}. {e.role} "{e.name}"' + (f' = "{e.value}"' if e.value and e.value != e.name else '') \
            + f' ({where(e.rect, frame)})'
        n += len(s) + 1
        if n > max_chars:
            lines.append(f'... {len(els) - len(lines)} more')
            break
        lines.append(s)
    return '\n'.join(lines)


# ------------------------------------------------------------------ answers that point
MARK = re.compile(r'\s?\[\[\s*(\d+)\s*\]\]')
COORD = re.compile(r'\s?\[\[[^\]]*\]\]')
STOP = set('the a an to on in at of for and or click press tap button icon tab menu link field box top bottom left '
           'right corner here this that it is use open select choose find with your you there then next item'.split())


def _tokens(s):
    return {w for w in re.findall(r'[a-z0-9]+', s.lower()) if w not in STOP and len(w) > 1}


def parse_answer(text, els, frame=None):
    """Strip [[N]] markers from the model's answer and return (text, element to point at or None).
    A marker only counts if the words just before it name that element (a wrong number would send
    the companion to the wrong place, which is worse than not pointing) and the element isn't
    most of the window."""
    by_n = {e.n: e for e in els}
    target = None
    for m in MARK.finditer(text):
        e = by_n.get(int(m.group(1)))
        if e is None:
            continue
        before = re.split(r'[.!?\n]\s', text[:m.start()])[-1][-90:]
        if not (_tokens(before) & _tokens(e.name + ' ' + e.value)):
            continue
        if frame is not None and _area(e.rect) > 0.4 * _area(frame):
            continue
        target = e
        break
    clean = COORD.sub('', text)
    clean = re.sub(r' +([.,;:!?)])', r'\1', clean)
    return clean.strip(), target


def visible_text(partial):
    """Streaming: hide markers, including one that is only half written."""
    t = partial.split('```')[0]           # a drawing's code is never shown as text
    t = NEXT.sub(' ', t)
    t = COORD.sub('', t)
    t = re.sub(r'\s?\[\[?[^\]]*\]?$', '', t)
    return re.sub(r' +([.,;:!?)])', r'\1', t).strip()


# ------------------------------------------------------------------ Windows: UI Automation and pixels
class Seer:
    """UI Automation client.  COM objects belong to the thread that made them: create one per
    worker thread and use it only there."""

    def __init__(self):
        import comtypes
        import comtypes.client
        self._co = None
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self._co = comtypes     # we initialized COM on this thread, so we uninitialize it
        except OSError:
            pass                    # already initialized here (e.g. the main thread): fine as it is
        mod = comtypes.client.GetModule('UIAutomationCore.dll')
        self.uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
        self.cache = self.uia.CreateCacheRequest()
        for pid in (30005, 30003, 30001, 30045):   # name, control type, bounding rect, value
            self.cache.AddProperty(pid)
        self.visible = self.uia.CreatePropertyCondition(30022, False)  # IsOffscreen == False

    def elements(self, hwnd, refs=False, offscreen=False):
        """[(role, name, rect, value[, ref])] for every on-screen element of a window (with
        offscreen=True, also the ones scrolled out of view)."""
        out = []
        root = self.uia.ElementFromHandle(hwnd)
        cond = self.uia.CreateTrueCondition() if offscreen else self.visible
        arr = root.FindAllBuildCache(4, cond, self.cache)  # TreeScope_Descendants
        for i in range(arr.Length):
            e = arr.GetElement(i)
            role = ROLES.get(e.CachedControlType)
            if role is None:
                continue
            r = e.CachedBoundingRectangle
            try:
                value = e.GetCachedPropertyValue(30045) or ''
            except Exception:
                value = ''
            row = (role, e.CachedName or '', (r.left, r.top, r.right, r.bottom), value if isinstance(value, str) else '')
            out.append(row + (e,) if refs else row)
        return out

    def focused_rect(self):
        """Bounding rect (physical) of whatever has keyboard focus, or None."""
        r = self.uia.GetFocusedElement().CurrentBoundingRectangle
        return (r.left, r.top, r.right, r.bottom) if r.right - r.left > 1 and r.bottom - r.top > 1 else None

    def under(self, x, y):
        """'button "Run" in toolbar "Debug"' for the element at a physical point, or ''."""
        from ctypes import wintypes
        try:
            e = self.uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
        except Exception:
            return ''
        walker = self.uia.ControlViewWalker
        parts = []
        for _ in range(3):
            if not e:
                break
            role = ROLES.get(e.CurrentControlType, 'element')
            name = ' '.join((e.CurrentName or '').split())[:80]
            if name or not parts:
                parts.append(f'{role} "{name}"' if name else role)
            try:
                e = walker.GetParentElement(e)
            except Exception:
                break
        return ' in '.join(parts)

    def pages(self, hwnd):
        """The web pages showing in a browser window (two in a split view), as
        [(title, url, rect, element)].  Background tabs are left out: only pages on screen."""
        cache = self.uia.CreateCacheRequest()
        for pid in (30005, 30003, 30001, 30045, 30022):
            cache.AddProperty(pid)
        cond = self.uia.CreateOrCondition(self.uia.CreatePropertyCondition(30003, 50030),   # document
                                          self.uia.CreatePropertyCondition(30003, 50033))   # pane
        arr = self.uia.ElementFromHandle(hwnd).FindAllBuildCache(4, cond, cache)
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            url = e.GetCachedPropertyValue(30045)
            r = e.CachedBoundingRectangle
            if not isinstance(url, str) or not url.startswith('http') or e.GetCachedPropertyValue(30022):
                continue
            rect = (r.left, r.top, r.right, r.bottom)
            if rect[2] - rect[0] < 200 or rect[3] - rect[1] < 200:
                continue
            if any(abs(rect[0] - o[2][0]) < 4 and abs(rect[2] - o[2][2]) < 4 for o in out):
                continue   # the same page reported twice (a pane and its document)
            out.append((e.CachedName or '', url, rect, e))
        return out

    def text(self, element, max_chars=60000):
        """All the text of a page in reading order, including rows scrolled out of view."""
        cache = self.uia.CreateCacheRequest()
        for pid in (30005, 30003, 30045):
            cache.AddProperty(pid)
        arr = element.FindAllBuildCache(4, self.uia.CreateTrueCondition(), cache)
        lines, last, n = [], None, 0
        for i in range(arr.Length):
            e = arr.GetElement(i)
            if e.CachedControlType in (50033, 50026, 50021, 50030):  # containers repeat their children's text
                continue
            name = ' '.join((e.CachedName or '').split())
            value = e.GetCachedPropertyValue(30045)
            value = ' '.join(value.split()) if isinstance(value, str) else ''
            line = name if not value or value == name else f'{name}: {value}' if name else value
            if not line or line == last:
                continue
            lines.append(line)
            last = line
            n += len(line) + 1
            if n > max_chars:
                break
        return '\n'.join(lines)

    def close(self):
        self.uia = self.cache = self.visible = None
        if self._co is not None:
            try:
                self._co.CoUninitialize()
            except Exception:
                pass
            self._co = None


def grab(rect):
    """Screenshot of a physical-pixel rect (the process must be per-monitor DPI aware, as Caps is)."""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=tuple(int(v) for v in rect), all_screens=True)


def capture_window(hwnd):
    """A window's own pixels even when other windows cover it (PrintWindow), without bringing it
    forward.  Returns (PIL image, window rect) in physical pixels."""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    import winutil
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    l, t, r, b = winutil.window_rect(hwnd)
    w, h = max(1, r - l), max(1, b - t)
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        user32.PrintWindow(hwnd, mdc, 2)   # PW_RENDERFULLCONTENT: works for GPU-drawn windows (browsers)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                        ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                        ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                        ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                        ('biClrImportant', wintypes.DWORD)]
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    return img, (l, t, r, b)


def encode(img, pointer=None, max_side=1536, quality=86):
    """JPEG, base64, scaled to fit max_side; a red ring marks the pointer (image coordinates)."""
    from PIL import ImageDraw
    img = img.convert('RGB')
    if pointer is not None:
        d = ImageDraw.Draw(img)
        x, y = pointer
        rad = max(14, img.width // 110)
        for k, col in ((rad + 3, (255, 255, 255)), (rad, (235, 40, 40))):
            d.ellipse((x - k, y - k, x + k, y + k), outline=col, width=max(3, rad // 4))
    s = min(1.0, max_side / max(img.size))
    if s < 1:
        img = img.resize((round(img.width * s), round(img.height * s)))
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ drawings and lessons in answers
BOARD_BLOCK = re.compile(r'```(?:board|python|py)?[ \t]*\r?\n(.*?)```', re.S)
NEXT = re.compile(r'\s*\[\[\s*NEXT\s*\]\]\s*', re.I)


def split_board(text):
    """The model's answer -> (what to say, drawing code or '', whether more lesson steps follow)."""
    m = BOARD_BLOCK.search(text)
    code = m.group(1).strip() if m else ''
    rest = BOARD_BLOCK.sub(' ', text) if m else text
    if '```' in rest:                      # a block that was cut off: drop it, don't show code as text
        rest = rest[:rest.index('```')]
    more = bool(NEXT.search(rest))
    rest = NEXT.sub(' ', rest)
    return re.sub(r'[ \t]+\n', '\n', re.sub(r'  +', ' ', rest)).strip(), code, more
