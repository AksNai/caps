import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import commands  # noqa: E402
import gestures  # noqa: E402
import search  # noqa: E402
import winutil  # noqa: E402
from focus import Focus, site_hit  # noqa: E402
from keys import CapsKeys  # noqa: E402
from winutil import VK_BACK, VK_CAPITAL, VK_ESCAPE, VK_RETURN, VK_SHIFT, VK_TAB, Win  # noqa: E402


# --------------------------------------------------------------------------- Caps Lock
def make_keys():
    ev = []
    return CapsKeys(lambda n, a: ev.append((n, a))), ev


def tap(k, t=0.0):
    k.feed(VK_CAPITAL, True, t)
    k.feed(VK_CAPITAL, False, t + 0.08)


def test_tap_summons_and_listens():
    k, ev = make_keys()
    assert k.feed(VK_CAPITAL, True, 0.0) == 'suppress'
    assert k.feed(VK_CAPITAL, True, 0.03) == 'suppress'  # auto-repeat
    assert k.feed(VK_CAPITAL, False, 0.1) == 'suppress'
    assert ev == [('caps_down', None), ('tap', None)]
    assert k.capturing


def test_second_tap_dismisses():
    k, ev = make_keys()
    tap(k)
    tap(k, 1.0)
    assert ev[-1] == ('dismiss', 'caps') and not k.capturing


def test_shift_caps_is_real_caps_lock():
    k, ev = make_keys()
    assert k.feed(VK_CAPITAL, True, 0.0, shift=True) is None
    assert k.feed(VK_CAPITAL, False, 0.1, shift=True) is None
    assert ev == []


def test_long_hold_is_a_release_not_a_tap():
    k, ev = make_keys()
    k.feed(VK_CAPITAL, True, 0.0)
    k.feed(VK_CAPITAL, False, 0.9)
    assert ev == [('caps_down', None), ('release', {'held': 0.9, 'used': False})]
    assert not k.capturing


def test_quick_gesture_is_not_a_tap():
    k, ev = make_keys()
    k.feed(VK_CAPITAL, True, 0.0)
    k.moved = True                      # the UI saw the pointer move (or the wheel hook saw a scroll)
    k.feed(VK_CAPITAL, False, 0.2)
    assert ev[-1][0] == 'release' and not k.capturing


def test_gesture_while_listening_stops_listening():
    k, ev = make_keys()
    tap(k)
    k.feed(VK_CAPITAL, True, 1.0)
    k.moved = True
    k.feed(VK_CAPITAL, False, 1.5)
    assert ev[-1][0] == 'release' and not k.capturing


def test_rollover_letter_starts_command():
    k, ev = make_keys()
    k.feed(VK_CAPITAL, True, 0.0)
    assert k.feed(ord('F'), True, 0.05) == 'suppress'
    assert k.feed(VK_CAPITAL, False, 0.08) == 'suppress'
    assert ev == [('caps_down', None), ('rollover', 'f'), ('release', {'held': 0.08, 'used': True})]
    assert k.feed(ord('F'), False, 0.1) == 'suppress'  # its key-up is swallowed too
    assert k.feed(ord('O'), True, 0.12) == 'suppress'  # and the next letters go to the prompt
    assert ev[-1] == ('char', 'o')


def test_esc_while_holding_cancels():
    k, ev = make_keys()
    k.feed(VK_CAPITAL, True, 0.0)
    assert k.feed(VK_ESCAPE, True, 0.5) == 'suppress'
    k.feed(VK_CAPITAL, False, 0.6)
    assert ('cancel', None) in ev and ev[-1][0] == 'release'


def test_listening_forwards_keys():
    k, ev = make_keys()
    tap(k)
    for vk, t in [(ord('D'), 0.1), (VK_BACK, 0.2), (VK_RETURN, 0.3), (VK_ESCAPE, 0.35)]:
        assert k.feed(vk, True, t) == 'suppress'
        assert k.feed(vk, False, t + 0.01) == 'suppress'
    assert ev[2:] == [('char', 'd'), ('key', 'back'), ('key', 'enter'), ('key', 'esc')]
    k.stop_capture()
    assert k.feed(ord('X'), True, 0.5) is None and k.feed(ord('X'), False, 0.6) is None


def test_shortcuts_pass_through_and_end_listening():
    for kw, vk in [({'alt_win': True}, VK_TAB), ({'ctrl': True}, ord('S')), ({'alt_win': True}, ord('D'))]:
        k, ev = make_keys()
        tap(k)
        assert k.feed(vk, True, 0.2, **kw) is None       # Alt+Tab, Ctrl+S, Win+D work as usual
        assert ev[-1][0] == 'dismiss' and not k.capturing
        assert k.feed(vk, False, 0.25) is None


def test_paste_and_delete_word_stay_in_the_prompt():
    k, ev = make_keys()
    tap(k)
    assert k.feed(0x56, True, 0.2, ctrl=True) == 'suppress'
    assert k.feed(VK_BACK, True, 0.3, ctrl=True) == 'suppress'
    assert ev[-2:] == [('key', 'paste'), ('key', 'backword')] and k.capturing


def test_capture_shift_gives_capitals_and_modifiers_pass():
    k, ev = make_keys()
    k.start_capture(0.0)
    assert k.feed(VK_SHIFT, True, 0.1, shift=True) is None
    k.feed(ord('A'), True, 0.15, shift=True)
    assert ev[-1] == ('char', 'A')


def test_capture_expires_if_ui_hangs():
    k, ev = make_keys()
    k.start_capture(0.0)
    assert k.feed(ord('A'), True, 1.0) == 'suppress'
    assert k.feed(ord('B'), True, 10.0) is None   # nobody acknowledged for > 3 s: typing goes through
    assert not k.capturing and ev[-1] == ('dismiss', 'expired')


def test_normal_typing_untouched():
    k, ev = make_keys()
    for c in 'HELLO':
        assert k.feed(ord(c), True, 0.0) is None
        assert k.feed(ord(c), False, 0.0) is None
    assert ev == []


# --------------------------------------------------------------------------- gestures
def line(dx, dy, n=24, jitter=1.5, seed=0):
    rnd = random.Random(seed)
    return [(i * dx / n + rnd.uniform(-jitter, jitter), i * dy / n + rnd.uniform(-jitter, jitter))
            for i in range(n + 1)]


def circle(r=45, cw=True, turns=1.0, n=60, seed=0):
    rnd, s = random.Random(seed), 1 if cw else -1
    return [(r * math.cos(s * 2 * math.pi * turns * i / n) + rnd.uniform(-1.5, 1.5),
             r * math.sin(s * 2 * math.pi * turns * i / n) + rnd.uniform(-1.5, 1.5)) for i in range(n + 1)]


def zigzag(strokes=4, amp=60):
    corners = [((amp if i % 2 else 0), i * 6) for i in range(strokes + 1)]
    pts = []
    for a, b in zip(corners, corners[1:]):
        pts += [(a[0] + (b[0] - a[0]) * t / 8, a[1] + (b[1] - a[1]) * t / 8) for t in range(8)]
    return pts + [corners[-1]]


def test_flicks_in_eight_directions():
    for d in range(8):
        ang = math.radians(d * 45)
        assert gestures.classify(line(120 * math.cos(ang), -120 * math.sin(ang), seed=d)) == ('flick', d)


def test_casual_flick_snaps_to_left_right_up_down():
    assert gestures.classify(line(100, -45)) == ('flick', 0)   # 24 degrees up: still "right"
    assert gestures.classify(line(-30, 100)) == ('flick', 6)   # mostly down
    assert gestures.classify(line(80, -80)) == ('flick', 1)    # a real diagonal


def test_circles_either_way():
    assert gestures.classify(circle()) == ('circle', 'cw')
    assert gestures.classify(circle(cw=False)) == ('circle', 'ccw')
    assert gestures.classify(circle(r=25, seed=3))[0] == 'circle'
    assert gestures.classify(circle(turns=0.6)) is None      # an arc isn't a circle


def test_scribble_and_non_gestures():
    assert gestures.classify(zigzag(4)) == ('scribble', None)
    assert gestures.classify(zigzag(2)) is None               # a Z is not a scribble
    assert gestures.classify(line(20, 0)) is None             # too short: jitter while holding still
    assert gestures.classify(line(80, 0) + [(80, y) for y in range(0, 80, 8)]) is None  # an L


def test_ring_slots_and_dead_zone():
    assert gestures.ring_slot(5, 5) is None
    assert gestures.ring_slot(60, 0) == 0 and gestures.ring_slot(0, -60) == 2
    assert gestures.ring_slot(-60, 0) == 4 and gestures.ring_slot(0, 60) == 6
    assert gestures.ring_slot(-40, 40) == 5


def test_window_placements():
    area = (0, 0, 3840, 2088)
    assert gestures.placement(area, 'left') == (0, 0, 1920, 2088)
    assert gestures.placement(area, 'br') == (1920, 1044, 3840, 2088)
    assert gestures.placement(area, 'max') == area
    assert gestures.placement(area, 'min') is None
    assert {k for k, _ in gestures.SLOTS.values()} == {'left', 'right', 'tl', 'tr', 'bl', 'br', 'max', 'min'}


def test_wheel_notches_and_smooth_scrolling():
    w = gestures.Wheel()
    assert [w.add(d) for d in (-120, 120, 120)] == [-1, 1, 1]
    assert [w.add(30) for _ in range(4)] == [0, 0, 0, 1]      # a touchpad's small pieces add up
    assert w.add(240) == 2


# --------------------------------------------------------------------------- window search
W = [Win(1, 'Projects — Akshay Nair (index.html) - Portfolio - Visual Studio Code', 1, r'C:\x\Code.exe'),
     Win(2, 'Friends - Discord', 2, r'C:\x\Discord.exe'),
     Win(3, '(274) Indiana once tried to legally define pi as 3.2 - YouTube — Zen Browser', 3, r'C:\x\zen.exe'),
     Win(4, 'math-autocorrect - File Explorer', 4, r'C:\Windows\explorer.exe'),
     Win(5, 'Spotify Premium', 5, r'C:\x\Spotify.exe'),
     Win(6, 'Teams and Channels | Microsoft Teams', 6, r'C:\x\ms-teams.exe'),
     Win(7, 'main.tex - Online LaTeX Editor Overleaf — Zen Browser', 7, r'C:\x\zen.exe')]


def top(q):
    r = search.rank_windows(q, W)
    return r[0].hwnd if r else None


def test_search_finds_by_title_and_app():
    assert top('disc') == 2
    assert top('spot') == 2 or top('spot') == 5
    assert top('spotify') == 5
    assert top('yt') == 3
    assert top('youtube') == 3
    assert top('overleaf') == 7
    assert top('ovl') == 7
    assert top('code') == 1
    assert top('teams') == 6
    assert top('explorer') == 4
    assert top('zen over') == 7


def test_search_rejects_non_matches_and_keeps_order_when_empty():
    assert search.rank_windows('qqq', W) == []
    assert [w.hwnd for w in search.rank_windows('', W)] == [1, 2, 3, 4, 5, 6, 7]


def test_word_starts_beat_scattered_letters():
    assert search.word_score('fe', 'File Explorer') > search.word_score('fe', 'coffee')


# --------------------------------------------------------------------------- commands
def test_focus_commands():
    st = {'focus': None, 'autocorrect': True, 'buddy': True}
    assert commands.suggest('focus 50 cs 210', st)[0].action == ('focus_start', 50, 'cs 210')
    assert commands.suggest('focus', st)[0].action == ('focus_start', 25, '')
    assert commands.suggest('focus 1.5h essay', st)[0].action == ('focus_start', 90, 'essay')
    assert commands.suggest('focus essay 45', st)[0].action == ('focus_start', 45, 'essay')
    assert commands.suggest('foc', st)[0].action[0] == 'focus_start'
    assert commands.suggest('allow 5', st) == []  # nothing to allow without a session
    active = {'focus': {'remaining_min': 20, 'label': 'cs'}, 'autocorrect': True, 'buddy': True}
    assert commands.suggest('focus off', active)[0].action == ('focus_stop',)
    assert commands.suggest('unfocus', active)[0].action == ('focus_stop',)
    assert commands.suggest('allow 10', active)[0].action == ('allow', 10)
    assert commands.suggest('allow', active)[0].action == ('allow', 5)
    assert commands.suggest('focus +10', active)[0].action == ('focus_extend', 10)
    assert commands.suggest('focus', active)[0].action == ('noop',)
    assert commands.suggest('autocorrect', st)[0].action == ('autocorrect', False)
    assert commands.suggest('quit caps', st)[0].action == ('quit',)
    assert commands.suggest('discord', st) == []


def test_parse_minutes():
    assert commands.parse_minutes('50') == 50
    assert commands.parse_minutes('2h') == 120
    assert commands.parse_minutes('90 min') == 90
    assert commands.parse_minutes('0') is None
    assert commands.parse_minutes('cs') is None


# --------------------------------------------------------------------------- focus
def test_focus_blocks_apps_and_sites(tmp_path):
    f = Focus({}, str(tmp_path / 's.json'), str(tmp_path / 'log.jsonl'))
    discord, yt, overleaf, code_yt = W[1], W[2], W[6], Win(9, 'youtube.py - Visual Studio Code', 9, r'C:\x\Code.exe')
    assert f.check(discord, 0) is None  # no session
    f.start(25, 'cs', now=0)
    assert f.check(discord, 10) == ('minimize', 'Discord')
    assert f.check(yt, 10) == ('shield', 'YouTube')
    assert f.check(overleaf, 10) is None
    assert f.check(code_yt, 10) is None   # 'youtube' in an editor title is not the site
    f.allow(5, now=100)
    assert f.check(yt, 200) is None and f.check(yt, 401) == ('shield', 'YouTube')
    assert f.check(yt, 25 * 60 + 1) is None  # session over


def test_focus_x_suffix_rule():
    assert site_hit('Home / X — Zen Browser', ['/ X'])
    assert not site_hit('Fix / Xylophone notes — Zen Browser', ['/ X'])


def test_focus_persists_and_logs(tmp_path):
    f = Focus({}, str(tmp_path / 's.json'), str(tmp_path / 'log.jsonl'))
    f.start(50, 'essay', now=1000)
    f.count_block()
    g = Focus({}, str(tmp_path / 's.json'), str(tmp_path / 'log.jsonl'))  # restart of the app
    assert g.session.label == 'essay' and g.session.blocked == 1
    summary = g.stop(now=1000 + 30 * 60)
    assert summary['actual_min'] == 30 and summary['blocked'] == 1 and not summary['completed']
    assert (tmp_path / 'log.jsonl').read_text().count('essay') == 1
    assert Focus({}, str(tmp_path / 's.json')).session is None


def test_allow_titles_exception(tmp_path):
    f = Focus({'allow_titles': ['lecture']})
    f.start(25, now=0)
    assert f.check(Win(1, 'Linear Algebra Lecture 3 - YouTube — Zen Browser', 1, r'C:\x\zen.exe'), 5) is None


# --------------------------------------------------------------------------- shared constants
def test_magic_and_mutex_match_math_autocorrect():
    ac = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'math-autocorrect', 'daemon.py')
    if not os.path.exists(ac):
        return
    src = open(ac, encoding='utf-8').read()
    assert f'MAGIC = {hex(winutil.MAGIC).upper().replace("0X", "0x")}' in src
    main_src = open(os.path.join(os.path.dirname(HERE), 'main.py'), encoding='utf-8').read()
    assert "r'Local\\CapsMathAutocorrectKeyboardHook'" in src and "r'Local\\CapsMathAutocorrectKeyboardHook'" in main_src


def test_vk_char():
    assert winutil.vk_char(ord('A')) == 'a' and winutil.vk_char(ord('A'), True) == 'A'
    assert winutil.vk_char(ord('5')) == '5' and winutil.vk_char(0x20) == ' '


def test_capture_expiry_calls_back_from_the_hook():
    fired = []
    k = CapsKeys(lambda n, a: None, on_expire=lambda: fired.append(True))
    k.start_capture(0.0)
    k.feed(ord('A'), True, 10.0)
    assert fired == [True] and not k.capturing
