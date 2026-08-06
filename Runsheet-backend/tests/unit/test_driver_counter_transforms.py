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


class _FakeNotFound(Exception):
    status_code = 404


class _FakeClient:
    """The subset of the ES client ``update_by_query`` drives, in memory."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.searches: List[Dict[str, Any]] = []

    def get(self, *, index: str, id: str) -> Dict[str, Any]:
        if id not in self.docs:
            raise _FakeNotFound(id)
        return {"_source": dict(self.docs[id]), "_seq_no": 1, "_primary_term": 1}

    def index(self, *, index: str, id: str, body: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        self.docs[id] = dict(body)
        return {"result": "updated"}

    def search(self, *, index: str, body: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        # Matching is not the point here — the point is that every returned id is
        # re-read and transformed. The DSL itself is exercised against the real
        # implementation in the Postgres tests.
        self.searches.append(dict(body))
        return {"hits": {"hits": [{"_id": doc_id} for doc_id in self.docs]}}


class _FakeService:
    """Runs the REAL facade methods against :class:`_FakeClient`.

    Borrowed rather than reimplemented: a reimplementation would let the shipped
    fan-out and this fake drift, and the cap assertion below would then be testing
    the fake. Real circuit breakers because ``search_documents`` executes through
    one.
    """

    from services.elasticsearch_service import ElasticsearchService as _Real

    atomic_update = _Real.atomic_update
    update_by_query = _Real.update_by_query
    search_documents = _Real.search_documents
    _is_retired_index = _Real._is_retired_index
    _handle_circuit_breaker_exception = _Real._handle_circuit_breaker_exception
    _handle_elasticsearch_error = _Real._handle_elasticsearch_error
    UPDATE_BY_QUERY_MAX_DOCS = _Real.UPDATE_BY_QUERY_MAX_DOCS
    del _Real

    def __init__(self, client: _FakeClient) -> None:
        from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        self.client = client
        self._circuit_breaker = CircuitBreaker(
            name="test_write", config=CircuitBreakerConfig(failure_threshold=10)
        )
        self._read_circuit_breaker = CircuitBreaker(
            name="test_read", config=CircuitBreakerConfig(failure_threshold=10)
        )

    def _pg_store(self) -> Optional[object]:
        """Exercise the Elasticsearch branch."""
        return None


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def service(client: _FakeClient) -> _FakeService:
    return _FakeService(client)


class TestUpdateByQueryOnElasticsearch:
    async def test_it_transforms_every_hit_and_returns_the_count(
        self, service: _FakeService, client: _FakeClient
    ):
        client.docs = {"a": {"completed_today": 3}, "b": {"completed_today": 7}}

        changed = await service.update_by_query(
            "drivers", {"match_all": {}}, lambda doc: {**doc, "completed_today": 0}
        )

        assert changed == 2
        assert client.docs["a"]["completed_today"] == 0
        assert client.docs["b"]["completed_today"] == 0

    async def test_a_transform_returning_none_is_not_counted(
        self, service: _FakeService, client: _FakeClient
    ):
        """Matching the store, where ``None`` skips the row."""
        client.docs = {"a": {"n": 1}, "b": {"n": 2}}

        changed = await service.update_by_query(
            "drivers",
            {"match_all": {}},
            lambda doc: {**doc, "n": 0} if doc["n"] == 1 else None,
        )

        assert changed == 1
        assert client.docs["b"]["n"] == 2

    async def test_it_fetches_ids_only(self, service: _FakeService, client: _FakeClient):
        """Each hit is re-read inside ``atomic_update`` under its own version
        assertion. Transforming a body fetched by the search instead would
        reintroduce the lost update the whole primitive exists to avoid."""
        client.docs = {"a": {"n": 1}}

        await service.update_by_query("drivers", {"match_all": {}}, lambda doc: doc)

        assert client.searches[0]["_source"] is False

    async def test_it_refuses_to_apply_a_partial_update(
        self, service: _FakeService, client: _FakeClient, monkeypatch
    ):
        """Over the cap it raises rather than updating a prefix.

        A partially-applied ``update_by_query`` leaves the index in a state no
        caller asked for and no caller can detect — worse than a loud failure.
        """
        monkeypatch.setattr(service, "UPDATE_BY_QUERY_MAX_DOCS", 2)
        client.docs = {"a": {"n": 1}, "b": {"n": 1}, "c": {"n": 1}}

        with pytest.raises(Exception) as excinfo:
            await service.update_by_query(
                "drivers", {"match_all": {}}, lambda doc: {**doc, "n": 0}
            )

        assert "partial" in str(excinfo.value).lower()
        assert all(doc["n"] == 1 for doc in client.docs.values())

    async def test_a_retired_index_is_skipped(
        self, service: _FakeService, client: _FakeClient, monkeypatch
    ):
        """A retired index has been dropped; writing would recreate it with
        dynamic mappings, which is how a ``keyword`` tenant filter silently
        becomes ``text`` and matches nothing."""
        client.docs = {"a": {"n": 1}}
        monkeypatch.setattr(service, "_is_retired_index", lambda index: True)

        assert await service.update_by_query("drivers", {"match_all": {}}, lambda d: d) == 0
        assert client.docs["a"]["n"] == 1
