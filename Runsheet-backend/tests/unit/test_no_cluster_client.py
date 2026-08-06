"""The client stand-in used once there is no Elasticsearch cluster.

Phase 5 stopped talking to the cluster. The obstacle was not the document plane —
that had already moved — it was everything around it: ``connect()`` pinged and
**raised** on failure, and this class builds a module-level singleton, so stopping
the container did not degrade the service, it stopped the application importing.
Verified by stopping the container and watching uvicorn die at
``ConnectionError: Failed to ping Elasticsearch`` inside ``import data_endpoints``.

Eighteen ``setup_*_indices`` functions, three ILM methods and two schema validators
also ran at bootstrap. They are control plane — they manage indices and lifecycle
policies, and there is nothing to manage when the store is one Postgres table — so
they become no-ops.

The split this file pins is the whole point of the class. Control plane no-ops,
because there is nothing to do. Data plane **raises**, because a write that
silently vanishes is worse than an outage: nothing downstream can tell it happened,
and that is the failure mode this migration has spent its whole length eliminating.
"""

from __future__ import annotations

import pytest

from services.no_cluster import ClusterRemovedError, NoClusterClient


@pytest.fixture
def client() -> NoClusterClient:
    return NoClusterClient()


class TestControlPlaneIsANoOp:
    def test_index_creation_does_nothing_and_does_not_raise(self, client):
        """Called by eighteen ``setup_*_indices`` functions at bootstrap."""
        assert client.indices.create(index="anything", body={}) == {}

    def test_index_existence_is_false(self, client):
        """``if not client.indices.exists(...)`` must not report a phantom index.

        ``False`` plus a no-op ``create`` is a consistent story: the index is absent
        and cannot be made. Answering ``True`` would make the setup functions skip
        creation and then act as though the index were usable.
        """
        assert client.indices.exists(index="trucks") is False
        assert client.indices.exists_alias(name="assets") is False

    def test_mapping_reads_return_an_empty_mapping(self, client):
        """Callers do ``result.get(index, {}).get("mappings", {})``, so an empty
        dict walks cleanly instead of raising an AttributeError two frames later."""
        assert client.indices.get_mapping(index="trucks") == {}
        assert client.indices.get_settings(index="trucks") == {}

    def test_ilm_management_does_nothing(self, client):
        assert client.ilm.put_lifecycle(name="p", body={}) == {}
        assert client.ilm.get_lifecycle() == {}

    def test_ping_is_false_because_that_is_true(self, client):
        """The health check reports what it probed; lying here would make a missing
        cluster look present."""
        assert client.ping() is False

    def test_it_is_truthy_so_configured_guards_still_pass(self, client):
        """Several call sites use ``if not self.client: return`` to mean "not
        configured". Falsy would skip work that now has a Postgres implementation."""
        assert bool(client) is True


class TestDataPlaneRefuses:
    @pytest.mark.parametrize(
        "operation",
        ["index", "update", "delete", "get", "search", "count", "bulk",
         "msearch", "update_by_query", "delete_by_query", "exists", "scan",
         "mget", "scroll", "reindex"],
    )
    def test_every_document_operation_raises(self, client, operation):
        with pytest.raises(ClusterRemovedError):
            getattr(client, operation)(index="i", id="d")

    def test_an_unrecognised_method_also_raises(self, client):
        """The safe direction. A new client method silently no-opping is how a
        write disappears, so anything not explicitly control plane refuses."""
        with pytest.raises(ClusterRemovedError):
            client.some_method_added_in_a_later_version()

    def test_the_error_names_the_operation_and_the_replacement(self, client):
        """An error that does not say what to do instead gets worked around."""
        with pytest.raises(ClusterRemovedError) as excinfo:
            client.index(index="i", id="d", body={})

        message = str(excinfo.value)
        assert "client.index" in message
        assert "PostgresDocumentStore" in message or "ElasticsearchService" in message

    def test_control_plane_and_data_plane_names_do_not_overlap(self, client):
        """``client.indices.exists`` is control plane; ``client.exists`` is not.

        They differ by one attribute and mean entirely different things — one asks
        whether an index exists, the other reads a document.
        """
        assert client.indices.exists(index="i") is False
        with pytest.raises(ClusterRemovedError):
            client.exists(index="i", id="d")


class TestTheServiceUsesItWhenThereIsNoCluster:
    def test_connect_installs_it_on_the_postgres_backend(self, monkeypatch):
        """Rather than connecting, pinging, and raising.

        Constructed directly instead of through the module singleton so the test
        does not depend on import order.
        """
        from config.settings import Environment
        from services.elasticsearch_service import ElasticsearchService

        service = ElasticsearchService.__new__(ElasticsearchService)
        service.client = None
        service._document_store = None

        class _Settings:
            environment = Environment.DEVELOPMENT
            document_store_is_postgres = True

        service.settings = _Settings()
        service.connect()

        assert isinstance(service.client, NoClusterClient)

    def test_there_is_no_longer_an_elasticsearch_branch_to_take(self):
        """``connect()`` installs the stand-in unconditionally now.

        This test previously asserted the opposite half: that
        ``document_store_is_postgres = False`` still built a real client, so a
        rollback worked by flipping one variable. Phase 6 removed that branch along
        with the ``Elasticsearch`` import, so rolling back is no longer a flag — it
        means restoring the cluster from ``es-full-backup`` and reverting the commit.
        Asserted rather than assumed, because a stray credentials read here would be
        a connection attempt to a cluster that does not exist.
        """
        import inspect

        from services.elasticsearch_service import ElasticsearchService

        source = inspect.getsource(ElasticsearchService.connect)

        assert "NoClusterClient" in source
        for gone in ("Elasticsearch(", "elastic_api_key", "elastic_endpoint", "ping()"):
            assert gone not in source, (
                f"connect() still references {gone!r}; there is no cluster to "
                "connect to, and reading the credentials implies there is"
            )
