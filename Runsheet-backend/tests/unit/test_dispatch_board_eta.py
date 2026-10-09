"""Dispatch Board ETA module (plan task 8, design K3.6; E10, E12, P5)."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fuel.services import dispatch_board_eta as eta

TZ = "America/Chicago"
DEPOT = eta.P(41.80, -87.70)


def _hav(a, b):
    r = 6371.0
    dlat, dlon = math.radians(b.lat - a.lat), math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a.lat)) * math.cos(math.radians(b.lat)) * math.sin(dlon / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def test_start_location_rule():
    terminal, prev, truck = eta.P(1, 1), eta.P(2, 2), eta.P(3, 3)
    assert eta.resolve_start_location(terminal_location=terminal, previous_load_last_stop=prev, truck_position=truck) == terminal
    assert eta.resolve_start_location(terminal_location=None, previous_load_last_stop=prev, truck_position=truck) == prev
    assert eta.resolve_start_location(terminal_location=None, previous_load_last_stop=None, truck_position=truck) == truck
    assert eta.resolve_start_location(terminal_location=None, previous_load_last_stop=None, truck_position=None) is None


def test_load_times_include_lift_service_and_return_leg():
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)
    stop = eta.P(41.90, -87.70)
    times = eta.compute_load_times(start_location=DEPOT, start_time=start, stop_locations=[stop], has_terminal=True, tz_name=TZ)
    leg = _hav(DEPOT, stop) / eta.SPEED_KMH * 60
    assert times.available
    assert abs(eta.minutes_between(start, times.stop_etas[0]) - (eta.TERMINAL_LIFT_MINUTES + leg)) < 1e-6
    total = eta.TERMINAL_LIFT_MINUTES + leg + eta.SERVICE_MINUTES + leg
    assert abs(eta.minutes_between(start, times.end) - total) < 1e-6
    assert abs(times.drive_km - 2 * _hav(DEPOT, stop)) < 1e-9
    assert eta.distance_km([DEPOT, stop, DEPOT]) == times.drive_km


def test_missing_location_makes_etas_unavailable():
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)
    times = eta.compute_load_times(start_location=DEPOT, start_time=start, stop_locations=[None, eta.P(41.9, -87.7)], has_terminal=False, tz_name=TZ)
    assert not times.available and times.stop_etas == [None, None]
    assert eta.minutes_between(start, times.end) == 2 * eta.SERVICE_MINUTES
    assert eta.distance_km([DEPOT, None]) is None


def test_window_lateness_and_early():
    end = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
    assert eta.lateness_minutes(end + timedelta(minutes=25), end) == 25
    assert eta.lateness_minutes(end - timedelta(minutes=5), end) == 0
    assert eta.lateness_minutes(None, end) == 0
    assert eta.early_minutes(end - timedelta(minutes=10), end) == 10
    assert eta.early_minutes(end, None) == 0


def test_best_insertion_prefers_least_added_time_and_earlier_index_on_ties():
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)
    a, b = eta.P(41.90, -87.70), eta.P(42.00, -87.70)
    stops = [eta.InsertionStop(a), eta.InsertionStop(b)]
    between = eta.InsertionStop(eta.P(41.95, -87.70))
    assert eta.best_insertion_index(stops, between, start_location=DEPOT, start_time=start, has_terminal=False, tz_name=TZ) == 1
    # Same place as the depot: index 0 and 2 cost the same; the earlier wins.
    same = eta.InsertionStop(DEPOT)
    assert eta.best_insertion_index([], same, start_location=DEPOT, start_time=start, has_terminal=False, tz_name=TZ) == 0


def test_best_insertion_avoids_creating_lateness():
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)
    near = eta.P(41.81, -87.70)
    far = eta.P(42.50, -87.70)
    # The far stop has a tight window: putting the new (near) stop first would make it late.
    tight_end = eta.add_minutes(start, _hav(DEPOT, far) / eta.SPEED_KMH * 60 + 1)
    stops = [eta.InsertionStop(far, window_end=tight_end)]
    new = eta.InsertionStop(near)
    assert eta.best_insertion_index(stops, new, start_location=DEPOT, start_time=start, has_terminal=False, tz_name=TZ) == 1


def test_best_insertion_appends_without_locations():
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)
    stops = [eta.InsertionStop(eta.P(41.9, -87.7)), eta.InsertionStop(None)]
    assert eta.best_insertion_index(stops, eta.InsertionStop(eta.P(41.9, -87.7)), start_location=DEPOT, start_time=start, has_terminal=False, tz_name=TZ) == 2


_points = st.builds(eta.P, st.floats(41.5, 42.5), st.floats(-88.2, -87.2))
_offsets = st.one_of(st.none(), st.integers(10, 600))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    existing=st.lists(st.tuples(_points, _offsets), min_size=0, max_size=6),
    new=st.tuples(_points, _offsets),
)
def test_p5_best_fit_soundness(existing, new):
    """P5: the chosen index's added time is minimal among indices that create no lateness."""
    start = eta.local_at(date(2026, 10, 8), eta.SHIFT_TIMES["day"][0], TZ)

    def mk(p, off):
        return eta.InsertionStop(p, window_end=eta.add_minutes(start, off) if off is not None else None)

    stops = [mk(p, o) for p, o in existing]
    new_stop = mk(*new)
    chosen = eta.best_insertion_index(stops, new_stop, start_location=DEPOT, start_time=start, has_terminal=False, tz_name=TZ)

    # Independent brute force on raw haversine.
    def route(seq):
        t, prev, etas = 0.0, DEPOT, []
        for s in seq:
            t += _hav(prev, s.location) / eta.SPEED_KMH * 60
            etas.append(t)
            t += eta.SERVICE_MINUTES
            prev = s.location
        if seq:
            t += _hav(prev, DEPOT) / eta.SPEED_KMH * 60
        late = [s.window_end is not None and e > eta.minutes_between(start, s.window_end) + 1e-9 for s, e in zip(seq, etas)]
        return t, late

    base_t, base_late = route(stops)
    clean = []
    for i in range(len(stops) + 1):
        seq = stops[:i] + [new_stop] + stops[i:]
        t, late = route(seq)
        created = late[i] or any(late[j] and not base_late[j if j < i else j - 1] for j in range(len(seq)) if j != i)
        if not created:
            clean.append((t - base_t, i))
    if clean:
        best = min(a for a, _ in clean)
        chosen_added = next(a for a, i in clean if i == chosen) if any(i == chosen for _, i in clean) else None
        assert chosen_added is not None, "chose an index that creates lateness although a clean one exists"
        assert chosen_added <= best + 1e-6


def test_shift_windows_and_midnight_crossing_e10():
    day = date(2026, 10, 8)
    start, end = eta.shift_window(day, "day", TZ)
    assert (start.hour, end.hour, start.date(), end.date()) == (6, 18, day, day)
    n_start, n_end = eta.shift_window(day, "night", TZ)
    assert n_start.hour == 18 and n_start.date() == day
    assert n_end.hour == 6 and n_end.date() == day + timedelta(days=1)
    assert eta.minutes_between(n_start, n_end) == 12 * 60
    one_am_next = eta.local_at(day + timedelta(days=1), datetime.min.time().replace(hour=1), TZ)
    assert eta.service_date_of(one_am_next, TZ) == day + timedelta(days=1)
    assert eta.default_load_start(day, "night", TZ) == n_start
    assert eta.default_load_start(day, "all", TZ).hour == 6


def test_dst_days_e12():
    # 2026-11-01: US DST ends (25-hour day); 2026-03-08: DST starts (23-hour day).
    fall = date(2026, 11, 1)
    a_start, a_end = eta.shift_window(fall, "all", TZ)
    assert eta.minutes_between(a_start, a_end) == 25 * 60
    spring = date(2026, 3, 8)
    s_start, s_end = eta.shift_window(spring, "all", TZ)
    assert eta.minutes_between(s_start, s_end) == 23 * 60
    # Night of Oct 31 → Nov 1 lasts 13 real hours.
    n_start, n_end = eta.shift_window(date(2026, 10, 31), "night", TZ)
    assert eta.minutes_between(n_start, n_end) == 13 * 60
    # Adding real time across the change lands on the right wall clock.
    late = eta.add_minutes(n_start, 10 * 60, TZ)  # 18:00 CDT + 10 h = 03:00 CST
    assert (late.hour, late.utcoffset()) == (3, timedelta(hours=-6))


def test_interval_overlap_and_outside_window():
    t0 = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    assert eta.intervals_overlap((t0, t0 + h), (t0 + h / 2, t0 + 2 * h))
    assert not eta.intervals_overlap((t0, t0 + h), (t0 + h, t0 + 2 * h))
    assert eta.outside_window((t0, t0 + 3 * h), (t0, t0 + 2 * h))
    assert not eta.outside_window((t0, t0 + h), (t0, t0 + 2 * h))
