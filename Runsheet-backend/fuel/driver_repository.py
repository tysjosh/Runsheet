"""
Driver Repository — tenant-scoped CRUD for ``drivers_current`` Elasticsearch index.

Implements :class:`DriverRepository` with:

* ``get`` — single driver by ID, tenant-scoped.
* ``create`` — persist a new Driver.
* ``update`` — partial update of a Driver document, with the duty-status
  projection fields refused (see below).
* ``project_duty_status`` — the one sanctioned writer of
  ``drivers_current.status``, called only by ``DutyStatusService``.
* ``list_for_tenant`` — paginated listing with tenant isolation.
* ``search`` — search drivers with filters.
* ``increment_counters`` — atomically adjust ``active_order_count`` and
  ``completed_today`` via the painless script from design §3.
* ``reset_completed_today`` — bulk-reset ``completed_today`` for all
  drivers in a tenant (daily cron).

Every method wraps reads through
:func:`ops.middleware.tenant_guard.inject_tenant_filter` and validates
returned documents re-match the caller's tenant before crossing the
repository boundary. Cross-tenant reads degrade to ``None`` (for ``get``)
or empty lists (for ``search``/``list_for_tenant``).
Cross-tenant writes raise :class:`DriverCrossTenantAccessError`.

``drivers_current.status`` — one writer only
-------------------------------------------

``status`` is the current-value projection of the ``duty_status_events`` log,
and ``DutyStatusService`` is its only writer (driver-mobile-app R13.16). This
repository is the module that made a second write path possible: ``update``
accepted an arbitrary partial document, so ``PATCH /api/ops/drivers/{id}`` with
``{"status": "off_duty"}`` moved the projection without appending an event, and
the projection then disagreed with the log until something happened to repair it.

Two changes close that path:

* :meth:`DriverRepository.update` **refuses** the duty-status projection fields
  (:data:`DUTY_STATUS_PROJECTION_FIELDS`) and raises
  :class:`DutyStatusWriteNotPermittedError`. The refusal is structural — a
  caller cannot reach the field by naming it — rather than a convention a future
  edit could forget.
* :meth:`DriverRepository.project_duty_status` is the sanctioned write, used by
  ``DutyStatusService`` after it has appended the event. It is the only method
  that names ``status`` in a write.

Record creation is not a second write path in this sense: :meth:`create` sets
the initial value on a record that has no event log behind it yet, and
``DutyStatusService.current`` falls back to the projection precisely while no
event exists. Drift is only possible once an event exists, and from that point
on this module offers exactly one way to change the field.

Presence is a different axis entirely. ``DriverWSManager`` writes the
``driver_presence`` index, never ``drivers_current``, so a WebSocket connect or
disconnect leaves duty status at the last value set (R13.9).

Validates: Requirements 3.1, 3.2 (order-intake-pipeline) and 13.9, 13.16
(driver-mobile-app).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fuel.order_models import Driver
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


#: The ``drivers_current`` fields only ``DutyStatusService`` may write (R13.16).
#: ``status`` is the projection of the latest ``duty_status_events`` document;
#: the other two record *which* event it projects and are meaningless without
#: it, so all three move together or not at all.
DUTY_STATUS_PROJECTION_FIELDS: tuple[str, ...] = (
    "status",
    "duty_status_event_id",
    "duty_status_updated_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DriverCrossTenantAccessError(PermissionError):
    """Raised when a write targets a driver owned by another tenant.

    Cross-tenant reads degrade silently to ``None`` / empty lists so the
    REST layer can return a uniform HTTP 404 without leaking existence.
    Cross-tenant writes are a security violation and MUST raise.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.driver_id = driver_id
        self.owning_tenant_id = owning_tenant_id
        super().__init__(
            f"Tenant {tenant_id!r} attempted cross-tenant access on "
            f"driver {driver_id!r} (owner={owning_tenant_id!r})"
        )


class DutyStatusWriteNotPermittedError(PermissionError):
    """Raised when a caller other than ``DutyStatusService`` writes ``status``.

    A programming error rather than a request error: every duty-status change
    has to append an event first, so a caller reaching for the projection
    directly has skipped the log. The exception names the offending fields and
    the method to use instead, because the fix is always the same one.

    Validates: Requirement 13.16
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        fields: List[str],
    ) -> None:
        self.tenant_id = tenant_id
        self.driver_id = driver_id
        self.fields = list(fields)
        super().__init__(
            "drivers_current"
            f".{{{', '.join(self.fields)}}} is written only by "
            "DutyStatusService, which appends a duty_status_events document "
            "first (R13.16). Route this change through "
            "DutyStatusService.transition(), or call "
            "DriverRepository.project_duty_status() if you are that service. "
            f"(tenant={tenant_id!r}, driver={driver_id!r})"
        )


# ---------------------------------------------------------------------------
# Atomic counter transforms (design §3)
# ---------------------------------------------------------------------------
#
# These were painless scripts sent to ``client.update`` and
# ``client.update_by_query``. They are Python now, run by
# ``ElasticsearchService.atomic_update`` / ``update_by_query`` under a row lock on
# Postgres and under a ``_seq_no`` assertion on Elasticsearch. The atomicity is
# unchanged; what changed is that the rule is no longer written in a language that
# only executes inside Elasticsearch, which is what let these two call sites
# bypass the document-store backend switch.


def _apply_counter_deltas(
    current: Dict[str, Any],
    *,
    delta_active: int,
    delta_completed: int,
    now: str,
) -> Dict[str, Any]:
    """Adjust the two driver counters, clamping ``active_order_count`` at zero.

    A faithful translation of the painless original, including the two details
    that are easy to lose:

    * a missing counter reads as ``0`` rather than raising — drivers created
      before the counters existed have neither field;
    * the clamp applies only to ``active_order_count``. ``completed_today`` is
      never decremented in practice, and clamping it would hide a caller that
      started doing so.

    The timestamps are stamped unconditionally, so this never returns ``None``
    and the update is never a no-op — matching the script, which also always
    touched them.
    """
    updated = dict(current)
    if delta_active:
        active = updated.get("active_order_count") or 0
        updated["active_order_count"] = max(0, active + delta_active)
    if delta_completed:
        completed = updated.get("completed_today") or 0
        updated["completed_today"] = completed + delta_completed
    updated["last_event_timestamp"] = now
    updated["updated_at"] = now
    return updated


def _reset_completed_today(current: Dict[str, Any], *, now: str) -> Dict[str, Any]:
    """Zero ``completed_today``. Paired with a ``completed_today > 0`` filter."""
    updated = dict(current)
    updated["completed_today"] = 0
    updated["updated_at"] = now
    return updated


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
    hits = hits_outer.get("hits") if hasattr(hits_outer, 'get') else []
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


def _safe_driver_load(source: Dict[str, Any]) -> Optional[Driver]:
    """Build a :class:`Driver` from a raw ES source, logging on failure."""
    try:
        return Driver(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "DriverRepository: dropping drivers_current doc that "
            "failed model validation (driver_id=%s): %s",
            source.get("driver_id"),
            exc,
        )
        return None


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class DriverRepository:
    """Tenant-scoped CRUD repository for drivers.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``
        * ``await es.atomic_update(index, doc_id, transform)``
        * ``await es.update_by_query(index, query, transform)``

    which matches :class:`services.elasticsearch_service.ElasticsearchService`.
    Every entry is a facade method — nothing here reaches ``es.client`` — so all
    of it follows ``DOCUMENT_STORE_BACKEND``. The last two used to be painless
    scripts sent straight to the raw client, which would have kept the counter
    writes on Elasticsearch while the reads moved to Postgres.

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
        drivers_index: str = DRIVERS_CURRENT_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        self._es = es_service
        self._drivers_index = drivers_index

    # ------------------------------------------------------------------
    # Get (single driver)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, driver_id: str
    ) -> Optional[Driver]:
        """Return the driver or ``None`` if it does not exist / is not owned.

        Cross-tenant fetches degrade to ``None`` so the REST layer can
        return a uniform HTTP 404 without leaking existence.
        """
        self._require_tenant(tenant_id)
        if not driver_id or not driver_id.strip():
            raise ValueError("driver_id must be a non-empty string")

        query = inject_tenant_filter(
            {"query": {"term": {"driver_id": driver_id}}},
            tenant_id,
        )
        query["size"] = 1

        try:
            resp = await self._es.search_documents(
                self._drivers_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DriverRepository.get: search failed for driver=%s: %s",
                driver_id,
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
                "DriverRepository.get: suppressing cross-tenant hit "
                "for driver=%s (owner=%s, requester=%s)",
                driver_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None

        return _safe_driver_load(source)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        driver: Driver | Dict[str, Any],
    ) -> Driver:
        """Persist a new Driver and return the stored model.

        ``status`` is accepted here because provisioning a record establishes its
        initial value, and there is no event log to drift from yet — every later
        change goes through ``DutyStatusService`` (R13.16, see the module
        docstring).

        Raises :class:`DriverCrossTenantAccessError` if the driver's
        ``tenant_id`` does not match the caller's ``tenant_id``.
        """
        self._require_tenant(tenant_id)

        payload = self._coerce_driver_to_dict(driver)
        payload.setdefault("tenant_id", tenant_id)

        if payload["tenant_id"] != tenant_id:
            raise DriverCrossTenantAccessError(
                tenant_id=tenant_id,
                driver_id=str(payload.get("driver_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now

        # Validate through the Pydantic model before touching ES
        model = Driver(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._drivers_index, model.driver_id, doc
        )
        return model

    # ------------------------------------------------------------------
    # Update (partial)
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        driver_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Driver]:
        """Partially update a driver document, minus the duty-status projection.

        Fetches the existing driver first to validate tenant ownership,
        then applies the partial update. Returns the updated Driver model
        or ``None`` if the driver does not exist for this tenant.

        Raises:
            DriverCrossTenantAccessError: The driver belongs to another tenant.
            DutyStatusWriteNotPermittedError: ``updates`` names ``status`` or one
                of its bookkeeping fields. Those belong to
                ``DutyStatusService``, which appends the event first and then
                calls :meth:`project_duty_status` (R13.16).

        Validates: Requirement 13.16
        """
        blocked = [
            field
            for field in DUTY_STATUS_PROJECTION_FIELDS
            if field in (updates or {})
        ]
        if blocked:
            raise DutyStatusWriteNotPermittedError(
                tenant_id=tenant_id,
                driver_id=driver_id,
                fields=blocked,
            )
        return await self._apply_updates(tenant_id, driver_id, updates)

    # ------------------------------------------------------------------
    # Duty-status projection (the ONLY write path for ``status``) — R13.16
    # ------------------------------------------------------------------

    async def project_duty_status(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        status: str,
        event_id: Optional[str] = None,
        updated_at: Optional[Any] = None,
    ) -> Optional[Driver]:
        """Write the duty-status projection onto ``drivers_current``.

        Called by ``DutyStatusService`` **after** the ``duty_status_events``
        append has succeeded, and by nothing else. The three fields move together
        so the projection always records which event produced it.

        Args:
            tenant_id: The caller's tenant.
            driver_id: The driver whose projection to write.
            status: The new duty status, taken from the appended event's
                ``new_status``.
            event_id: The id of that event.
            updated_at: That event's ``server_received_at``.

        Returns:
            The updated :class:`Driver`, or ``None`` when this tenant holds no
            record for ``driver_id`` — which the service reports as a lagging
            projection (202 ``DUTY_STATUS_PROJECTION_PENDING``, R13.18) rather
            than as a failed transition.

        Raises:
            DriverCrossTenantAccessError: The driver belongs to another tenant.

        Validates: Requirements 13.3, 13.15, 13.16
        """
        return await self._apply_updates(
            tenant_id,
            driver_id,
            {
                "status": status,
                "duty_status_event_id": event_id,
                "duty_status_updated_at": updated_at,
            },
        )

    async def _apply_updates(
        self,
        tenant_id: str,
        driver_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Driver]:
        """Apply a partial update after ownership validation.

        The shared body of :meth:`update` and :meth:`project_duty_status`. It
        deliberately performs **no** duty-status field check: the check belongs
        to the public entry points, so the sanctioned writer has one way in and
        every other caller has none.
        """
        self._require_tenant(tenant_id)
        if not driver_id or not driver_id.strip():
            raise ValueError("driver_id must be a non-empty string")

        # Fetch existing to validate ownership
        existing = await self.get(tenant_id, driver_id)
        if existing is None:
            return None

        # Build the updated payload
        existing_dict = existing.model_dump(mode="python")
        existing_dict.update(updates)
        existing_dict["updated_at"] = _utcnow_iso()

        # Prevent tenant_id mutation
        if existing_dict.get("tenant_id") != tenant_id:
            raise DriverCrossTenantAccessError(
                tenant_id=tenant_id,
                driver_id=driver_id,
                owning_tenant_id=existing_dict.get("tenant_id"),
            )

        # Validate through the Pydantic model before touching ES
        model = Driver(**existing_dict)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._drivers_index, model.driver_id, doc
        )
        return model

    # ------------------------------------------------------------------
    # List for tenant
    # ------------------------------------------------------------------

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[Driver]:
        """List all drivers for the tenant (up to ``size``).

        Results are tenant-scoped and re-validated before returning.
        """
        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant_id,
        )
        query["size"] = size
        query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._drivers_index, query, size
        )
        sources = _extract_sources(resp)

        out: List[Driver] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "DriverRepository.list_for_tenant: dropping doc "
                    "with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_driver_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        availability: Optional[str] = None,
        assigned_truck_id: Optional[str] = None,
        page: int = 1,
        size: int = DEFAULT_PAGE_SIZE,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search drivers with filters.

        Parameters:
            tenant_id: The caller's tenant.
            status: Filter by driver status.
            availability: Filter by availability.
            assigned_truck_id: Filter by assigned truck.
            page: 1-based page number.
            size: Page size.
            sort: Sort field and direction (e.g. "created_at:desc").

        Returns:
            A dict with ``drivers`` (list of Driver), ``total`` (int),
            ``page`` (int), ``size`` (int).

        Cross-tenant results are silently dropped (empty list).
        """
        self._require_tenant(tenant_id)
        if page < 1:
            page = 1
        if size <= 0:
            size = self.DEFAULT_PAGE_SIZE

        # Build filter clauses
        filters: List[Dict[str, Any]] = []
        if status:
            filters.append({"term": {"status": status}})
        if availability:
            filters.append({"term": {"availability": availability}})
        if assigned_truck_id:
            filters.append({"term": {"assigned_truck_id": assigned_truck_id}})

        # Build the inner query
        if filters:
            inner_query: Dict[str, Any] = {
                "query": {"bool": {"must": filters}}
            }
        else:
            inner_query = {"query": {"match_all": {}}}

        # Wrap with tenant filter
        query = inject_tenant_filter(inner_query, tenant_id)

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
            self._drivers_index, query, size
        )
        sources = _extract_sources(resp)
        total = _extract_total(resp)

        drivers: List[Driver] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "DriverRepository.search: dropping doc with "
                    "mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_driver_load(source)
            if model is not None:
                drivers.append(model)

        return {
            "drivers": drivers,
            "total": total,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Increment counters (atomic read-modify-write — design §3)
    # ------------------------------------------------------------------

    async def increment_counters(
        self,
        tenant_id: str,
        driver_id: str,
        delta_active: int = 0,
        delta_completed: int = 0,
    ) -> bool:
        """Atomically adjust ``active_order_count`` and ``completed_today``.

        Race-free by read-modify-write through
        :meth:`~services.elasticsearch_service.ElasticsearchService.atomic_update`
        (design §3 specified a painless script; the arithmetic is identical, see
        :func:`_apply_counter_deltas`). ``active_order_count`` is clamped at 0 and
        never goes negative.

        Args:
            tenant_id: The caller's tenant (used for ownership validation).
            driver_id: The driver whose counters to adjust.
            delta_active: Amount to add to ``active_order_count`` (can be
                negative for decrements).
            delta_completed: Amount to add to ``completed_today`` (typically
                +1 on delivery).

        Returns:
            ``True`` if the update was applied, ``False`` if the driver
            was not found or the operation was a noop.

        Raises:
            :class:`DriverCrossTenantAccessError` if the driver belongs
            to another tenant.
        """
        self._require_tenant(tenant_id)
        if not driver_id or not driver_id.strip():
            raise ValueError("driver_id must be a non-empty string")

        # Validate tenant ownership before touching the counters. This is a
        # separate read, so it is advisory rather than atomic with the update —
        # unchanged from the painless version, which had the same gap.
        existing = await self.get(tenant_id, driver_id)
        if existing is None:
            return False

        if existing.tenant_id != tenant_id:
            raise DriverCrossTenantAccessError(
                tenant_id=tenant_id,
                driver_id=driver_id,
                owning_tenant_id=existing.tenant_id,
            )

        now = _utcnow_iso()

        try:
            _doc, applied = await self._es.atomic_update(
                self._drivers_index,
                driver_id,
                lambda current: _apply_counter_deltas(
                    current,
                    delta_active=delta_active,
                    delta_completed=delta_completed,
                    now=now,
                ),
            )
            return applied
        except Exception as exc:
            logger.error(
                "DriverRepository.increment_counters: failed for "
                "driver=%s: %s",
                driver_id,
                exc,
            )
            raise

    # ------------------------------------------------------------------
    # Reset completed_today (daily cron)
    # ------------------------------------------------------------------

    async def reset_completed_today(self, tenant_id: str) -> int:
        """Reset ``completed_today`` to 0 for all drivers in a tenant.

        Used by the daily cron job that fires at 00:00 in each tenant's
        configured timezone. Returns the number of documents updated.

        Args:
            tenant_id: The tenant whose drivers to reset.

        Returns:
            The number of driver documents that were updated.
        """
        self._require_tenant(tenant_id)

        now = _utcnow_iso()

        try:
            updated = await self._es.update_by_query(
                self._drivers_index,
                {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"range": {"completed_today": {"gt": 0}}},
                        ]
                    }
                },
                lambda current: _reset_completed_today(current, now=now),
            )
            logger.info(
                "DriverRepository.reset_completed_today: reset %d drivers "
                "for tenant=%s",
                updated,
                tenant_id,
            )
            return updated
        except Exception as exc:
            logger.error(
                "DriverRepository.reset_completed_today: failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        """Validate that tenant_id is a non-empty string."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_driver_to_dict(
        driver: Driver | Dict[str, Any],
    ) -> Dict[str, Any]:
        """Coerce a Driver or dict into a mutable dict."""
        if isinstance(driver, Driver):
            return driver.model_dump(mode="python")
        if isinstance(driver, dict):
            return dict(driver)
        raise TypeError(
            f"driver must be a Driver or dict, got {type(driver).__name__}"
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "DUTY_STATUS_PROJECTION_FIELDS",
    "DriverRepository",
    "DriverCrossTenantAccessError",
    "DutyStatusWriteNotPermittedError",
]
