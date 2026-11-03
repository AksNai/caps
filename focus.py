"""Focus sessions: a timer plus a list of distractions that get pushed out of the way.

Decisions are pure (`check(window, now)` says what to do); the app performs them.  Everything
here is reversible: apps are minimized, never closed, and a browser tab on a blocked site is
covered by a shield that offers to go back or close the tab (Ctrl+Shift+T reopens it).
"""
import json
import os
import time
from dataclasses import asdict, dataclass

BROWSERS = {'zen', 'firefox', 'chrome', 'msedge', 'brave', 'opera', 'opera_gx', 'vivaldi', 'arc', 'librewolf',
            'floorp', 'thorium', 'iexplore'}

DEFAULTS = {
    'default_minutes': 25,
    'block_apps': ['Discord', 'Steam', 'steamwebhelper', 'EpicGamesLauncher', 'Battle.net', 'RiotClientServices',
                   'Telegram', 'WhatsApp', 'Messenger', 'Instagram', 'TikTok', 'Netflix'],
    'block_sites': ['YouTube', 'Reddit', 'Twitter', '/ X', 'Instagram', 'TikTok', 'Netflix', 'Twitch', 'Facebook',
                    'Hulu', 'Disney+', 'Prime Video', 'Crunchyroll', 'Discord'],
    'allow_titles': [],          # e.g. "lecture": a YouTube title containing it is fine
    'show_timer': True,
}


@dataclass
class Session:
    start: float
    end: float
    label: str = ''
    blocked: int = 0
    allow_until: float = 0.0

    def remaining(self, now):
        return max(0.0, self.end - now)


def site_hit(title, sites):
    """Browser window titles end with the page title, e.g. 'Some video - YouTube — Zen Browser'."""
    t = title.lower()
    for s in sites:
        sl = s.lower()
        if sl.startswith('/'):  # '/ X' style suffix rules match the page title end: 'Home / X'
            page = t.rsplit(' — ', 1)[0].rsplit(' - ', 1)[0] if (' — ' in t or ' - ' in t) else t
            if page.rstrip().endswith(sl):
                return s
        elif sl in t:
            return s
    return None


class Focus:
    def __init__(self, cfg, state_path=None, log_path=None):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.state_path, self.log_path = state_path, log_path
        self.session = None
        self._load()

    # ------------------------------------------------------------------ session
    def start(self, minutes, label='', now=None):
        now = time.time() if now is None else now
        self.session = Session(now, now + 60 * minutes, label)
        self._save()
        return self.session

    def stop(self, now=None, completed=False):
        now = time.time() if now is None else now
        s = self.session
        if not s:
            return None
        self.session = None
        summary = {'label': s.label, 'planned_min': round((s.end - s.start) / 60),
                   'actual_min': round((min(now, s.end) - s.start) / 60), 'blocked': s.blocked,
                   'completed': completed, 'start': s.start}
        self._save()
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(summary) + '\n')
        return summary

    def extend(self, minutes):
        if self.session:
            self.session.end += 60 * minutes
            self._save()

    def allow(self, minutes, now=None):
        now = time.time() if now is None else now
        if self.session:
            self.session.allow_until = now + 60 * minutes
            self._save()

    def active(self):
        return self.session is not None

    def expired(self, now=None):
        now = time.time() if now is None else now
        return bool(self.session) and now >= self.session.end

    def status(self, now=None):
        now = time.time() if now is None else now
        if not self.session:
            return None
        return {'remaining_min': int(-(-self.session.remaining(now) // 60)), 'label': self.session.label}

    # ------------------------------------------------------------------ decisions
    def distraction(self, win):
        """('app', name) for a blocked app, ('site', name) for a blocked site in a browser, else None."""
        if win is None:
            return None
        if any(a.lower() in win.title.lower() for a in self.cfg['allow_titles']):
            return None
        app = win.app.lower()
        if app in BROWSERS:
            hit = site_hit(win.title, self.cfg['block_sites'])
            return ('site', hit.strip('/ ') or hit) if hit else None
        for a in self.cfg['block_apps']:
            if app == a.lower():
                return ('app', win.app)
        return None

    def check(self, win, now=None):
        """What to do about the foreground window right now: None, ('minimize', name), ('shield', name)."""
        now = time.time() if now is None else now
        s = self.session
        if not s or now >= s.end or now < s.allow_until:
            return None
        d = self.distraction(win)
        if d is None:
            return None
        return ('minimize', d[1]) if d[0] == 'app' else ('shield', d[1])

    def count_block(self):
        if self.session:
            self.session.blocked += 1
            self._save()

    # ------------------------------------------------------------------ persistence
    def _save(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump({'session': asdict(self.session) if self.session else None}, f)

    def _load(self):
        if self.state_path and os.path.exists(self.state_path):
            try:
                d = json.load(open(self.state_path, encoding='utf-8')).get('session')
                self.session = Session(**d) if d else None
            except (OSError, ValueError, TypeError):
                self.session = None
