"""Drawings and lessons: the sandbox, the drawing API, the geometry, and reading answers."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import agent  # noqa: E402
import board_api  # noqa: E402
import drawing  # noqa: E402
import sandbox  # noqa: E402
import screen  # noqa: E402


# --------------------------------------------------------------------------- the sandbox
def test_sandbox_runs_math_and_returns_output():
    r = sandbox.run('import itertools\nprint(len(list(itertools.permutations(range(5)))))')
    assert r['ok'] and r['stdout'].strip() == '120'
    r = sandbox.run('import numpy as np\nprint(round(np.linalg.det(np.eye(3) * 2)))')
    assert r['ok'] and r['stdout'].strip() == '8'


def test_sandbox_blocks_files_processes_and_escapes():
    for code in ["open('x.txt', 'w')", 'import os', 'import subprocess', 'from os import path', '__import__("os")',
                 '().__class__.__bases__', 'eval("1")', 'exec("x=1")', 'getattr(1, "real")',
                 'import numpy as np\nnp.save("x", np.zeros(2))', 'import json\njson.load(1)',
                 'import sympy\nsympy.sympify("x")']:
        r = sandbox.run(code)
        assert not r['ok'] and 'rejected' in r['error'], code


def test_sandbox_reports_errors_and_stops_runaways():
    r = sandbox.run('x = 1\ny = 1 / 0')
    assert not r['ok'] and 'ZeroDivisionError' in r['error'] and 'line 2' in r['error']
    r = sandbox.run('while True:\n    pass', timeout=2)
    assert not r['ok'] and 'longer than 2 s' in r['error']


# --------------------------------------------------------------------------- the drawing API
def fresh():
    board_api.B.reset()
    return board_api


def test_steps_split_a_drawing_into_parts():
    b = fresh()
    b.title('Supply and demand')
    b.axes(0, 10, 0, 10, 'Q', 'P')
    b.plot(lambda q: 9 - q, label='D')
    b.step('Supply slopes up.')
    b.plot(lambda q: 1 + q, label='S')
    b.point(4, 5, 'E')
    res = b.result()
    assert res['title'] == 'Supply and demand' and not res['three_d']
    assert [len(s['items']) for s in res['steps']] == [1, 2]
    assert res['steps'][1]['caption'] == 'Supply slopes up.'
    assert res['steps'][0]['items'][0]['label'] == 'D' and len(res['steps'][0]['items'][0]['pts']) > 100


def test_a_first_step_caption_does_not_make_an_empty_step():
    b = fresh()
    b.step('First, the axes.')
    b.line(0, 0, 1, 1)
    assert len(b.result()['steps']) == 1 and b.result()['steps'][0]['caption'] == 'First, the axes.'


def test_plot_skips_undefined_points():
    b = fresh()
    b.plot(lambda x: 1 / x, -1, 1)          # undefined at 0: two pieces, no crash
    polys = b.result()['steps'][0]['items']
    assert len(polys) == 2 and all(p['t'] == 'poly' for p in polys)


def test_3d_shapes_become_triangles():
    b = fresh()
    b.sphere((0, 0, 0), 1)
    b.box((3, 0, 0), 1)
    b.surface(lambda x, y: x * y, -1, 1, -1, 1, n=4)
    res = b.result()
    tris = [i for i in res['steps'][0]['items'] if i['t'] == 'tri']
    assert res['three_d'] and len(tris) == 10 * 16 * 2 + 12 + 32


def test_marks_number_themselves():
    b = fresh()
    b.mark(12, 'Click File')
    b.step('then')
    b.mark(30, 'Then Save As')
    b.highlight(7)
    items = [i for s in b.result()['steps'] for i in s['items']]
    assert [(i['t'], i.get('num')) for i in items] == [('mark', 1), ('mark', 2), ('box', None)]
    assert drawing.kinds(b.result()) == (False, True)


def test_bad_input_is_explained():
    b = fresh()
    for bad in (lambda: b.axes(5, 1, 0, 1), lambda: b.polygon([(0, 0), (1, 1)]), lambda: b.point(float('nan'), 1)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError('expected a ValueError')


# --------------------------------------------------------------------------- geometry
def test_ticks_and_bounds():
    assert drawing.nice_ticks(0, 10) == [0, 2, 4, 6, 8, 10]
    assert drawing.nice_ticks(-1.2, 3.7) == [-1, 0, 1, 2, 3]
    board = {'axes': None, 'steps': [{'caption': '', 'items': [{'t': 'pt', 'p': [2, 3]}, {'t': 'pt', 'p': [4, 7]}]}]}
    x0, x1, y0, y1 = drawing.bounds2d(board)
    assert x0 < 2 < 4 < x1 and y0 < 3 < 7 < y1


def test_strokes_draw_part_way():
    pts = [(0, 0), (10, 0), (10, 10)]
    assert drawing.partial(pts, 0.75)[-1] == (10.0, 5.0)
    assert drawing.partial(pts, 1) == pts and drawing.partial(pts, 0) == [(0, 0)]


def test_3d_camera_and_painter_order():
    cam = drawing.Camera((0, 0, 0), 1, 0.0, 0.0, (0, 0, 400, 400))
    top = cam.project((0, 0, 1))
    assert top[0] == 200 and top[1] < 200                      # up is up on screen
    near, far = cam.project((0, -0.5, 0)), cam.project((0, 0.5, 0))
    assert near[2] < far[2]                                     # depth grows away from the viewer
    board = {'steps': [{'caption': '', 'items': [{'t': 'tri', 'p': [[0, 0.5, 0], [1, 0.5, 0], [0, 0.5, 1]], 'color': 'x'},
                                                 {'t': 'tri', 'p': [[0, -.5, 0], [1, -.5, 0], [0, -.5, 1]], 'color': 'y'}]}]}
    order = drawing.draw_order(board, cam)
    assert [o[2][2] for o in order] == ['x', 'y']               # far one first


def test_lopsided_graphs_are_evened_out_but_objects_keep_their_shape():
    tall = {'graph3d': True,
            'steps': [{'caption': '', 'items': [{'t': 'pt3', 'p': [-1, -1, -8]}, {'t': 'pt3', 'p': [1, 1, 8]}]}]}
    c, r, k = drawing.scene_frame(tall)
    assert c == (0, 0, 0) and k[0] == k[1] == 1 and abs(k[2] - 0.15) < 1e-9    # 16 tall squeezed to 1.2x of 2 wide
    cube = {'steps': [{'caption': '', 'items': [{'t': 'pt3', 'p': [0, 0, 0]}, {'t': 'pt3', 'p': [3, 3, 3]}]}]}
    assert drawing.scene_frame(cube)[2] == (1.0, 1.0, 1.0)
    pole = dict(tall, graph3d=False)                           # a long thin object stays long and thin
    assert drawing.scene_frame(pole)[2] == (1.0, 1.0, 1.0)


def test_shapes_lie_along_any_axis():
    import board_api as api
    api.B.reset()
    api.cylinder((0, 0, 0), 0.5, 6, axis='x')
    api.cone((5, 0, 0), 0.5, 2, axis='-x')
    api.sphere((0, 0, 3), (3, 1, 0.5))
    b = api.result()
    pts = [q for s in b['steps'] for it in s['items'] for q in it['p']]
    cyl = [q for it in b['steps'][0]['items'][:80] for q in it['p']]
    assert max(abs(q[0]) for q in cyl) == 3 and max(abs(q[1]) for q in cyl) <= 0.5 + 1e-6   # lies along x
    assert [4.0, 0.0, 0.0] in pts                                 # cone tip points back along -x
    ell = [q for q in pts if q[2] > 2]
    assert abs(max(q[0] for q in ell) - 3) < 1e-6 and abs(max(q[2] for q in ell) - 3.5) < 1e-6
    assert b['graph3d'] is False
    api.B.reset()
    api.surface(lambda x, y: x * y, -1, 1, -1, 1)
    assert api.result()['graph3d'] is True
    try:
        api.cylinder((0, 0, 0), 1, 1, axis='sideways')
        raise AssertionError('should have refused')
    except ValueError as e:
        assert "'x', 'y', 'z'" in str(e)


def test_captions_drop_markdown_and_blank_runs():
    import board_ui
    assert board_ui.tidy('Here it is:\n\n\n\nDrag is **78 N**.\n\n') == 'Here it is:\nDrag is 78 N.'


# --------------------------------------------------------------------------- answers with drawings and lessons
def test_answers_split_into_words_code_and_next():
    reply = ('Click the Insert tab [[4]] first.\n```board\nmark(4, "Insert")\n```\n[[NEXT]]')
    said, code, more = screen.split_board(reply)
    assert said == 'Click the Insert tab [[4]] first.' and code == 'mark(4, "Insert")' and more
    said, code, more = screen.split_board('Demand shifts right.\n```python\nplot(lambda x: x, 0, 1)\n```')
    assert code.startswith('plot') and not more and said == 'Demand shifts right.'
    said, code, more = screen.split_board('Here it is:\n```board\nplot(lambda x: x')   # cut off mid-block
    assert said == 'Here it is:' and code == ''
    assert screen.visible_text('Look here\n```board\nmark(3') == 'Look here'


def test_new_agent_tools_never_need_approval():
    for name, args in [('compute', {'code': 'print(1)'}), ('show', {'code': 'plot(lambda x: x, 0, 1)'}),
                       ('find', {'text': 'Search'}), ('read_page', {}), ('scroll', {'direction': 'down'})]:
        for level in agent.LEVELS:
            assert agent.risk(name, args, None, level) is None, (name, level)
    assert agent.describe('type_text', {'text': 'MA 234', 'element': 7}).startswith('Type "MA 234" into element 7')
