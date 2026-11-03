"""Agent mode: type `do ...` (or `!...`) in the prompt and Caps carries it out with the local model.

The model first writes a short plan (shown on the task card), then works one tool call at a time.
Code, not the model, decides which steps need your OK, at the permission level you chose (tray
menu, or agent.permission in data/config.json):

  careful   asks before typing, clicking, shortcuts, closing windows and any command
  normal    asks before sending (Enter in a chat or mail app, Send/Post buttons), deleting, buying,
            closing windows, and commands that change things; everything else just runs
  trusted   asks only before sending, deleting, buying and risky commands

Those steps wait on the card until you allow them (Enter), allow everything for this task (A), or
stop (Esc / tap Caps Lock).  Each task and every step, including your answers, is written to
data/agent-log.md.
"""
import json
import os
import re
import subprocess
import threading
import time

import apps as apps_mod
import screen
import winutil

MAX_STEPS = 20
OBSERVE_ELEMENTS = 140

SYSTEM = """You are Caps, an assistant that operates the user's Windows PC for them, using tools one call at a time.

How to work:
- You get the open windows (w1, w2, ...) and the elements of the active window (numbered). Use those numbers exactly as listed. After each action you get a fresh listing and older numbers stop being valid.
- Use the most direct tool: open_app to start or bring up an app; open_url for any website or web search, with the full address (e.g. https://www.youtube.com/results?search_query=lofi+beats or https://www.google.com/search?q=...), never by typing into a browser; arrange for window layout; find_files then open_path for documents; make_folder for new folders; start_focus and jot for focus sessions and notes; run_command only when nothing else fits.
- To type into a field, click it first (or use a shortcut such as ctrl+l for a browser's address bar), then type_text.
- If something doesn't work, try one other way. If you are stuck, call done and say what is blocking you.
- Never type passwords or payment details and never buy anything. Don't send messages, emails or posts unless the task explicitly asks for that exact message.
- Some actions wait for the user's OK. If the user declines, stop; don't try to get around it.
- When the task is finished, call done with one or two short sentences for the user."""

PLAN_ASK = ('\n\nYour tools: {tools}.\n\nFirst reply with your plan only: at most 5 short numbered steps, one line '
            'each, using those tools. No tool calls yet.')
GO = 'Go ahead: carry out the plan, one tool call at a time, and call done when finished.'


def _fn(tool, desc, required=(), /, **props):
    return {'type': 'function', 'function': {'name': tool, 'description': desc, 'parameters': {
        'type': 'object', 'properties': props, 'required': list(required)}}}


S, I, B = {'type': 'string'}, {'type': 'integer'}, {'type': 'boolean'}
POSITIONS = ['left', 'right', 'top-left', 'top-right', 'bottom-left', 'bottom-right', 'maximize', 'minimize', 'restore']
TOOLS = [
    _fn('open_app', 'Open an installed app by name, or bring it to the front if it is already running.', ['name'],
        name=S),
    _fn('open_url', 'Open a web page (or a search results page) in the default browser.', ['url'], url=S),
    _fn('open_path', 'Open a file or folder with its default app.', ['path'], path=S),
    _fn('make_folder', 'Create a folder (and any missing parents). Does nothing if it already exists.', ['path'],
        path=S),
    _fn('find_files', "Find files or folders in the user's folders whose name contains all these words.", ['query'],
        query=S, folder={'type': 'string', 'description': 'optional folder to search, e.g. ~/Documents'}),
    _fn('switch_to', 'Bring a window to the front.', ['window'], window={**I, 'description': 'window number (w#)'}),
    _fn('arrange', 'Move or resize a window.', ['window', 'position'], window=I,
        position={'type': 'string', 'enum': POSITIONS}),
    _fn('close_window', 'Close a window (it may ask to save first).', ['window'], window=I),
    _fn('read_screen', 'Look at a window again: its elements, and a screenshot. Defaults to the active window.', [],
        window=I),
    _fn('click', 'Click an element from the latest listing.', ['element'], element=I,
        button={'type': 'string', 'enum': ['left', 'right']}),
    _fn('type_text', 'Type text into the focused field of a window, optionally pressing Enter after it.', ['text'],
        text=S, window=I, enter=B),
    _fn('press_keys', 'Press a key or shortcut in a window, e.g. "ctrl+t", "enter", "alt+left", "playpause".',
        ['keys'], keys=S, window=I),
    _fn('run_command', 'Run a PowerShell command on the PC and get its output.', ['command'], command=S),
    _fn('jot', "Add a note to the user's jot planner (it files gym logs, todos, exams... itself).", ['text'], text=S),
    _fn('start_focus', 'Start a focus session that blocks distracting apps and sites.', ['minutes'], minutes=I,
        label=S),
    _fn('wait', 'Wait for something to load (at most 10 seconds).', ['seconds'], seconds={'type': 'number'}),
    _fn('done', 'Finish and tell the user what happened, or what you need from them.', ['message'], message=S),
]
TOOL_NAMES = {t['function']['name'] for t in TOOLS}
REPEATABLE = {'wait', 'read_screen', 'press_keys', 'click'}   # fine to do several times in one task
CHANGES_SCREEN = {'open_app', 'open_url', 'open_path', 'switch_to', 'arrange', 'close_window', 'click', 'type_text',
                  'press_keys'}

# ------------------------------------------------------------------ what needs your OK (decided here, not by the model)
SAFE_KEYS = {'tab', 'shift+tab', 'up', 'down', 'left', 'right', 'pageup', 'pagedown', 'home', 'end', 'esc', 'escape',
             'ctrl+t', 'ctrl+l', 'ctrl+f', 'ctrl+n', 'ctrl+plus', 'ctrl+minus', 'ctrl+0', 'f5', 'alt+left',
             'alt+right', 'ctrl+tab', 'ctrl+shift+tab', 'playpause', 'nexttrack', 'prevtrack', 'volumeup',
             'volumedown', 'mute'}
RUNS_CODE = {'.exe', '.bat', '.cmd', '.ps1', '.psm1', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.msi', '.msp',
             '.scr', '.com', '.cpl', '.hta', '.jar', '.py', '.pyw', '.reg', '.lnk', '.url', '.appref-ms', '.sh',
             '.application', '.msc', '.inf'}


LEVELS = ('careful', 'normal', 'trusted')
LEVEL_INFO = {
    'careful': 'Asks before typing, clicking, shortcuts, closing windows and any command',
    'normal': 'Asks before sending, deleting, buying, closing windows and commands that change things',
    'trusted': 'Asks only before sending, deleting, buying and risky commands',
}
# apps and pages where Enter (or a Send button) sends something to other people
MESSAGING_APPS = {'discord', 'slack', 'teams', 'ms-teams', 'whatsapp', 'telegram', 'signal', 'messenger', 'outlook',
                  'olk', 'hxoutlook', 'thunderbird', 'mail', 'zoom', 'skype', 'element', 'beeper', 'instagram'}
MESSAGING_TITLES = re.compile(r'gmail|outlook|inbox|\bmail\b|messenger|whatsapp|discord|slack|\bteams\b|instagram|'
                              r'twitter|/ x\b|reddit|linkedin|facebook|tiktok|\bchat\b|chatgpt|groupme|imessage', re.I)
SENDS = re.compile(r'\b(send|post|submit|reply|tweet|publish|share|comment|upload|message)\b', re.I)
MONEY = re.compile(r'\b(buy|purchase|pay|payment|checkout|check out|place order|order now|add to cart|subscribe|'
                   r'donate|transfer|upgrade)\b', re.I)
DELETES = re.compile(r'\b(delete|remove|erase|uninstall|discard|empty|format|wipe|trash|clear all)\b', re.I)
CHANGES = re.compile(r'\b(sign out|log ?out|reset|leave|block|report|kick|ban|unfriend|unsubscribe|install|allow|'
                     r'accept|agree|confirm|disable|turn off)\b', re.I)
CLOSE_KEYS = {'alt+f4', 'ctrl+w', 'ctrl+shift+w', 'ctrl+q', 'ctrl+f4'}
# commands that only look at things run without asking (no script blocks, redirects or call operators)
READ_ONLY_CMDS = {'get-childitem', 'gci', 'dir', 'ls', 'get-content', 'gc', 'cat', 'type', 'get-item', 'gi',
                  'get-location', 'pwd', 'get-process', 'gps', 'ps', 'get-service', 'gsv', 'get-date', 'get-volume',
                  'get-psdrive', 'get-netipaddress', 'get-netadapter', 'get-command', 'gcm', 'get-help', 'get-clipboard',
                  'get-itemproperty', 'get-filehash', 'get-ciminstance', 'get-computerinfo', 'get-uptime',
                  'select-object', 'select', 'where-object', 'where', 'sort-object', 'sort', 'measure-object',
                  'measure', 'format-table', 'ft', 'format-list', 'fl', 'out-string', 'group-object', 'test-path',
                  'test-connection', 'resolve-path', 'split-path', 'join-path', 'convertto-json', 'select-string',
                  'sls', 'write-output', 'echo', 'ipconfig', 'whoami', 'hostname', 'systeminfo', 'tasklist',
                  'nslookup', 'ping', 'findstr', 'tree', 'nvidia-smi'}
DANGEROUS_CMD = re.compile(r'remove-|\brm\b|\bdel\b|\berase\b|\brd\b|rmdir|format-volume|clear-|stop-computer|'
                           r'restart-computer|shutdown|set-executionpolicy|\breg\b|uninstall|disable-|\bkill\b|'
                           r'stop-process|taskkill|invoke-webrequest|\biwr\b|curl|wget|invoke-restmethod|\birm\b|'
                           r'\biex\b|invoke-expression|start-process|new-service|set-itemproperty|bcdedit|diskpart|'
                           r'cipher|takeown|icacls|-encodedcommand|move-item|\bmv\b|\bmove\b|rename-item|\bren\b|'
                           r'set-content|out-file|add-content|copy-item.*-force', re.I)


def norm_keys(k):
    return '+'.join(p for p in str(k).lower().replace(' ', '').split('+') if p)


def is_messaging(app, title):
    a = (app or '').lower()
    return a in MESSAGING_APPS or bool(MESSAGING_TITLES.search(title or ''))


def command_is_harmless(cmd):
    """Only looks at things (Get-ChildItem, ipconfig, ... piped into Select/Sort/Format), or only
    creates a folder.  Anything with script blocks, redirection or call operators doesn't count."""
    c = str(cmd).strip()
    if not c or re.search(r'[{}<>&`]|\$\(|\binvoke-|-command\b', c, re.I):
        return False
    for seg in re.split(r'[|;\n]', c):
        words = seg.strip().lstrip('(').split()
        if not words:
            continue
        head = words[0].lower()
        if head in READ_ONLY_CMDS:
            continue
        if head in ('mkdir', 'md') or (head == 'new-item' and re.search(r"-it(emtype)?\s+['\"]?dir", seg, re.I)):
            continue
        return False
    return True


def risk(name, args, ctx=None, level='normal'):
    """None if the step can run straight away at this permission level, else why it needs your OK.
    ctx (the Hands) says which app a step targets and what an element is called."""
    level = level if level in LEVELS else 'normal'
    careful, trusted = level == 'careful', level == 'trusted'
    app, title = ctx.target(args.get('window')) if ctx is not None and hasattr(ctx, 'target') else ('', '')
    chat = is_messaging(app, title)
    where = app or 'a chat or mail app'
    if name == 'type_text':
        if careful:
            return 'Types into an app' + (' and presses Enter, which can send or submit' if args.get('enter') else '')
        return f'Presses Enter in {where}, which sends it' if args.get('enter') and chat else None
    if name == 'click':
        label = ctx.element(args.get('element')) if ctx is not None and hasattr(ctx, 'element') else ''
        if careful:
            return 'A click can change or send things'
        if MONEY.search(label):
            return 'Looks like it spends money'
        if DELETES.search(label):
            return 'Looks like it deletes something'
        if SENDS.search(label):
            return 'Looks like it sends or posts something'
        if not trusted and CHANGES.search(label):
            return 'Looks like it changes a setting or your account'
        return None
    if name == 'press_keys':
        k = norm_keys(args.get('keys', ''))
        parts = k.split('+')
        if k in SAFE_KEYS:
            return None
        if careful:
            return 'A shortcut can send, submit or delete'
        if 'delete' in parts or 'del' in parts:
            return 'Deletes something'
        if parts[-1] in ('enter', 'return') and chat:
            return f'Presses Enter in {where}, which sends it'
        if k in CLOSE_KEYS and not trusted:
            return 'Closes something; unsaved work could be lost'
        return None
    if name == 'close_window':
        return None if trusted else 'Unsaved work could be lost'
    if name == 'run_command':
        cmd = args.get('command', '')
        if careful:
            return 'Runs a command on your PC'
        if command_is_harmless(cmd) or (trusted and not DANGEROUS_CMD.search(str(cmd))):
            return None
        return 'Runs a command that changes things on your PC'
    if name == 'open_path':
        ext = os.path.splitext(str(args.get('path', '')))[1].lower()
        return 'Runs a program' if ext in RUNS_CODE and not trusted else None
    return None


def _q(s, n=70):
    s = ' '.join(str(s).split())
    return f'"{s[:n]}…"' if len(s) > n else f'"{s}"'


def describe(name, a, names=None):
    """What a step does, in plain words, for the task card and the log (built from the arguments,
    never from the model's own description).  names: optional object with window(n), element(n)."""
    win = (lambda n: names.window(n)) if names else (lambda n: f'window {n}')
    el = (lambda n: names.element(n)) if names else (lambda n: f'element {n}')
    pos = {'left': 'the left half', 'right': 'the right half', 'top-left': 'the top-left quarter',
           'top-right': 'the top-right quarter', 'bottom-left': 'the bottom-left quarter',
           'bottom-right': 'the bottom-right quarter'}
    if name == 'open_app':
        return f"Open {a.get('name', '?')}"
    if name == 'open_url':
        return f"Open {a.get('url', '?')}"
    if name == 'open_path':
        return f"Open {a.get('path', '?')}"
    if name == 'make_folder':
        return f"Create the folder {a.get('path', '?')}"
    if name == 'find_files':
        return f"Look for files named {_q(a.get('query', ''))}"
    if name == 'switch_to':
        return f"Switch to {win(a.get('window'))}"
    if name == 'arrange':
        p = a.get('position', '')
        if p in ('maximize', 'minimize', 'restore'):
            return f"{p.capitalize()} {win(a.get('window'))}"
        return f"Move {win(a.get('window'))} to {pos.get(p, p)}"
    if name == 'close_window':
        return f"Close {win(a.get('window'))}"
    if name == 'read_screen':
        return f"Look at {win(a['window']) if a.get('window') else 'the screen'}"
    if name == 'click':
        return f"{'Right-click' if a.get('button') == 'right' else 'Click'} {el(a.get('element'))}"
    if name == 'type_text':
        where = f" into {win(a['window'])}" if a.get('window') else ''
        return f"Type {_q(a.get('text', ''))}{where}" + (', then press Enter' if a.get('enter') else '')
    if name == 'press_keys':
        return f"Press {norm_keys(a.get('keys', '?'))}" + (f" in {win(a['window'])}" if a.get('window') else '')
    if name == 'run_command':
        return 'Run a PowerShell command'
    if name == 'jot':
        return f"Jot {_q(a.get('text', ''))}"
    if name == 'start_focus':
        return f"Start a {a.get('minutes', '?')} min focus session" + (f" on {a['label']}" if a.get('label') else '')
    if name == 'wait':
        return f"Wait {a.get('seconds', '?')} s"
    return name


def detail_of(name, a):
    """The exact thing you are approving, shown in full under the step."""
    if name == 'run_command':
        return str(a.get('command', ''))
    if name == 'type_text':
        return str(a.get('text', ''))
    if name == 'open_path':
        return str(a.get('path', ''))
    return ''


def parse_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or '{}')
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


# ------------------------------------------------------------------ the loop
class Agent:
    """brain: .chat(messages, tools, on_text, stop) and .vision.  hands: Hands (or a fake in tests).
    ui: plan(text, final=False), step(text) -> id, update(id, state, detail), approve(id, text, why, detail) ->
    'allow' | 'all' | 'stop', finish(kind, message).  Runs on a worker thread."""

    def __init__(self, brain, hands, ui, log=None, stop=None, max_steps=MAX_STEPS, level='normal'):
        self.brain, self.hands, self.ui = brain, hands, ui
        self.level = level
        self.log = log or (lambda line: None)
        self.stop = stop or threading.Event()
        self.max_steps = max_steps
        self.allow_all = False
        self.seen = {}            # (tool, arguments) -> how often; loops get cut off (see _step)

    def run(self, goal):
        try:
            self._run(goal)
        except Exception as e:  # never leave the card spinning
            self.log(f'- failed: {type(e).__name__}: {e}')
            self.ui.finish('failed', f'Something went wrong: {e}')
        finally:
            self.hands.close()

    def _stopped(self):
        if self.stop.is_set():
            self.log('- stopped by you')
            self.ui.finish('stopped', 'Stopped.')
            return True
        return False

    def _run(self, goal):
        self.log(f'\n## {time.strftime("%Y-%m-%d %H:%M")} · {goal}  ({self.level})')
        state, images = self.hands.observe(images=True)
        tools = ', '.join(t['function']['name'] for t in TOOLS if t['function']['name'] != 'done')
        first = {'role': 'user', 'content': f'Task: {goal}\n\n{state}' + PLAN_ASK.format(tools=tools)}
        if images:
            first['images'] = images
        msgs = [{'role': 'system', 'content': SYSTEM}, first]
        reply = self.brain.chat(msgs, tools=None, on_text=self.ui.plan, stop=self.stop)
        if self._stopped():
            return
        plan = reply.get('content', '').strip()
        self.ui.plan(plan, final=True)
        self.log('plan:\n' + '\n'.join('    ' + ln for ln in plan.splitlines() if ln.strip()))
        msgs += [{'role': 'assistant', 'content': plan}, {'role': 'user', 'content': GO}]
        for _ in range(self.max_steps):
            reply = self.brain.chat(msgs, tools=TOOLS, stop=self.stop)
            if self._stopped():
                return
            calls = reply.get('tool_calls') or []
            msgs.append(reply)
            if not calls:
                msg = reply.get('content', '').strip() or 'Done.'
                self.log(f'- done: {msg}')
                self.ui.finish('done', msg)
                return
            fresh = None
            for call in calls[:3]:
                fn = call.get('function') or {}
                name, args = fn.get('name', ''), parse_args(fn.get('arguments'))
                if name == 'done':
                    msg = str(args.get('message') or 'Done.')
                    self.log(f'- done: {msg}')
                    self.ui.finish('done', msg)
                    return
                result = self._step(name, args)
                if result is None:
                    return  # stopped
                text, imgs = result
                msgs.append({'role': 'tool', 'tool_name': name, 'content': text})
                if name in CHANGES_SCREEN or name == 'read_screen':
                    fresh = (name == 'read_screen', imgs)
            if fresh is not None:
                self._forget_old_screens(msgs)
                st, imgs = self.hands.observe(images=fresh[0])
                m = {'role': 'user', 'content': 'SCREEN NOW:\n' + st}
                if imgs:
                    m['images'] = imgs
                msgs.append(m)
        self.log('- gave up: step limit')
        self.ui.finish('done', f'I stopped after {self.max_steps} steps without finishing. '
                               'Try a more specific request.')

    def _step(self, name, args):
        if name not in TOOL_NAMES:
            self.log(f'- unknown tool {name}')
            return f'There is no tool called {name}.', None
        sig = (name, json.dumps(args, sort_keys=True))
        self.seen[sig] = self.seen.get(sig, 0) + 1
        if self.seen[sig] >= 3 and name not in REPEATABLE:
            self.log(f'- refused a third identical step: {name}')
            return ('You have already done exactly this twice in this task. Do something different, '
                    'or call done and say what is blocking you.'), None
        text = describe(name, args, self.hands)
        sid = self.ui.step(text)
        why = risk(name, args, self.hands, self.level)
        if why and not self.allow_all:
            self.ui.update(sid, 'ask')
            if hasattr(self.hands, 'preview'):
                self.hands.preview(name, args)   # the companion shows what it's asking about
            answer = self.ui.approve(sid, text, why, detail_of(name, args))
            self.log(f'- needs OK: {text}' + (f' [{detail_of(name, args)}]' if detail_of(name, args) else '')
                     + f' -> {"allowed" if answer != "stop" else "you said stop"}'
                     + (' (and everything after it)' if answer == 'all' else ''))
            if answer == 'stop' or self.stop.is_set():
                self.ui.update(sid, 'denied')
                self.stop.set()
                self._stopped()
                return None
            if answer == 'all':
                self.allow_all = True
            self.ui.update(sid, 'run')
        if self.stop.is_set():
            self._stopped()
            return None
        try:
            ok, out, imgs = self.hands.do(name, args)
        except Exception as e:
            ok, out, imgs = False, f'{type(e).__name__}: {e}', None
        self.ui.update(sid, 'ok' if ok else 'fail', '' if ok else out[:160])
        self.log(f'- {text}: {"ok" if ok else "failed"}' + ('' if ok else f' ({out[:200]})'))
        return out, imgs

    @staticmethod
    def _forget_old_screens(msgs):
        for m in msgs:
            if m.get('role') == 'user' and m.get('content', '').startswith('SCREEN NOW:'):
                m['content'] = 'SCREEN NOW: (replaced by a newer listing)'
                m.pop('images', None)
            else:
                m.pop('images', None)


# ------------------------------------------------------------------ the hands: real actions on Windows
SKIP_DIRS = {'appdata', 'node_modules', '.git', '__pycache__', '$recycle.bin', '.cache', '.venv', 'venv', '.conda',
             'site-packages', '.vscode', '.idea', 'windows', 'program files', 'program files (x86)'}


class Hands:
    """Carries out tool calls.  Lives on the agent's thread (UI Automation objects are per-thread);
    anything that must touch the UI goes through ui_call."""

    def __init__(self, app_list, own_pid, ui_call=None, point=None, grab_hidden=None, jot=None, focus=None,
                 vision=True):
        self.apps = app_list
        self.own_pid = own_pid
        self.ui_call = ui_call or (lambda fn: fn())
        self.point = point or (lambda rect: None)
        self.grab_hidden = grab_hidden or screen.grab
        self.jot_fn, self.focus_fn = jot, focus
        self.vision = vision
        self.windows, self.elements, self.el_window = {}, {}, None
        self._seer = None

    # ---------------------------------------------------------------- names for describe()
    def window(self, n):
        w = self.windows.get(_int(n))
        return f'{w.app} ("{w.title[:40]}")' if w else f'window w{n}'

    def element(self, n):
        e = self.elements.get(_int(n))
        return f'{e.role} "{e.name[:50]}"' if e else f'element {n}'

    def target(self, n=None):
        """(app, title) of the window a step acts on: window n, or the active window."""
        w = self.windows.get(_int(n)) if n else None
        w = w or winutil.foreground()
        return (w.app, w.title) if w else ('', '')

    # ---------------------------------------------------------------- where the companion goes
    # It goes where each step happens and stays there (it doesn't fly back to the task card).
    def _show_window(self, hwnd):
        if hwnd and winutil.exists(hwnd):
            l, t, r, b = winutil.frame_rect(hwnd)
            cx = (l + r) // 2
            self.point((cx - 30, t + 8, cx + 30, t + 36))   # its title bar

    def _show_focus(self, hwnd):
        """The text field that has keyboard focus, else the window."""
        rect = None
        try:
            rect = self.seer.focused_rect()
        except Exception:
            pass
        if rect and hwnd and winutil.exists(hwnd):
            fl, ft, fr, fb = winutil.frame_rect(hwnd)
            if not (fl <= (rect[0] + rect[2]) / 2 <= fr and ft <= (rect[1] + rect[3]) / 2 <= fb):
                rect = None
        if rect:
            l, t, r, b = rect
            self.point((l, t, min(r, l + 60), b))           # the start of the field
        else:
            self._show_window(hwnd)

    def preview(self, name, a):
        """Point at what a step that needs your OK would act on, while you decide."""
        try:
            if name == 'click':
                e = self.elements.get(_int(a.get('element')))
                if e is not None:
                    self.point(e.rect)
            elif name in ('type_text', 'press_keys'):
                w = self.windows.get(_int(a.get('window'))) if a.get('window') else None
                hwnd = w.hwnd if w else winutil.foreground_hwnd()
                self._show_focus(hwnd)
            elif name == 'close_window':
                w = self.windows.get(_int(a.get('window')))
                if w:
                    self._show_window(w.hwnd)
        except Exception:
            pass

    @property
    def seer(self):
        if self._seer is None:
            self._seer = screen.Seer()
        return self._seer

    def close(self):
        if self._seer is not None:
            self._seer.close()

    # ---------------------------------------------------------------- looking
    def observe(self, images=False, hwnd=None):
        wins = winutil.list_windows(exclude_pid=self.own_pid)[:15]
        self.windows = {i + 1: w for i, w in enumerate(wins)}
        fg = hwnd or winutil.foreground_hwnd()
        lines = []
        active = None
        for n, w in self.windows.items():
            mark = ''
            if w.hwnd == fg:
                active, mark = (n, w), '  <- active'
            lines.append(f'w{n}. "{w.title[:80]}" ({w.app}){mark}')
        out = 'Open windows:\n' + ('\n'.join(lines) or '(none)')
        imgs = None
        self.elements, self.el_window = {}, None
        if active is not None:
            n, w = active
            frame = winutil.frame_rect(w.hwnd)
            try:
                els = screen.select(self.seer.elements(w.hwnd), frame, None, OBSERVE_ELEMENTS)
            except Exception:
                els = []
            self.elements, self.el_window = {e.n: e for e in els}, w.hwnd
            out += f'\n\nElements of the active window w{n} ({w.app}):\n' + \
                   (screen.format_elements(els, frame, 6000) or '(this app does not list its elements)')
            if images and self.vision:
                try:
                    imgs = [screen.encode(self.grab_hidden(frame))]
                except Exception:
                    imgs = None
        else:
            out += '\n\n(No window is active.)'
        return out, imgs

    # ---------------------------------------------------------------- doing
    def do(self, name, a):
        """-> (ok, text for the model, images or None)"""
        f = getattr(self, 't_' + name)
        params = f.__code__.co_varnames[1:f.__code__.co_argcount]
        r = f(**{k: v for k, v in a.items() if k in params})
        return (r[0], r[1], r[2] if len(r) > 2 else None)

    def _win(self, n):
        w = self.windows.get(_int(n))
        if w is None or not winutil.exists(w.hwnd):
            raise ValueError(f'There is no window w{n} now. Use the numbers from the latest listing.')
        return w

    def _front(self, hwnd):
        winutil.activate(hwnd)
        if not winutil.wait_until(lambda: winutil.foreground_hwnd() == hwnd, 1.5):
            raise ValueError('Could not bring that window to the front (it may be running as administrator).')

    def t_open_app(self, name=''):
        app = apps_mod.find(name, self.apps)
        if app is None:
            return False, f'No installed app matches "{name}".'
        for w in winutil.list_windows(exclude_pid=self.own_pid):
            if w.app and apps_mod.good_match(w.app, app.name):
                winutil.activate(w.hwnd)
                time.sleep(0.4)
                self._show_window(w.hwnd)
                return True, f'{app.name} was already open; it is in front now.'
        before = {w.hwnd for w in winutil.list_windows(exclude_pid=self.own_pid)}
        apps_mod.launch(app)
        new = []
        winutil.wait_until(lambda: bool(new.extend(w for w in winutil.list_windows(exclude_pid=self.own_pid)
                                                   if w.hwnd not in before) or new), 10, 0.25)
        time.sleep(0.8)
        if new:
            self._show_window(new[0].hwnd)
            return True, f'Opened {app.name}: window "{new[0].title[:60]}".'
        return True, f'Started {app.name}; no new window yet (it may still be loading, or opened in the tray).'

    def t_open_url(self, url=''):
        u = str(url).strip()
        if re.match(r'^[\w.-]+\.[a-z]{2,}(/|$)', u, re.I):
            u = 'https://' + u
        if not re.match(r'^https?://', u, re.I):
            return False, 'Only http(s) web addresses can be opened.'
        os.startfile(u)
        time.sleep(1.5)
        self._show_window(winutil.foreground_hwnd())
        return True, f'Opened {u} in the browser.'

    def t_open_path(self, path=''):
        p = _path(path)
        if not os.path.exists(p):
            return False, f'{p} does not exist.'
        os.startfile(p)
        time.sleep(1.2)
        self._show_window(winutil.foreground_hwnd())
        return True, f'Opened {p}.'

    def t_make_folder(self, path=''):
        p = _path(path)
        if os.path.isdir(p):
            return True, f'{p} already exists.'
        if os.path.exists(p):
            return False, f'{p} exists and is a file.'
        os.makedirs(p)
        return True, f'Created {p}.'

    def t_find_files(self, query='', folder='~'):
        root = os.path.abspath(os.path.expandvars(os.path.expanduser(str(folder or '~'))))
        words = [w for w in str(query).lower().split() if w]
        if not words or not os.path.isdir(root):
            return False, 'Give some words to look for and an existing folder.'
        found, t0 = [], time.time()
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x.lower() not in SKIP_DIRS and not x.startswith('.')]
            for f in dirs + files:
                if all(w in f.lower() for w in words):
                    found.append(os.path.join(d, f))
            if len(found) >= 25 or time.time() - t0 > 4:
                break
        if not found:
            return True, f'Nothing named like "{query}" under {root}.'
        found.sort(key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)
        return True, 'Found (newest first):\n' + '\n'.join(found[:25])

    def t_switch_to(self, window=None):
        w = self._win(window)
        self._front(w.hwnd)
        self._show_window(w.hwnd)
        return True, f'{w.app} is in front.'

    def t_arrange(self, window=None, position=''):
        import gestures
        w = self._win(window)
        key = {'top-left': 'tl', 'top-right': 'tr', 'bottom-left': 'bl', 'bottom-right': 'br',
               'maximize': 'max'}.get(position, position)
        if key == 'minimize':
            winutil.minimize(w.hwnd)
            return True, f'Minimized {w.app}.'
        if key == 'restore':
            winutil.restore(w.hwnd)
            return True, f'Restored {w.app}.'
        if key == 'max':
            return winutil.maximize(w.hwnd, True), f'Maximized {w.app}.'
        rect = gestures.placement(winutil.work_area(w.hwnd), key)
        if rect is None:
            return False, f'Unknown position {position}.'
        ok = winutil.move_frame(w.hwnd, rect)
        winutil.activate(w.hwnd)
        self._show_window(w.hwnd)
        return ok, f'Moved {w.app} to {position}.' if ok else f'Could not move {w.app}.'

    def t_close_window(self, window=None):
        w = self._win(window)
        winutil.close_window(w.hwnd)
        closed = winutil.wait_until(lambda: not winutil.exists(w.hwnd), 2.5)
        return True, f'Closed {w.app}.' if closed else f'Asked {w.app} to close; it is still open (maybe a save prompt).'

    def t_read_screen(self, window=None):
        hwnd = None
        if window:
            hwnd = self._win(window).hwnd
            self._front(hwnd)
        return True, 'Here is the screen now.'

    def t_click(self, element=None, button='left'):
        e = self.elements.get(_int(element))
        if e is None or not winutil.exists(self.el_window):
            return False, f'There is no element {element} in the latest listing.'
        if winutil.foreground_hwnd() != self.el_window:
            self._front(self.el_window)
        self.point(e.rect)
        time.sleep(0.45)   # let the companion get there first, so you see what is being clicked
        x, y = e.center
        winutil.click_at(x, y, 'right' if button == 'right' else 'left')
        time.sleep(0.7)
        return True, f'Clicked {e.role} "{e.name[:60]}".'

    def t_type_text(self, text='', window=None, enter=False):
        if window:
            self._front(self._win(window).hwnd)
        fg = winutil.foreground()
        self._show_focus(fg.hwnd if fg else None)
        winutil.type_text(str(text))
        if enter:
            time.sleep(0.05)
            winutil.tap(winutil.VK_RETURN)
        time.sleep(0.6)
        return True, f'Typed into {fg.app if fg else "the active window"}' + (' and pressed Enter.' if enter else '.')

    def t_press_keys(self, keys='', window=None):
        vks = winutil.parse_keys(str(keys))
        if not vks:
            return False, f'"{keys}" is not a key combination I know (use e.g. ctrl+t, enter, alt+left, f5).'
        if window:
            self._front(self._win(window).hwnd)
        self._show_focus(winutil.foreground_hwnd())
        winutil.chord(*vks)
        time.sleep(0.6)
        return True, f'Pressed {norm_keys(keys)}.'

    def t_run_command(self, command=''):
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', str(command)],
                               capture_output=True, text=True, timeout=60, cwd=os.path.expanduser('~'),
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            return False, 'The command was still running after 60 s and was stopped.'
        out = (r.stdout or '') + (('\n[stderr]\n' + r.stderr) if r.stderr.strip() else '')
        out = out.strip()[:3000] or '(no output)'
        return r.returncode == 0, f'Exit code {r.returncode}.\n{out}'

    def t_jot(self, text=''):
        if not self.jot_fn:
            return False, 'jot is not set up.'
        return self.ui_call(lambda: self.jot_fn(str(text)))

    def t_start_focus(self, minutes=25, label=''):
        if not self.focus_fn:
            return False, 'Focus mode is not available.'
        m = max(1, min(12 * 60, _int(minutes) or 25))
        self.ui_call(lambda: self.focus_fn(m, str(label or '')))
        return True, f'Started a {m} min focus session.'

    def t_wait(self, seconds=2):
        try:
            s = max(0.0, min(10.0, float(seconds)))
        except (TypeError, ValueError):
            s = 2.0
        time.sleep(s)
        return True, f'Waited {s:g} s.'


def _int(v):
    try:
        return int(str(v).lstrip('wWeE#'))
    except (TypeError, ValueError):
        return None


def _path(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(p).strip().strip('"'))))
