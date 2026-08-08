"""The Postgres document store must be a drop-in for the ElasticsearchService API.

The property test next door proves the *filter* translation. This module covers
everything else a caller depends on, in the shape a caller depends on it:

* the response envelope, because 130 files index into
  ``response["hits"]["hits"][0]["_source"]``;
* ``total`` reporting the full match count, not the page size — the distinction
  that produced the "3 unassigned orders, then 912" contradiction in the agent;
* sorting through jsonb ordering, which is the reason numbers sort numerically
  instead of ``"10" < "9"``;
* aggregations computed over the whole match set rather than the returned page;
* the write path's id rules, since two of the three fuel-asset indices could not
  use the obvious id field;
* and that an unsupported clause RAISES. That last one is the point of the
  design: a store that quietly dropped a clause it could not translate would
  return wrong rows and no caller could tell.

Real PostgreSQL required — see ``conftest.py``.
"""

from __future__ import annotations

import pytest

from persistence.document_aggregations import (
    AggregationInputTooLarge,
    UnsupportedAggregationError,
)
from persistence.document_query import UnsupportedQueryError
from persistence.document_store import DocumentNotFound

TENANT = "demo-tenant"


async def _seed(store, index, docs):
    for doc_id, doc in docs.items():
        await store.index_document(index, doc_id, dict(doc))


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def test_index_then_get_round_trips_the_body(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "n": 1})
    stored = await store.get_document(index_name, "a")
    assert stored["tenant_id"] == TENANT
    assert stored["n"] == 1
    # The ES facade stamps both timestamps; callers read them back off the
    # response and off the index, so the store stamps them too.
    assert "created_at" in stored and "updated_at" in stored


async def test_reindexing_replaces_rather_than_merges(store, index_name):
    """``index_document`` is a whole-document write, as it is in Elasticsearch."""
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "keep": 1, "drop": 2})
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "keep": 3})
    stored = await store.get_document(index_name, "a")
    assert stored["keep"] == 3
    assert "drop" not in stored


async def test_update_document_merges_only_the_named_fields(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT, "keep": 1, "status": "new"})
    await store.update_document(index_name, "a", {"status": "done"})
    stored = await store.get_document(index_name, "a")
    assert stored["status"] == "done"
    assert stored["keep"] == 1


async def test_update_replaces_a_nested_object_it_does_not_deep_merge(store, index_name):
    """Matches the ES ``_update`` ``{"doc": ...}`` contract exactly.

    A deep merge would preserve subfields the Elasticsearch path drops. Callers
    were written against the ES behaviour, so reproducing it — even though a deep
    merge is arguably nicer — is what keeps them correct.
    """
    await store.index_document(
        index_name, "a", {"tenant_id": TENANT, "loc": {"lat": 1.0, "lon": 2.0}}
    )
    await store.update_document(index_name, "a", {"loc": {"lat": 9.0}})
    stored = await store.get_document(index_name, "a")
    assert stored["loc"] == {"lat": 9.0}


async def test_update_of_a_missing_document_raises(store, index_name):
    with pytest.raises(DocumentNotFound) as exc:
        await store.update_document(index_name, "nope", {"status": "done"})
    # 404 so the ElasticsearchService shim can map it the way callers expect.
    assert exc.value.status_code == 404


async def test_index_document_refuses_an_empty_id(store, index_name):
    """An empty id in ES mints a random one, producing an unreachable document."""
    with pytest.raises(ValueError):
        await store.index_document(index_name, "", {"tenant_id": TENANT})


async def test_delete_reports_whether_anything_was_removed(store, index_name):
    await store.index_document(index_name, "a", {"tenant_id": TENANT})
    assert await store.delete_document(index_name, "a") is True
    assert await store.delete_document(index_name, "a") is False
    assert await store.get_document(index_name, "a") is None


async def test_documents_are_scoped_to_their_index(store, index_name):
    """The primary key is ``(index, id)``, so the same id in two indices is two rows."""
    other = f"{index_name}_other"
    await store.index_document(index_name, "shared", {"tenant_id": TENANT, "which": "first"})
    await store.index_document(other, "shared", {"tenant_id": TENANT, "which": "second"})
    assert (await store.get_document(index_name, "shared"))["which"] == "first"
    assert (await store.get_document(other, "shared"))["which"] == "second"
    await store.delete_index(other)


async def test_bulk_index_reports_documents_it_cannot_key(store, index_name):
    """The ES path warns and lets the cluster mint an id, which is unreachable.

    Counting it as a failure is the deliberate difference: the caller gets a
    number that does not match what it sent, instead of a document nobody can
    find.

    Deliberately uses the per-test index name, not a real one. An earlier version
    ran against ``trucks`` to exercise the index-specific id map and cleaned up
    with ``delete_index("trucks")`` — which deleted the ``trucks`` documents the
    parity tool had copied into the shared development database. The id map is
    covered by the pure-function tests in
    ``tests/unit/test_document_store_backend_switch.py``, which need no database
    and cannot collide with anything.
    """
    result = await store.bulk_index_documents(
        index_name, [{"id": "T1", "tenant_id": TENANT}, {"tenant_id": TENANT}]
    )
    assert result["total"] == 2
    assert result["successful"] == 1
    assert result["failed"] == 1
    assert result["success"] is False
    assert "no id field found" in result["errors"][0]["reason"]
    assert await store.get_document(index_name, "T1") is not None


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


async def test_search_returns_the_elasticsearch_envelope(store, index_name):
    await _seed(store, index_name, {"a": {"tenant_id": TENANT, "status": "active"}})
    response = await store.search_documents(
        index_name, {"query": {"term": {"tenant_id": TENANT}}}
    )
    assert response["hits"]["total"] == {"value": 1, "relation": "eq"}
    hit = response["hits"]["hits"][0]
    assert hit["_id"] == "a"
    assert hit["_index"] == index_name
    assert hit["_score"] is None
    assert hit["_source"]["status"] == "active"


async def test_total_counts_the_whole_match_not_the_page(store, index_name):
    """The distinction that produced a self-contradicting agent answer.

    ``len(hits)`` is the page. A caller reporting it as a count says "3 orders"
    about a 912-order result set, which is exactly the defect fixed in the fuel
    agent's tools. The store must report both, separately and correctly.
    """
    await _seed(store, index_name, {f"d{i}": {"tenant_id": TENANT} for i in range(25)})
    response = await store.search_documents(
        index_name, {"query": {"term": {"tenant_id": TENANT}}, "size": 5}
    )
    assert response["hits"]["total"]["value"] == 25
    assert len(response["hits"]["hits"]) == 5


async def test_size_zero_returns_a_count_and_no_hits(store, index_name):
    await _seed(store, index_name, {f"d{i}": {"tenant_id": TENANT} for i in range(4)})
    response = await store.search_documents(index_name, {"query": {"match_all": {}}, "size": 0})
    assert response["hits"]["total"]["value"] == 4
    assert response["hits"]["hits"] == []


async def test_from_and_size_page_without_repeating_or_skipping(store, index_name):
    await _seed(
        store, index_name,
        {f"d{i:02d}": {"tenant_id": TENANT, "n": i} for i in range(10)},
    )
    seen = []
    for offset in (0, 4, 8):
        response = await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "sort": [{"n": {"order": "asc"}}],
                "from": offset,
                "size": 4,
            },
        )
        seen.extend(hit["_id"] for hit in response["hits"]["hits"])
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 10


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


async def test_numeric_sort_is_numeric_not_lexicographic(store, index_name):
    """The reason ordering goes through jsonb rather than ``->>``.

    Compared as text, ``"10"`` sorts before ``"9"``. Every "top N by quantity"
    read in the codebase would be wrong in a way that looks plausible.
    """
    await _seed(
        store, index_name,
        {"a": {"n": 9}, "b": {"n": 10}, "c": {"n": 100}, "d": {"n": 2}},
    )
    response = await store.search_documents(
        index_name, {"query": {"match_all": {}}, "sort": [{"n": {"order": "asc"}}]}
    )
    assert [h["_source"]["n"] for h in response["hits"]["hits"]] == [2, 9, 10, 100]


async def test_descending_sort_puts_documents_missing_the_field_last(store, index_name):
    """ES omits them from the head; leading with nulls would look like real data."""
    await _seed(store, index_name, {"a": {"n": 1}, "b": {}, "c": {"n": 5}})
    response = await store.search_documents(
        index_name, {"query": {"match_all": {}}, "sort": [{"n": {"order": "desc"}}]}
    )
    assert [h["_id"] for h in response["hits"]["hits"]] == ["c", "a", "b"]


async def test_sort_shorthand_forms_are_accepted(store, index_name):
    await _seed(store, index_name, {"a": {"n": 2}, "b": {"n": 1}})
    for sort in ("n", ["n"], [{"n": "asc"}], [{"n": {"order": "asc"}}]):
        response = await store.search_documents(
            index_name, {"query": {"match_all": {}}, "sort": sort}
        )
        assert [h["_id"] for h in response["hits"]["hits"]] == ["b", "a"], sort


async def test_ties_break_on_document_id_so_pages_are_stable(store, index_name):
    """``created_at`` is often stamped at second resolution, so ties are common."""
    await _seed(
        store, index_name,
        {f"d{i}": {"created_at": "2026-08-06T12:00:00+00:00"} for i in range(6)},
    )
    first = await store.search_documents(
        index_name,
        {"query": {"match_all": {}}, "sort": [{"created_at": "desc"}], "size": 3},
    )
    second = await store.search_documents(
        index_name,
        {
            "query": {"match_all": {}},
            "sort": [{"created_at": "desc"}],
            "from": 3,
            "size": 3,
        },
    )
    ids = [h["_id"] for h in first["hits"]["hits"]] + [
        h["_id"] for h in second["hits"]["hits"]
    ]
    assert len(set(ids)) == 6


# ---------------------------------------------------------------------------
# _source filtering
# ---------------------------------------------------------------------------


async def test_source_includes_and_excludes(store, index_name):
    await _seed(store, index_name, {"a": {"keep": 1, "drop": 2, "tenant_id": TENANT}})
    only = await store.search_documents(
        index_name, {"query": {"match_all": {}}, "_source": ["keep"]}
    )
    assert only["hits"]["hits"][0]["_source"] == {"keep": 1}

    without = await store.search_documents(
        index_name, {"query": {"match_all": {}}, "_source": {"excludes": ["drop"]}}
    )
    body = without["hits"]["hits"][0]["_source"]
    assert "drop" not in body and body["keep"] == 1

    none = await store.search_documents(
        index_name, {"query": {"match_all": {}}, "_source": False}
    )
    assert none["hits"]["hits"][0]["_source"] == {}


async def test_source_exclude_does_not_mutate_the_stored_document(store, index_name):
    """A shallow copy here would delete the field from the row and persist it."""
    await _seed(store, index_name, {"a": {"outer": {"keep": 1, "drop": 2}}})
    await store.search_documents(
        index_name, {"query": {"match_all": {}}, "_source": {"excludes": ["outer.drop"]}}
    )
    stored = await store.get_document(index_name, "a")
    assert stored["outer"] == {"keep": 1, "drop": 2}


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


async def test_aggregations_cover_the_full_match_set_not_the_page(store, index_name):
    """A page-limited aggregation reports a wrong number that looks plausible."""
    await _seed(
        store, index_name,
        {f"d{i}": {"tenant_id": TENANT, "amount": 1} for i in range(30)},
    )
    response = await store.search_documents(
        index_name,
        {
            "query": {"term": {"tenant_id": TENANT}},
            "size": 2,
            "aggs": {"total": {"sum": {"field": "amount"}}},
        },
    )
    assert len(response["hits"]["hits"]) == 2
    assert response["aggregations"]["total"]["value"] == 30.0


async def test_terms_aggregation_with_nested_metric(store, index_name):
    await _seed(
        store, index_name,
        {
            "a": {"status": "active", "amount": 10},
            "b": {"status": "active", "amount": 5},
            "c": {"status": "closed", "amount": 2},
        },
    )
    response = await store.search_documents(
        index_name,
        {
            "query": {"match_all": {}},
            "size": 0,
            "aggs": {
                "by_status": {
                    "terms": {"field": "status"},
                    "aggs": {"total": {"sum": {"field": "amount"}}},
                }
            },
        },
    )
    buckets = {b["key"]: b for b in response["aggregations"]["by_status"]["buckets"]}
    assert buckets["active"]["doc_count"] == 2
    assert buckets["active"]["total"]["value"] == 15.0
    assert buckets["closed"]["total"]["value"] == 2.0


async def test_terms_aggregation_counts_each_element_of_an_array(store, index_name):
    """ES treats an array field as several values, and so does the engine."""
    await _seed(store, index_name, {"a": {"grades": ["DIESEL_2", "PROPANE"]}})
    response = await store.search_documents(
        index_name,
        {"query": {"match_all": {}}, "size": 0,
         "aggs": {"g": {"terms": {"field": "grades"}}}},
    )
    keys = {b["key"] for b in response["aggregations"]["g"]["buckets"]}
    assert keys == {"DIESEL_2", "PROPANE"}


async def test_date_histogram_fills_empty_intervals(store, index_name):
    """``min_doc_count`` defaults to 0 and callers plot the result as a series.

    A missing interval would be drawn as a line between two non-adjacent points
    rather than as a zero, which reads as "no dip happened".
    """
    await _seed(
        store, index_name,
        {
            "a": {"ts": "2026-08-01T00:00:00+00:00"},
            "b": {"ts": "2026-08-04T00:00:00+00:00"},
        },
    )
    response = await store.search_documents(
        index_name,
        {
            "query": {"match_all": {}},
            "size": 0,
            "aggs": {"h": {"date_histogram": {"field": "ts", "calendar_interval": "day"}}},
        },
    )
    buckets = response["aggregations"]["h"]["buckets"]
    assert [b["doc_count"] for b in buckets] == [1, 0, 0, 1]
    assert buckets[0]["key_as_string"].startswith("2026-08-01")


async def test_min_on_a_timestamp_field_returns_epoch_millis(store, index_name):
    """ES returns epoch millis for a date field, and callers subtract them.

    Returning the ISO string instead would make ``params.ack - params.assign``
    style arithmetic raise a TypeError rather than produce a duration.
    """
    await _seed(
        store, index_name,
        {"a": {"ts": "2026-08-06T00:00:00+00:00"}, "b": {"ts": "2026-08-07T00:00:00+00:00"}},
    )
    response = await store.search_documents(
        index_name,
        {"query": {"match_all": {}}, "size": 0, "aggs": {"first": {"min": {"field": "ts"}}}},
    )
    agg = response["aggregations"]["first"]
    assert isinstance(agg["value"], float)
    assert agg["value_as_string"].startswith("2026-08-06")


async def test_empty_min_reports_null_not_zero(store, index_name):
    await _seed(store, index_name, {"a": {"other": 1}})
    response = await store.search_documents(
        index_name,
        {"query": {"match_all": {}}, "size": 0, "aggs": {"m": {"min": {"field": "absent"}}}},
    )
    assert response["aggregations"]["m"]["value"] is None


async def test_filter_aggregation_uses_the_query_matcher(store, index_name):
    await _seed(
        store, index_name,
        {"a": {"kind": "x", "n": 1}, "b": {"kind": "y", "n": 2}},
    )
    response = await store.search_documents(
        index_name,
        {
            "query": {"match_all": {}},
            "size": 0,
            "aggs": {
                "only_x": {
                    "filter": {"term": {"kind": "x"}},
                    "aggs": {"total": {"sum": {"field": "n"}}},
                }
            },
        },
    )
    assert response["aggregations"]["only_x"]["doc_count"] == 1
    assert response["aggregations"]["only_x"]["total"]["value"] == 1.0


async def test_cardinality_counts_distinct_values(store, index_name):
    await _seed(
        store, index_name,
        {"a": {"who": "u1"}, "b": {"who": "u1"}, "c": {"who": "u2"}},
    )
    response = await store.search_documents(
        index_name,
        {"query": {"match_all": {}}, "size": 0,
         "aggs": {"n": {"cardinality": {"field": "who"}}}},
    )
    assert response["aggregations"]["n"]["value"] == 2


# ---------------------------------------------------------------------------
# Failing loudly
# ---------------------------------------------------------------------------


async def test_an_unsupported_query_clause_raises(store, index_name):
    """The central design decision: never silently drop a clause.

    A dropped clause widens or narrows the result set and the caller cannot tell
    that from a genuine result. Every silent-empty defect this migration has
    found had that shape.
    """
    with pytest.raises(UnsupportedQueryError) as exc:
        await store.search_documents(
            index_name, {"query": {"geo_distance": {"distance": "1km"}}}
        )
    assert "geo_distance" in str(exc.value)


async def test_two_clauses_in_one_object_raise_rather_than_picking_one(store, index_name):
    with pytest.raises(UnsupportedQueryError):
        await store.search_documents(
            index_name,
            {"query": {"term": {"a": 1}, "range": {"b": {"gte": 2}}}},
        )


async def test_an_unsupported_aggregation_raises(store, index_name):
    """Pipeline aggregations need a painless interpreter; refusing is honest."""
    await _seed(store, index_name, {"a": {"n": 1}})
    with pytest.raises(UnsupportedAggregationError) as exc:
        await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 0,
                "aggs": {"x": {"bucket_script": {"buckets_path": {}, "script": "1"}}},
            },
        )
    assert "bucket_script" in str(exc.value)


async def test_a_script_valued_metric_raises(store, index_name):
    await _seed(store, index_name, {"a": {"n": 1}})
    with pytest.raises(UnsupportedAggregationError):
        await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 0,
                "aggs": {"x": {"sum": {"script": {"source": "doc['n'].value * 2"}}}},
            },
        )


async def test_a_calendar_month_histogram_raises_rather_than_approximating(store, index_name):
    """A 30-day "month" shifts every bucket boundary after the first."""
    await _seed(store, index_name, {"a": {"ts": "2026-08-06T00:00:00+00:00"}})
    with pytest.raises(UnsupportedAggregationError):
        await store.search_documents(
            index_name,
            {
                "query": {"match_all": {}},
                "size": 0,
                "aggs": {"h": {"date_histogram": {"field": "ts", "calendar_interval": "month"}}},
            },
        )


async def test_rounded_date_math_raises_rather_than_being_ignored(store, index_name):
    """``now/d`` and ``now`` select different rows; guessing ships a bad window."""
    with pytest.raises(UnsupportedQueryError):
        await store.search_documents(
            index_name, {"query": {"range": {"ts": {"gte": "now/d"}}}}
        )


# ---------------------------------------------------------------------------
# Other facade methods
# ---------------------------------------------------------------------------


async def test_get_all_documents_returns_bodies_newest_first(store, index_name):
    await store.index_document(index_name, "old", {"created_at": "2026-01-01T00:00:00+00:00"})
    await store.index_document(index_name, "new", {"created_at": "2026-08-01T00:00:00+00:00"})
    bodies = await store.get_all_documents(index_name)
    assert [b["created_at"][:4] for b in bodies] == ["2026", "2026"]
    assert bodies[0]["created_at"] > bodies[1]["created_at"]


async def test_semantic_search_is_tenant_scoped(store, index_name):
    await _seed(
        store, index_name,
        {
            "mine": {"tenant_id": TENANT, "name": "Hoboken depot"},
            "theirs": {"tenant_id": "other-tenant", "name": "Hoboken depot"},
        },
    )
    found = await store.semantic_search(TENANT, index_name, "hoboken", ["name"])
    assert [d["tenant_id"] for d in found] == [TENANT]


async def test_semantic_search_requires_a_tenant(store, index_name):
    with pytest.raises(ValueError):
        await store.semantic_search("", index_name, "x", ["name"])


async def test_multi_search_returns_one_response_per_search_in_order(store, index_name):
    other = f"{index_name}_b"
    await store.index_document(index_name, "a", {"tenant_id": TENANT})
    await store.index_document(other, "b", {"tenant_id": TENANT})
    result = await store.multi_search(
        [
            {"index": index_name, "query": {"query": {"match_all": {}}}},
            {"index": other, "query": {"query": {"match_all": {}}}},
        ]
    )
    assert [r["hits"]["hits"][0]["_id"] for r in result["responses"]] == ["a", "b"]
    await store.delete_index(other)


async def test_multi_search_isolates_a_failing_entry(store, index_name):
    """One bad search must not lose the results of the others.

    ``ignore_unavailable`` gives the ES version this property; reproducing it
    means a caller that batched five lookups still gets four.
    """
    result = await store.multi_search(
        [
            {"index": index_name, "query": {"query": {"match_all": {}}}},
            {"index": index_name, "query": {"query": {"geo_distance": {}}}},
        ]
    )
    assert "error" in result["responses"][1]
    assert "error" not in result["responses"][0]


async def test_empty_multi_search_touches_nothing(store):
    assert await store.multi_search([]) == {"responses": []}


async def test_aggregation_input_cap_is_enforced(store, index_name, monkeypatch):
    """Past the cap it must refuse, not truncate.

    A truncated aggregation returns a plausible wrong number. Verified by lowering
    the cap rather than by inserting 50,000 rows, so the guard is exercised
    without a slow test.
    """
    import persistence.document_aggregations as aggs_mod

    await _seed(store, index_name, {f"d{i}": {"n": 1} for i in range(5)})
    monkeypatch.setattr(aggs_mod, "MAX_AGGREGATION_ROWS", 2)
    with pytest.raises(AggregationInputTooLarge):
        await store.search_documents(
            index_name,
            {"query": {"match_all": {}}, "size": 0, "aggs": {"t": {"sum": {"field": "n"}}}},
        )


# ---------------------------------------------------------------------------
# Unsearchable fields
# ---------------------------------------------------------------------------


async def test_a_field_elasticsearch_cannot_search_is_refused_here_too(store):
    """The delivery OTP is ``index: false`` in the mapping.

    In jsonb it would be filterable, which turns "you cannot search the OTP" into
    "you can confirm a guessed OTP one query at a time". Uses the real index name
    because the policy is keyed on it.
    """
    from persistence.document_field_policy import UnsearchableFieldError

    with pytest.raises(UnsearchableFieldError):
        await store.search_documents(
            "fuel_orders_current", {"query": {"term": {"pod_otp": "123456"}}}
        )


async def test_the_refusal_covers_sort_and_aggregations_not_just_filters(store):
    """Otherwise the guard is bypassed by asking for a bucket instead of a filter."""
    from persistence.document_field_policy import UnsearchableFieldError

    with pytest.raises(UnsearchableFieldError):
        await store.search_documents(
            "fuel_orders_current",
            {"query": {"match_all": {}}, "sort": [{"pod_otp": "desc"}]},
        )
    with pytest.raises(UnsearchableFieldError):
        await store.search_documents(
            "fuel_orders_current",
            {
                "query": {"match_all": {}},
                "size": 0,
                "aggs": {"otps": {"terms": {"field": "pod_otp"}}},
            },
        )


async def test_the_document_is_still_returned_whole(store, index_name):
    """Only querying is blocked; ES stores and returns these fields too."""
    await store.index_document(
        "fuel_orders_current", "parity-probe", {"tenant_id": TENANT, "pod_otp": "999"}
    )
    try:
        stored = await store.get_document("fuel_orders_current", "parity-probe")
        assert stored["pod_otp"] == "999"
        found = await store.search_documents(
            "fuel_orders_current", {"query": {"ids": {"values": ["parity-probe"]}}}
        )
        assert found["hits"]["hits"][0]["_source"]["pod_otp"] == "999"
    finally:
        await store.delete_document("fuel_orders_current", "parity-probe")


async def test_an_ordinary_field_on_a_restricted_index_still_works(store):
    """The guard must not become a blanket ban on the index."""
    response = await store.search_documents(
        "fuel_orders_current", {"query": {"term": {"status": "delivered"}}, "size": 1}
    )
    assert "hits" in response
