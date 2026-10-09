"""Unit tests for services.date_range (data-export G1/G2/G3 date params)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from errors.exceptions import AppException
from services.date_range import (
    DateRange,
    doc_range_clause,
    parse_date_range,
    to_doc_bound,
)

UTC = timezone.utc


def test_both_omitted_is_empty():
    r = parse_date_range(None, None)
    assert r == DateRange(None, None, None)
    assert r.is_empty
    assert doc_range_clause("generated_at", r, z_suffix=True) is None


def test_blank_values_are_omitted():
    assert parse_date_range("  ", "").is_empty


def test_date_only_start_is_midnight_inclusive():
    r = parse_date_range("2026-10-01", None)
    assert r.gte == datetime(2026, 10, 1, tzinfo=UTC)
    assert r.lt is None and r.lte is None


def test_date_only_end_includes_whole_day():
    r = parse_date_range(None, "2026-10-31")
    assert r.lt == datetime(2026, 11, 1, tzinfo=UTC)
    assert r.lte is None


def test_datetime_end_is_inclusive_lte():
    r = parse_date_range(None, "2026-10-31T12:30:00")
    assert r.lte == datetime(2026, 10, 31, 12, 30, tzinfo=UTC)
    assert r.lt is None


def test_trailing_z_accepted():
    r = parse_date_range("2026-10-01T05:00:00Z", None)
    assert r.gte == datetime(2026, 10, 1, 5, tzinfo=UTC)


def test_offset_converted_to_utc():
    r = parse_date_range("2026-10-01T00:00:00-05:00", None)
    assert r.gte == datetime(2026, 10, 1, 5, tzinfo=UTC)
    assert r.gte.utcoffset().total_seconds() == 0


def test_whitespace_stripped():
    assert parse_date_range(" 2026-10-01 ", None).gte == datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize("field,args", [
    ("start_date", ("not-a-date", None)),
    ("end_date", (None, "2026-13-01")),
    ("start_date", ("2026/10/01", None)),
])
def test_invalid_is_422(field, args):
    with pytest.raises(AppException) as exc:
        parse_date_range(*args)
    assert exc.value.status_code == 422
    assert exc.value.details["field"] == field
    assert "value" in exc.value.details


def test_over_40_chars_is_422():
    with pytest.raises(AppException) as exc:
        parse_date_range("2026-10-01T00:00:00.000000+00:00" + "x" * 20, None)
    assert exc.value.status_code == 422
    assert exc.value.details["field"] == "start_date"


def test_reversed_is_422():
    with pytest.raises(AppException) as exc:
        parse_date_range("2026-10-02", "2026-10-01")
    assert exc.value.status_code == 422
    assert exc.value.details == {"field": "start_date", "reason": "after_end_date"}


def test_same_day_is_valid():
    r = parse_date_range("2026-10-01", "2026-10-01")
    assert r.gte < r.lt


def test_reversed_datetime_end_is_422():
    with pytest.raises(AppException):
        parse_date_range("2026-10-01T10:00:00Z", "2026-10-01T09:00:00Z")


def test_to_doc_bound_suffixes():
    dt = datetime(2026, 10, 1, tzinfo=UTC)
    assert to_doc_bound(dt, z_suffix=True, op="gte") == "2026-10-01T00:00:00.000000Z"
    assert to_doc_bound(dt, z_suffix=True, op="lt") == "2026-10-01T00:00:00.000000Z"
    assert to_doc_bound(dt, z_suffix=True, op="lte") == "2026-10-01T00:00:00Z"
    assert to_doc_bound(dt, z_suffix=False) == "2026-10-01T00:00:00+00:00"


def test_doc_range_clause_bounds():
    r = parse_date_range("2026-10-01", "2026-10-31")
    assert doc_range_clause("generated_at", r, z_suffix=True) == {
        "range": {"generated_at": {
            "gte": "2026-10-01T00:00:00.000000Z", "lt": "2026-11-01T00:00:00.000000Z",
        }}
    }


def _pydantic_form(dt: datetime) -> str:
    # What model_dump(mode="json") writes for a UTC datetime.
    from pydantic import TypeAdapter
    return TypeAdapter(datetime).dump_python(dt, mode="json")


_INSTANTS = [
    datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
    datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC),
    datetime(2026, 10, 1, tzinfo=UTC),
    datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC),
    datetime(2026, 10, 1, 0, 0, 1, tzinfo=UTC),
    datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC),
    datetime(2026, 11, 1, tzinfo=UTC),
    datetime(2026, 11, 1, 0, 0, 0, 1, tzinfo=UTC),
]


@pytest.mark.parametrize("z_suffix", [True, False])
def test_date_only_bounds_match_chronology_for_both_stored_forms(z_suffix):
    """gte/lt text comparison agrees with instant comparison for every stored form."""
    r = parse_date_range("2026-10-01", "2026-10-31")
    gte = to_doc_bound(r.gte, z_suffix=z_suffix, op="gte")
    lt = to_doc_bound(r.lt, z_suffix=z_suffix, op="lt")
    for inst in _INSTANTS:
        stored = _pydantic_form(inst) if z_suffix else inst.isoformat()
        assert (gte <= stored < lt) == (r.gte <= inst < r.lt), stored


def test_z_lte_includes_exact_equal_value():
    bound = to_doc_bound(datetime(2026, 10, 1, 12, tzinfo=UTC), z_suffix=True, op="lte")
    assert _pydantic_form(datetime(2026, 10, 1, 12, tzinfo=UTC)) <= bound
    assert _pydantic_form(datetime(2026, 10, 1, 11, 59, 59, 999999, tzinfo=UTC)) <= bound
    assert not _pydantic_form(datetime(2026, 10, 1, 12, 0, 1, tzinfo=UTC)) <= bound
