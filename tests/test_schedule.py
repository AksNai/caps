"""Class schedules: reading meeting patterns, building the plan, the solver, the calendar file."""
import itertools
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import commands  # noqa: E402
import planner  # noqa: E402
import schedule as S  # noqa: E402


def test_meeting_patterns_as_workday_writes_them():
    m = S.parse_pattern('Mon/Wed | 10:00 AM - 11:15 AM')
    assert (m.days, m.start, m.end) == ('MW', 600, 675)
    m = S.parse_pattern('Tuesday 6:15 PM - 8:45 PM | 01/19/2027 - 03/05/2027')
    assert (m.days, m.start, m.end, m.start_date, m.end_date) == ('T', 1095, 1245, '2027-01-19', '2027-03-05')
    assert S.parse_pattern('TR 2:00-3:15 PM').label() == 'Tue/Thu 2:00pm–3:15pm'
    assert S.parse_pattern('MWF 9:00-9:50').label() == 'Mon/Wed/Fri 9:00am–9:50am'
    assert S.parse_pattern('1:00 PM - 2:15 PM Mon/Wed').days == 'MW'
    assert S.parse_pattern('Thu | 12:00 PM - 1:50 PM').start == 720
    assert S.parse_pattern('Online - Asynchronous') is None and S.parse_pattern('TBA') is None


def test_day_names():
    for text, days in [('Mon/Wed/Fri', 'MWF'), ('TTh', 'TR'), ('Tuesday, Thursday', 'TR'), ('MTWRF', 'MTWRF'),
                       ('Thu', 'R'), ('Sat', 'S'), ('M W', 'MW')]:
        assert S.parse_days(text) == days, text


def sec(code, s, pattern, kind='Lecture', status='Open', dates=''):
    return S.from_model([{'course': code, 'section': s, 'kind': kind, 'patterns': [pattern] if pattern else [],
                          'status': status, 'dates': dates}])[0]


def test_half_semester_sections_can_share_a_slot():
    a = sec('MA 398', 'A', 'Tue | 2:00 PM - 4:30 PM', dates='01/19/2027 - 03/05/2027')
    b = sec('MA 499', 'A', 'Tue | 3:00 PM - 4:00 PM', dates='03/15/2027 - 05/05/2027')
    c = sec('MA 410', 'A', 'Tue/Thu | 3:00 PM - 4:15 PM', dates='01/19/2027 - 05/05/2027')
    assert not a.meetings[0].clashes(b.meetings[0])     # different halves of the term
    assert a.meetings[0].clashes(c.meetings[0]) and b.meetings[0].clashes(c.meetings[0])


def test_build_items_matches_placeholders_and_reports_missing():
    sections = [sec('PRV 201', 'A', 'Wed | 4:00 PM - 4:50 PM'), sec('PRV 203', 'A', 'Mon | 12:30 PM - 1:20 PM'),
                sec('MA 234', 'A', 'Mon/Wed | 9:30 AM - 10:45 AM'), sec('MA 441', 'A', 'Mon/Wed | 12:30 PM - 1:45 PM'),
                sec('MA 410', 'A', 'Tue/Thu | 2:00 PM - 3:15 PM')]
    plan = [{'code': 'MA 234'}, {'code': 'PRV 20X'}, {'code': 'Math Technical Elective'}, {'code': 'QF 102'}]
    items, missing = S.build_items(plan, sections, {'Math Technical Elective': ['MA 410', 'ma441']})
    assert [(i.name, sorted(i.options)) for i in items] == [('MA 234', ['MA 234']), ('PRV 20X', ['PRV 201', 'PRV 203']),
                                                           ('Math Technical Elective', ['MA 410', 'MA 441'])]
    assert missing == ['QF 102']


def brute(items, prefs):
    """Every combination, checked the slow way."""
    alts = []
    for it in items:
        a = []
        for course, kinds in it.options.items():
            for combo in itertools.product(*[[s for s in v if not s.closed] or v for v in kinds.values()]):
                a.append(list(combo))
        alts.append(a)
    good = 0
    for pick in itertools.product(*alts):
        ms = [m for alt in pick for s in alt for m in s.meetings]
        if not any(x.clashes(y) for x, y in itertools.combinations(ms, 2)):
            good += 1
    return good


def test_solver_finds_exactly_the_schedules_without_overlaps():
    sections = [sec('MA 234', 'A', 'Mon/Wed | 9:30 AM - 10:45 AM'), sec('MA 234', 'B', 'Tue/Thu | 12:30 PM - 1:45 PM'),
                sec('MA 240', 'A', 'Tue/Thu | 11:00 AM - 12:15 PM'),
                sec('MA 240', 'B', 'Mon/Wed | 2:00 PM - 3:15 PM', status='Closed'),
                sec('MA 331', 'A', 'Mon/Wed | 11:00 AM - 12:15 PM'),
                sec('MA 331', 'RA', 'Fri | 10:00 AM - 10:50 AM', kind='Recitation'),
                sec('MA 331', 'RB', 'Thu | 3:30 PM - 4:20 PM', kind='Recitation'),
                sec('QF 102', 'A', 'Mon/Wed | 9:00 AM - 10:15 AM'), sec('QF 102', 'B', 'Tue/Thu | 9:30 AM - 10:45 AM'),
                sec('QF 102', 'C', 'Mon/Wed | 3:30 PM - 4:45 PM'), sec('QF 103', 'A', '')]
    plan = [{'code': c} for c in ('MA 234', 'MA 240', 'MA 331', 'QF 102', 'QF 103')]
    items, missing = S.build_items(plan, sections)
    prefs = S.Prefs.from_dict({'earliest': '10:00', 'days_off': ['Friday']})
    opts = S.solve(items, prefs)
    assert len(opts) == brute(items, prefs) > 0 and not missing
    for o in opts:
        ms = [m for _, s, m in o.meetings]
        assert not any(x.clashes(y) for x, y in itertools.combinations(ms, 2))
        assert all(not s.closed for _, s in o.picks)            # MA 240-B is full: never picked
        assert {s.course for _, s in o.picks} == {'MA 234', 'MA 240', 'MA 331', 'QF 102', 'QF 103'}
        assert sum(1 for _, s in o.picks if s.course == 'MA 331') == 2   # lecture and recitation
    best = opts[0]
    assert not any('F' in m.days for _, _, m in best.meetings)  # Fridays off when possible
    assert [o.score for o in opts] == sorted(o.score for o in opts)


def test_preferences_and_summary():
    p = S.Prefs.from_dict({'earliest': '10:00', 'latest': '17:00', 'days_off': ['Fri', 'Monday'], 'lunch': ['12:00', '13:00']})
    assert (p.earliest, p.latest, p.days_off, p.lunch) == (600, 1020, 'MF', (720, 780))
    a = sec('MA 234', 'A', 'Mon/Wed | 9:30 AM - 10:45 AM')
    pts, notes = S.score([('MA 234', a)], p)
    assert '2 classes before 10:00am' in notes and 'class on Mon/Fri' in notes
    text = S.summary(S.Choice([('MA 234', a)], pts, notes), 'Spring 2027')
    assert text.splitlines()[1].split() == ['MA', '234-A', 'Lecture', 'Mon/Wed', '9:30am–10:45am']


def test_calendar_file():
    a = sec('MA 234', 'A', 'Mon/Wed | 9:30 AM - 10:45 AM')
    h = sec('MA 398', 'A', 'Tue | 2:00 PM - 4:30 PM', dates='01/19/2027 - 03/05/2027')
    ics = S.to_ics(S.Choice([('MA 234', a), ('MA 398', h)], 0, []), '2027-01-19', '2027-05-05', 'Spring 2027')
    assert ics.count('BEGIN:VEVENT') == 2
    assert 'DTSTART:20270120T093000' in ics            # first Monday/Wednesday on or after Tue Jan 19
    assert 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20270505T235959' in ics
    assert 'DTSTART:20270119T140000' in ics and 'UNTIL=20270305T235959' in ics   # its own half-term dates


def test_model_rows_are_cleaned_up():
    rows = [{'course': 'ma234', 'section': 'A', 'kind': 'LEC', 'patterns': ['MW 10:00-11:15 AM']},
            {'course': 'MA 234', 'section': 'A', 'kind': 'Lecture', 'patterns': ['Mon/Wed | 10:00 AM - 11:15 AM']},
            {'course': 'MA 331', 'section': 'RB', 'kind': 'Recitation',
             'meetings': [{'days': 'Thu', 'start': '15:30', 'end': '16:20'}]},
            {'bad': 'row'}, 'junk']
    secs = S.from_model(rows)
    assert [(s.label(), s.kind, [(m.days, m.start, m.end) for m in s.meetings]) for s in secs] == [
        ('MA 234-A', 'Lecture', [('MW', 600, 675)]), ('MA 331-RB', 'Recitation', [('R', 930, 980)])]


def test_wanted_courses_for_the_page_reader():
    plan = {'courses': [{'code': 'MA 234'}, {'code': 'Math Technical Elective'}, {'code': 'PRV 20X'}],
            'choices': [{'slot': 'Math Technical Elective', 'courses': ['MA 410']}]}
    assert planner.wanted_text(plan) == 'MA 234, PRV 20X, MA 410'


def test_schedule_command():
    assert commands.suggest('build my schedule', {})[0].action == ('schedule', 'build my schedule')
    assert commands.suggest('schedule, no classes before 10', {})[0].action[0] == 'schedule'
    assert not any(s.action[:1] == ('schedule',) for s in commands.suggest('reschedule', {}))
