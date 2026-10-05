"""parse_ts: the one timestamp comparison helper (loading-plan-executor K5a)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from persistence.timestamps import parse_ts

UTC = timezone.utc


@pytest.mark.parametrize("value", [None, ""])
def test_empty_values_stay_none(value):
    assert parse_ts(value) is None


def test_aware_datetime_is_returned_as_is():
    dt = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert parse_ts(dt) is dt


def test_naive_datetime_is_taken_as_utc():
    assert parse_ts(datetime(2026, 10, 4, 12, 0)) == datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_z_suffix_and_offset_forms_compare_equal():
    z = parse_ts("2026-10-04T12:00:00Z")
    offset = parse_ts("2026-10-04T12:00:00+00:00")
    assert z == offset == datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert z.tzinfo is not None


def test_naive_string_is_taken_as_utc():
    assert parse_ts("2026-10-04T12:00:00") == datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_non_utc_offset_is_compared_by_instant():
    assert parse_ts("2026-10-04T14:00:00+02:00") == parse_ts("2026-10-04T12:00:00Z")


def test_dropped_zero_microseconds_do_not_misorder():
    # pydantic writes ...:00Z for a whole second; a later stamp is ...:00.5Z.
    assert parse_ts("2026-10-04T12:00:00Z") < parse_ts("2026-10-04T12:00:00.5Z")
    assert parse_ts("2026-10-04T12:00:00Z") == parse_ts("2026-10-04T12:00:00.000000+00:00")


def test_datetime_and_stored_string_of_same_instant_are_equal():
    dt = datetime(2026, 10, 4, 12, 0, 0, 250000, tzinfo=UTC)
    assert parse_ts(dt) == parse_ts("2026-10-04T12:00:00.250000Z")


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-40T00:00:00Z", "yesterday"])
def test_garbage_raises_value_error(value):
    with pytest.raises(ValueError):
        parse_ts(value)


@pytest.mark.parametrize(
    ("age_seconds", "stale"),
    [(60, False), (121, True)],
)
def test_lease_age_comparison_on_iso_strings(age_seconds, stale):
    # The plan lease (K4 step 5) is a 120 s comparison on a stored ISO string.
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    claimed_at = (now - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
    is_stale = now - parse_ts(claimed_at) > timedelta(seconds=120)
    assert is_stale is stale
