"""Unit tests for the parity-check comparison helpers.

These guard the value-equivalence rules that let the ES ``_source`` and the
Postgres projection compare equal despite benign representation differences
(null vs empty collection, date vs full-datetime), while still flagging real
divergences.
"""

from __future__ import annotations

from persistence.parity_check import _diff_doc, _values_equal


def test_null_equals_empty_collection():
    assert _values_equal(None, [])
    assert _values_equal(None, {})
    assert _values_equal([], None)


def test_date_only_equals_full_datetime_same_day():
    assert _values_equal("2026-05-29T19:52:17.259901+00:00", "2026-05-29")
    assert _values_equal("2026-05-29", "2026-05-29T00:00:00+00:00")


def test_different_dates_not_equal():
    assert not _values_equal("2026-05-29T00:00:00+00:00", "2026-05-30")


def test_scalar_equality_and_inequality():
    assert _values_equal(3500, 3500)
    assert not _values_equal(3500, 3600)
    assert _values_equal("open", "open")
    assert not _values_equal("open", "paid")


def test_diff_doc_ignores_configured_fields():
    # updated_at is in the ignore set for invoices -> no diff reported.
    es = {"invoice_id": "inv_1", "updated_at": "2026-01-01T00:00:00+00:00",
          "status": "open", "exemptions_applied": None}
    pg = {"invoice_id": "inv_1", "updated_at": "2026-02-02T00:00:00+00:00",
          "status": "open", "exemptions_applied": []}
    assert _diff_doc("invoice", es, pg) == []


def test_diff_doc_flags_real_divergence():
    es = {"invoice_id": "inv_1", "status": "open", "total_cents": 35000}
    pg = {"invoice_id": "inv_1", "status": "paid", "total_cents": 35000}
    diffs = _diff_doc("invoice", es, pg)
    assert len(diffs) == 1
    assert "status" in diffs[0]
