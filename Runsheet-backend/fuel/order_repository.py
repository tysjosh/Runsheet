"""
Fuel Order Repository — tenant-scoped CRUD for ``fuel_orders_current`` and
``fuel_order_events`` Elasticsearch indices.

Implements :class:`FuelOrderRepository` with:

* ``get`` — single order by ID, tenant-scoped.
* ``create`` — persist a new FuelOrder.
* ``upsert_with_last_event_timestamp`` — scripted upsert that noops on stale
  events (incoming ``last_event_timestamp`` ≤ stored).
* ``list_for_tenant`` — paginated listing with tenant isolation.
* ``search`` — full filter set from Req 2.5.1 (status, customer_id,
  driver_id, call_type, product_code, start_date, end_date,
  intake_channel, pagination, sort).
* ``append_event`` — append an immutable event to ``fuel_order_events``.
* ``get_events_for_order`` — retrieve the event timeline for an order.

Every method wraps reads through
:func:`ops.middleware.tenant_guard.inject_tenant_filter` and validates
returned documents re-match the caller's tenant before crossing the
repository boundary. Cross-tenant reads degrade to ``None`` (for ``get``)
or empty lists (for ``search``/``list_for_tenant``/``get_events_for_order``).
Cross-tenant writes raise :class:`OrderCrossTenantAccessError`.

Validates: Requirements 1.1.6, 9.1.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fuel.order_models import FuelOrder, FuelOrderEvent
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrderCrossTenantAccessError(PermissionError):
    """Raised when a write targets an order owned by another tenant.

    Cross-tenant reads degrade silently to ``None`` / empty lists so the
    REST layer can return a uniform HTTP 404 without leaking existence.
    Cross-tenant writes are a security violation and MUST raise.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        order_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.order_id = order_id
        self.owning_tenant_id = owning_tenant_id
        super().__init__(
            f"Tenant {tenant_id!r} attempted cross-tenant access on "
            f"order {order_id!r} (owner={owning_tenant_id!r})"
        )


# ---------------------------------------------------------------------------
# Painless script for timestamp-guarded upsert
# ---------------------------------------------------------------------------

#: Compares incoming ``last_event_timestamp`` against the stored value.
#: If the incoming timestamp is older or equal, the operation is a noop.
#: Otherwise all incoming fields overwrite the stored document.
_ORDER_UPSERT_SCRIPT = """
    if (ctx._source.containsKey('last_event_timestamp') && ctx._source.last_event_timestamp != null) {
        ZonedDateTime existing = ZonedDateTime.parse(ctx._source.last_event_timestamp);
        ZonedDateTime incoming = ZonedDateTime.parse(params.last_event_timestamp);
        if (incoming.isBefore(existing) || incoming.isEqual(existing)) {
            ctx.op = 'noop';
            return;
        }
    }
    for (entry in params.entrySet()) {
        ctx._source[entry.getKey()] = entry.getValue();
    }
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not resp:
        return []
    # Handle both dict and ObjectApiResponse (which has .get() but isn't a dict)
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _extract_total(resp: Any) -> int:
    """Extract the total hit count from an ES search response."""
    if not resp:
        return 0
    # Handle both dict and ObjectApiResponse
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return 0
    total = hits_outer.get("total") if hasattr(hits_outer, 'get') else None
    if hasattr(total, 'get'):
        return total.get("value", 0)
    if isinstance(total, int):
        return total
    return 0


def _safe_order_load(source: Dict[str, Any]) -> Optional[FuelOrder]:
    """Build a :class:`FuelOrder` from a raw ES source, logging on failure."""
    try:
        return FuelOrder(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "FuelOrderRepository: dropping fuel_orders_current doc that "
            "failed model validation (order_id=%s): %s",
            source.get("order_id"),
            exc,
        )
        return None


def _safe_event_load(source: Dict[str, Any]) -> Optional[FuelOrderEvent]:
    """Build a :class:`FuelOrderEvent` from a raw ES source, logging on failure."""
    try:
        return FuelOrderEvent(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "FuelOrderRepository: dropping fuel_order_events doc that "
            "failed model validation (event_id=%s): %s",
            source.get("event_id"),
            exc,
        )
        return None


#: Fields the order free-text search matches against, paired with the concrete
#: index field a ``wildcard`` query should target. ``customer_name`` is an
#: analyzed ``text`` field with a ``.keyword`` subfield, so we wildcard the raw
#: keyword for whole-value substring matching; the others are already keyword
#: (order_id, customer_id) or analyzed-only (ship_to_address).
_ORDER_TEXT_WILDCARD_FIELDS = (
    "order_id",
    "customer_name.keyword",
    "customer_id",
    "ship_to_address",
)


def _build_text_should(q: Optional[str]) -> List[Dict[str, Any]]:
    """Build the ES ``should`` clauses for an order free-text search.

    Returns case-insensitive ``wildcard`` ``*q*`` queries over the searchable
    fields, or an empty list when ``q`` is blank. The ``*`` / ``?`` / ``\\``
    wildcard metacharacters in the user input are escaped so they match
    literally rather than as wildcards.
    """
    if not q or not q.strip():
        return []
    needle = q.strip()
    escaped = (
        needle.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
    )
    pattern = f"*{escaped}*"
    return [
        {
            "wildcard": {
                field: {"value": pattern, "case_insensitive": True}
            }
        }
        for field in _ORDER_TEXT_WILDCARD_FIELDS
    ]


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class FuelOrderRepository:
    """Tenant-scoped CRUD repository for fuel orders and order events.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``

    which matches :class:`services.elasticsearch_service.ElasticsearchService`.

    Tenant isolation is enforced at two points for defense-in-depth:
        1. Every ES query is wrapped through
           :func:`ops.middleware.tenant_guard.inject_tenant_filter`.
        2. Every returned document is re-validated against the caller's
           ``tenant_id`` before it crosses the repository boundary.
    """

    DEFAULT_LIST_SIZE: int = 500
    DEFAULT_PAGE_SIZE: int = 20

    def __init__(
        self,
        es_service: Any,
        *,
        orders_index: str = FUEL_ORDERS_CURRENT_INDEX,
        events_index: str = FUEL_ORDER_EVENTS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        self._es = es_service
        self._orders_index = orders_index
        self._events_index = events_index

    # ------------------------------------------------------------------
    # Get (single order)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, order_id: str
    ) -> Optional[FuelOrder]:
        """Return the order or ``None`` if it does not exist / is not owned.

        Cross-tenant fetches degrade to ``None`` so the REST layer can
        return a uniform HTTP 404 without leaking existence.
        """
        self._require_tenant(tenant_id)
        if not order_id or not order_id.strip():
            raise ValueError("order_id must be a non-empty string")

        # Read-cutover: serve from Postgres when enabled.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_get,
        )
        pg = await read_hybrid_get("fuel_order", tenant_id, order_id)
        if pg is not _NOT_CUT_OVER:
            return _safe_order_load(pg) if pg is not None else None

        query = inject_tenant_filter(
            {"query": {"term": {"order_id": order_id}}},
            tenant_id,
        )
        query["size"] = 1

        try:
            resp = await self._es.search_documents(
                self._orders_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "FuelOrderRepository.get: search failed for order=%s: %s",
                order_id,
                exc,
            )
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        source = sources[0]
        # Defense-in-depth: re-validate tenant ownership
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "FuelOrderRepository.get: suppressing cross-tenant hit "
                "for order=%s (owner=%s, requester=%s)",
                order_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None

        return _safe_order_load(source)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        order: FuelOrder | Dict[str, Any],
    ) -> FuelOrder:
        """Persist a new FuelOrder and return the stored model.

        Raises :class:`OrderCrossTenantAccessError` if the order's
        ``tenant_id`` does not match the caller's ``tenant_id``.
        """
        self._require_tenant(tenant_id)

        payload = self._coerce_order_to_dict(order)
        payload.setdefault("tenant_id", tenant_id)

        if payload["tenant_id"] != tenant_id:
            raise OrderCrossTenantAccessError(
                tenant_id=tenant_id,
                order_id=str(payload.get("order_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now

        # Validate through the Pydantic model before touching ES
        model = FuelOrder(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._orders_index, model.order_id, doc
        )
        # Dual-write the order current-state to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("fuel_order", doc)
        return model

    # ------------------------------------------------------------------
    # Upsert with last_event_timestamp (scripted)
    # ------------------------------------------------------------------

    async def upsert_with_last_event_timestamp(
        self,
        tenant_id: str,
        order: FuelOrder | Dict[str, Any],
    ) -> bool:
        """Scripted upsert that noops when incoming timestamp is stale.

        Compares incoming ``last_event_timestamp`` against the stored
        value. If the incoming event is older or equal, the operation is
        a noop and returns ``False``. Otherwise the document is updated
        and returns ``True``.

        Raises :class:`OrderCrossTenantAccessError` if the order's
        ``tenant_id`` does not match the caller's ``tenant_id``.
        """
        self._require_tenant(tenant_id)

        payload = self._coerce_order_to_dict(order)
        payload.setdefault("tenant_id", tenant_id)

        if payload["tenant_id"] != tenant_id:
            raise OrderCrossTenantAccessError(
                tenant_id=tenant_id,
                order_id=str(payload.get("order_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        order_id = payload.get("order_id")
        if not order_id:
            raise ValueError("order must have an order_id for upsert")

        # Validate through the Pydantic model before touching ES
        model = FuelOrder(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        try:
            response = self._es.client.update(
                index=self._orders_index,
                id=order_id,
                body={
                    "scripted_upsert": True,
                    "script": {
                        "source": _ORDER_UPSERT_SCRIPT,
                        "lang": "painless",
                        "params": doc,
                    },
                    "upsert": doc,
                },
                refresh=True,
            )
            result = response.get("result", "")
            if result == "noop":
                # Two cases produce a scripted_upsert "noop":
                #   (a) genuine stale-event discard — the doc EXISTS and the
                #       incoming last_event_timestamp is older-or-equal; OR
                #   (b) a serverless-ES quirk where ``scripted_upsert`` reports
                #       "noop" AND fails to materialise the ``upsert`` body on a
                #       FRESH insert (the doc does not exist afterwards).
                # Case (b) silently dropped every new order from BOTH stores —
                # and since reads are served from Postgres, the dispatcher hit a
                # 404 immediately after a 201 create. Distinguish them: if the
                # doc is absent, index it directly (fresh insert); only a true
                # existing-doc noop is a stale discard.
                exists = self._es.client.exists(
                    index=self._orders_index, id=order_id
                )
                if exists:
                    logger.info(
                        "FuelOrderRepository.upsert_with_last_event_timestamp: "
                        "discarded stale event for order=%s, "
                        "incoming_timestamp=%s",
                        order_id,
                        doc.get("last_event_timestamp"),
                    )
                    return False
                # Fresh insert that the scripted upsert failed to apply — index
                # the document directly so ES and Postgres both receive it.
                self._es.client.index(
                    index=self._orders_index,
                    id=order_id,
                    body=doc,
                    refresh=True,
                )
                logger.info(
                    "FuelOrderRepository.upsert_with_last_event_timestamp: "
                    "scripted_upsert no-op'd a fresh insert for order=%s; "
                    "indexed directly (serverless-ES fallback)",
                    order_id,
                )
            # Dual-write the order current-state to Postgres. The repository's
            # own stale-event guard mirrors the ES scripted-upsert semantics.
            from commerce.services.commerce_persistence_bridge import (
                mirror_current_state_upsert,
            )
            await mirror_current_state_upsert("fuel_order", doc)
            return True
        except Exception as exc:
            logger.error(
                "FuelOrderRepository.upsert_with_last_event_timestamp: "
                "failed for order=%s: %s",
                order_id,
                exc,
            )
            raise

    # ------------------------------------------------------------------
    # List for tenant
    # ------------------------------------------------------------------

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[FuelOrder]:
        """List all orders for the tenant (up to ``size``).

        Results are tenant-scoped and re-validated before returning.
        """
        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        # Read-cutover: serve from Postgres when enabled. Matches the ES query
        # (match_all, sort created_at desc, capped at ``size``).
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )
        pg = await read_hybrid_search(
            "fuel_order", tenant_id,
            sort_field="created_at", sort_order="desc",
            page=1, size=size,
        )
        if pg is not _NOT_CUT_OVER:
            out: List[FuelOrder] = []
            for source in pg["items"]:
                model = _safe_order_load(source)
                if model is not None:
                    out.append(model)
            return out

        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant_id,
        )
        query["size"] = size
        query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._orders_index, query, size
        )
        sources = _extract_sources(resp)

        out: List[FuelOrder] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "FuelOrderRepository.list_for_tenant: dropping doc "
                    "with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_order_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Search (full filter set from Req 2.5.1)
    # ------------------------------------------------------------------

    async def search(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        customer_phone: Optional[str] = None,
        driver_id: Optional[str] = None,
        call_type: Optional[str] = None,
        product_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        intake_channel: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        size: int = DEFAULT_PAGE_SIZE,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search orders with the full filter set from Req 2.5.1.

        Parameters:
            tenant_id: The caller's tenant.
            status: Filter by order status.
            customer_id: Filter by customer_id.
            customer_phone: Filter by customer_phone (exact match).
            driver_id: Filter by assigned_driver_id.
            call_type: Filter by call_type.
            product_code: Filter by product_code.
            start_date: Filter orders created on or after this ISO date.
            end_date: Filter orders created on or before this ISO date.
            intake_channel: Filter by intake_channel.
            q: Free-text "contains" search (case-insensitive) over order_id,
                customer_name, customer_id, and ship_to_address. ANDed with the
                structured filters above.
            page: 1-based page number.
            size: Page size.
            sort: Sort field and direction (e.g. "created_at:desc").

        Returns:
            A dict with ``orders`` (list of FuelOrder), ``total`` (int),
            ``page`` (int), ``size`` (int).

        Cross-tenant results are silently dropped (empty list).
        """
        self._require_tenant(tenant_id)
        if page < 1:
            page = 1
        if size <= 0:
            size = self.DEFAULT_PAGE_SIZE

        # Read-cutover: serve from Postgres when enabled. Maps the ES filter set
        # onto document term-filters + a created_at range, preserving the
        # offset/total contract and the sort semantics ("field:order").
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )
        term_filters = {
            "status": status,
            "customer_id": customer_id,
            "customer_phone": customer_phone,
            "assigned_driver_id": driver_id,
            "call_type": call_type,
            "product_code": product_code,
            "intake_channel": intake_channel,
        }
        if sort:
            parts = sort.split(":")
            pg_sort_field = parts[0]
            pg_sort_order = parts[1] if len(parts) > 1 else "desc"
        else:
            pg_sort_field, pg_sort_order = "created_at", "desc"
        pg = await read_hybrid_search(
            "fuel_order", tenant_id,
            term_filters=term_filters,
            range_field="created_at", range_gte=start_date, range_lte=end_date,
            text_query=q,
            text_fields=[
                "order_id",
                "customer_name",
                "customer_id",
                "ship_to_address",
            ],
            sort_field=pg_sort_field, sort_order=pg_sort_order,
            page=page, size=size,
        )
        if pg is not _NOT_CUT_OVER:
            orders_pg: List[FuelOrder] = []
            for source in pg["items"]:
                model = _safe_order_load(source)
                if model is not None:
                    orders_pg.append(model)
            return {
                "orders": orders_pg,
                "total": pg["total"],
                "page": pg["page"],
                "size": pg["size"],
            }

        # Build filter clauses
        filters: List[Dict[str, Any]] = []
        if status:
            filters.append({"term": {"status": status}})
        if customer_id:
            filters.append({"term": {"customer_id": customer_id}})
        if customer_phone:
            filters.append({"term": {"customer_phone": customer_phone}})
        if driver_id:
            filters.append({"term": {"assigned_driver_id": driver_id}})
        if call_type:
            filters.append({"term": {"call_type": call_type}})
        if product_code:
            filters.append({"term": {"product_code": product_code}})
        if intake_channel:
            filters.append({"term": {"intake_channel": intake_channel}})

        # Date range filter on created_at
        if start_date or end_date:
            date_range: Dict[str, Any] = {}
            if start_date:
                date_range["gte"] = start_date
            if end_date:
                date_range["lte"] = end_date
            filters.append({"range": {"created_at": date_range}})

        # Build the inner query from the structured filters.
        if filters:
            inner_query: Dict[str, Any] = {
                "query": {"bool": {"must": filters}}
            }
        else:
            inner_query = {"query": {"match_all": {}}}

        # Wrap with tenant filter
        query = inject_tenant_filter(inner_query, tenant_id)

        # Layer the free-text "contains" search onto the (now tenant-scoped)
        # top-level bool as a should-clause requiring at least one match. This
        # ANDs with the tenant filter and the structured filters while keeping
        # the same substring semantics as the former client-side filter.
        # Wildcard with case_insensitive works on keyword fields (order_id,
        # customer_id, customer_name.keyword) and the analyzed ship_to_address.
        text_should = _build_text_should(q)
        if text_should:
            query["query"]["bool"]["should"] = text_should
            query["query"]["bool"]["minimum_should_match"] = 1

        # Pagination
        from_offset = (page - 1) * size
        query["from"] = from_offset
        query["size"] = size

        # Sort
        if sort:
            parts = sort.split(":")
            sort_field = parts[0]
            sort_order = parts[1] if len(parts) > 1 else "desc"
            query["sort"] = [{sort_field: {"order": sort_order}}]
        else:
            query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._orders_index, query, size
        )
        sources = _extract_sources(resp)
        total = _extract_total(resp)

        orders: List[FuelOrder] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "FuelOrderRepository.search: dropping doc with "
                    "mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_order_load(source)
            if model is not None:
                orders.append(model)

        return {
            "orders": orders,
            "total": total,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Append event
    # ------------------------------------------------------------------

    async def append_event(
        self,
        tenant_id: str,
        event: FuelOrderEvent | Dict[str, Any],
    ) -> FuelOrderEvent:
        """Append an immutable event to the ``fuel_order_events`` index.

        Raises :class:`OrderCrossTenantAccessError` if the event's
        ``tenant_id`` does not match the caller's ``tenant_id``.
        """
        self._require_tenant(tenant_id)

        payload = self._coerce_event_to_dict(event)
        payload.setdefault("tenant_id", tenant_id)

        if payload["tenant_id"] != tenant_id:
            raise OrderCrossTenantAccessError(
                tenant_id=tenant_id,
                order_id=str(payload.get("order_id", "<unknown>")),
                owning_tenant_id=payload["tenant_id"],
            )

        now = _utcnow_iso()
        if not payload.get("ingested_at"):
            payload["ingested_at"] = now

        # Validate through the Pydantic model before touching ES
        model = FuelOrderEvent(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._events_index, model.event_id, doc
        )
        return model

    # ------------------------------------------------------------------
    # Get events for order
    # ------------------------------------------------------------------

    async def get_events_for_order(
        self,
        tenant_id: str,
        order_id: str,
        *,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[FuelOrderEvent]:
        """Retrieve the event timeline for an order, sorted ascending.

        Cross-tenant results are silently dropped (empty list).
        """
        self._require_tenant(tenant_id)
        if not order_id or not order_id.strip():
            raise ValueError("order_id must be a non-empty string")

        query = inject_tenant_filter(
            {"query": {"term": {"order_id": order_id}}},
            tenant_id,
        )
        query["size"] = size
        query["sort"] = [{"event_timestamp": {"order": "asc"}}]

        resp = await self._es.search_documents(
            self._events_index, query, size
        )
        sources = _extract_sources(resp)

        events: List[FuelOrderEvent] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "FuelOrderRepository.get_events_for_order: dropping "
                    "event with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_event_load(source)
            if model is not None:
                events.append(model)
        return events

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        """Validate that tenant_id is a non-empty string."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_order_to_dict(
        order: FuelOrder | Dict[str, Any],
    ) -> Dict[str, Any]:
        """Coerce a FuelOrder or dict into a mutable dict."""
        if isinstance(order, FuelOrder):
            return order.model_dump(mode="python")
        if isinstance(order, dict):
            return dict(order)
        raise TypeError(
            f"order must be a FuelOrder or dict, got {type(order).__name__}"
        )

    @staticmethod
    def _coerce_event_to_dict(
        event: FuelOrderEvent | Dict[str, Any],
    ) -> Dict[str, Any]:
        """Coerce a FuelOrderEvent or dict into a mutable dict."""
        if isinstance(event, FuelOrderEvent):
            return event.model_dump(mode="python")
        if isinstance(event, dict):
            return dict(event)
        raise TypeError(
            f"event must be a FuelOrderEvent or dict, got "
            f"{type(event).__name__}"
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "FuelOrderRepository",
    "OrderCrossTenantAccessError",
]
