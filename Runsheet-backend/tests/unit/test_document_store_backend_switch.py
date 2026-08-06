"""The document-store backend switch must be complete, inert by default, and loud.

``ElasticsearchService`` keeps its identity, its circuit breakers and its raw
``client`` while the document plane moves to Postgres; the switch lives inside the
nine async document methods. Three things about that have to hold, and none of
them is obvious from reading the diff:

1. **Every document method delegates.** A method that was missed keeps writing to
   Elasticsearch after the cutover, so the two stores diverge silently and the
   divergence is only visible as a stale read much later.
2. **It is inert without the flag, and without a database.** A default-on switch,
   or one that routes to a dormant persistence layer, breaks every request.
3. **A misspelled backend value fails at startup.** ``postgresql`` instead of
   ``postgres`` would otherwise leave the service on the legacy path while the
   operator believed the cutover had happened.
"""

from __future__ import annotations

import inspect

import pytest

from config.settings import Settings, clear_settings_cache


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
    """A missed method keeps writing to Elasticsearch after the cutover.

    Asserted against the source rather than by calling each method, because
    calling them needs a live backend and the property under test is structural:
    the delegation exists at all.
    """
    from services.elasticsearch_service import ElasticsearchService

    source = inspect.getsource(getattr(ElasticsearchService, method))
    assert "_pg_store()" in source, (
        f"{method} does not consult _pg_store(), so it would keep using "
        "Elasticsearch after the document store is cut over to Postgres"
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
            "method, so the cutover would raise AttributeError at runtime"
        )


# ---------------------------------------------------------------------------
# Inert by default
# ---------------------------------------------------------------------------


def _settings(**over) -> Settings:
    base = {
        # ``development`` because every other environment additionally requires
        # redis_url / supertokens credentials, and none of that is what these
        # tests are about.
        "environment": "development",
        "elastic_endpoint": "http://localhost:9200",
        "elastic_api_key": "k",
    }
    base.update(over)
    return Settings(**base)


class TestDefaultIsElasticsearch:
    def test_the_default_backend_is_elasticsearch(self):
        assert _settings().document_store_backend == "elasticsearch"

    def test_the_default_does_not_route_to_postgres(self):
        assert _settings(database_url="postgresql+psycopg://x/y").document_store_is_postgres is False

    def test_postgres_without_a_database_url_stays_on_elasticsearch(self):
        """Routing reads at a dormant persistence layer would fail every request.

        Inert rather than fatal so the flag can be pre-set in a config template
        before the database exists.
        """
        assert _settings(document_store_backend="postgres").document_store_is_postgres is False

    def test_postgres_with_a_database_url_routes(self):
        settings = _settings(
            document_store_backend="postgres",
            database_url="postgresql+psycopg://runsheet@localhost/runsheet",
        )
        assert settings.document_store_is_postgres is True

    def test_the_value_is_case_and_whitespace_tolerant(self):
        settings = _settings(
            document_store_backend="  Postgres ",
            database_url="postgresql+psycopg://runsheet@localhost/runsheet",
        )
        assert settings.document_store_is_postgres is True


class TestAMisspelledBackendIsRejected:
    @pytest.mark.parametrize("value", ["postgresql", "pg", "psql", "elastic", "none", ""])
    def test_unknown_values_raise_at_construction(self, value):
        """Not silently ignored: the operator would believe the cutover happened."""
        with pytest.raises(Exception) as exc:
            _settings(document_store_backend=value)
        assert "document_store_backend" in str(exc.value)


# ---------------------------------------------------------------------------
# The delegation actually fires
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Stands in for the real store so the switch can be tested without a database."""

    def __init__(self):
        self.calls = []

    async def search_documents(self, index, query, size=100, request_timeout=10):
        self.calls.append(("search_documents", index))
        return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}

    async def index_document(self, index, doc_id, document):
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


async def test_search_is_served_by_the_store_when_the_flag_is_on(service_with_stub_store):
    service, store = service_with_stub_store
    result = await service.search_documents("some_index", {"query": {"match_all": {}}})
    assert store.calls == [("search_documents", "some_index")]
    assert result["hits"]["total"]["value"] == 0


async def test_index_is_served_by_the_store(service_with_stub_store):
    service, store = service_with_stub_store
    await service.index_document("some_index", "a", {"tenant_id": "t"})
    assert store.calls == [("index_document", "some_index", "a")]


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

    A retired index has been dropped and its writes must go nowhere at all — not
    to Elasticsearch and not to the document store, which would resurrect it
    under a different roof.
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
        """Copied from the Elasticsearch path so both backends key identically."""
        from persistence.document_store import _id_field_for

        assert _id_field_for("customers") == "customer_id"

    def test_a_plain_id_field_takes_precedence(self):
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"id": "X", "truck_id": "Y"}) == "X"

    def test_the_index_specific_field_is_used_when_there_is_no_plain_id(self):
        from persistence.document_store import _bulk_doc_id

        assert _bulk_doc_id("trucks", {"truck_id": "Y"}) == "Y"

    def test_no_derivable_id_returns_none_rather_than_inventing_one(self):
        """The ES path lets the cluster mint a random id, which nothing can fetch."""
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
