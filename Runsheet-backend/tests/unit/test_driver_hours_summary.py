"""Pure aggregation behind the driver hours export (OI-20)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from compliance.services.driver_hours_summary import (
    ADVISORY_BASIS,
    summarize_duty_status_days,
)

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 4, tzinfo=timezone.utc)  # exclusive
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _ev(driver, ts, status, event_id=None):
    return {
        "driver_id": driver,
        "event_timestamp": ts,
        "new_status": status,
        "event_id": event_id or f"{driver}-{ts}",
    }


def _rows_by_key(rows):
    return {(r["date"], r["driver_id"]): r for r in rows}


def test_two_events_on_one_day_attribute_minutes_to_the_earlier_status():
    rows = summarize_duty_status_days(
        [
            _ev("D1", "2026-10-01T08:00:00+00:00", "active"),
            _ev("D1", "2026-10-01T10:30:00+00:00", "off_duty"),
        ],
        START, datetime(2026, 10, 1, 12, tzinfo=timezone.utc), NOW,
    )
    [row] = rows
    assert row["date"] == "2026-10-01"
    assert row["active_minutes"] == 150.0
    assert row["off_duty_minutes"] == 90.0  # 10:30 → range end 12:00
    assert row["on_break_minutes"] == 0.0 and row["inactive_minutes"] == 0.0
    assert row["event_count"] == 2
    assert row["first_event_at"] == "2026-10-01T08:00:00+00:00"
    assert row["last_event_at"] == "2026-10-01T10:30:00+00:00"
    assert row["basis"] == ADVISORY_BASIS


def test_a_span_over_utc_midnight_splits_across_two_dates():
    rows = _rows_by_key(summarize_duty_status_days(
        [
            _ev("D1", "2026-10-01T22:00:00Z", "active"),
            _ev("D1", "2026-10-02T01:00:00Z", "off_duty"),
        ],
        START, datetime(2026, 10, 2, 2, tzinfo=timezone.utc), NOW,
    ))
    assert rows[("2026-10-01", "D1")]["active_minutes"] == 120.0
    assert rows[("2026-10-02", "D1")]["active_minutes"] == 60.0
    assert rows[("2026-10-02", "D1")]["off_duty_minutes"] == 60.0


def test_last_event_runs_to_range_end_or_now_whichever_is_earlier():
    events = [_ev("D1", "2026-10-03T23:00:00Z", "on_break")]
    [to_end] = summarize_duty_status_days(events, START, END, NOW)
    assert to_end["on_break_minutes"] == 60.0  # 23:00 → 00:00 range end
    early_now = datetime(2026, 10, 3, 23, 15, tzinfo=timezone.utc)
    [to_now] = summarize_duty_status_days(events, START, END, early_now)
    assert to_now["on_break_minutes"] == 15.0


def test_time_before_the_first_event_is_not_attributed():
    [row] = summarize_duty_status_days(
        [
            _ev("D1", "2026-10-02T12:00:00Z", "active"),
            _ev("D1", "2026-10-02T13:00:00Z", "inactive"),
        ],
        START, datetime(2026, 10, 2, 14, tzinfo=timezone.utc), NOW,
    )
    total = sum(row[c] for c in (
        "active_minutes", "on_break_minutes", "off_duty_minutes", "inactive_minutes",
    ))
    assert total == 120.0  # 12:00 → 14:00 only, nothing from 10-01 or 10-02 00:00
    assert row["first_event_at"] == "2026-10-02T12:00:00+00:00"


def test_carried_over_days_have_minutes_but_no_events():
    rows = _rows_by_key(summarize_duty_status_days(
        [_ev("D1", "2026-10-01T00:00:00Z", "off_duty")], START, END, NOW,
    ))
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        assert rows[(day, "D1")]["off_duty_minutes"] == 1440.0
    assert rows[("2026-10-02", "D1")]["event_count"] == 0
    assert rows[("2026-10-02", "D1")]["first_event_at"] is None


def test_drivers_are_kept_separate():
    rows = _rows_by_key(summarize_duty_status_days(
        [
            _ev("D1", "2026-10-01T08:00:00Z", "active"),
            _ev("D2", "2026-10-01T09:00:00Z", "on_break"),
            _ev("D1", "2026-10-01T09:00:00Z", "off_duty"),
            _ev("D2", "2026-10-01T09:30:00Z", "active"),
        ],
        START, datetime(2026, 10, 1, 10, tzinfo=timezone.utc), NOW,
    ))
    d1, d2 = rows[("2026-10-01", "D1")], rows[("2026-10-01", "D2")]
    assert (d1["active_minutes"], d1["off_duty_minutes"]) == (60.0, 60.0)
    assert (d2["on_break_minutes"], d2["active_minutes"]) == (30.0, 30.0)


def test_unknown_status_counts_in_no_column_and_is_logged_once(caplog):
    with caplog.at_level(logging.WARNING, logger="compliance.services.driver_hours_summary"):
        [row] = summarize_duty_status_days(
            [
                _ev("D1", "2026-10-01T08:00:00Z", "driving"),
                _ev("D1", "2026-10-01T09:00:00Z", "yard_move"),
                _ev("D1", "2026-10-01T10:00:00Z", "active"),
            ],
            START, datetime(2026, 10, 1, 11, tzinfo=timezone.utc), NOW,
        )
    assert row["active_minutes"] == 60.0
    assert row["event_count"] == 3
    unknown = [r for r in caplog.records if "unknown duty status" in r.getMessage()]
    assert len(unknown) == 1
