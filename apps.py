"""Installed apps, so the prompt (and the agent) can open things that aren't running yet.

The list comes from the Start menu: its shortcuts (.lnk, with their icons) plus `Get-StartApps`
for Store apps.  It is cached in data/apps.json and refreshed in the background, so the prompt
never waits for it."""
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass

import search

SKIP = re.compile(r'uninstall|readme|read me|help|documentation|release notes|website|license|changelog|manual|'
                  r'what\'s new|support|feedback hub|on the web|\burl\b', re.I)
START_DIRS = [os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'), r'Microsoft\Windows\Start Menu\Programs'),
              os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs')]


@dataclass
class App:
    name: str
    target: str   # a .lnk path, or 'shell:AppsFolder\<AppUserModelID>'
    icon: str = ''  # file to take the icon from ('' for Store apps)


def scan_shortcuts(dirs=START_DIRS):
    out = []
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith('.lnk'):
                    name = f[:-4]
                    if not SKIP.search(name):
                        p = os.path.join(root, f)
                        out.append(App(name, p, p))
    return out


def scan_store_apps(timeout=20):
    """Get-StartApps lists everything on the Start menu, Store apps included."""
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            'Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress'],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        rows = json.loads(r.stdout or '[]')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    rows = [rows] if isinstance(rows, dict) else rows
    return [App(x['Name'], 'shell:AppsFolder\\' + x['AppID']) for x in rows
            if x.get('Name') and x.get('AppID') and not SKIP.search(x['Name'])]


def merge(shortcuts, store):
    """One entry per name; a shortcut wins (it has an icon)."""
    seen, out = set(), []
    for a in shortcuts + store:
        k = a.name.lower()
        if k not in seen:
            seen.add(k)
            out.append(a)
    return sorted(out, key=lambda a: a.name.lower())


def scan():
    return merge(scan_shortcuts(), scan_store_apps())


def load(path):
    try:
        return [App(**x) for x in json.load(open(path, encoding='utf-8'))]
    except (OSError, ValueError, TypeError):
        return []


def save(path, apps):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump([asdict(a) for a in apps], f)
    os.replace(tmp, path)


ALIASES = {'vs code': 'visual studio code', 'vscode': 'visual studio code', 'cmd': 'command prompt',
           'chrome': 'google chrome', 'word': 'microsoft word', 'excel': 'microsoft excel', 'ppt': 'powerpoint',
           'task manager': 'task manager', 'control panel': 'control panel', 'browser': 'zen'}


def _words(s):
    return [w for w in re.split(r'[^0-9a-z]+', s.lower()) if w]


def good_match(query, name):
    """Stricter than window search (there are hundreds of apps): the query must appear in the name,
    or every query word must start a word of the name, or it must be the name's initials ('vsc')."""
    q, n = query.lower().strip(), name.lower()
    if q in n:
        return True
    words = _words(n)
    if all(any(w.startswith(qw) for w in words) for qw in _words(q)):
        return True
    initials = ''.join(w[0] for w in words)
    return len(q) >= 2 and ' ' not in q and initials.startswith(q)


def rank(query, apps, limit=3):
    """Apps whose name matches the query, best first."""
    q = query.strip()
    if len(q) < 2:
        return []
    q = ALIASES.get(q.lower(), q)
    scored = []
    for a in apps:
        if not good_match(q, a.name):
            continue
        s = search.score(q, a.name)
        if s is not None:
            scored.append((s - 0.02 * len(a.name), a))
    scored.sort(key=lambda t: -t[0])
    return [a for _, a in scored[:limit]]


def find(name, apps):
    """The best app for a name the agent gave ('spotify', 'VS Code'), or None."""
    n = name.lower().strip()
    n = ALIASES.get(n, n)
    for a in apps:
        if a.name.lower() == n:
            return a
    best = rank(name, apps, 1)
    return best[0] if best else None


def launch(app):
    if app.target.startswith('shell:'):
        subprocess.Popen(['explorer.exe', app.target], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        os.startfile(app.target)
