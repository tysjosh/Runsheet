"""
Driver Repository — tenant-scoped CRUD for ``drivers_current`` Elasticsearch index.

Implements :class:`DriverRepository` with:

* ``get`` — single driver by ID, tenant-scoped.
* ``create`` — persist a new Driver.
* ``update`` — partial update of a Driver document.
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

Validates: Requirements 3.1, 3.2.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fuel.order_models import Driver
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Painless script for atomic counter increment (design §3)
# ---------------------------------------------------------------------------

_DRIVER_COUNTER_SCRIPT = """
if (params.delta_active != 0) {
    ctx._source.active_order_count =
        (ctx._source.active_order_count != null ? ctx._source.active_order_count : 0)
        + params.delta_active;
    if (ctx._source.active_order_count < 0) ctx._source.active_order_count = 0;
}
if (params.delta_completed != 0) {
    ctx._source.completed_today =
        (ctx._source.completed_today != null ? ctx._source.completed_today : 0)
        + params.delta_completed;
}
ctx._source.last_event_timestamp = params.now;
ctx._source.updated_at = params.now;
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not resp:
        return []
    hits_outer = resp.get("hits") if isinstance(resp, dict) else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if isinstance(hit, dict) and isinstance(hit.get("_source"), dict):
            out.append(hit["_source"])
    return out


def _extract_total(resp: Any) -> int:
    """Extract the total hit count from an ES search response."""
    if not resp:
        return 0
    hits_outer = resp.get("hits") if isinstance(resp, dict) else None
    if not hits_outer:
        return 0
    total = hits_outer.get("total")
    if isinstance(total, dict):
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
        * ``es.client.update(index, id, body, refresh)``
        * ``es.client.update_by_query(index, body, refresh)``

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
        """Partially update a driver document.

        Fetches the existing driver first to validate tenant ownership,
        then applies the partial update. Returns the updated Driver model
        or ``None`` if the driver does not exist for this tenant.

        Raises :class:`DriverCrossTenantAccessError` if the driver
        belongs to another tenant.
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
    # Increment counters (atomic, painless script — design §3)
    # ------------------------------------------------------------------

    async def increment_counters(
        self,
        tenant_id: str,
        driver_id: str,
        delta_active: int = 0,
        delta_completed: int = 0,
    ) -> bool:
        """Atomically adjust ``active_order_count`` and ``completed_today``.

        Uses the painless script from design §3 to ensure race-free
        counter updates. The script clamps ``active_order_count`` at 0
        (never goes negative).

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

        # Validate tenant ownership before running the script
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
            response = self._es.client.update(
                index=self._drivers_index,
                id=driver_id,
                body={
                    "script": {
                        "source": _DRIVER_COUNTER_SCRIPT,
                        "lang": "painless",
                        "params": {
                            "delta_active": delta_active,
                            "delta_completed": delta_completed,
                            "now": now,
                        },
                    },
                },
                refresh=True,
            )
            result = response.get("result", "")
            if result == "noop":
                return False
            return True
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
            response = self._es.client.update_by_query(
                index=self._drivers_index,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"tenant_id": tenant_id}},
                                {"range": {"completed_today": {"gt": 0}}},
                            ]
                        }
                    },
                    "script": {
                        "source": (
                            "ctx._source.completed_today = 0; "
                            "ctx._source.updated_at = params.now;"
                        ),
                        "lang": "painless",
                        "params": {"now": now},
                    },
                },
                refresh=True,
            )
            updated = response.get("updated", 0)
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
    "DriverRepository",
    "DriverCrossTenantAccessError",
]
