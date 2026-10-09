"""Dispatch Board ETA and time-window math (design K3.6).

Pure functions over the route solver's haversine distance; no I/O. All
arithmetic runs in UTC and is converted to the tenant zone only at the edges,
because Python adds ``timedelta`` to an aware datetime on the wall clock, which
is wrong across a DST change (E12). Shift windows are built with ``zoneinfo``
so 23- and 25-hour days and night shifts that cross midnight come out right
(E10, E12).

Model (K3.6): a load starts at its start location (the terminal when one is
set and has coordinates, else the previous load's last stop, else the truck's
last known position), spends ``TERMINAL_LIFT_MINUTES`` at the terminal when it
has one, drives at ``SPEED_KMH`` to each stop, spends ``SERVICE_MINUTES`` at
each stop, and drives back to the start location.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

from Agents.support.route_solver import compute_distance

#: The solver's default speed (K3.6).
SPEED_KMH: float = 40.0
#: Time spent at each delivery stop.
SERVICE_MINUTES: float = 30.0
#: Time spent lifting at the terminal before the first stop.
TERMINAL_LIFT_MINUTES: float = 45.0

#: Q2 presets: (start, end) local times; an end at or before the start is the next day.
SHIFT_TIMES = {
    "day": (time(6, 0), time(18, 0)),
    "night": (time(18, 0), time(6, 0)),
    "all": (time(0, 0), time(0, 0)),
}
#: Where a load with no earlier load starts when its shift is "all".
ALL_DAY_DEFAULT_START = time(6, 0)

_EPS = 1e-9


class Point(Protocol):
    lat: float
    lon: float


@dataclass(frozen=True)
class P:
    """A plain coordinate, for callers that hold dicts."""

    lat: float
    lon: float


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Chicago")


def add_minutes(dt: datetime, minutes: float, tz_name: Optional[str] = None) -> datetime:
    """``dt + minutes`` in real elapsed time, returned in ``tz_name`` (or dt's zone)."""
    result = _utc(dt) + timedelta(minutes=minutes)
    target = zone(tz_name) if tz_name else dt.tzinfo
    return result.astimezone(target)


def minutes_between(a: datetime, b: datetime) -> float:
    """Elapsed minutes from ``a`` to ``b`` (negative when ``b`` is earlier)."""
    return (_utc(b) - _utc(a)).total_seconds() / 60.0


def local_at(service_date: date, at: time, tz_name: str) -> datetime:
    """``service_date`` at local wall time ``at`` in the zone (DST-aware)."""
    return datetime.combine(service_date, at, tzinfo=zone(tz_name))


def shift_window(service_date: date, shift_id: str, tz_name: str) -> Tuple[datetime, datetime]:
    """The shift's local window on ``service_date`` (E10: night ends next morning)."""
    start_t, end_t = SHIFT_TIMES.get(shift_id, SHIFT_TIMES["all"])
    start = local_at(service_date, start_t, tz_name)
    end_date = service_date + timedelta(days=1) if end_t <= start_t else service_date
    return start, local_at(end_date, end_t, tz_name)


def default_load_start(service_date: date, shift_id: str, tz_name: str) -> datetime:
    """Where a load with no earlier load in the lane starts."""
    if shift_id == "all":
        return local_at(service_date, ALL_DAY_DEFAULT_START, tz_name)
    return shift_window(service_date, shift_id, tz_name)[0]


def service_date_of(dt: datetime, tz_name: str) -> date:
    """The service day a moment belongs to: its local calendar date (E10)."""
    return _utc(dt).astimezone(zone(tz_name)).date()


def today_in(tz_name: str, now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    return service_date_of(now, tz_name)


def distance_between(a: Point, b: Point) -> float:
    """Haversine km between two points."""
    return compute_distance(a.lat, a.lon, b.lat, b.lon)


def distance_km(points: Sequence[Optional[Point]]) -> Optional[float]:
    """Total km along ``points`` in order; ``None`` if any point is missing."""
    if any(p is None for p in points):
        return None
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += distance_between(a, b)  # type: ignore[arg-type]
    return total


def travel_minutes(a: Point, b: Point, speed_kmh: float = SPEED_KMH) -> float:
    if speed_kmh <= 0:
        return 0.0
    return distance_between(a, b) / speed_kmh * 60.0


def resolve_start_location(
    *,
    terminal_location: Optional[Point],
    previous_load_last_stop: Optional[Point],
    truck_position: Optional[Point],
) -> Optional[Point]:
    """K3.6 start-location rule: terminal, else previous load's last stop, else truck position."""
    if terminal_location is not None:
        return terminal_location
    if previous_load_last_stop is not None:
        return previous_load_last_stop
    return truck_position


@dataclass
class LoadTimes:
    """Times for one load. ``available`` is False when any location is missing."""

    start: datetime
    stop_etas: List[Optional[datetime]]
    end: datetime
    available: bool
    drive_km: Optional[float] = None
    drive_minutes: float = 0.0
    on_duty_minutes: float = 0.0
    lateness_minutes: List[float] = field(default_factory=list)


def compute_load_times(
    *,
    start_location: Optional[Point],
    start_time: datetime,
    stop_locations: Sequence[Optional[Point]],
    has_terminal: bool,
    tz_name: str,
    windows_end: Optional[Sequence[Optional[datetime]]] = None,
    speed_kmh: float = SPEED_KMH,
) -> LoadTimes:
    """ETAs per stop and the load's end time, including the return leg.

    With a missing start or stop location the ETAs are ``None`` and the end is
    a duration-only estimate (lift plus service time), so loads can still be
    sequenced while the lane shows ``eta_unavailable``.
    """
    lift = TERMINAL_LIFT_MINUTES if has_terminal else 0.0
    n = len(stop_locations)
    if start_location is None or any(loc is None for loc in stop_locations):
        end = add_minutes(start_time, lift + n * SERVICE_MINUTES, tz_name)
        return LoadTimes(
            start=start_time,
            stop_etas=[None] * n,
            end=end,
            available=False,
            on_duty_minutes=lift + n * SERVICE_MINUTES,
            lateness_minutes=[0.0] * n,
        )
    elapsed = lift
    drive = 0.0
    km = 0.0
    prev: Point = start_location
    etas: List[Optional[datetime]] = []
    for loc in stop_locations:
        leg_km = distance_between(prev, loc)  # type: ignore[arg-type]
        leg = leg_km / speed_kmh * 60.0 if speed_kmh > 0 else 0.0
        km += leg_km
        drive += leg
        elapsed += leg
        etas.append(add_minutes(start_time, elapsed, tz_name))
        elapsed += SERVICE_MINUTES
        prev = loc  # type: ignore[assignment]
    if n:
        back_km = distance_between(prev, start_location)
        back = back_km / speed_kmh * 60.0 if speed_kmh > 0 else 0.0
        km += back_km
        drive += back
        elapsed += back
    lateness = [
        lateness_minutes(eta, (windows_end[i] if windows_end is not None else None))
        for i, eta in enumerate(etas)
    ]
    return LoadTimes(
        start=start_time,
        stop_etas=etas,
        end=add_minutes(start_time, elapsed, tz_name),
        available=True,
        drive_km=km,
        drive_minutes=drive,
        on_duty_minutes=elapsed,
        lateness_minutes=lateness,
    )


def lateness_minutes(eta: Optional[datetime], window_end: Optional[datetime]) -> float:
    """Minutes ``eta`` is after the window end (0 when on time or unknown)."""
    if eta is None or window_end is None:
        return 0.0
    return max(0.0, minutes_between(window_end, eta))


def early_minutes(eta: Optional[datetime], window_start: Optional[datetime]) -> float:
    """Minutes ``eta`` is before the window start (0 when not early or unknown)."""
    if eta is None or window_start is None:
        return 0.0
    return max(0.0, minutes_between(eta, window_start))


@dataclass
class InsertionStop:
    location: Optional[Point]
    window_end: Optional[datetime] = None


def _late_set(times: LoadTimes) -> set:
    return {i for i, late in enumerate(times.lateness_minutes) if late > _EPS}


def insertion_options(
    stops: Sequence[InsertionStop],
    new_stop: InsertionStop,
    *,
    start_location: Optional[Point],
    start_time: datetime,
    has_terminal: bool,
    tz_name: str,
) -> List[Tuple[int, float, bool, float]]:
    """``(index, added_minutes, creates_lateness, total_lateness)`` for every index."""
    base = compute_load_times(
        start_location=start_location,
        start_time=start_time,
        stop_locations=[s.location for s in stops],
        has_terminal=has_terminal,
        tz_name=tz_name,
        windows_end=[s.window_end for s in stops],
    )
    base_late = _late_set(base)
    options: List[Tuple[int, float, bool, float]] = []
    for index in range(len(stops) + 1):
        trial = list(stops[:index]) + [new_stop] + list(stops[index:])
        times = compute_load_times(
            start_location=start_location,
            start_time=start_time,
            stop_locations=[s.location for s in trial],
            has_terminal=has_terminal,
            tz_name=tz_name,
            windows_end=[s.window_end for s in trial],
        )
        added = minutes_between(base.end, times.end)
        # Map the trial's late positions back to the original stop positions.
        late = _late_set(times)
        created = False
        for pos in late:
            if pos == index:
                created = True
                break
            original = pos if pos < index else pos - 1
            if original not in base_late:
                created = True
                break
        options.append((index, added, created, sum(times.lateness_minutes)))
    return options


def best_insertion_index(
    stops: Sequence[InsertionStop],
    new_stop: InsertionStop,
    *,
    start_location: Optional[Point],
    start_time: datetime,
    has_terminal: bool,
    tz_name: str,
) -> int:
    """K3.6: least added time that creates no ``eta_after_window``; ties keep the earlier index.

    When every position creates lateness, the least total lateness wins. With
    ETAs unavailable (a missing location) the stop is appended.
    """
    locations = [s.location for s in stops] + [new_stop.location]
    if start_location is None or any(loc is None for loc in locations):
        return len(stops)
    options = insertion_options(
        stops,
        new_stop,
        start_location=start_location,
        start_time=start_time,
        has_terminal=has_terminal,
        tz_name=tz_name,
    )
    clean = [o for o in options if not o[2]]
    if clean:
        return min(clean, key=lambda o: (round(o[1], 6), o[0]))[0]
    return min(options, key=lambda o: (round(o[3], 6), o[0]))[0]


def intervals_overlap(a: Tuple[datetime, datetime], b: Tuple[datetime, datetime]) -> bool:
    """Half-open overlap of two time intervals (shift / load windows)."""
    return _utc(a[0]) < _utc(b[1]) and _utc(b[0]) < _utc(a[1])


def outside_window(interval: Tuple[datetime, datetime], window: Tuple[datetime, datetime]) -> bool:
    """True when ``interval`` starts before or ends after ``window``."""
    return _utc(interval[0]) < _utc(window[0]) or _utc(interval[1]) > _utc(window[1])
