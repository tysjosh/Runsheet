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
* ``search_for_driver`` — the driver "my work" read: one tenant-filtered
  search scoped to a single ``assigned_driver_id``, a ``terms`` filter over
  statuses, an optional ``delivery_window_start`` range, sorted by
  ``delivery_window_start`` ascending.
* ``get_current`` — the authoritative stored document (no hybrid read).
* ``claim_assignment`` / ``release_assignment`` — run-link CAS and release
  by claim-id ownership (loading-plan-executor K5, FREEZE rule 2).
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
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence

from fuel.order_models import FuelOrder, FuelOrderEvent
from fuel.services.order_es_mappings import (
    FUEL_ORDER_EVENTS_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from persistence.timestamps import parse_ts
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: Value comparison for stored vs in-memory timestamps (design K5a).
_ts = parse_ts

#: "Argument not given" for guards where ``None`` is a meaningful value.
_UNSET: Any = object()

#: Statuses an order's run links can no longer be released from (P3).
_UNRELEASABLE_STATUSES = frozenset({"dispatched", "in_transit", "delivered"})


def _link(value: Any) -> Optional[Any]:
    """Normalise a run/asset link: ``""`` and ``None`` both mean unlinked."""
    return value or None


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


class OrderChangedConcurrentlyError(Exception):
    """A guarded write found the stored order changed since it was read (K5a).

    ``actual_status`` is ``None`` when the document no longer exists.
    """

    def __init__(
        self,
        order_id: str,
        expected_status: Optional[str],
        actual_status: Optional[str],
    ) -> None:
        self.order_id = order_id
        self.expected_status = expected_status
        self.actual_status = actual_status
        super().__init__(
            f"order {order_id!r} changed concurrently "
            f"(expected status {expected_status!r}, found {actual_status!r})"
        )


class OrderWriteDiscardedError(Exception):
    """A guarded write was not newer than the stored order and was discarded (K5a)."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(
            f"write to order {order_id!r} discarded: stored "
            "last_event_timestamp is newer or equal"
        )


@dataclass(frozen=True)
class AssignmentClaim:
    """Outcome of :meth:`FuelOrderRepository.claim_assignment` (design K5).

    ``order`` is the stored document for ``linked`` and ``already_linked``,
    the current same-tenant document for a ``refused`` claim whose order
    exists, and ``None`` for ``order_not_found``.
    """

    outcome: Literal["linked", "already_linked", "refused"]
    reason: Optional[str]
    order: Optional[Dict[str, Any]]


# The timestamp-guarded upsert used to be a painless script here, byte-identical
# to the one in ``ops/services/ops_es_service.py``. Both now go through
# ``ElasticsearchService.upsert_if_newer``, which holds one copy and lets the
# Postgres document store answer the same call under a row lock instead.


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
    #: Page size for the driver "my work" read (Req 3.15 budgets 50 orders).
    DEFAULT_DRIVER_PAGE_SIZE: int = 50
    #: Statuses a driver's work list carries when the caller supplies none.
    DEFAULT_DRIVER_STATUSES: tuple = ("dispatched", "in_transit")

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

    async def get_current(
        self, tenant_id: str, order_id: str
    ) -> Optional[Dict[str, Any]]:
        """The authoritative stored order as a JSON-mode dict, or ``None``.

        Reads ``es_documents`` by id, never the hybrid projection, so it sees
        exactly what :meth:`claim_assignment` and the guarded upsert compare
        against (design K3). Missing, cross-tenant and invalid documents all
        return ``None``.
        """
        self._require_tenant(tenant_id)
        if not order_id or not order_id.strip():
            raise ValueError("order_id must be a non-empty string")
        source = await self._es.get_document(self._orders_index, order_id)
        if not source or source.get("tenant_id") != tenant_id:
            return None
        model = _safe_order_load(source)
        return model.model_dump(mode="json") if model is not None else None

    # ------------------------------------------------------------------
    # Run-link claim and release (design K5, FREEZE rule 2)
    # ------------------------------------------------------------------

    async def claim_assignment(
        self,
        tenant_id: str,
        order_id: str,
        *,
        run_id: str,
        asset_id: str,
        expected_status: str,
        claim_id: str,
        expected_last_event_timestamp: Any = _UNSET,
    ) -> AssignmentClaim:
        """Link an order to one run and truck under the row lock (link CAS).

        Refused when the order is missing or cross-tenant
        (``order_not_found``), its status is not ``expected_status``
        (``order_changed_since_plan``), or it is linked to another run or
        truck (``order_committed_elsewhere``). When
        ``expected_last_event_timestamp`` is given, a stored
        ``last_event_timestamp`` that differs by value is also refused as
        ``order_changed_since_plan``: the caller's read is stale, so a
        same-status edit (an ERP/CSV re-sync changing quantity or tank)
        cannot slip in between the caller's read and this claim.
        ``already_linked`` when both links already name the targets;
        ``assigned_claim_id`` is then left as is. ``linked`` writes both
        links and ``assigned_claim_id``.

        Never touches ``last_event_timestamp`` or ``assigned_driver_id``, and
        writes ``es_documents`` only: the guarded upsert that follows mirrors
        the links to the relational row.
        """
        self._require_tenant(tenant_id)
        for name, value in (
            ("order_id", order_id),
            ("run_id", run_id),
            ("asset_id", asset_id),
            ("claim_id", claim_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        verdict: Dict[str, Optional[str]] = {"outcome": None, "reason": None}

        def transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if current.get("tenant_id") != tenant_id:
                verdict["reason"] = "order_not_found"
                return None
            if current.get("status") != expected_status:
                verdict["reason"] = "order_changed_since_plan"
                return None
            run = _link(current.get("assigned_run_id"))
            asset = _link(current.get("assigned_asset_id"))
            if run not in (None, run_id) or asset not in (None, asset_id):
                verdict["reason"] = "order_committed_elsewhere"
                return None
            if expected_last_event_timestamp is not _UNSET:
                try:
                    same = _ts(current.get("last_event_timestamp")) == _ts(
                        expected_last_event_timestamp
                    )
                except (TypeError, ValueError):
                    same = False  # unparseable is never a match (K5a)
                if not same:
                    verdict["reason"] = "order_changed_since_plan"
                    return None
            if (run, asset) == (run_id, asset_id):
                verdict["outcome"] = "already_linked"
                return None
            return {
                **current,
                "assigned_run_id": run_id,
                "assigned_asset_id": asset_id,
                "assigned_claim_id": claim_id,
            }

        doc, applied = await self._es.atomic_update(
            self._orders_index, order_id, transform
        )
        if doc is None:
            return AssignmentClaim("refused", "order_not_found", None)
        if applied:
            return AssignmentClaim("linked", None, dict(doc))
        if verdict["outcome"] == "already_linked":
            return AssignmentClaim("already_linked", None, dict(doc))
        reason = verdict["reason"] or "order_not_found"
        visible = None if reason == "order_not_found" else dict(doc)
        return AssignmentClaim("refused", reason, visible)

    async def release_assignment(
        self,
        tenant_id: str,
        order_id: str,
        *,
        run_id: str,
        asset_id: str,
        claim_id: str,
    ) -> bool:
        """Clear the run links this caller's claim wrote; ``True`` if cleared.

        Release by ownership (FREEZE rule 2, plan P3): clears
        ``assigned_run_id``, ``assigned_asset_id`` and ``assigned_claim_id``
        only when the tenant matches, both links equal ``(run_id, asset_id)``,
        ``assigned_claim_id == claim_id`` and the status is not dispatched,
        in transit or delivered. It ignores ``last_event_timestamp`` and
        ``assigned_driver_id`` and never writes them, so a concurrent driver
        assignment or quantity edit cannot leave this claim's links behind.
        Writes no event and ``es_documents`` only, as the claim does.
        """
        self._require_tenant(tenant_id)

        def transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if current.get("tenant_id") != tenant_id:
                return None
            links = (
                _link(current.get("assigned_run_id")),
                _link(current.get("assigned_asset_id")),
            )
            if links != (run_id, asset_id):
                return None
            if current.get("assigned_claim_id") != claim_id:
                return None
            if current.get("status") in _UNRELEASABLE_STATUSES:
                return None
            return {
                **current,
                "assigned_run_id": None,
                "assigned_asset_id": None,
                "assigned_claim_id": None,
            }

        _doc, applied = await self._es.atomic_update(
            self._orders_index, order_id, transform
        )
        if not applied:
            logger.info(
                "FuelOrderRepository.release_assignment: no-op for order=%s "
                "run=%s (links, claim or status no longer owned by this claim)",
                order_id,
                run_id,
            )
        return bool(applied)

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
        *,
        expected_status: Optional[str] = None,
        expected_last_event_timestamp: Any = _UNSET,
    ) -> bool | Dict[str, Any]:
        """Scripted upsert that noops when incoming timestamp is stale.

        Compares incoming ``last_event_timestamp`` against the stored
        value. If the incoming event is older or equal, the operation is
        a noop and returns ``False``. Otherwise the document is updated
        and returns ``True``.

        **Guarded form** (design K5a): when ``expected_status`` or
        ``expected_last_event_timestamp`` is given, the write is one
        ``atomic_update`` with no upsert that applies only when the stored
        status and ``last_event_timestamp`` (compared by value) still equal
        the expected ones and the incoming timestamp is newer. It returns the
        stored document, or raises :class:`OrderChangedConcurrentlyError`
        (changed or missing) / :class:`OrderWriteDiscardedError` (not newer).

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

        if expected_status is not None or expected_last_event_timestamp is not _UNSET:
            return await self._guarded_upsert(
                order_id,
                doc,
                expected_status=expected_status,
                expected_last_event_timestamp=expected_last_event_timestamp,
            )

        try:
            # The stale-event comparison, the painless script that expressed it,
            # and the serverless-Elasticsearch fresh-insert fallback all moved to
            # ``ElasticsearchService.upsert_if_newer``. They were reaching past
            # the facade to ``client.update`` / ``client.exists`` /
            # ``client.index``, which meant they would keep writing to
            # Elasticsearch after the document plane was cut over to Postgres
            # while everything around them wrote to Postgres. Going through the
            # facade means one implementation per backend, and on Postgres the
            # comparison happens under a row lock rather than in a script.
            #
            # The return value keeps its meaning: False is a discarded stale
            # event, and callers branch on it.
            applied = await self._es.upsert_if_newer(
                self._orders_index, order_id, doc
            )
            if not applied:
                return False
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

    async def _guarded_upsert(
        self,
        order_id: str,
        doc: Dict[str, Any],
        *,
        expected_status: Optional[str],
        expected_last_event_timestamp: Any,
    ) -> Dict[str, Any]:
        """The K5a compare-and-set write behind the guarded upsert form."""
        verdict: Dict[str, Any] = {"changed": False, "stale": False, "status": None}
        incoming_ts = _ts(doc.get("last_event_timestamp"))

        def transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            verdict["status"] = current.get("status")
            if expected_status is not None and current.get("status") != expected_status:
                verdict["changed"] = True
                return None
            stored_ts = _ts(current.get("last_event_timestamp"))
            if (
                expected_last_event_timestamp is not _UNSET
                and stored_ts != _ts(expected_last_event_timestamp)
            ):
                verdict["changed"] = True
                return None
            if stored_ts is not None and (incoming_ts is None or incoming_ts <= stored_ts):
                verdict["stale"] = True
                return None
            return {**current, **doc}

        stored, applied = await self._es.atomic_update(
            self._orders_index, order_id, transform
        )
        if stored is None:
            raise OrderChangedConcurrentlyError(order_id, expected_status, None)
        if not applied:
            if verdict["stale"]:
                raise OrderWriteDiscardedError(order_id)
            raise OrderChangedConcurrentlyError(
                order_id, expected_status, verdict["status"]
            )
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("fuel_order", stored)
        return stored

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
        keyset: bool = False,
        after: Optional[tuple] = None,
        with_total: bool = True,
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
            keyset: Data-export keyset mode. Sorts on ``(sort field,
                order_id ASC)`` with offset 0 on every call, so ``last_key``
                always has two values.
            after: ``(sort_value, order_id)`` of the previous page's last raw
                row. Requires ``keyset=True``.
            with_total: ``False`` skips the count on the Postgres path.

        Returns:
            A dict with ``orders`` (list of FuelOrder), ``total`` (int),
            ``page`` (int), ``size`` (int), plus ``raw_count`` and
            ``last_key`` computed from the store result before any row is
            dropped.

        Cross-tenant results are silently dropped (empty list).
        """
        self._require_tenant(tenant_id)
        if after is not None and not keyset:
            raise ValueError("after requires keyset=True")
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
            page=1 if keyset else page, size=size,
            **({"after": after, "with_total": with_total} if keyset else {}),
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
                "raw_count": pg.get("raw_count", len(pg["items"])),
                "last_key": pg.get("last_key"),
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
        if keyset:
            # Same 2-key sort on every call (count probe and first page
            # included), so every hit carries two sort values.
            query["sort"] = [
                {pg_sort_field: {"order": pg_sort_order}},
                {"order_id": {"order": "asc"}},
            ]
            query["from"] = 0
            if after is not None:
                query["search_after"] = [after[0], after[1]]
        elif sort:
            parts = sort.split(":")
            sort_field = parts[0]
            sort_order = parts[1] if len(parts) > 1 else "desc"
            query["sort"] = [{sort_field: {"order": sort_order}}]
        else:
            query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._orders_index, query, size
        )
        from services.keyset_pagination import raw_keyset_info
        raw_count, last_key = raw_keyset_info(resp, keyset=keyset)
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
            "raw_count": raw_count,
            "last_key": last_key,
        }

    # ------------------------------------------------------------------
    # Search for a single driver (Req 3.1, 3.3, 3.4, 3.5)
    # ------------------------------------------------------------------

    async def search_for_driver(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        statuses: Sequence[str] = DEFAULT_DRIVER_STATUSES,
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        page: int = 1,
        size: int = DEFAULT_DRIVER_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """Return the orders assigned to one driver, window-ordered.

        One tenant-filtered search: ``term assigned_driver_id`` +
        ``terms status`` + an optional ``range`` on
        ``delivery_window_start``, sorted ``delivery_window_start``
        ascending with ``from`` / ``size`` paging. ``search()`` cannot serve
        this because it takes a single ``status`` string and ranges only on
        ``created_at``.

        Parameters:
            tenant_id: The caller's tenant.
            driver_id: The canonical driver identity to scope to. Never
                accepted from a client — derived from the session.
            statuses: Statuses to include. Falls back to
                ``("dispatched", "in_transit")`` when empty or blank.
            window_start: Include orders whose ``delivery_window_start`` is
                on or after this ISO-8601 timestamp.
            window_end: Include orders whose ``delivery_window_start`` is on
                or before this ISO-8601 timestamp.
            page: 1-based page number.
            size: Page size.

        Returns:
            A dict with ``orders`` (list of FuelOrder), ``total`` (int),
            ``page`` (int), ``size`` (int).

        Every returned source is re-validated on ``tenant_id`` **and**
        ``assigned_driver_id`` before inclusion, so a filter regression on
        either axis drops the document instead of leaking another tenant's
        or another driver's work (Req 15.11).
        """
        self._require_tenant(tenant_id)
        if not isinstance(driver_id, str) or not driver_id.strip():
            raise ValueError("driver_id must be a non-empty string")
        if page < 1:
            page = 1
        if size <= 0:
            size = self.DEFAULT_DRIVER_PAGE_SIZE

        status_values = [
            s for s in (statuses or ()) if isinstance(s, str) and s.strip()
        ]
        if not status_values:
            status_values = list(self.DEFAULT_DRIVER_STATUSES)

        # Read-cutover: serve from Postgres when enabled. ``in_filters`` is
        # the Postgres analogue of the ES ``terms`` filter, and the range is
        # a lexical comparison on the same ISO-8601 window field.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )
        pg = await read_hybrid_search(
            "fuel_order", tenant_id,
            term_filters={"assigned_driver_id": driver_id},
            in_filters={"status": status_values},
            range_field="delivery_window_start",
            range_gte=window_start, range_lte=window_end,
            sort_field="delivery_window_start", sort_order="asc",
            page=page, size=size,
        )
        if pg is not _NOT_CUT_OVER:
            return {
                "orders": self._validated_driver_orders(
                    pg["items"], tenant_id, driver_id
                ),
                "total": pg["total"],
                "page": pg["page"],
                "size": pg["size"],
            }

        filters: List[Dict[str, Any]] = [
            {"term": {"assigned_driver_id": driver_id}},
            {"terms": {"status": status_values}},
        ]
        if window_start or window_end:
            window_range: Dict[str, Any] = {}
            if window_start:
                window_range["gte"] = window_start
            if window_end:
                window_range["lte"] = window_end
            filters.append(
                {"range": {"delivery_window_start": window_range}}
            )

        query = inject_tenant_filter(
            {"query": {"bool": {"must": filters}}},
            tenant_id,
        )
        query["from"] = (page - 1) * size
        query["size"] = size
        query["sort"] = [{"delivery_window_start": {"order": "asc"}}]

        resp = await self._es.search_documents(
            self._orders_index, query, size
        )

        return {
            "orders": self._validated_driver_orders(
                _extract_sources(resp), tenant_id, driver_id
            ),
            "total": _extract_total(resp),
            "page": page,
            "size": size,
        }

    @staticmethod
    def _validated_driver_orders(
        sources: List[Dict[str, Any]],
        tenant_id: str,
        driver_id: str,
    ) -> List[FuelOrder]:
        """Re-validate tenant and driver ownership on every source.

        Defense-in-depth for :meth:`search_for_driver`: a document whose
        ``tenant_id`` or ``assigned_driver_id`` does not match what was asked
        for is dropped rather than returned (Req 15.11).
        """
        out: List[FuelOrder] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "FuelOrderRepository.search_for_driver: dropping doc "
                    "with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            if source.get("assigned_driver_id") != driver_id:
                logger.warning(
                    "FuelOrderRepository.search_for_driver: dropping "
                    "order %s assigned to a different driver",
                    source.get("order_id"),
                )
                continue
            model = _safe_order_load(source)
            if model is not None:
                out.append(model)
        return out

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
    "AssignmentClaim",
    "FuelOrderRepository",
    "OrderChangedConcurrentlyError",
    "OrderCrossTenantAccessError",
    "OrderWriteDiscardedError",
]
