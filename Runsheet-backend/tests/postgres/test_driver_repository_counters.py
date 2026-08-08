"""``DriverRepository`` counter writes, over the real Postgres document store.

The two methods here were the last painless scripts in the codebase. They ran
inside Elasticsearch, which is why they had to reach past ``ElasticsearchService``
to ``client.update`` and ``client.update_by_query`` — and why they would have gone
on writing to Elasticsearch after ``DOCUMENT_STORE_BACKEND=postgres`` moved the
reads. ``active_order_count`` decides who the dispatcher assigns the next load to,
so counters frozen at their cutover values means every subsequent assignment is
made on stale data.

Unit tests cover the arithmetic. What only a real store can cover is the part that
was written in the query DSL rather than in painless: ``reset_completed_today``
filters on ``tenant_id`` and ``completed_today > 0``, and those predicates are now
compiled to SQL. A test with a hand-written matcher would be checking the
hand-written matcher.

Both methods are driven through the facade, not the store, because the facade is
what holds the backend switch — that is the seam the migration turns on.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fuel.driver_repository import DriverRepository

TENANT = "demo-tenant"
OTHER_TENANT = "other-tenant"


class _PostgresBackedFacade:
    """``ElasticsearchService`` with ``_pg_store()`` pinned to the test store.

    Every method is the real one. They all short-circuit to the store when
    ``_pg_store()`` returns something, so no client and no circuit breaker are
    needed — which is the same reason the production Postgres path does not touch
    them either.
    """

    from services.elasticsearch_service import ElasticsearchService as _Real

    atomic_update = _Real.atomic_update
    update_by_query = _Real.update_by_query
    search_documents = _Real.search_documents
    index_document = _Real.index_document
    update_document = _Real.update_document
    get_document = _Real.get_document
    _is_retired_index = _Real._is_retired_index
    del _Real

    def __init__(self, store: Any) -> None:
        self._store = store

    def _pg_store(self) -> Any:
        return self._store


def _driver_doc(driver_id: str, *, tenant_id: str = TENANT, **overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "driver_id": driver_id,
        "tenant_id": tenant_id,
        "driver_name": f"Driver {driver_id}",
        "status": "active",
        "active_order_count": 0,
        "completed_today": 0,
        "last_event_timestamp": "2026-01-01T00:00:00+00:00",
        "source_schema_version": "1.0.0",
        "trace_id": f"trace-{driver_id}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def facade(store) -> _PostgresBackedFacade:
    return _PostgresBackedFacade(store)


@pytest.fixture
def repo(facade: _PostgresBackedFacade, index_name: str) -> DriverRepository:
    return DriverRepository(facade, drivers_index=index_name)


async def _seed(store, index_name: str, *docs: Dict[str, Any]) -> None:
    for doc in docs:
        await store.index_document(index_name, doc["driver_id"], dict(doc))


# ---------------------------------------------------------------------------
# increment_counters
# ---------------------------------------------------------------------------


async def test_increment_counters_writes_to_postgres(repo, store, index_name):
    await _seed(store, index_name, _driver_doc("d1", active_order_count=2))

    assert await repo.increment_counters(TENANT, "d1", delta_active=1) is True

    stored = await store.get_document(index_name, "d1")
    assert stored["active_order_count"] == 3


async def test_increment_counters_clamps_at_zero(repo, store, index_name):
    await _seed(store, index_name, _driver_doc("d1", active_order_count=1))

    await repo.increment_counters(TENANT, "d1", delta_active=-5)

    stored = await store.get_document(index_name, "d1")
    assert stored["active_order_count"] == 0


async def test_increment_counters_moves_both_counters(repo, store, index_name):
    await _seed(
        store, index_name, _driver_doc("d1", active_order_count=1, completed_today=4)
    )

    await repo.increment_counters(TENANT, "d1", delta_active=-1, delta_completed=1)

    stored = await store.get_document(index_name, "d1")
    assert stored["active_order_count"] == 0
    assert stored["completed_today"] == 5


async def test_increment_counters_returns_false_for_an_unknown_driver(repo):
    assert await repo.increment_counters(TENANT, "nope", delta_active=1) is False


async def test_increment_counters_will_not_touch_another_tenants_driver(
    repo, store, index_name
):
    """The ownership check runs before the write, and no write happens.

    Reported as "not found" rather than as a permission error: ``get`` degrades a
    cross-tenant hit to ``None`` so existence does not leak. What matters here is
    the counter, which must be exactly as it was — a tenant able to move another
    tenant's ``active_order_count`` could starve or overload their dispatch.
    """
    await _seed(store, index_name, _driver_doc("d1", tenant_id=OTHER_TENANT))

    assert await repo.increment_counters(TENANT, "d1", delta_active=1) is False

    stored = await store.get_document(index_name, "d1")
    assert stored["active_order_count"] == 0


async def test_increment_counters_leaves_the_driver_model_valid(repo, store, index_name):
    """``Driver`` forbids extra fields and requires ``active_order_count >= 0``.

    A transform that added a stray key, or left a negative counter, would make the
    document unloadable — and ``_safe_driver_load`` swallows that into a warning
    and a dropped driver, so the driver would simply vanish from every listing.
    """
    await _seed(store, index_name, _driver_doc("d1", active_order_count=1))

    await repo.increment_counters(TENANT, "d1", delta_active=-3, delta_completed=1)

    assert await repo.get(TENANT, "d1") is not None


# ---------------------------------------------------------------------------
# reset_completed_today
# ---------------------------------------------------------------------------


async def test_reset_completed_today_zeroes_matching_drivers(repo, store, index_name):
    await _seed(
        store,
        index_name,
        _driver_doc("d1", completed_today=3),
        _driver_doc("d2", completed_today=7),
    )

    assert await repo.reset_completed_today(TENANT) == 2

    assert (await store.get_document(index_name, "d1"))["completed_today"] == 0
    assert (await store.get_document(index_name, "d2"))["completed_today"] == 0


async def test_reset_completed_today_skips_drivers_already_at_zero(
    repo, store, index_name
):
    """The ``completed_today > 0`` filter, compiled to SQL rather than to painless.

    Without it the nightly job rewrites every driver document in the tenant every
    night, which is a lot of write amplification for no change.
    """
    await _seed(
        store,
        index_name,
        _driver_doc("d1", completed_today=0),
        _driver_doc("d2", completed_today=5),
    )

    assert await repo.reset_completed_today(TENANT) == 1


async def test_reset_completed_today_does_not_cross_tenants(repo, store, index_name):
    """The tenant term, evaluated by the real predicate builder.

    This is the assertion that would have been vacuous against a fake: the filter
    is two clauses of query DSL and the question is whether they compile to SQL
    that actually excludes the other tenant.
    """
    await _seed(
        store,
        index_name,
        _driver_doc("mine", completed_today=3),
        _driver_doc("theirs", tenant_id=OTHER_TENANT, completed_today=9),
    )

    assert await repo.reset_completed_today(TENANT) == 1

    assert (await store.get_document(index_name, "mine"))["completed_today"] == 0
    assert (await store.get_document(index_name, "theirs"))["completed_today"] == 9


async def test_reset_completed_today_leaves_last_event_timestamp_alone(
    repo, store, index_name
):
    """A bumped ``last_event_timestamp`` would make pre-midnight events look stale
    to ``upsert_if_newer`` for the rest of the day."""
    await _seed(
        store,
        index_name,
        _driver_doc(
            "d1", completed_today=3, last_event_timestamp="2025-12-31T23:00:00+00:00"
        ),
    )

    await repo.reset_completed_today(TENANT)

    stored = await store.get_document(index_name, "d1")
    assert stored["last_event_timestamp"] == "2025-12-31T23:00:00+00:00"


async def test_reset_completed_today_is_zero_when_nothing_matches(repo, store, index_name):
    await _seed(store, index_name, _driver_doc("d1", completed_today=0))

    assert await repo.reset_completed_today(TENANT) == 0
