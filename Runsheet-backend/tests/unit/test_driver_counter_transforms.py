"""The two painless scripts ``DriverRepository`` used, translated into Python.

``increment_counters`` sent a painless script to ``es.client.update`` and
``reset_completed_today`` sent one to ``es.client.update_by_query``. Both reached
past ``ElasticsearchService``, so both would have kept writing to Elasticsearch
after ``DOCUMENT_STORE_BACKEND=postgres`` moved the reads to Postgres — driver
counters would have frozen at whatever value they held at cutover, and
``active_order_count`` is what the dispatcher uses to decide who gets the next
load.

The arithmetic is now :func:`fuel.driver_repository._apply_counter_deltas` and
:func:`fuel.driver_repository._reset_completed_today`, run by the facade. A
translation is exactly the kind of change that passes review and is subtly wrong,
so the two details that were easy to lose are pinned individually: a missing
counter reads as zero, and the clamp applies to ``active_order_count`` only.

The end-to-end behaviour over the real query DSL lives in
``tests/postgres/test_driver_repository_counters.py``, which runs the same two
methods against the actual document store. This file covers the transforms and
the Elasticsearch branch of ``ElasticsearchService.update_by_query``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fuel.driver_repository import _apply_counter_deltas, _reset_completed_today


# ---------------------------------------------------------------------------
# _apply_counter_deltas
# ---------------------------------------------------------------------------


class TestApplyCounterDeltas:
    def test_adds_both_deltas(self):
        result = _apply_counter_deltas(
            {"active_order_count": 2, "completed_today": 5},
            delta_active=1,
            delta_completed=1,
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["active_order_count"] == 3
        assert result["completed_today"] == 6

    def test_clamps_active_at_zero(self):
        """The painless original clamped; a negative count would break the ge=0 model."""
        result = _apply_counter_deltas(
            {"active_order_count": 1},
            delta_active=-5,
            delta_completed=0,
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["active_order_count"] == 0

    def test_does_not_clamp_completed(self):
        """Only ``active_order_count`` was clamped, and copying the clamp to both
        would hide a caller that started decrementing ``completed_today``."""
        result = _apply_counter_deltas(
            {"completed_today": 1},
            delta_active=0,
            delta_completed=-5,
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["completed_today"] == -4

    @pytest.mark.parametrize("stored", [{}, {"active_order_count": None}])
    def test_a_missing_or_null_counter_reads_as_zero(self, stored):
        """Drivers created before the counters existed have neither field.

        The painless script used ``!= null ? : 0``; a plain ``current[...] + 1``
        would raise on exactly those documents.
        """
        result = _apply_counter_deltas(
            dict(stored),
            delta_active=1,
            delta_completed=0,
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["active_order_count"] == 1

    def test_a_zero_delta_leaves_the_counter_absent_rather_than_creating_it(self):
        """``if (params.delta_active != 0)`` guarded the write, so zero touched nothing."""
        result = _apply_counter_deltas(
            {},
            delta_active=0,
            delta_completed=0,
            now="2026-01-01T00:00:00+00:00",
        )
        assert "active_order_count" not in result
        assert "completed_today" not in result

    def test_timestamps_are_always_stamped(self):
        """Unconditional in the script, and what makes the update never a no-op."""
        result = _apply_counter_deltas(
            {}, delta_active=0, delta_completed=0, now="2026-01-01T00:00:00+00:00"
        )
        assert result["last_event_timestamp"] == "2026-01-01T00:00:00+00:00"
        assert result["updated_at"] == "2026-01-01T00:00:00+00:00"

    def test_the_stored_document_is_not_mutated_in_place(self):
        """``atomic_update`` compares the return against the original."""
        stored = {"active_order_count": 2}
        _apply_counter_deltas(
            stored, delta_active=1, delta_completed=0, now="2026-01-01T00:00:00+00:00"
        )
        assert stored == {"active_order_count": 2}

    def test_unrelated_fields_survive(self):
        result = _apply_counter_deltas(
            {"driver_name": "Ada", "active_order_count": 0},
            delta_active=1,
            delta_completed=0,
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["driver_name"] == "Ada"


class TestResetCompletedToday:
    def test_zeroes_the_counter_and_stamps_updated_at(self):
        result = _reset_completed_today(
            {"completed_today": 9}, now="2026-01-01T00:00:00+00:00"
        )
        assert result["completed_today"] == 0
        assert result["updated_at"] == "2026-01-01T00:00:00+00:00"

    def test_does_not_touch_last_event_timestamp(self):
        """The reset script set only ``completed_today`` and ``updated_at``.

        ``last_event_timestamp`` drives the out-of-order guard in
        ``upsert_if_newer``; a nightly cron bumping it would make every event that
        arrived before midnight look stale for the rest of the day.
        """
        result = _reset_completed_today(
            {"completed_today": 9, "last_event_timestamp": "2025-12-31T23:00:00+00:00"},
            now="2026-01-01T00:00:00+00:00",
        )
        assert result["last_event_timestamp"] == "2025-12-31T23:00:00+00:00"


# ---------------------------------------------------------------------------
# ElasticsearchService.update_by_query — the Elasticsearch branch
# ---------------------------------------------------------------------------














# The Elasticsearch branch of ``update_by_query`` was tested here — a fake client,
# a fake facade borrowing the real method, and four tests covering the fan-out, the
# no-op transform, the ids-only fetch and the refusal to apply a partial update.
# Phase 6 deleted that branch, so all of it went with it.
#
# ``update_by_query`` is now one delegation to
# ``PostgresDocumentStore.update_by_query``, which is covered directly in
# ``tests/postgres/test_document_store_atomic.py`` (including the transform
# returning ``None``, and refusing an unsearchable field), and end to end through
# ``DriverRepository.reset_completed_today`` in
# ``tests/postgres/test_driver_repository_counters.py`` — where the tenant filter
# and the ``completed_today > 0`` range are compiled to real SQL rather than matched
# by a fake.
#
# The scan cap that lived on the facade went with the branch: the store applies the
# transform in one statement over locked rows, so there is no round-trip count to
# bound.
