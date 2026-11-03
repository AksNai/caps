"""Plan a term's class schedule from what's on screen.

    "build my schedule, no classes before 10, fridays off"

1. Look: every page showing on screen (a browser split view counts as two), as a picture (taken
   with PrintWindow, so a window behind others works too) and as text (UI Automation, including
   rows scrolled out of view).  Background tabs are never read.
2. Read the plan (vision model): which term, which courses (a degree-plan spreadsheet such as a
   Google Sheet is drawn on a canvas, so the picture is what can be read), plus your preferences.
3. Read the sections (text): every section the registration site (Workday) shows for those courses,
   with its meeting pattern copied exactly as written.
4. Solve (schedule.py, exact): every combination with no overlaps, ranked by your preferences.

Runs on a worker thread; progress(text) reports each stage.
"""
import datetime as dt
import json
import re

import schedule
import screen
import winutil

PLAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'term': {'type': 'string'},
        'courses': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'code': {'type': 'string'}, 'title': {'type': 'string'}, 'credits': {'type': 'number'},
            'note': {'type': 'string'}}, 'required': ['code']}},
        'prefs': {'type': 'object', 'properties': {
            'earliest': {'type': 'string'}, 'latest': {'type': 'string'},
            'days_off': {'type': 'array', 'items': {'type': 'string'}},
            'lunch': {'type': 'array', 'items': {'type': 'string'}}}},
        'choices': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'slot': {'type': 'string'}, 'courses': {'type': 'array', 'items': {'type': 'string'}}}}},
    },
    'required': ['term', 'courses'],
}
SECTIONS_SCHEMA = {
    'type': 'object',
    'properties': {'sections': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'course': {'type': 'string'}, 'section': {'type': 'string'}, 'kind': {'type': 'string'},
        'title': {'type': 'string'}, 'patterns': {'type': 'array', 'items': {'type': 'string'}},
        'dates': {'type': 'string'}, 'status': {'type': 'string'}, 'instructor': {'type': 'string'},
        'location': {'type': 'string'}, 'credits': {'type': 'number'}}, 'required': ['course', 'patterns']}}},
    'required': ['sections'],
}
PLAN_PROMPT = """The student asked: "{request}"
Today is {today}. The pictures show what is on their screen: a degree plan or a list of courses, and maybe a course registration site.

1. term: the term they are planning, as written on the plan. Unless they named one, it is the next term they would register for after today.
2. courses: every course planned for that term, in order, exactly as written (code, title, credits). Keep placeholders such as "Math Technical Elective" or "PRV 20X" as they are, with note "placeholder".
3. prefs: preferences from their request only, with 24-hour times: earliest (first class may start), latest (last class may end), days_off (day names), lunch [start, end]. Leave out anything they didn't ask for.
4. choices: if they said which course should fill a placeholder, {{"slot": placeholder, "courses": [codes]}}.
Reply with JSON."""
SECTIONS_PROMPT = """Below is the text of a course registration page (for example Workday's Find Course Sections). List every course section on it for these courses: {wanted}.

A code with X in it is a placeholder: "PRV 20X" means any PRV course numbered 200-209, "MA 3XX" any MA course numbered 300-399.

For each section give: course (its real code exactly as on the page, like "MA 234" or "PRV 203", never a placeholder), section (like "A" or "LB1"), kind (Lecture, Lab, Recitation...), title, patterns (each meeting pattern copied exactly as written on the page: days and times, plus its dates if shown), dates (start - end of the section if shown), status (Open, Closed, Waitlist), instructor, location, credits.
Only list what is on the page. If a section has no meeting time (online, TBA), give patterns as [].

PAGE TEXT:
{text}"""


class Look:
    """What is on screen, read without bringing anything forward."""

    def __init__(self):
        self.pages = []       # [{'title', 'url', 'rect', 'text', 'image'}]


def visible_windows(own_pid, pointer=None):
    """Top-level windows you can actually see right now, front first (sampled across the monitor)."""
    px, py = pointer or winutil.cursor_pos()
    mon, _ = winutil.monitor_at(px, py)
    l, t, r, b = mon
    seen = []
    for i in range(1, 12):
        for j in range(1, 8):
            h = winutil.window_at(l + (r - l) * i // 12, t + (b - t) * j // 8)
            if h and h not in seen and winutil.pid_of(h) != own_pid:
                seen.append(h)
    return seen


def look(own_pid, pointer=None, hwnds=None):
    """Pictures and text of every page on screen (both halves of a split view)."""
    out = Look()
    seer = screen.Seer()
    try:
        for hwnd in (hwnds or visible_windows(own_pid, pointer))[:4]:
            try:
                img, (wl, wt, _, _) = screen.capture_window(hwnd)
            except Exception:
                continue
            try:
                pages = seer.pages(hwnd)
            except Exception:
                pages = []
            if not pages:   # not a browser: the whole window
                w = winutil.win_info(hwnd)
                fl, ft, fr, fb = winutil.frame_rect(hwnd)
                try:
                    text = seer.text(seer.uia.ElementFromHandle(hwnd), 20000)
                except Exception:
                    text = ''
                out.pages.append({'title': w.title if w else '', 'url': '', 'rect': (fl, ft, fr, fb), 'text': text,
                                  'image': img.crop((fl - wl, ft - wt, fr - wl, fb - wt))})
                continue
            for title, url, rect, el in pages:
                try:
                    text = seer.text(el)
                except Exception:
                    text = ''
                l, t, r, b = rect
                out.pages.append({'title': title, 'url': url, 'rect': rect, 'text': text,
                                  'image': img.crop((l - wl, t - wt, r - wl, b - wt))})
    finally:
        seer.close()
    return out


def page_image(img):
    """Half-size JPEG: a spreadsheet stays readable (checked) and the model sees the whole page."""
    w, h = img.size
    s = min(1.0, 1100 / max(w, h)) if max(w, h) > 1100 else 1.0
    if w > 1500:
        s = min(s, 0.5) if s < 1 else 0.5
    return screen.encode(img, None, max_side=max(1, int(max(w, h) * s)))


def read_plan(brain, request, pages, stop=None):
    msg = {'role': 'user', 'content': PLAN_PROMPT.format(request=request, today=dt.date.today().strftime('%B %d, %Y'))
           + '\n\nPages: ' + '; '.join(f'{i + 1}. {p["title"] or p["url"]}' for i, p in enumerate(pages)),
           'images': [page_image(p['image']) for p in pages]}
    reply = brain.chat([msg], fmt=PLAN_SCHEMA, stop=stop, think=False)
    try:
        plan = json.loads(reply.get('content') or '{}')
    except ValueError:
        plan = {}
    plan.setdefault('courses', [])
    return plan


def wanted_text(plan):
    parts = []
    for c in plan.get('courses', []):
        code = c.get('code', '')
        if re.match(r'[A-Za-z]{2,5}\s*\d', code):
            parts.append(code)
    for ch in plan.get('choices') or []:
        parts += ch.get('courses') or []
    return ', '.join(dict.fromkeys(parts)) or 'all courses'


def read_sections(brain, plan, pages, progress=None, stop=None):
    """Sections for the planned courses from every page's text (the registration site's page)."""
    wanted = wanted_text(plan)
    codes = [schedule.norm_code(w) for w in wanted.split(', ')]
    prefixes = {c.split()[0] for c in codes if ' ' in c}
    rows = []
    for p in pages:
        text = p['text']
        # only pages that mention a planned course (by code) are worth reading
        if not text or not any(re.search(re.escape(c).replace(r'\ ', r'[\s\-]*'), text) for c in codes) \
                and not any(re.search(rf'\b{pre}[\s\-]*\d', text) for pre in prefixes):
            continue
        if progress:
            progress(f'Reading sections from {p["title"] or "the page"}')
        found = [0]

        def on_text(t, found=found):
            n = t.count('"course"')
            if n != found[0] and progress:
                found[0] = n
                progress(f'Reading sections: {n} found')

        reply = brain.chat([{'role': 'user', 'content': SECTIONS_PROMPT.format(wanted=wanted, text=text)}],
                           fmt=SECTIONS_SCHEMA, on_text=on_text, stop=stop, think=False)
        try:
            rows += json.loads(reply.get('content') or '{}').get('sections', [])
        except ValueError:
            pass
    return schedule.from_model(rows)


class Result:
    def __init__(self):
        self.term, self.plan, self.sections, self.items, self.missing = '', {}, [], [], []
        self.no_time = []     # sections with no meeting time (online/TBA): always fit
        self.options = []     # [schedule.Choice], best first
        self.prefs = schedule.Prefs()
        self.term_dates = ('', '')
        self.error = ''


def plan_schedule(brain, request, own_pid, pointer=None, progress=None, stop=None, hwnds=None):
    progress = progress or (lambda s: None)
    res = Result()
    progress('Looking at your screen')
    pages = look(own_pid, pointer, hwnds).pages
    if not pages:
        res.error = 'Nothing on screen to read. Open your plan and your registration site side by side.'
        return res
    progress('Reading your plan')
    res.plan = read_plan(brain, request, pages, stop)
    res.term = res.plan.get('term', '')
    res.prefs = schedule.Prefs.from_dict(res.plan.get('prefs'))
    if not res.plan['courses']:
        res.error = "I couldn't find a list of courses on screen."
        return res
    progress(f'{len(res.plan["courses"])} courses planned for {res.term or "next term"}. Reading sections')
    res.sections = read_sections(brain, res.plan, pages, progress, stop)
    choices = {c.get('slot', ''): c.get('courses') or [] for c in res.plan.get('choices') or []}
    res.items, res.missing = schedule.build_items(res.plan['courses'], res.sections, choices)
    if not res.items:
        res.error = ('No class times on screen for those courses. In Workday, open Find Course Sections for '
                     f'{res.term or "that term"}, search your courses, then ask again.')
        return res
    # sections with no set time don't take part in clash checks but still count as chosen
    res.no_time = [s for s in res.sections if not s.meetings]
    progress('Finding every schedule without overlaps')
    res.options = schedule.solve(res.items, res.prefs)
    dates = sorted(m.start_date for s in res.sections for m in s.meetings if m.start_date)
    ends = sorted(m.end_date for s in res.sections for m in s.meetings if m.end_date)
    res.term_dates = (dates[0] if dates else '', ends[-1] if ends else '')
    if not res.options:
        res.error = 'Every combination of those sections overlaps somewhere.'
    return res
