"""Per-driver, per-UTC-day duty-status minutes for the driver hours export (OI-20).

Runsheet is not an ELD (see ``driver/services/hos_advisory_service.py``): the
only hours data it holds is the driver app's ``duty_status_events``. This
module turns those events into daily minutes per status. The result is
advisory, never a record of duty status, and every row says so in ``basis``.

Attribution rule:

* the time between two consecutive events of one driver belongs to the
  earlier event's ``new_status``, split at UTC midnight;
* the driver's last event runs to ``min(range_end, now)``;
* time before a driver's first event in the range is NOT attributed (its
  status is unknown to the export); ``first_event_at`` makes that gap visible.

Pure: no I/O, so it is unit-tested without HTTP.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: The four duty statuses the driver app records, and their CSV column.
STATUS_COLUMNS: Dict[str, str] = {
    "active": "active_minutes",
    "on_break": "on_break_minutes",
    "off_duty": "off_duty_minutes",
    "inactive": "inactive_minutes",
}

#: Constant ``basis`` column value: advisory app data, not ELD records.
ADVISORY_BASIS = "runsheet_app_duty_status_advisory_not_eld"


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_midnight(moment: datetime) -> datetime:
    day = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    return day + timedelta(days=1)


def summarize_duty_status_days(
    events: Iterable[Mapping[str, Any]],
    range_start: datetime,
    range_end: datetime,
    now: datetime,
) -> List[Dict[str, Any]]:
    """Return one row per ``(UTC date, driver_id)``, sorted by date then driver.

    Args:
        events: ``duty_status_events`` documents (``driver_id``,
            ``new_status``, ``event_timestamp``, ``event_id``), any order.
        range_start: Inclusive start of the export range (UTC). Time before
            it is never attributed.
        range_end: Exclusive end of the export range (UTC).
        now: Current time; the last event never runs past it.
    """
    cutoff = min(range_end, now)
    by_driver: Dict[str, List[Tuple[datetime, str, str]]] = defaultdict(list)
    for event in events:
        driver_id = event.get("driver_id")
        ts = _parse_ts(event.get("event_timestamp"))
        if not isinstance(driver_id, str) or not driver_id or ts is None:
            logger.warning(
                "driver_hours_summary: skipping event %s without a driver or timestamp",
                event.get("event_id"),
            )
            continue
        by_driver[driver_id].append(
            (ts, str(event.get("event_id") or ""), str(event.get("new_status") or ""))
        )

    minutes: Dict[Tuple[date, str], Dict[str, float]] = defaultdict(
        lambda: {column: 0.0 for column in STATUS_COLUMNS.values()}
    )
    counts: Dict[Tuple[date, str], int] = defaultdict(int)
    first_at: Dict[Tuple[date, str], datetime] = {}
    last_at: Dict[Tuple[date, str], datetime] = {}
    unknown_logged = False

    for driver_id, rows in by_driver.items():
        rows.sort(key=lambda r: (r[0], r[1]))
        for index, (ts, _event_id, status) in enumerate(rows):
            key = (ts.date(), driver_id)
            counts[key] += 1
            first_at.setdefault(key, ts)
            last_at[key] = ts

            end = rows[index + 1][0] if index + 1 < len(rows) else cutoff
            start = max(ts, range_start)
            end = min(end, cutoff)
            column = STATUS_COLUMNS.get(status)
            if column is None:
                if not unknown_logged:
                    unknown_logged = True
                    logger.warning(
                        "driver_hours_summary: unknown duty status %r counted in no column",
                        status,
                    )
                continue
            # Split [start, end) at each UTC midnight.
            while start < end:
                boundary = min(_next_midnight(start), end)
                minutes[(start.date(), driver_id)][column] += (
                    boundary - start
                ).total_seconds() / 60.0
                start = boundary

    keys = sorted(set(minutes) | set(counts), key=lambda k: (k[0], k[1]))
    out: List[Dict[str, Any]] = []
    for key in keys:
        day, driver_id = key
        per_status = minutes.get(key) or {c: 0.0 for c in STATUS_COLUMNS.values()}
        row: Dict[str, Any] = {"date": day.isoformat(), "driver_id": driver_id}
        for column in STATUS_COLUMNS.values():
            row[column] = round(per_status[column], 2)
        row["event_count"] = counts.get(key, 0)
        row["first_event_at"] = first_at[key].isoformat() if key in first_at else None
        row["last_event_at"] = last_at[key].isoformat() if key in last_at else None
        row["basis"] = ADVISORY_BASIS
        out.append(row)
    return out
