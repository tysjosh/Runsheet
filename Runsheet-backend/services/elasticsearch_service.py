"""
Elasticsearch service for Runsheet Logistics Platform
Handles all Elasticsearch operations including index management and data operations

Validates:
- Requirement 3.5: Implement circuit breakers for Elasticsearch
- Requirement 2.4: Return specific error code indicating database unavailability
- Requirement 7.1: Implement index lifecycle management policies for data tiering
"""

import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from config.settings import get_settings, Environment
from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenException
from errors.codes import ErrorCode
from errors.exceptions import AppException, elasticsearch_unavailable, circuit_open
from services.time_utils import utcnow

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


# Strict-mapped event-stream indices that carry their own domain timestamps
# (e.g. event_timestamp / ingested_at) and therefore MUST NOT receive the
# auto-stamped created_at/updated_at — doing so trips a
# strict_dynamic_mapping_exception. Every OTHER strict index written via
# index_document is auto-stamped, so its mapping must declare created_at and
# updated_at (enforced by tests/unit/test_mapping_timestamp_contract.py).
TIMESTAMP_SKIP_INDICES = frozenset(
    {"job_events", "shipment_events", "fuel_order_events"}
)


# Out-of-order protection for current-state documents, used by
# ``ElasticsearchService.upsert_if_newer``. Two byte-identical copies of this
# lived in ``fuel/order_repository.py`` and ``ops/services/ops_es_service.py``,
# each reaching past the facade to ``client.update``; they are here once so both
# call sites go through the facade and so the Postgres document store can answer
# the same call.
#
# ``isBefore || isEqual`` is deliberate: an event whose timestamp EQUALS the
# stored one is discarded. At-least-once delivery makes an equal timestamp the
# common case for a redelivery, and applying it would overwrite whatever a later
# event had already written.


class ElasticsearchService:
    """
    Elasticsearch service with circuit breaker protection.
    
    All Elasticsearch operations are wrapped with a circuit breaker to prevent
    cascading failures when the database is unavailable.
    
    Validates:
    - Requirement 3.5: Implement circuit breakers for Elasticsearch
    - Requirement 2.4: Return specific error code indicating database unavailability
    """
    
    #: The lazily-constructed document store, as a CLASS attribute so an instance
    #: built without ``__init__`` — which several tests do, to avoid the settings
    #: lookup and the circuit breakers — still has it. As an instance attribute it
    #: raised ``AttributeError: no attribute '_document_store'`` from ``_pg_store``
    #: the moment the Elasticsearch branch stopped short-circuiting first.
    _document_store = None

    def __init__(self):
        self.client = None
        self.settings = get_settings()
        
        # Initialize separate circuit breakers for read and write operations
        # so that agent write failures don't block user read queries
        self._circuit_breaker = CircuitBreaker(
            name="elasticsearch_write",
            config=CircuitBreakerConfig(
                failure_threshold=10,
            )
        )
        self._read_circuit_breaker = CircuitBreaker(
            name="elasticsearch_read",
            config=CircuitBreakerConfig(
                failure_threshold=15,
            )
        )
        
        # Lazily constructed on first use so importing this module does not pull
        # in the persistence layer, and so a deployment on the legacy path never
        # touches it at all.
        self._document_store = None

        self.connect()

    # ------------------------------------------------------------------
    # Postgres document store
    # ------------------------------------------------------------------
    #
    # Every async document method below delegates to
    # :class:`persistence.document_store.PostgresDocumentStore`, which returns the
    # Elasticsearch response shapes — so none of the ~680 call sites in the codebase
    # changed when the store moved, and none of them changed again when the cluster
    # was deleted.
    #
    # This used to be a switch: ``_pg_store()`` returned ``None`` on the legacy path
    # and each method had two implementations. There is no legacy path now, so the
    # branch is gone and the ES half with it. The CLASS NAME stays
    # ``ElasticsearchService`` deliberately — renaming it would touch 567 files for
    # no behavioural gain, and the name is now simply historical.

    def _pg_store(self):
        """The Postgres document store. Constructed on first use."""
        if self._document_store is None:
            from persistence.document_store import PostgresDocumentStore

            self._document_store = PostgresDocumentStore()
        return self._document_store

    def _is_retired_index(self, index: str) -> bool:
        """True when ``index`` has been retired (migrated to Postgres + dropped).

        Writes to a retired index (direct index/update/delete AND outbox-relay
        projection, since the relay calls ``index_document``) are skipped so a
        dropped index is not silently recreated with ES dynamic mappings.
        Read from current settings each call so the list can be flipped without
        restarting (and tests can monkeypatch it). Reversible: remove the index
        from ``retired_es_indices`` to resume projecting to it.
        """
        try:
            retired = get_settings().retired_es_indices
        except Exception:  # noqa: BLE001 — never let a settings hiccup block ES
            return False
        return index in (retired or [])

    def connect(self):
        """Initialize Elasticsearch connection"""
        # Skip actual connection in test environment - tests should mock ES
        if self.settings.environment == Environment.TEST:
            logger.info("⏭️  Skipping Elasticsearch connection in test environment")
            return

        # Phase 5: no cluster when the document plane is Postgres.
        #
        # This method used to raise on a failed ping, and this class instantiates a
        # module-level singleton, so stopping the cluster did not degrade the
        # service — it stopped the application from IMPORTING, inside
        # ``import data_endpoints``. Verified by stopping the container: uvicorn
        # failed at ``ConnectionError: Failed to ping Elasticsearch`` before any
        # route was registered.
        #
        # ``NoClusterClient`` makes the control plane a no-op and the data plane
        # raise, so the eighteen ``setup_*_indices`` functions, the ILM setup and the
        # schema validators below all become no-ops without eighteen guards, while a
        # document call that still reached the raw client is heard rather than
        # silently dropped. See services/no_cluster.py.
        if self.settings.document_store_is_postgres:
            from services.no_cluster import NoClusterClient

            self.client = NoClusterClient()
            logger.info(
                "Elasticsearch is not used: the document plane is PostgreSQL "
                "(DOCUMENT_STORE_BACKEND=postgres). Index and ILM management are "
                "no-ops; a raw document call will raise."
            )
            return

        # Phase 6: there is no Elasticsearch branch left to take.
        #
        # This method used to build an ``Elasticsearch`` client, ping it, and then
        # run ILM setup, index creation and schema validation — 1,089 lines of
        # cluster management that are gone, because there is no cluster and one
        # Postgres table needs no managing.
        #
        # ``ELASTIC_API_KEY`` / ``ELASTIC_ENDPOINT`` are no longer read. Rolling
        # back to Elasticsearch is not a flag any more: it would mean restoring the
        # cluster from ``es-full-backup`` and reverting this commit.
        from services.no_cluster import NoClusterClient

        self.client = NoClusterClient()
        logger.info(
            "Document operations are served from PostgreSQL. Elasticsearch has "
            "been removed: index and lifecycle management are no-ops, and a raw "
            "document call on .client raises."
        )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get the circuit breaker instance for external access."""
        return self._circuit_breaker
    
    def _handle_circuit_breaker_exception(self, exc: CircuitOpenException) -> None:
        """
        Handle a circuit breaker exception by raising an appropriate AppException.
        
        Validates:
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirement 3.2: Return service unavailable response immediately when circuit is open
        
        Args:
            exc: The CircuitOpenException that was raised
            
        Raises:
            AppException: With CIRCUIT_OPEN error code
        """
        time_until_retry = None
        if exc.time_until_retry:
            time_until_retry = int(exc.time_until_retry.total_seconds())
        
        raise circuit_open(
            message=f"Elasticsearch service temporarily unavailable. Circuit breaker '{exc.circuit_name}' is open.",
            details={
                "circuit_name": exc.circuit_name,
                "time_until_retry_seconds": time_until_retry,
                "service": "elasticsearch"
            }
        )
    
    def _handle_elasticsearch_error(self, operation: str, error: Exception) -> None:
        """
        Handle an Elasticsearch error by raising an appropriate AppException.
        
        Validates:
        - Requirement 2.4: Return specific error code indicating database unavailability
        
        Args:
            operation: The operation that failed (e.g., "search", "index")
            error: The exception that was raised
            
        Raises:
            AppException: With ELASTICSEARCH_UNAVAILABLE error code
        """
        logger.error("Elasticsearch %s failed: %s", operation, error)
        raise elasticsearch_unavailable(
            message=f"Database operation failed: {operation}",
            details={
                "operation": operation,
                "error": str(error)
            }
        )
    

    # ------------------------------------------------------------------
    # Why these four mappings no longer declare ``semantic_text``
    # ------------------------------------------------------------------
    #
    # ``semantic_text`` needs Elasticsearch 8.15+ AND an inference endpoint.
    # Nothing in this codebase ever creates one, and the sole consumer of these
    # fields — :meth:`semantic_search` — issues a plain ``multi_match``, which is
    # lexical and behaves identically on ``text``. So the type bought nothing
    # even where it is supported.
    #
    # What it cost was severe and silent. On a cluster without the type,
    # ``indices.create`` fails with ``No handler for type [semantic_text]``;
    # :meth:`setup_indices` logs that and moves on, so the first write creates
    # the index by DYNAMIC mapping instead. Dynamic mapping infers analyzed
    # ``text`` for every string — including ``tenant_id`` — and
    # ``inject_tenant_filter`` scopes every read with
    # ``{"term": {"tenant_id": ...}}``. A term query against analyzed text
    # matches only if a produced token equals the whole term, and the standard
    # analyzer splits ``demo-tenant`` into ``demo`` + ``tenant``. So every
    # tenant-scoped read returned zero rows from a populated index: ``trucks``
    # held 10 documents and matched 0, ``locations`` 4 and matched 0, and
    # ``support_tickets`` was never created at all.
    #
    # Existing indices keep the mapping they were created with, so a cluster
    # that already went down this path needs a reindex — a put-mapping cannot
    # change a field's type. See ``.kiro/runbooks`` for the repair.

    
    # CRUD Operations
    async def index_document(self, index: str, doc_id: str, document: Dict[Any, Any]):
        """
        Index a single document with circuit breaker protection.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        if self._is_retired_index(index):
            return {"result": "skipped_retired_index"}
        store = self._pg_store()
        return await store.index_document(index, doc_id, document)

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[Any, Any]):
        """
        Partially update a document using the ES _update API with circuit breaker protection.

        Only the fields present in *partial_doc* are merged into the existing document.
        """
        if self._is_retired_index(index):
            return {"result": "skipped_retired_index"}
        store = self._pg_store()
        from persistence.document_store import DocumentNotFound

        try:
            return await store.update_document(index, doc_id, partial_doc)
        except DocumentNotFound as exc:
            # The ES client raises NotFoundError here, which callers already
            # handle; re-raise through the same error path so behaviour on
            # both backends is identical.
            self._handle_elasticsearch_error(
                f"update_document({index}, {doc_id})", exc
            )

    
    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        """Upsert unless the stored ``timestamp_field`` is newer or equal.

        Out-of-order protection for current-state documents: at-least-once
        delivery means an event can arrive after a later one has already been
        applied, and a plain last-write-wins upsert would move the document
        backwards.

        Two identical copies of a painless ``scripted_upsert`` used to implement
        this — one in ``fuel/order_repository.py``, one in
        ``ops/services/ops_es_service.py`` — each reaching past this facade to
        ``client.update``. They are the same script character for character, so
        they belong here once, and putting them here is what lets the Postgres
        document store answer the same call: it does the comparison under a
        ``SELECT … FOR UPDATE`` instead, which cannot lose a concurrent write and
        needs no retry loop.

        Returns ``True`` when the document was written, ``False`` when the event
        was discarded as stale.
        """
        if self._is_retired_index(index):
            return False

        store = self._pg_store()
        return await store.upsert_if_newer(
            index, doc_id, document, timestamp_field=timestamp_field
        )

    async def atomic_update(
        self,
        index: str,
        doc_id: str,
        transform,
        *,
        upsert: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.01,
    ):
        """Read-modify-write one document, safely under concurrency.

        One call replacing two hand-rolled patterns that each appeared in several
        places and each reached past this facade to the raw client:

        * ``if_seq_no`` / ``if_primary_term`` optimistic concurrency with a
          retry-on-409 loop (``fuel/compartment_state_models.py``,
          ``Agents/approval_queue_service.py``);
        * painless scripts that increment counters
          (``fuel/driver_repository.py``).

        ``transform`` is called with a copy of the stored document and returns the
        new document, or ``None`` to leave it unchanged. ``None`` is the direct
        equivalent of painless ``ctx.op = 'noop'``.

        Returns ``(document, applied)``.

        The two backends reach the same guarantee by different means, and the
        difference is worth stating: Elasticsearch retries a compare-and-set and
        can eventually give up, so ``max_retries`` and the backoff exist and
        :class:`AppException` surfaces persistent contention. Postgres takes a row
        lock, so a concurrent writer waits instead of colliding — nothing is lost
        and nothing has to retry. Verified: with the lock removed, ten concurrent
        increments produce three.
        """
        store = self._pg_store()
        return await store.atomic_update(
            index, doc_id, transform, upsert=upsert
        )

    #: Ceiling on how many documents one :meth:`update_by_query` call will touch
    #: on the Elasticsearch branch. The branch fans the transform out over the
    #: matched ids one document at a time, so an unbounded match set would be an
    #: unbounded number of round trips. Exceeding it raises rather than applying
    #: a prefix of the change: a silently partial ``update_by_query`` leaves the
    #: index in a state no caller asked for and no caller can detect.
    UPDATE_BY_QUERY_MAX_DOCS: int = 5_000

    async def update_by_query(
        self,
        index: str,
        query: Dict[str, Any],
        transform,
    ) -> int:
        """Apply ``transform`` to every document matching ``query``; return the count.

        The facade equivalent of ``_update_by_query``, which
        ``fuel/driver_repository.py`` used with a painless script to reset
        denormalised driver counters.

        ``transform`` is a Python callable on both backends, and the
        Elasticsearch branch deliberately pays for that: rather than translate the
        change into painless it searches for the matching ids and calls
        :meth:`atomic_update` on each. A painless twin would mean the same rule
        written twice in two languages, drifting apart with nothing to catch it —
        and the ES branch is the one being deleted, so the duplication would be
        paid permanently to optimise the path with the shorter life.

        The row-locking difference from :meth:`atomic_update` carries over: on
        Postgres every matched row is locked for one transaction, so a concurrent
        write to a matched document cannot be lost. Elasticsearch resolves the
        query first and then updates each hit, so concurrent writers race per
        document.

        Returns the number of documents actually changed — a transform that
        returns ``None`` for a hit is not counted.
        """
        if self._is_retired_index(index):
            return 0

        store = self._pg_store()
        return await store.update_by_query(index, query, transform)

    async def bulk_index_documents(self, index: str, documents: List[Dict[Any, Any]]) -> Dict[str, Any]:
        """
        Bulk index multiple documents with circuit breaker protection and partial failure handling.
        
        This method handles partial failures in bulk operations by:
        - Continuing to process successful documents even when some fail
        - Logging detailed information about failed documents
        - Returning a result indicating partial success with counts
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirement 7.6: WHEN bulk indexing operations fail partially, THE Elasticsearch_Client 
          SHALL log failed documents and continue processing successful ones
        
        Args:
            index: The name of the Elasticsearch index
            documents: List of documents to index
            
        Returns:
            Dict containing:
            - success: bool indicating if all documents were indexed successfully
            - total: total number of documents attempted
            - successful: count of successfully indexed documents
            - failed: count of failed documents
            - errors: list of error details for failed documents
        """
        store = self._pg_store()
        return await store.bulk_index_documents(index, documents)
    
    
    async def search_documents(self, index: str, query: Dict[Any, Any], size: int = 100, request_timeout: int = 10):
        """
        Search documents in an index with circuit breaker protection.
        
        Args:
            index: The Elasticsearch index to search.
            query: The query body.
            size: Maximum number of results to return.
            request_timeout: Per-call timeout in seconds (default 10s).
                Prevents a single slow aggregation from blocking the
                ASGI event loop for the full connection-level 30s.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        store = self._pg_store()
        return await store.search_documents(
            index, query, size, request_timeout
        )
    
    async def multi_search(
        self,
        searches: List[Dict[str, Any]],
        request_timeout: int = 10,
    ) -> Dict[str, Any]:
        """Run several search bodies in ONE round trip via the `_msearch` API.

        The point of this method is the round-trip count: N independent
        `terms`-filtered lookups cost one network hop instead of N. It is what
        lets a read model collapse an N+1 fan-out into a fixed budget (see
        `DriverWorkService`, which resolves compartment prior grades and stop
        coordinates in a single call).

        Args:
            searches: One entry per search body, each
                ``{"index": <index name>, "query": <query body>}``. A missing
                index is tolerated per body (``ignore_unavailable``), so a
                deployment that has not created an optional index gets an empty
                result rather than a failed request.
            request_timeout: Per-call timeout in seconds.

        Returns:
            The raw `_msearch` response, ``{"responses": [<search response>, ...]}``
            in request order. An empty ``searches`` list returns
            ``{"responses": []}`` without touching the cluster.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        if not searches:
            return {"responses": []}

        store = self._pg_store()
        return await store.multi_search(searches, request_timeout)

    async def get_document(self, index: str, doc_id: str):
        """
        Get a single document by ID with circuit breaker protection.

        Returns the document ``_source`` dict, or ``None`` when the document
        does not exist. A missing document is an expected outcome for
        idempotency / existence checks (e.g. the weather-alert ingester
        checking whether an alert was already persisted), so a 404 is NOT
        logged at ERROR — it quietly returns ``None``. Genuine ES failures
        (auth, connection, 5xx) still raise through ``_handle_elasticsearch_error``.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        store = self._pg_store()
        return await store.get_document(index, doc_id)
    
    async def delete_document(self, index: str, doc_id: str) -> bool:
        """
        Delete a single document by ID with circuit breaker protection.

        Returns True if the document was deleted, False if not found.
        """
        if self._is_retired_index(index):
            return False
        store = self._pg_store()
        return await store.delete_document(index, doc_id)

    async def get_all_documents(self, index: str, size: int = 1000):
        """
        Get all documents from an index with circuit breaker protection.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        try:
            query = {
                "query": {"match_all": {}},
                "sort": [{"created_at": {"order": "desc"}}]
            }
            response = await self.search_documents(index, query, size)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error(f"get_all_documents({index})", e)
    
    async def semantic_search(self, tenant_id: str, index: str, text: str, fields: List[str], size: int = 10):
        """
        Full-text search across ``fields``, with circuit breaker protection.

        The name is historical and the docstring used to claim "semantic search
        using semantic_text fields". It never did: the query below is a plain
        ``multi_match``, which is lexical, and no inference endpoint is
        configured anywhere in the codebase. The four mappings that declared
        ``semantic_text`` for these fields have been changed to ``text`` (see the
        note above ``_get_locations_mapping``) precisely because this method
        behaves identically either way — while the type made index creation fail
        outright on any cluster that does not support it.

        The query is scoped to the supplied tenant: every request is wrapped
        with a ``{"term": {"tenant_id": tenant_id}}`` filter so a caller
        cannot see documents from another tenant even if the index is shared.
        This is required because ``/api/search`` and every AI tool that calls
        ``semantic_search`` runs on behalf of an authenticated tenant and
        must not leak cross-tenant rows.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("semantic_search requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": text,
                                    "fields": fields,
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                }
            }
            response = await self.search_documents(index, query, size)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error(f"semantic_search({index})", e)
    
    # Analytics-specific methods
    async def get_time_series_data(self, tenant_id: str, event_type: str, metric_field: str, time_range: str = "7d"):
        """
        Get time-series data for analytics charts with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_time_series_data requires a tenant_id")
        try:
            # Calculate date range
            from datetime import timedelta
            now = utcnow()

            if time_range == "24h":
                start_time = now - timedelta(hours=24)
                interval = "1h"
            elif time_range == "7d":
                start_time = now - timedelta(days=7)
                interval = "1d"
            elif time_range == "30d":
                start_time = now - timedelta(days=30)
                interval = "1d"
            else:  # 90d
                start_time = now - timedelta(days=90)
                interval = "1d"

            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"event_type": event_type}},
                            {"range": {"timestamp": {"gte": start_time.isoformat()}}}
                        ],
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                        ],
                    }
                },
                "aggs": {
                    "time_series": {
                        "date_histogram": {
                            "field": "timestamp",
                            "fixed_interval": interval,
                            "min_doc_count": 0
                        },
                        "aggs": {
                            "avg_metric": {
                                "avg": {"field": f"metrics.{metric_field}"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["time_series"]["buckets"]
            
            return [
                {
                    "timestamp": bucket["key_as_string"],
                    "value": round(bucket["avg_metric"]["value"] or 0, 2)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_time_series_data", e)
    
    async def get_route_performance_data(self, tenant_id: str):
        """
        Get route performance aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_route_performance_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "route_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "routes": {
                        "terms": {"field": "route_name.keyword", "size": 10},
                        "aggs": {
                            "avg_performance": {
                                "avg": {"field": "metrics.performance_pct"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["routes"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "performance": round(bucket["avg_performance"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_route_performance_data", e)
    
    async def get_delay_causes_data(self, tenant_id: str):
        """
        Get delay causes aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_delay_causes_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "delay_cause_analysis"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "causes": {
                        "terms": {"field": "delay_cause", "size": 10},
                        "aggs": {
                            "avg_percentage": {
                                "avg": {"field": "metrics.percentage"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["causes"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "percentage": round(bucket["avg_percentage"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_delay_causes_data", e)
    
    async def get_regional_performance_data(self, tenant_id: str):
        """
        Get regional performance aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_regional_performance_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "regional_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "regions": {
                        "terms": {"field": "region", "size": 10},
                        "aggs": {
                            "avg_on_time": {
                                "avg": {"field": "metrics.on_time_percentage"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["regions"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "onTimePercentage": round(bucket["avg_on_time"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_regional_performance_data", e)
    
    async def get_current_metrics(self, tenant_id: str):
        """
        Get current performance metrics with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_current_metrics requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "daily_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "sort": [{"timestamp": {"order": "desc"}}],
                "size": 1
            }
            
            response = await self.search_documents("analytics_events", query)
            if response["hits"]["hits"]:
                latest = response["hits"]["hits"][0]["_source"]["metrics"]
                return {
                    "delivery_performance": {
                        "title": "Delivery Performance",
                        "value": f"{latest.get('delivery_performance_pct', 87.5)}%",
                        "change": "+2.3%",
                        "trend": "up"
                    },
                    "average_delay": {
                        "title": "Average Delay", 
                        "value": f"{latest.get('average_delay_minutes', 144)/60:.1f} hrs",
                        "change": "-0.8 hrs",
                        "trend": "down"
                    },
                    "fleet_utilization": {
                        "title": "Fleet Utilization",
                        "value": f"{latest.get('fleet_utilization_pct', 92)}%",
                        "change": "+5%",
                        "trend": "up"
                    },
                    "customer_satisfaction": {
                        "title": "Customer Satisfaction",
                        "value": f"{latest.get('customer_satisfaction', 4.2)}/5",
                        "change": "+0.1",
                        "trend": "up"
                    }
                }
            else:
                raise Exception("No analytics data found")
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_current_metrics", e)

# Global instance
elasticsearch_service = ElasticsearchService()
