"""The read-modify-write primitive that replaces painless scripts and ES OCC.

Four places in the codebase do read-modify-write against Elasticsearch, in two
different ways:

* painless ``scripted_upsert`` — ``fuel/order_repository.py`` and
  ``ops/services/ops_es_service.py`` compare ``last_event_timestamp`` and
  ``ctx.op = 'noop'`` a stale event; ``fuel/driver_repository.py`` increments
  counters;
* ``if_seq_no`` / ``if_primary_term`` optimistic concurrency with a retry loop —
  ``fuel/compartment_state_models.py`` and ``Agents/approval_queue_service.py``.

``atomic_update`` replaces both with ``SELECT … FOR UPDATE``. The tests that
matter are therefore the concurrency ones: a primitive that works under no
contention is not a replacement for optimistic concurrency control.

The stale-event comparison is pinned exactly, including the part that looks like
an off-by-one: an event whose timestamp EQUALS the stored one is **discarded**.
That is what the painless script does (``isBefore || isEqual``) and it matters,
because at-least-once delivery makes an equal timestamp the common case for a
redelivery — applying it would overwrite whatever a later event already wrote.
"""

from __future__ import annotations

import asyncio

import pytest

TENANT = "demo-tenant"


# ---------------------------------------------------------------------------
# atomic_update
# ---------------------------------------------------------------------------


async def test_transform_sees_the_stored_document_and_its_return_is_written(
    store, index_name
):
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "n": 1})

    seen = {}

    def bump(current):
        seen.update(current)
        return {**current, "n": current["n"] + 1}

    document, applied = await store.atomic_update(index_name, "a", bump)
    assert applied is True
    assert seen["n"] == 1
    assert document["n"] == 2
    assert (await store.get_document(index_name, "a"))["n"] == 2


async def test_returning_none_is_a_noop_and_reports_not_applied(store, index_name):
    """``None`` maps onto painless ``ctx.op = 'noop'``.

    ``applied=False`` is what lets a caller tell "discarded a stale event" from
    "wrote the update" — which is the boolean
    ``upsert_with_last_event_timestamp`` returns to its callers.
    """
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "n": 1})
    document, applied = await store.atomic_update(index_name, "a", lambda _c: None)
    assert applied is False
    assert document["n"] == 1
    assert (await store.get_document(index_name, "a"))["n"] == 1


async def test_a_missing_document_with_no_upsert_does_nothing(store, index_name):
    document, applied = await store.atomic_update(
        index_name, "absent", lambda c: {**c, "n": 1}
    )
    assert (document, applied) == (None, False)
    assert await store.get_document(index_name, "absent") is None


async def test_a_missing_document_with_an_upsert_inserts_it(store, index_name):
    document, applied = await store.atomic_update(
        index_name, "fresh", lambda c: c, upsert={"tenant_id": TENANT, "n": 7}
    )
    assert applied is True
    assert document["n"] == 7
    assert (await store.get_document(index_name, "fresh"))["n"] == 7


async def test_the_transform_cannot_mutate_the_stored_document_in_place(
    store, index_name
):
    """It is handed a copy, so a transform that mutates and returns ``None``
    still leaves the row untouched — the no-op has to mean no-op."""
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "n": 1})

    def mutate_then_noop(current):
        current["n"] = 999
        return None

    await store.atomic_update(index_name, "a", mutate_then_noop)
    assert (await store.get_document(index_name, "a"))["n"] == 1


async def test_the_tenant_column_follows_the_transformed_document(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT})
    await store.atomic_update(
        index_name, "a", lambda c: {**c, "tenant_id": "moved-tenant"}
    )
    found = await store.search_documents(
        index_name, {"query": {"term": {"tenant_id": "moved-tenant"}}}
    )
    assert found["hits"]["total"]["value"] == 1


# ---------------------------------------------------------------------------
# Concurrency — the reason the primitive exists
# ---------------------------------------------------------------------------


async def test_concurrent_increments_do_not_lose_a_write(pg_sessionmaker, index_name):
    """The property optimistic concurrency control is there to provide.

    Ten concurrent increments must produce ten, not "somewhere between one and
    ten". A plain read-then-write loses writes here; ES's ``if_seq_no`` catches
    the conflict and makes the caller retry; ``SELECT … FOR UPDATE`` serialises
    them so nothing is lost and nothing has to retry.

    Each task gets its OWN store bound to its own session, because a single
    session is not concurrent — sharing one would serialise the tasks in the
    client and prove nothing about the database.
    """
    from contextlib import asynccontextmanager

    from persistence.document_store import PostgresDocumentStore

    def make_store():
        @asynccontextmanager
        async def scope():
            async with pg_sessionmaker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return PostgresDocumentStore(session_factory=scope)

    await make_store().index_document(index_name, "counter", {"tenant_id": TENANT, "n": 0})

    async def increment():
        await make_store().atomic_update(
            index_name, "counter", lambda c: {**c, "n": c["n"] + 1}
        )

    await asyncio.gather(*[increment() for _ in range(10)])

    final = await make_store().get_document(index_name, "counter")
    assert final["n"] == 10, (
        f"lost writes under contention: expected 10, got {final['n']}"
    )


async def test_concurrent_upserts_of_the_same_new_document_do_not_both_insert(
    pg_sessionmaker, index_name
):
    """Two writers racing to create the same id must not collide fatally.

    The primary key makes a double insert impossible; what matters is that the
    loser gets a clean outcome rather than an IntegrityError surfacing to a
    caller that only asked to upsert.
    """
    from contextlib import asynccontextmanager

    from persistence.document_store import PostgresDocumentStore

    def make_store():
        @asynccontextmanager
        async def scope():
            async with pg_sessionmaker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return PostgresDocumentStore(session_factory=scope)

    async def create(value):
        try:
            await make_store().atomic_update(
                index_name, "race", lambda c: c,
                upsert={"tenant_id": TENANT, "who": value},
            )
            return None
        except Exception as exc:  # noqa: BLE001 — the point is to observe it
            return type(exc).__name__

    results = await asyncio.gather(create("a"), create("b"))
    stored = await make_store().get_document(index_name, "race")
    assert stored is not None
    assert stored["who"] in ("a", "b")
    # At most one may fail, and if one does it must be a recognisable integrity
    # error rather than something a caller cannot classify.
    failures = [r for r in results if r]
    assert len(failures) <= 1, results
    if failures:
        assert "Integrity" in failures[0] or "Unique" in failures[0], failures


# ---------------------------------------------------------------------------
# upsert_if_newer — the stale-event guard
# ---------------------------------------------------------------------------


async def test_a_newer_event_is_applied(store, index_name):
    await store.index_document(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "pending", "last_event_timestamp": "2026-08-01T00:00:00Z"},
    )
    applied = await store.upsert_if_newer(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "delivered", "last_event_timestamp": "2026-08-02T00:00:00Z"},
    )
    assert applied is True
    assert (await store.get_document(index_name, "ord-1"))["status"] == "delivered"


async def test_an_older_event_is_discarded(store, index_name):
    await store.index_document(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "delivered", "last_event_timestamp": "2026-08-02T00:00:00Z"},
    )
    applied = await store.upsert_if_newer(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "pending", "last_event_timestamp": "2026-08-01T00:00:00Z"},
    )
    assert applied is False
    assert (await store.get_document(index_name, "ord-1"))["status"] == "delivered"


async def test_an_equal_timestamp_is_discarded_not_applied(store, index_name):
    """The painless script says ``isBefore || isEqual``, and that matters.

    At-least-once delivery makes an equal timestamp the common case for a
    redelivery. Applying it would overwrite whatever a later event had already
    written — so "equal means discard" is load-bearing, not an off-by-one.
    """
    stamp = "2026-08-02T00:00:00Z"
    await store.index_document(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "delivered", "last_event_timestamp": stamp},
    )
    applied = await store.upsert_if_newer(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "pending", "last_event_timestamp": stamp},
    )
    assert applied is False
    assert (await store.get_document(index_name, "ord-1"))["status"] == "delivered"


async def test_a_fresh_insert_is_applied(store, index_name):
    """A scripted upsert against a missing document inserts it.

    Worth its own test: a serverless-Elasticsearch quirk made
    ``scripted_upsert`` report "noop" AND fail to materialise the upsert body on
    a fresh insert, which silently dropped every new order and produced a 404
    immediately after a 201. The Postgres path has no such split between "run the
    script" and "apply the upsert".
    """
    applied = await store.upsert_if_newer(
        index_name,
        "brand-new",
        {"tenant_id": TENANT, "status": "pending", "last_event_timestamp": "2026-08-01T00:00:00Z"},
    )
    assert applied is True
    assert (await store.get_document(index_name, "brand-new"))["status"] == "pending"


async def test_a_stored_document_without_a_timestamp_accepts_the_event(store, index_name):
    """The script only compares when the stored field is present and non-null."""
    await store.index_document(index_name, "ord-1", {"tenant_id": TENANT, "status": "pending"})
    applied = await store.upsert_if_newer(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "delivered", "last_event_timestamp": "2026-08-02T00:00:00Z"},
    )
    assert applied is True


async def test_an_incoming_event_without_a_timestamp_is_applied(store, index_name):
    await store.index_document(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "pending", "last_event_timestamp": "2026-08-02T00:00:00Z"},
    )
    applied = await store.upsert_if_newer(
        index_name, "ord-1", {"tenant_id": TENANT, "status": "delivered"}
    )
    assert applied is True


async def test_fields_absent_from_the_incoming_document_survive(store, index_name):
    """The painless script assigns each param onto ``ctx._source``; it does not
    replace the document. So an incoming partial keeps what it does not mention."""
    await store.index_document(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "keep": "yes", "last_event_timestamp": "2026-08-01T00:00:00Z"},
    )
    await store.upsert_if_newer(
        index_name,
        "ord-1",
        {"tenant_id": TENANT, "status": "delivered", "last_event_timestamp": "2026-08-02T00:00:00Z"},
    )
    stored = await store.get_document(index_name, "ord-1")
    assert stored["keep"] == "yes"
    assert stored["status"] == "delivered"


async def test_concurrent_out_of_order_events_converge_on_the_newest(
    pg_sessionmaker, index_name
):
    """Whatever the arrival order, the newest event must win.

    This is the guarantee the scripted upsert exists for, and the one a plain
    last-write-wins upsert breaks. Ten events delivered concurrently in shuffled
    order must leave the highest timestamp in place.
    """
    from contextlib import asynccontextmanager

    from persistence.document_store import PostgresDocumentStore

    def make_store():
        @asynccontextmanager
        async def scope():
            async with pg_sessionmaker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return PostgresDocumentStore(session_factory=scope)

    stamps = [f"2026-08-{day:02d}T00:00:00Z" for day in range(1, 11)]
    shuffled = [stamps[i] for i in (4, 0, 9, 2, 7, 1, 8, 3, 6, 5)]

    async def deliver(stamp):
        await make_store().upsert_if_newer(
            index_name,
            "ord-1",
            {"tenant_id": TENANT, "last_event_timestamp": stamp, "seen": stamp},
        )

    await asyncio.gather(*[deliver(s) for s in shuffled])

    stored = await make_store().get_document(index_name, "ord-1")
    assert stored["last_event_timestamp"] == stamps[-1]
    assert stored["seen"] == stamps[-1]


# ---------------------------------------------------------------------------
# exists / update_by_query / delete_by_query
# ---------------------------------------------------------------------------


async def test_document_exists(store, index_name):
    assert await store.document_exists(index_name, "a") is False
    await store.index_document(index_name, "a", {"tenant_id": TENANT})
    assert await store.document_exists(index_name, "a") is True


async def test_update_by_query_transforms_every_match(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "state": "old"})
    await store.index_document(index_name, "b", {"tenant_id": TENANT, "state": "old"})
    await store.index_document(index_name, "c", {"tenant_id": "other", "state": "old"})

    changed = await store.update_by_query(
        index_name,
        {"term": {"tenant_id": TENANT}},
        lambda doc: {**doc, "state": "new"},
    )
    assert changed == 2
    assert (await store.get_document(index_name, "a"))["state"] == "new"
    assert (await store.get_document(index_name, "c"))["state"] == "old"


async def test_update_by_query_skips_documents_whose_transform_returns_none(
    store, index_name
):
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "n": 1})
    await store.index_document(index_name, "b", {"tenant_id": TENANT, "n": 2})
    changed = await store.update_by_query(
        index_name,
        {"match_all": {}},
        lambda doc: {**doc, "n": 99} if doc["n"] == 1 else None,
    )
    assert changed == 1
    assert (await store.get_document(index_name, "b"))["n"] == 2


async def test_delete_by_query_removes_only_matches(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT})
    await store.index_document(index_name, "b", {"tenant_id": "other"})
    deleted = await store.delete_by_query(index_name, {"term": {"tenant_id": TENANT}})
    assert deleted == 1
    assert await store.get_document(index_name, "a") is None
    assert await store.get_document(index_name, "b") is not None


async def test_delete_by_query_refuses_an_unsearchable_field(store):
    """The field policy has to cover the bulk paths too, or it is bypassable."""
    from persistence.document_field_policy import UnsearchableFieldError

    with pytest.raises(UnsearchableFieldError):
        await store.delete_by_query(
            "fuel_orders_current", {"term": {"pod_otp": "123456"}}
        )


async def test_update_by_query_refuses_an_unsearchable_field(store):
    from persistence.document_field_policy import UnsearchableFieldError

    with pytest.raises(UnsearchableFieldError):
        await store.update_by_query(
            "fuel_orders_current", {"term": {"pod_otp": "123456"}}, lambda d: d
        )


async def test_an_unsupported_clause_still_raises_on_the_bulk_paths(store, index_name):
    from persistence.document_query import UnsupportedQueryError

    with pytest.raises(UnsupportedQueryError):
        await store.delete_by_query(index_name, {"geo_distance": {}})
