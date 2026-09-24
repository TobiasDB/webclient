"""Human mouse paths: one deterministic generator for the live driver and the replay."""

import math

from webclient.dom.mouse import human_mouse_path, mouse_duration_ms, path_seed, path_timings_ms


def test_path_starts_and_ends_exactly_and_is_deterministic():
    a = human_mouse_path(100, 100, 640, 400)
    b = human_mouse_path(100, 100, 640, 400)
    assert a == b and a[0] == (100.0, 100.0) and a[-1] == (640.0, 400.0)
    assert 8 <= len(a) <= 48
    # a different destination bends differently; the same one with a different seed too
    assert human_mouse_path(100, 100, 640, 401) != a
    assert human_mouse_path(100, 100, 640, 400, seed=7) != a


def test_path_bows_off_the_straight_line_but_stays_near_it():
    pts = human_mouse_path(0, 0, 600, 0, resolution=30)
    ys = [y for _, y in pts]
    assert max(abs(y) for y in ys) > 20  # a visible bow, not a ruler
    assert max(abs(y) for y in ys) < 160  # ...but not a detour
    xs = [x for x, _ in pts]
    assert all(b >= a - 3 for a, b in zip(xs, xs[1:]))  # progress is (almost) monotone


def test_points_bunch_at_the_end_the_settle():
    pts = human_mouse_path(0, 0, 500, 0, resolution=20)
    first_gap = pts[1][0] - pts[0][0]
    last_gap = pts[-1][0] - pts[-2][0]
    mid_gap = pts[10][0] - pts[9][0]
    assert first_gap < mid_gap and last_gap < mid_gap  # ease in-out


def test_timings_and_duration():
    assert 120 <= mouse_duration_ms(0) <= 121 and mouse_duration_ms(400) > mouse_duration_ms(100)
    assert mouse_duration_ms(1e9) == 700
    t = path_timings_ms(10, 300)
    assert t[0] == 0 and t[-1] == 300 and all(b >= a for a, b in zip(t, t[1:]))


def test_seed_is_stable_and_pixel_rounded():
    assert path_seed(1, 2, 3, 4) == path_seed(1.2, 2.4, 2.6, 4.1)
    assert path_seed(1, 2, 3, 4) != path_seed(1, 2, 3, 5)


def test_typescript_parity_vector():
    """The first points of a known path -- the TS port must produce exactly these (see the
    parity check in the UI repo, `node scripts/mouse-parity.mjs`)."""
    pts = human_mouse_path(100, 100, 640, 400, resolution=8)
    assert len(pts) == 8 and pts[0] == (100.0, 100.0) and pts[-1] == (640.0, 400.0)
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in pts)
    print("PARITY", pts)
