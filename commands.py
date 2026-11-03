"""Typed commands for the prompt.  `suggest(query, state)` is pure: it returns what the prompt
should offer for the text typed so far; the app carries out the chosen action.  `intent(query)`
tells a question about the screen ('why is this red?') from a task for the agent ('open spotify
and play lofi'); `?` and `!` force one or the other."""
import re
from dataclasses import dataclass, field

DEFAULT_FOCUS_MIN = 25


@dataclass
class Suggestion:
    title: str
    subtitle: str = ''
    action: tuple = ()          # ('focus_start', minutes, label), ('switch', hwnd), ...
    icon: str = 'command'       # 'command' | 'focus' | 'window' | 'app' | 'ask' | 'agent' | 'jot'
    win: object = field(default=None, repr=False)
    icon_path: str = ''         # a file to take the row's icon from (an app's Start menu shortcut)


def parse_minutes(tok):
    """'50' '50m' '50min' '1h' '1.5h' '90 mins' -> minutes, else None."""
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?', tok.strip().lower())
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) and m.group(2).startswith('h'):
        v *= 60
    return int(round(v)) if 0 < v <= 12 * 60 else None


def _split_focus_args(rest):
    """'50 cs 210' -> (50, 'cs 210');  'cs' -> (None, 'cs');  '1.5 h essay' -> (90, 'essay')."""
    rest = rest.strip()
    m = re.match(r'(\d+(?:\.\d+)?\s*(?:minutes?|mins?|m|hours?|hrs?|hr|h)?)(?:\s+|$)(.*)', rest, re.I)
    if m and parse_minutes(m.group(1)) is not None:
        return parse_minutes(m.group(1)), m.group(2).strip()
    m = re.match(r'(.*?)\s+(\d+(?:\.\d+)?\s*(?:minutes?|mins?|m|hours?|hrs?|hr|h)?)$', rest, re.I)
    if m and parse_minutes(m.group(2)) is not None:
        return parse_minutes(m.group(2)), m.group(1).strip()
    return None, rest


def fmt_min(m):
    return f'{m // 60} h {m % 60} min' if m >= 60 and m % 60 else f'{m // 60} h' if m >= 60 else f'{m} min'


END_WORDS = ('unfocus', 'focus off', 'focus stop', 'focus end', 'end focus', 'stop focus')


def suggest(query, state):
    """state: {'focus': None | {'remaining_min', 'label'}, 'autocorrect': bool|None, 'buddy': bool}"""
    q = ' '.join(query.lower().split())
    out = []
    focus = state.get('focus')
    if not q:
        return out

    # ---- focus
    if 'focus'.startswith(q) or q.startswith('focus'):
        rest = q[5:] if q.startswith('focus') else ''
        if rest.strip().startswith('+'):
            add = parse_minutes(rest.strip()[1:])
            if focus and add:
                out.append(Suggestion(f'Add {fmt_min(add)} to focus', f"{focus['remaining_min'] + add} min total left",
                                      ('focus_extend', add), 'focus'))
        elif not any(q.startswith(w) for w in END_WORDS):
            minutes, label = _split_focus_args(rest)
            if focus and not rest.strip():
                out.append(Suggestion(f"Focusing{' on ' + focus['label'] if focus['label'] else ''}",
                                      f"{focus['remaining_min']} min left · type 'focus off' to end, 'allow 5' for a break",
                                      ('noop',), 'focus'))
            else:
                m = minutes or DEFAULT_FOCUS_MIN
                what = f' on {label}' if label else ''
                verb = 'Restart focus' if focus else 'Focus'
                out.append(Suggestion(f'{verb}{what} for {fmt_min(m)}',
                                      'Distracting apps and sites are blocked until the timer ends',
                                      ('focus_start', m, label), 'focus'))
    if any(w.startswith(q) or q.startswith(w) for w in END_WORDS) and focus and len(q) >= 3:
        out.append(Suggestion('End focus session', f"{focus['remaining_min']} min left", ('focus_stop',), 'focus'))
    if focus and ('allow'.startswith(q) or q.startswith('allow ')) and len(q) >= 2:
        m = parse_minutes(q[5:]) if q.startswith('allow ') and q[5:].strip() else 5
        if m:
            out.append(Suggestion(f'Allow distractions for {fmt_min(m)}', 'Focus timer keeps running',
                                  ('allow', m), 'focus'))

    # ---- class schedule (from the plan and the registration site on screen)
    if re.search(r'\bschedul', q) or q in ('plan classes', 'plan my classes', 'classes'):
        out.append(Suggestion('Plan a class schedule', 'From your course plan and the sections on screen (Workday...)',
                              ('schedule', query.strip()), 'schedule'))

    # ---- jot
    if q.startswith('jot ') and q[4:].strip():
        text = query.strip()[4:].strip()
        out.append(Suggestion(f'Jot: {text}', 'Send it to your jot planner, which files it for you', ('jot', text), 'jot'))
    elif len(q) >= 2 and 'jot'.startswith(q):
        out.append(Suggestion('Jot a note', "Type jot and the note, e.g. 'jot bench 135 3x5'", ('noop',), 'jot'))

    # ---- toggles
    ac = state.get('autocorrect')
    if ac is not None and len(q) >= 3 and ('autocorrect'.startswith(q.split()[0]) or q.startswith('math')):
        out.append(Suggestion(f"Turn math autocorrect {'off' if ac else 'on'}", 'Ctrl+Alt+M also toggles it',
                              ('autocorrect', not ac)))
    if len(q) >= 3 and ('buddy'.startswith(q.split()[0]) or 'companion'.startswith(q.split()[0])):
        b = state.get('buddy', True)
        out.append(Suggestion(f"Turn the companion {'off' if b else 'on'}",
                              'It pops up when you press Caps Lock', ('buddy', not b)))
    if len(q) >= 4 and ('quit caps'.startswith(q) or q in ('quit', 'exit caps')):
        out.append(Suggestion('Quit Caps', 'Caps Lock goes back to normal', ('quit',)))
    return out


# --------------------------------------------------------------------------- ask / do
QUESTION_WORDS = {'what', 'whats', "what's", 'why', 'how', 'hows', "how's", 'who', 'whos', "who's", 'where', 'wheres',
                  "where's", 'when', 'which', 'is', 'are', 'was', 'were', 'can', 'could', 'does', 'did', 'should',
                  'would', 'will', 'explain', 'tell', 'show', 'translate', 'summarize', 'summarise', 'define', 'read',
                  'describe', 'whats', 'wtf', 'help'}
ACTION_WORDS = {'open', 'close', 'launch', 'start', 'play', 'pause', 'put', 'move', 'arrange', 'make', 'create', 'new',
                'search', 'google', 'look', 'go', 'type', 'write', 'send', 'email', 'set', 'turn', 'mute', 'unmute',
                'download', 'rename', 'delete', 'take', 'copy', 'click', 'tile', 'minimize', 'maximize', 'kill',
                'install', 'run', 'join', 'schedule', 'remind', 'find', 'organize', 'clean', 'empty', 'snap', 'split',
                'switch', 'lock', 'add', 'navigate', 'visit', 'watch', 'listen', 'pull', 'bring', 'save', 'skip',
                'reply', 'post', 'fill', 'sort', 'check', 'do'}
POLITE = ('please ', 'pls ', 'can you ', 'could you ', 'would you ', 'hey caps ', 'caps ')


def strip_marker(text):
    """'? why'/'ask why' -> 'why';  '! open x'/'do open x' -> 'open x'."""
    t = text.strip()
    for m in ('?', '!', '>'):
        if t.startswith(m):
            return t[1:].strip()
    low = t.lower()
    for m in ('ask ', 'do '):
        if low.startswith(m):
            return t[len(m):].strip()
    return t


def intent(text):
    """'ask' (a question about the screen), 'do' (a task for the agent) or None (probably a window
    or command search)."""
    t = ' '.join(text.lower().split())
    if not t:
        return None
    if t.startswith('?') or t.startswith('ask '):
        return 'ask'
    if t.startswith('!') or t.startswith('>') or t.startswith('do '):
        return 'do'
    polite = False
    for p in POLITE:
        if t.startswith(p):
            t, polite = t[len(p):], True
    words = t.split()
    if not words:
        return None
    if words[0] in ACTION_WORDS and (len(words) >= 2 or polite):
        return 'do'
    if text.rstrip().endswith('?'):
        return 'ask'
    if words[0] in QUESTION_WORDS and len(words) >= 2:
        return 'ask'
    return None


def ai_suggestions(query, followup=False):
    """The 'Ask: ...' and 'Do: ...' rows for free text."""
    body = strip_marker(query)
    if len(body) < 2:
        return []
    ask = Suggestion(f'Ask: {body}', ('Follow-up question' if followup else "About what's on screen")
                     + ' · local model', ('ask', body), 'ask')
    do = Suggestion(f'Do: {body}', 'Caps plans it, and asks you before anything risky', ('agent', body), 'agent')
    return [ask, do]
