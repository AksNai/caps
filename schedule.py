"""Course schedules: pure logic, no Windows, no model.

The model only *reads* (which courses you plan to take, what sections Workday lists); everything
that has a right answer is done here, exactly:

  parse_pattern("Mon/Wed | 10:00 AM - 11:15 AM")   -> Meeting(days='MW', start=600, end=675)
  build_items(plan, sections)                       -> what has to be scheduled, with every option
  solve(items, prefs)                               -> every schedule without overlaps, best first
  to_ics(choice, ...) / summary(choice)             -> a calendar file and a list to register with

A planned item is a course you must take (MA 234) or a slot with choices (Math Technical
Elective, PRV 20X).  For each item one course is chosen, and for that course one section of each
component (lecture, lab, recitation...).  Two meetings clash when they share a day, their times
overlap and their date ranges overlap (so two half-semester courses can share a time slot).
"""
import datetime as dt
import itertools
import re
from dataclasses import dataclass, field

DAYS = 'MTWRFSU'                       # Mon Tue Wed thuR Fri Sat sUn
DAY_NAMES = {'M': 'Mon', 'T': 'Tue', 'W': 'Wed', 'R': 'Thu', 'F': 'Fri', 'S': 'Sat', 'U': 'Sun'}
_DAY_WORDS = [('monday', 'M'), ('mon', 'M'), ('tuesday', 'T'), ('tues', 'T'), ('tue', 'T'), ('wednesday', 'W'),
              ('wed', 'W'), ('thursday', 'R'), ('thurs', 'R'), ('thur', 'R'), ('thu', 'R'), ('th', 'R'),
              ('friday', 'F'), ('fri', 'F'), ('saturday', 'S'), ('sat', 'S'), ('sunday', 'U'), ('sun', 'U')]
_TIME = r'(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?'


def parse_time(s):
    """'10:00 AM' '1:15pm' '13:15' '9' -> minutes after midnight, or None."""
    m = re.fullmatch(r'\s*' + _TIME + r'\s*', str(s), re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or '').lower()
    if ap == 'p' and h != 12:
        h += 12
    elif ap == 'a' and h == 12:
        h = 0
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_days(s):
    """'Mon/Wed/Fri' 'MWF' 'TTh' 'Tuesday, Thursday' 'TR' -> 'MWF' / 'TR' (canonical order)."""
    t = str(s).strip().lower()
    found = set()
    compact = t.replace(' ', '')
    if re.fullmatch(r'[mtwrfsu]+|(?:m|tu?|w|th|r|f|sa?|su)+', compact) and compact not in dict(_DAY_WORDS):
        compact = t.replace(' ', '')
        i = 0
        while i < len(compact):
            two = compact[i:i + 2]
            if two in ('th', 'tu', 'sa', 'su'):
                found.add({'th': 'R', 'tu': 'T', 'sa': 'S', 'su': 'U'}[two])
                i += 2
                continue
            found.add({'m': 'M', 't': 'T', 'w': 'W', 'r': 'R', 'f': 'F', 's': 'S', 'u': 'U'}[compact[i]])
            i += 1
    else:
        for word in re.findall(r'[a-z]+', t):
            for name, letter in _DAY_WORDS:
                if word == name:
                    found.add(letter)
                    break
    return ''.join(d for d in DAYS if d in found)


@dataclass
class Meeting:
    days: str            # 'MW'
    start: int           # minutes after midnight
    end: int
    start_date: str = ''  # 'YYYY-MM-DD' (half-semester sections), '' if unknown
    end_date: str = ''

    def clashes(self, o):
        if not set(self.days) & set(o.days) or not (self.start < o.end and o.start < self.end):
            return False
        if self.start_date and self.end_date and o.start_date and o.end_date:
            return self.start_date <= o.end_date and o.start_date <= self.end_date
        return True

    def label(self):
        return f"{'/'.join(DAY_NAMES[d] for d in self.days)} {fmt_time(self.start)}–{fmt_time(self.end)}"


def fmt_time(m):
    h, mi = divmod(m, 60)
    return f"{(h - 1) % 12 + 1}:{mi:02d}{'am' if h < 12 else 'pm'}"


def parse_date(s):
    """'01/19/2027' '2027-01-19' 'Jan 19, 2027' -> '2027-01-19', else ''."""
    s = str(s or '').strip()
    for f in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y', '%b %d, %Y', '%B %d, %Y'):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_pattern(text, start_date='', end_date=''):
    """One Workday-style meeting pattern -> Meeting, or None (online / TBA / unreadable).
    'Mon/Wed | 10:00 AM - 11:15 AM', 'TR 2:00-3:15 PM', 'Tuesday 6:15 PM - 8:45 PM | 01/19/2027 - 03/05/2027'."""
    t = str(text)
    dates = re.findall(r'\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}', t)
    if len(dates) >= 2:
        start_date, end_date = start_date or parse_date(dates[0]), end_date or parse_date(dates[1])
    no_dates = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}', ' ', t)
    m = re.search(_TIME + r'\s*(?:-|–|—|to)\s*' + _TIME, no_dates, re.I)
    if not m:
        return None
    a = f"{m.group(1)}:{m.group(2) or '00'}"
    b = f"{m.group(4)}:{m.group(5) or '00'}"
    ap1, ap2 = (m.group(3) or '').lower(), (m.group(6) or '').lower()
    if not ap1 and ap2:   # '2:00-3:15 PM': the first time shares the second's half of the day unless that breaks it
        s_pm, e = parse_time(a + ap2 + 'm'), parse_time(b + ap2 + 'm')
        start = s_pm if s_pm is not None and s_pm < e else parse_time(a + 'am')
        end = e
    else:
        start, end = parse_time(a + (ap1 + 'm' if ap1 else '')), parse_time(b + (ap2 + 'm' if ap2 else ''))
        if start is not None and end is not None and not ap1 and not ap2 and start < 7 * 60:
            start, end = start + 12 * 60, end + 12 * 60   # '2:00-3:15' with no am/pm: afternoon
    days = parse_days(no_dates[:m.start()]) or parse_days(no_dates[m.end():])
    if start is None or end is None or not days or end <= start:
        return None
    return Meeting(days, start, end, parse_date(start_date), parse_date(end_date))


@dataclass
class Section:
    course: str                    # 'MA 234'
    section: str                   # 'A', 'LB1'
    kind: str = 'Lecture'          # Lecture / Lab / Recitation / ...
    meetings: list = field(default_factory=list)
    title: str = ''
    instructor: str = ''
    status: str = ''               # Open / Closed / Waitlist
    location: str = ''
    credits: float = 0

    @property
    def closed(self):
        return bool(re.search(r'closed|full|cancel', self.status, re.I))

    def label(self):
        return f'{self.course}-{self.section}' if self.section else self.course


def norm_code(s):
    """'ma234' 'MA-234' 'MA 234 - Complex Variables' -> 'MA 234'."""
    m = re.match(r'\s*([A-Za-z]{2,5})[\s\-]*(\d{2,4}[A-Za-z]?)', str(s))
    return f'{m.group(1).upper()} {m.group(2).upper()}' if m else str(s).strip()


def kind_of(s):
    k = str(s or '').lower()
    for word, name in (('lab', 'Lab'), ('recit', 'Recitation'), ('disc', 'Discussion'), ('sem', 'Seminar'),
                       ('studio', 'Studio'), ('tut', 'Tutorial')):
        if word in k:
            return name
    return 'Lecture'


@dataclass
class Item:
    """Something on your plan: one course, or a slot that any of several courses can fill."""
    name: str                       # 'MA 234' or 'Math Technical Elective'
    options: dict                   # course code -> {kind: [Section, ...]}
    credits: float = 0


def wildcard(code):
    """'PRV 20X' -> regex for PRV 200-209; 'MA 3XX' -> MA 300-399."""
    m = re.fullmatch(r'([A-Z]{2,5}) (\d*)([Xx]+)', code)
    if not m:
        return None
    return re.compile(rf'{m.group(1)} {m.group(2)}' + r'\d' * len(m.group(3)) + '$')


def build_items(plan, sections, choices=None):
    """plan: [{'code', 'title', 'credits'}] in order.  sections: [Section].  choices: optional
    {item name: [course codes]} for slots like 'Math Technical Elective' (from the model or you).
    Returns (items, missing): items that have sections, names of planned courses with none."""
    by_course = {}
    for s in sections:
        by_course.setdefault(s.course, {}).setdefault(s.kind, []).append(s)
    items, missing = [], []
    for p in plan:
        name = p.get('code') or p.get('title') or '?'
        code = norm_code(name)
        if code in by_course:
            opts = {code: by_course[code]}
        else:
            pat = wildcard(code)
            allowed = [norm_code(c) for c in (choices or {}).get(name, [])]
            opts = {c: k for c, k in by_course.items() if (pat and pat.match(c)) or c in allowed}
        if opts:
            items.append(Item(name, opts, float(p.get('credits') or 0)))
        else:
            missing.append(name)
    # a course chosen to fill a slot can't also be a course you listed on its own
    listed = {c for it in items if len(it.options) == 1 for c in it.options}
    for it in items:
        if len(it.options) > 1:
            it.options = {c: k for c, k in it.options.items() if c not in listed} or it.options
    return items, missing


@dataclass
class Prefs:
    earliest: int = None            # minutes; classes starting earlier are penalised
    latest: int = None
    days_off: str = ''              # 'F'
    lunch: tuple = None             # (start, end) minutes kept free if possible
    allow_closed: bool = False

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        lunch = d.get('lunch')
        return cls(earliest=parse_time(d['earliest']) if d.get('earliest') else None,
                   latest=parse_time(d['latest']) if d.get('latest') else None,
                   days_off=parse_days(' '.join(d.get('days_off') or [])) if isinstance(d.get('days_off'), list)
                   else parse_days(d.get('days_off') or ''),
                   lunch=(parse_time(lunch[0]), parse_time(lunch[1])) if lunch and len(lunch) == 2 else None,
                   allow_closed=bool(d.get('allow_closed')))


@dataclass
class Choice:
    picks: list                     # [(item name, Section)]
    score: float
    notes: list

    @property
    def meetings(self):
        return [(name, s, m) for name, s in self.picks for m in s.meetings]


def score(picks, prefs):
    """Lower is better.  Each preference that a meeting breaks costs points; so do gaps and days on campus."""
    pts, notes = 0.0, []
    meetings = [m for _, s in picks for m in s.meetings]
    if prefs.earliest is not None:
        n = sum(len(m.days) for m in meetings if m.start < prefs.earliest)
        pts += 10 * n
        if n:
            notes.append(f'{n} class{"es" if n > 1 else ""} before {fmt_time(prefs.earliest)}')
    if prefs.latest is not None:
        n = sum(len(m.days) for m in meetings if m.end > prefs.latest)
        pts += 10 * n
        if n:
            notes.append(f'{n} class{"es" if n > 1 else ""} after {fmt_time(prefs.latest)}')
    if prefs.days_off:
        n = sum(1 for m in meetings for d in m.days if d in prefs.days_off)
        pts += 30 * n
        if n:
            notes.append('class on ' + '/'.join(DAY_NAMES[d] for d in prefs.days_off))
    if prefs.lunch:
        a, b = prefs.lunch
        n = sum(len(m.days) for m in meetings if m.start < b and a < m.end)
        pts += 4 * n
    closed = [s.label() for _, s in picks if s.closed]
    if closed:
        pts += 50 * len(closed)
        notes.append('full: ' + ', '.join(closed))
    by_day = {}
    for m in meetings:
        for d in m.days:
            by_day.setdefault(d, []).append((m.start, m.end))
    gaps = 0
    for spans in by_day.values():
        spans.sort()
        gaps += sum(max(0, b0 - a1) for (_, a1), (b0, _) in zip(spans, spans[1:]))
    pts += gaps / 60 + 2 * len(by_day)
    return pts, notes


def solve(items, prefs=None, limit=20000):
    """Every combination with no overlaps (up to `limit`), best first."""
    prefs = prefs or Prefs()
    slots = []   # per item: list of alternatives; an alternative = list of Sections (one per component)
    for it in items:
        alts = []
        for course, kinds in it.options.items():
            comps = []
            for kind, secs in kinds.items():
                usable = [s for s in secs if prefs.allow_closed or not s.closed] or secs
                comps.append(usable)
            for combo in itertools.product(*comps):
                if not any(a.clashes(b) for x, y in itertools.combinations(combo, 2)
                           for a in x.meetings for b in y.meetings):
                    alts.append(list(combo))
        slots.append((it.name, alts))
    slots.sort(key=lambda s: len(s[1]))   # most constrained first
    found = []

    def go(i, chosen, meetings):
        if len(found) >= limit:
            return
        if i == len(slots):
            pts, notes = score(chosen, prefs)
            found.append(Choice(list(chosen), pts, notes))
            return
        name, alts = slots[i]
        for alt in alts:
            ms = [m for s in alt for m in s.meetings]
            if any(a.clashes(b) for a in ms for b in meetings):
                continue
            go(i + 1, chosen + [(name, s) for s in alt], meetings + ms)

    go(0, [], [])
    order = [n for n, _ in sorted(((it.name, k) for k, it in enumerate(items)), key=lambda t: t[1])]
    for c in found:
        c.picks.sort(key=lambda p: order.index(p[0]) if p[0] in order else 99)
    found.sort(key=lambda c: c.score)
    return found


def summary(choice, term=''):
    """Plain text to keep or paste while registering."""
    lines = [f'{term} schedule'.strip() + ':']
    for name, s in choice.picks:
        when = '; '.join(m.label() for m in s.meetings) or 'no set meeting time'
        slot = f'  (for {name})' if norm_code(name) != s.course else ''
        lines.append(f'  {s.label():<14} {s.kind:<10} {when}{slot}')
    return '\n'.join(lines)


ICS_DAYS = {'M': 'MO', 'T': 'TU', 'W': 'WE', 'R': 'TH', 'F': 'FR', 'S': 'SA', 'U': 'SU'}


def to_ics(choice, term_start, term_end, term=''):
    """A calendar file: one weekly repeating event per meeting, from the first matching day on or
    after the start date until the end date (each section's own dates when it has them)."""
    out = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Caps//schedule//EN', 'CALSCALE:GREGORIAN']
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for k, (name, s, m) in enumerate(choice.meetings):
        first = dt.date.fromisoformat(m.start_date or term_start)
        last = dt.date.fromisoformat(m.end_date or term_end)
        while 'MTWRFSU'[first.weekday()] not in m.days:
            first += dt.timedelta(days=1)
        def t(mins, day=first):
            return f'{day:%Y%m%d}T{mins // 60:02d}{mins % 60:02d}00'
        out += ['BEGIN:VEVENT', f'UID:caps-{stamp}-{k}@caps', f'DTSTAMP:{stamp}',
                f'SUMMARY:{s.label()} {s.title or ""} ({s.kind})'.replace('  ', ' '),
                f'DTSTART:{t(m.start)}', f'DTEND:{t(m.end)}',
                f"RRULE:FREQ=WEEKLY;BYDAY={','.join(ICS_DAYS[d] for d in m.days)};UNTIL={last:%Y%m%d}T235959",
                f'LOCATION:{s.location}', f'DESCRIPTION:{term} · {name} · {s.instructor}', 'END:VEVENT']
    out.append('END:VCALENDAR')
    return '\r\n'.join(out) + '\r\n'


def from_model(rows):
    """Sections the model read off the page -> [Section].  Each row: {course, section, kind, title,
    pattern (the text as shown) or meetings [{days, start, end}], dates, status, instructor, location}."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get('course'):
            continue
        dates = re.findall(r'\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}', str(r.get('dates') or ''))
        sd, ed = (dates + ['', ''])[:2]
        meetings = []
        pats = r.get('patterns') or ([r['pattern']] if r.get('pattern') else [])
        for p in pats:
            m = parse_pattern(p, sd, ed)
            if m:
                meetings.append(m)
        if not meetings:
            for mm in r.get('meetings') or []:
                try:
                    a, b = parse_time(mm.get('start')), parse_time(mm.get('end'))
                    days = parse_days(mm.get('days', ''))
                except AttributeError:
                    continue
                if a is not None and b is not None and days and b > a:
                    meetings.append(Meeting(days, a, b, parse_date(sd), parse_date(ed)))
        try:
            credits = float(r.get('credits') or 0)
        except (TypeError, ValueError):
            credits = 0
        out.append(Section(norm_code(r['course']), str(r.get('section') or '').strip(), kind_of(r.get('kind')),
                           meetings, str(r.get('title') or ''), str(r.get('instructor') or ''),
                           str(r.get('status') or ''), str(r.get('location') or ''), credits))
    # the same section listed twice (page text repeats): keep one
    seen, uniq = set(), []
    for s in out:
        key = (s.course, s.section, s.kind, tuple((m.days, m.start, m.end) for m in s.meetings))
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq
