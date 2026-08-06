"""Every document method on the facade must reach the Postgres store.

``ElasticsearchService`` keeps its name, its circuit breakers and a ``client``
attribute; what it no longer keeps is an Elasticsearch implementation. The nine
async document methods delegate to
:class:`persistence.document_store.PostgresDocumentStore`, and a method that was
missed would reach ``NoClusterClient``, whose data plane raises.

Formerly ``test_document_store_backend_switch.py``. Two of its three concerns went
with ``DOCUMENT_STORE_BACKEND``: that the switch was inert without the flag and
without a database, and that a misspelled value (``postgresql``, ``pg``) failed at
startup rather than leaving the service quietly on the legacy path. There is no
flag and no legacy path — ``postgres`` is not a value to get wrong, it is the only
implementation — so both sets of assertions had nothing left to assert about.

What remains is the concern that outlived the switch: **completeness**. It was the
strongest of the three even then, since a missed delegation diverged the two stores
silently and only showed up as a stale read much later, and it is still load-bearing
now that the alternative branch raises instead of writing elsewhere.
"""

from __future__ import annotations

import inspect

import pytest

from config.settings import clear_settings_cache


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------

#: The async document-plane methods. ``get_all_documents`` and ``semantic_search``
#: are excluded deliberately: both are implemented in terms of
#: ``search_documents``, so they follow it and a delegation of their own would be
#: a second code path to keep in step.
_DELEGATING_METHODS = (
    "index_document",
    "update_document",
    "bulk_index_documents",
    "search_documents",
    "multi_search",
    "get_document",
    "delete_document",
)


@pytest.mark.parametrize("method", _DELEGATING_METHODS)
def test_every_document_method_consults_the_postgres_store(method):
    """A missed method reaches a client whose data plane raises.

    Asserted against the source rather than by calling each method, because
    calling them needs a live backend and the property under test is structural:
    the delegation exists at all.
    """
    from services.elasticsearch_service import ElasticsearchService

    source = inspect.getsource(getattr(ElasticsearchService, method))
    assert "_pg_store()" in source, (
        f"{method} does not consult _pg_store(), so it would fall through to the "
        "raw client, which has no cluster behind it"
    )


def test_the_indirect_methods_route_through_search_documents():
    """Their delegation is inherited, so it must stay inherited."""
    from services.elasticsearch_service import ElasticsearchService

    for method in ("get_all_documents", "semantic_search"):
        source = inspect.getsource(getattr(ElasticsearchService, method))
        assert "self.search_documents(" in source, (
            f"{method} no longer routes through search_documents, so it needs "
            "its own _pg_store() delegation"
        )


def test_the_store_implements_every_method_the_service_delegates_to():
    """The two sides must not drift apart in name or signature."""
    from persistence.document_store import PostgresDocumentStore

    for method in _DELEGATING_METHODS:
        assert hasattr(PostgresDocumentStore, method), (
            f"ElasticsearchService delegates {method} but the store has no such "
            "method, so the call would raise AttributeError at runtime"
        )


# ---------------------------------------------------------------------------
# The delegation actually fires
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Stands in for the real store so delegation can be tested without a database."""

    def __init__(self):
        self.calls = []

    async def search_documents(self, index, query, size=100, request_timeout=10):
        self.calls.append(("search_documents", index))
        return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}

    async def index_document(self, index, doc_id, document, *, stamp_timestamps=True):
        self.calls.append(("index_document", index, doc_id))
        return {"result": "created"}

    async def get_document(self, index, doc_id):
        self.calls.append(("get_document", index, doc_id))
        return None

    async def delete_document(self, index, doc_id):
        self.calls.append(("delete_document", index, doc_id))
        return True


@pytest.fixture
def service_with_stub_store(monkeypatch):
    from services.elasticsearch_service import ElasticsearchService

    service = ElasticsearchService()
    store = _RecordingStore()
    monkeypatch.setattr(service, "_pg_store", lambda: store)
    return service, store


async def test_search_is_served_by_the_store(service_with_stub_store):
    service, store = service_with_stub_store
    result = await service.search_documents("some_index", {"query": {"match_all": {}}})
    assert store.calls == [("search_documents", "some_index")]
    assert result["hits"]["total"]["value"] == 0


async def test_index_is_served_by_the_store(service_with_stub_store):
    service, store = service_with_stub_store
    await service.index_document("some_index", "a", {"tenant_id": "t"})
    assert store.calls == [("index_document", "some_index", "a")]


async def test_the_verbatim_opt_out_reaches_the_store(service_with_stub_store):
    """``stamp_timestamps`` has to be forwarded, not swallowed by the facade.

    The rebuild tool and ``seed_kfactor_demo`` pass ``False`` because their
    timestamps ARE the data. A facade that accepted the keyword and dropped it
    would restamp every rebuilt document to now() while the caller believed it had
    opted out — and the caller cannot tell, because the return value is the same.
    """
    service, store = service_with_stub_store
    seen = {}

    async def _capture(index, doc_id, document, *, stamp_timestamps=True):
        seen["stamp_timestamps"] = stamp_timestamps
        return {"result": "created"}

    store.index_document = _capture
    await service.index_document("i", "a", {"tenant_id": "t"}, stamp_timestamps=False)

    assert seen == {"stamp_timestamps": False}


async def test_get_and_delete_are_served_by_the_store(service_with_stub_store):
    service, store = service_with_stub_store
    assert await service.get_document("i", "a") is None
    assert await service.delete_document("i", "a") is True
    assert store.calls == [("get_document", "i", "a"), ("delete_document", "i", "a")]


async def test_semantic_search_reaches_the_store_through_search_documents(
    service_with_stub_store,
):
    """Confirms the inherited delegation is real and not just structural."""
    service, store = service_with_stub_store
    assert await service.semantic_search("t", "i", "text", ["name"]) == []
    assert store.calls == [("search_documents", "i")]


async def test_a_retired_index_is_still_skipped_before_the_store_is_consulted(
    service_with_stub_store, monkeypatch
):
    """Retirement is a stronger statement than backend choice.

    A retired index's relational table is its sole store, and its writes must go
    nowhere at all — not to a cluster and not to the document store, which would
    accumulate rows no read path consults.
    """
    service, store = service_with_stub_store
    monkeypatch.setattr(service, "_is_retired_index", lambda index: True)
    result = await service.index_document("gone", "a", {})
    assert result == {"result": "skipped_retired_index"}
    assert store.calls == []


@pytest.fixture(autouse=True)
def _clear_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


# ---------------------------------------------------------------------------
# Id and tenant derivation
# ---------------------------------------------------------------------------
#
# These are pure functions, and they are where a document goes missing. Two of
# the three fuel-asset indices could not use the obvious id field, and the
# seeder's id resolver silently collapsed four fixtures by picking a foreign key —
# so the rules are pinned rather than left to the reader.


class TestBulkIdDerivation:
    def test_the_explicit_index_map_wins(self):
        from persistence.document_store import _id_field_for

        assert _id_field_for("trucks") == "truck_id"
        assert _id_field_for("inventory") == "item_id"
        assert _id_field_for("support_tickets") == "ticket_id"

    def test_unmapped_indices_fall_back_to_singularise_and_suffix(self):
        """Copied from the Elasticsearch path so ids did not change at the cutover."""
        from persistence.document_store import _id_field_for

        assert _id_field_for("customers") == "customer_id"

    def test_a_plain_id_field_takes_precedence(self):
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"id": "X", "truck_id": "Y"}) == "X"

    def test_the_index_specific_field_is_used_when_there_is_no_plain_id(self):
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"truck_id": "Y"}) == "Y"

    def test_no_derivable_id_returns_none_rather_than_inventing_one(self):
        """The ES path let the cluster mint a random id, which nothing can fetch."""
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"plate": "AAA"}) is None

    def test_a_numeric_id_is_stringified(self):
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"id": 7}) == "7"


class TestTenantLifting:
    def test_a_string_tenant_is_lifted(self):
        from persistence.document_store import _tenant_of

        assert _tenant_of({"tenant_id": "demo-tenant"}) == "demo-tenant"

    def test_a_numeric_tenant_is_stringified(self):
        from persistence.document_store import _tenant_of

        assert _tenant_of({"tenant_id": 42}) == "42"

    def test_a_missing_or_empty_tenant_stays_null(self):
        """The legacy dynamically-mapped trucks/locations documents carry none.

        Coercing them to a sentinel would make a tenant-scoped query return
        documents belonging to nobody.
        """
        from persistence.document_store import _tenant_of

        assert _tenant_of({}) is None
        assert _tenant_of({"tenant_id": ""}) is None
        assert _tenant_of({"tenant_id": None}) is None

    def test_a_structured_tenant_value_is_ignored_rather_than_coerced(self):
        from persistence.document_store import _tenant_of

        assert _tenant_of({"tenant_id": {"nested": "x"}}) is None
