"""A stand-in for the Elasticsearch client once there is no cluster.

Phase 5 of the Elasticsearch → Postgres migration: stop talking to the cluster.
The document plane already moved — ``ElasticsearchService`` routes its nine async
document methods to :class:`persistence.document_store.PostgresDocumentStore` when
``DOCUMENT_STORE_BACKEND=postgres``. What was left was everything *around* the
document plane, which still opened a connection and still ran on startup:

* ``connect()`` pinged the cluster and **raised** if it could not, so with the
  cluster stopped the application would not even import — a module-level
  ``elasticsearch_service = ElasticsearchService()`` in
  ``services/elasticsearch_service.py`` meant the failure happened during
  ``import data_endpoints``.
* eighteen ``setup_*_indices`` functions, three ILM methods and two schema
  validators ran at bootstrap, creating indices and lifecycle policies for a store
  nothing reads any more.

Those are all *control plane*: they manage indices and lifecycle policies, and they
have no Postgres equivalent because there is nothing to manage — the document store
is one table. So they become no-ops, which is what
``tests/unit/test_raw_elasticsearch_client_inventory.py`` has said about them from
the start.

Why a stand-in rather than ``None``
-----------------------------------

Setting ``client = None`` would make all nineteen call sites raise
``AttributeError: 'NoneType' object has no attribute 'indices'`` at startup, so
every one would have to be guarded individually — nineteen edits in eighteen
modules, each of which is a place to forget.

Why not a plain mock that accepts everything
--------------------------------------------

Because that reintroduces the exact failure this migration keeps finding. A
permissive null object would also swallow ``client.index(...)`` and
``client.search(...)``, so a data-plane call that still reached the raw client would
silently do nothing and return nothing — a write that vanishes, with no error.

There are no raw-client users left. The last three were migration tooling — the
rebuild tool (now ``persistence/rebuild_document_store.py``),
``scripts/seed_kfactor_demo.py`` and ``services/data_seeder.py`` — and all three
moved onto the facade, which is what ``index_document(..., stamp_timestamps=False)``
exists for: they used the raw client to write a document without having its
``updated_at`` overwritten. ``tests/unit/test_raw_elasticsearch_client_inventory.py``
now pins the inventory at zero. This class is the backstop for the next one, and it
is deliberately the loud kind.

So the split is explicit: control plane no-ops, data plane raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ClusterRemovedError", "NoClusterClient"]


#: Namespaces that manage indices and lifecycle policies rather than documents.
#: ``client.indices.create``, ``client.ilm.put_lifecycle``, ``client.cluster.health``.
_CONTROL_PLANE_NAMESPACES = frozenset({"indices", "ilm", "cluster", "cat", "snapshot"})

#: Methods that read or write documents. Reaching these means a document operation
#: bypassed ``ElasticsearchService``, which is the thing the whole migration was
#: about, so they raise rather than pretending to succeed.
_DATA_PLANE_METHODS = frozenset(
    {
        "index", "update", "delete", "get", "search", "count", "msearch", "bulk",
        "update_by_query", "delete_by_query", "exists", "scan", "mget", "scroll",
        "reindex", "search_template", "termvectors", "explain",
    }
)


class ClusterRemovedError(RuntimeError):
    """A document operation reached the raw client after the cluster was removed.

    Not a warning and not a silent no-op: a write that quietly vanishes is worse
    than an outage, because nothing downstream can tell it happened.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"client.{operation}(...) was called, but there is no Elasticsearch "
            "cluster: the document plane is served from PostgreSQL "
            "(DOCUMENT_STORE_BACKEND=postgres). Route this through "
            "ElasticsearchService, or through PostgresDocumentStore directly. "
            "If this is migration tooling that genuinely needs a cluster, it "
            "cannot run any more — the cluster is gone."
        )
        self.operation = operation


class _ControlPlaneNamespace:
    """``client.indices`` / ``client.ilm`` — every call is a logged no-op.

    Returns shapes the callers actually branch on rather than ``None``, because
    several of them do ``if not client.indices.exists(...)`` and would then try to
    create the index. ``exists`` answering ``False`` and ``create`` doing nothing is
    a consistent story: the index is absent and cannot be made.
    """

    _FALSE_RETURNING = frozenset({"exists", "exists_alias", "exists_template", "exists_index_template"})

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def __getattr__(self, operation: str) -> Any:
        def _noop(*_args: Any, **_kwargs: Any) -> Any:
            logger.debug(
                "no-op: client.%s.%s() — Elasticsearch has been removed",
                self._namespace, operation,
            )
            if operation in self._FALSE_RETURNING:
                return False
            # ``get_mapping`` / ``get_settings`` / ``get_alias`` callers index into
            # the result by index name, and an empty dict makes ``.get(name, {})``
            # do the right thing.
            return {}

        return _noop


class NoClusterClient:
    """Stands in for ``elasticsearch.Elasticsearch`` with no cluster behind it.

    ``ping()`` answers ``False``, which is the truth and is what the health check
    reports. Control-plane namespaces no-op. Data-plane methods raise
    :class:`ClusterRemovedError`.
    """

    def ping(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def info(self, *_args: Any, **_kwargs: Any) -> dict:
        return {"cluster_name": "removed", "version": {"number": "0"}}

    def close(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name in _CONTROL_PLANE_NAMESPACES:
            return _ControlPlaneNamespace(name)
        if name in _DATA_PLANE_METHODS:
            def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise ClusterRemovedError(name)

            return _refuse
        # Anything unrecognised is treated as data plane — the safe direction. A
        # new client method silently no-opping is how a write disappears.
        def _refuse_unknown(*_args: Any, **_kwargs: Any) -> Any:
            raise ClusterRemovedError(name)

        return _refuse_unknown

    def __bool__(self) -> bool:
        """Truthy, so ``if es.client:`` guards still take the configured branch.

        Falsy would be tempting — there is no cluster — but several call sites use
        ``if not self.client: return`` as an "unconfigured" check and would then
        skip work that now has a Postgres implementation.
        """
        return True

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return "<NoClusterClient: Elasticsearch removed>"
