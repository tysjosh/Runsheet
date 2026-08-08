"""
Ops Elasticsearch Service for the Ops Intelligence Layer.

Manages ops-specific indices (shipments_current, shipment_events, riders_current,
ops_poison_queue) with strict mappings, scripted upserts for out-of-order event
reconciliation, ILM policies, and bulk operations.

Delegates to the existing ElasticsearchService for connection management and
circuit breaker protection.

Validates:
- Requirement 5.1-5.6: Elasticsearch index creation and strict mappings
- Requirement 6.1-6.9: Upsert logic with out-of-order event reconciliation
- Requirement 7.1-7.5: Index lifecycle and retention policies
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


class OpsElasticsearchService:
    """
    Manages ops-specific indices and operations.
    Delegates to the existing ElasticsearchService for connection/circuit breaker.
    """

    SHIPMENTS_CURRENT = "shipments_current"
    SHIPMENT_EVENTS = "shipment_events"
    RIDERS_CURRENT = "riders_current"
    POISON_QUEUE = "ops_poison_queue"

    # Painless script for current-state upsert with out-of-order reconciliation.
    # Compares incoming event_timestamp vs existing last_event_timestamp.
    # Discards (noop) if incoming is older-or-equal. Otherwise partial-updates
    # only the fields present in the incoming params.
    # Validates: Req 6.1, 6.2, 6.4, 6.7, 6.8
    #
    # BOUND to the canonical copy rather than restated. Three identical
    # transcriptions of this script existed — here, in
    # ``fuel/order_repository.py``, and now in ``ElasticsearchService`` — and a
    # painless script is exactly the kind of thing that gets fixed in one place
    # and left stale in the others, silently, because a wrong comparison shows up
    # as an order moving backwards weeks later rather than as an error.
    #
    # Nothing in this class runs it any more — ``bulk_upsert`` was the last
    # user and now calls ``ElasticsearchService.upsert_if_newer``. Kept as a
    # bound alias because it is a documented attribute of this class and the
    # binding is what guarantees it cannot drift from the canonical script.

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    @property
    def client(self):
        """Access the underlying Elasticsearch client."""
        return self._es.client

    async def search_documents(self, index, query, size=100, request_timeout=10):
        """Passthrough to the facade's search, so callers stay off the raw client.

        ``ops/api/endpoints.py`` and the two ops agent tools all held an
        ``OpsElasticsearchService`` and reached through it to
        ``es.client.search(...)``, which bypasses the document-store backend
        switch — those 23 reads would have kept going to Elasticsearch after the
        document plane moved to Postgres.

        Same shape as the existing ``client`` passthrough, so nothing has to know
        whether it is holding this class or ``ElasticsearchService``.
        """
        return await self._es.search_documents(
            index, query, size=size, request_timeout=request_timeout
        )

    # The rest of the document plane, passed through for the same reason: the
    # poison queue held an ``OpsElasticsearchService`` and reached
    # ``.client.index`` / ``.get`` / ``.update`` / ``.delete`` / ``.count``, all of
    # which bypass the backend switch. A poison-queue entry that lands in the
    # wrong store after the cutover is an incident nobody can find.

    async def index_document(self, index, doc_id, document):
        return await self._es.index_document(index, doc_id, document)

    async def get_document(self, index, doc_id):
        return await self._es.get_document(index, doc_id)

    async def update_document(self, index, doc_id, partial_doc):
        return await self._es.update_document(index, doc_id, partial_doc)

    async def delete_document(self, index, doc_id):
        return await self._es.delete_document(index, doc_id)

    @property
    def circuit_breaker(self):
        """Access the circuit breaker from the delegate service."""
        return self._es.circuit_breaker

    # ------------------------------------------------------------------
    # Index setup
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Index mappings
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Shipment events alias for time-based rollover
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Upsert operations
    # ------------------------------------------------------------------

    async def upsert_shipment_current(self, doc: Dict[str, Any]) -> bool:
        """
        Scripted upsert for shipments_current.
        Compares incoming event_timestamp vs existing last_event_timestamp.
        Discards if stale. Partial update only fields present in incoming event.
        Validates: Req 6.1, 6.4, 6.7, 6.8
        """
        shipment_id = doc.get("shipment_id")
        if not shipment_id:
            logger.error("Cannot upsert shipment: missing shipment_id")
            return False

        # Elasticsearch is the only store for shipments. Rev 0007 dropped the
        # ``shipments_current`` Postgres table, so the dual-write that used to
        # live here mirrored into a retired aggregate: every applied upsert
        # raised ``ValueError: Unknown hybrid aggregate_type: 'shipment'``
        # inside the bridge, which swallowed it and logged a misleading
        # "Postgres dual-write failed" for a table that no longer exists.
        return await self._scripted_upsert(
            index=self.SHIPMENTS_CURRENT,
            doc_id=shipment_id,
            doc=doc,
            entity_label="shipment",
        )

    async def upsert_rider_current(self, doc: Dict[str, Any]) -> bool:
        """
        Scripted upsert for riders_current.
        Same timestamp-based upsert logic as shipments.
        Validates: Req 6.2, 6.7
        """
        rider_id = doc.get("rider_id")
        if not rider_id:
            logger.error("Cannot upsert rider: missing rider_id")
            return False

        return await self._scripted_upsert(
            index=self.RIDERS_CURRENT,
            doc_id=rider_id,
            doc=doc,
            entity_label="rider",
        )

    async def _scripted_upsert(
        self,
        index: str,
        doc_id: str,
        doc: Dict[str, Any],
        entity_label: str,
    ) -> bool:
        """
        Execute a scripted upsert with out-of-order event reconciliation.

        The painless script compares incoming event_timestamp against the
        existing last_event_timestamp. If the incoming event is older or
        equal, the operation is a noop. Otherwise, only the fields present
        in the incoming document are updated.

        Returns True if the document was updated, False if discarded (noop).
        """
        from resilience.circuit_breaker import CircuitOpenException

        try:
            # ``ElasticsearchService.upsert_if_newer`` owns the comparison now.
            # This method's painless script was byte-identical to the one in
            # ``fuel/order_repository.py``; both reached past the facade to
            # ``client.update``, which would keep them writing to Elasticsearch
            # after the document plane moved to Postgres while everything around
            # them wrote to Postgres. The facade also carries the circuit breaker,
            # so the wrapper that used to be here is redundant.
            applied = await self._es.upsert_if_newer(index, doc_id, doc)
            if not applied:
                logger.info(
                    f"Discarded stale {entity_label} event: "
                    f"entity_id={doc_id}, "
                    f"incoming_timestamp={doc.get('last_event_timestamp')}, "
                    f"event_id={doc.get('trace_id', 'unknown')}"
                )
            return applied
        except CircuitOpenException as e:
            self._es._handle_circuit_breaker_exception(e)
            return False
        except Exception as e:
            self._es._handle_elasticsearch_error(
                f"upsert_{entity_label}({index})", e
            )
            return False

    # ------------------------------------------------------------------
    # Append operations
    # ------------------------------------------------------------------

    async def append_shipment_event(self, doc: Dict[str, Any]) -> None:
        """
        Always append to shipment_events regardless of ordering.
        Uses event_id as document ID.
        Validates: Req 6.3, 6.9
        """
        event_id = doc.get("event_id")
        if not event_id:
            logger.error("Cannot append shipment event: missing event_id")
            return

        await self._es.index_document(
            index=self.SHIPMENT_EVENTS,
            doc_id=event_id,
            document=doc,
        )

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    #: ``action -> (index, id field)``. Both upsert actions go through
    #: out-of-order reconciliation; ``append_event`` is an immutable event
    #: document and is written as-is.
    _BULK_ACTIONS = {
        "upsert_shipment": ("SHIPMENTS_CURRENT", "shipment_id", True),
        "upsert_rider": ("RIDERS_CURRENT", "rider_id", True),
        "append_event": ("SHIPMENT_EVENTS", "event_id", False),
    }

    async def bulk_upsert(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Batch ingestion of shipment / rider / event documents.

        Each operation dict must contain:
          - "action": "upsert_shipment" | "upsert_rider" | "append_event"
          - "doc": the document payload

        Returns a summary with success/failure counts and per-document errors.
        Validates: Req 6.5, 6.6

        This used to build ``elasticsearch.helpers.bulk`` actions carrying a
        ``scripted_upsert`` and hand ``self.client`` to the helper. That was the
        last application write in the codebase that bypassed the document-store
        backend switch, and it was invisible to the raw-client inventory because
        the client was passed as an ARGUMENT rather than called as
        ``.client.bulk(...)`` — the inventory now recognises that shape too.

        The batching is gone with it: each operation is one
        :meth:`~services.elasticsearch_service.ElasticsearchService.upsert_if_newer`
        or ``index_document`` call. On Postgres that costs nothing, because the
        store applies the timestamp comparison per row under a lock either way;
        on Elasticsearch it trades one round trip for N. Worth it — the
        alternative was a fourth transcription of the painless script living on a
        code path with no callers and no tests to catch it drifting.

        One behaviour deliberately changed: a stale document (older-or-equal
        ``last_event_timestamp``) now counts as ``discarded`` rather than
        ``successful``. The bulk helper reported a scripted no-op as a success, so
        an ingestion run that discarded every event as stale was indistinguishable
        from one that applied every event.
        """
        results: Dict[str, Any] = {
            "total": len(operations),
            "successful": 0,
            "discarded": 0,
            "failed": 0,
            "errors": [],
        }

        for op in operations:
            action_type = op.get("action")
            doc = op.get("doc", {}) or {}
            spec = self._BULK_ACTIONS.get(action_type)
            if spec is None:
                results["failed"] += 1
                results["errors"].append({
                    "action": action_type,
                    "error": f"Unknown action type: {action_type}",
                })
                continue

            index_attribute, id_field, reconcile = spec
            index = getattr(self, index_attribute)
            doc_id = doc.get(id_field)
            try:
                if reconcile:
                    applied = await self._es.upsert_if_newer(index, doc_id, doc)
                    if applied:
                        results["successful"] += 1
                    else:
                        results["discarded"] += 1
                else:
                    await self._es.index_document(index, doc_id, doc)
                    results["successful"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad document must not
                # abandon the rest of the batch, which is what the bulk helper's
                # ``raise_on_error=False`` bought.
                results["failed"] += 1
                results["errors"].append({
                    "action": action_type,
                    "doc_id": doc_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                })
                logger.error(
                    "Bulk ops ingestion failed: action=%s doc_id=%s error=%s: %s",
                    action_type, doc_id, type(exc).__name__, exc,
                )

        if not results["failed"]:
            logger.info(
                "Bulk ops ingested %d documents (%d discarded as stale)",
                results["successful"], results["discarded"],
            )
        return results

    # ------------------------------------------------------------------
    # ILM policies
    # ------------------------------------------------------------------


