"""
Driver Report Repository — tenant-scoped writes/reads for the
``driver_reports`` Elasticsearch index.

Implements :class:`DriverReportRepository` following the same
``inject_tenant_filter`` + post-fetch tenant re-validation pattern used by
:class:`fuel.order_repository.FuelOrderRepository` and
:class:`fuel.driver_repository.DriverRepository`.

Key discipline: **validate before write.** ``create`` verifies that the
supplied ``assignment_id`` references an order/assignment owned by the same
tenant *and* assigned to the same driver before anything is persisted. If that
validation fails, the repository raises and writes nothing (Req 21.4/21.5).

* ``create`` — validate assignment ownership, then persist a DriverReport.
* ``get`` — single report by ID, tenant-scoped.
* ``list_for_assignment`` — reports for an assignment, tenant-scoped.

Every read wraps its ES query through
:func:`ops.middleware.tenant_guard.inject_tenant_filter` and re-validates the
returned document's ``tenant_id`` before it crosses the repository boundary.
Cross-tenant reads degrade to ``None`` / empty lists so the REST layer can
return a uniform HTTP 404 without leaking existence. Cross-tenant writes raise
:class:`DriverReportCrossTenantAccessError`.

Validates: Requirements 21.1, 21.3, 21.4, 21.5.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fuel.driver_report_models import DriverReport
from fuel.services.order_es_mappings import DRIVER_REPORTS_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DriverReportCrossTenantAccessError(PermissionError):
    """Raised when a write targets a report owned by another tenant.

    Cross-tenant reads degrade silently to ``None`` / empty lists so the
    REST layer can return a uniform HTTP 404 without leaking existence.
    Cross-tenant writes are a security violation and MUST raise.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        report_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.report_id = report_id
        self.owning_tenant_id = owning_tenant_id
        super().__init__(
            f"Tenant {tenant_id!r} attempted cross-tenant access on "
            f"driver report {report_id!r} (owner={owning_tenant_id!r})"
        )


class DriverAssignmentNotFoundError(LookupError):
    """Raised when the report's ``assignment_id`` does not reference an
    order/assignment owned by the caller's tenant *and* assigned to the
    named driver.

    The REST layer translates this to a uniform HTTP 404 (Req 21.4). No
    report data is persisted when it is raised (Req 21.5).
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        assignment_id: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.driver_id = driver_id
        self.assignment_id = assignment_id
        super().__init__(
            f"Assignment {assignment_id!r} not found for tenant "
            f"{tenant_id!r} / driver {driver_id!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not resp:
        return []
    hits_outer = resp.get("hits") if hasattr(resp, "get") else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") if hasattr(hits_outer, "get") else []
    out: List[Dict[str, Any]] = []
    for hit in hits or []:
        if hasattr(hit, "get") and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _safe_report_load(source: Dict[str, Any]) -> Optional[DriverReport]:
    """Build a :class:`DriverReport` from a raw ES source, logging on failure."""
    try:
        return DriverReport(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "DriverReportRepository: dropping driver_reports doc that "
            "failed model validation (report_id=%s): %s",
            source.get("report_id"),
            exc,
        )
        return None


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class DriverReportRepository:
    """Tenant-scoped repository for driver reports.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The ES interface relied upon is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``

    which matches :class:`services.elasticsearch_service.ElasticsearchService`.

    ``order_repository`` is any object exposing
    ``await get(tenant_id, order_id) -> order | None`` where the returned order
    carries ``tenant_id`` and ``assigned_driver_id`` attributes (the
    :class:`fuel.order_repository.FuelOrderRepository` contract). It is used to
    validate assignment ownership before a write (Req 21.4/21.5).

    Tenant isolation is enforced at two points for defense-in-depth:
        1. Every ES read query is wrapped through
           :func:`ops.middleware.tenant_guard.inject_tenant_filter`.
        2. Every returned document is re-validated against the caller's
           ``tenant_id`` before it crosses the repository boundary.
    """

    DEFAULT_LIST_SIZE: int = 200

    def __init__(
        self,
        es_service: Any,
        *,
        order_repository: Any,
        reports_index: str = DRIVER_REPORTS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if order_repository is None:
            raise ValueError("order_repository must not be None")
        self._es = es_service
        self._order_repository = order_repository
        self._reports_index = reports_index

    # ------------------------------------------------------------------
    # Create (validate assignment ownership before any write)
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        report: DriverReport | Dict[str, Any],
    ) -> DriverReport:
        """Persist a driver report after validating assignment ownership.

        Validation order (nothing is persisted until all pass):
            1. ``tenant_id`` is a non-empty string.
            2. The report's ``tenant_id`` matches the caller (else
               :class:`DriverReportCrossTenantAccessError`).
            3. ``assignment_id`` references an order owned by the caller's
               tenant *and* assigned to the report's ``driver_id`` (else
               :class:`DriverAssignmentNotFoundError`).

        Only after every check succeeds is the document written (Req 21.5).

        Raises:
            DriverReportCrossTenantAccessError: report tenant mismatch.
            DriverAssignmentNotFoundError: assignment not owned by the
                tenant/driver.
        """
        self._require_tenant(tenant_id)

        payload = self._coerce_report_to_dict(report)
        payload.setdefault("tenant_id", tenant_id)

        if payload["tenant_id"] != tenant_id:
            raise DriverReportCrossTenantAccessError(
                tenant_id=tenant_id,
                report_id=str(payload.get("report_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        driver_id = payload.get("driver_id")
        assignment_id = payload.get("assignment_id")
        if not driver_id or not str(driver_id).strip():
            raise ValueError("driver_id must be a non-empty string")
        if not assignment_id or not str(assignment_id).strip():
            raise ValueError("assignment_id must be a non-empty string")

        # Assignment ownership check — the assignment MUST reference an order
        # owned by this tenant AND assigned to this driver. A cross-tenant
        # order degrades to ``None`` inside the order repository (its own
        # inject_tenant_filter + re-validation), so a missing/foreign order and
        # a wrong-driver order both fail closed here. Nothing is persisted.
        order = await self._order_repository.get(tenant_id, str(assignment_id))
        if order is None or not self._order_matches_driver(order, str(driver_id)):
            raise DriverAssignmentNotFoundError(
                tenant_id=tenant_id,
                driver_id=str(driver_id),
                assignment_id=str(assignment_id),
            )

        if not payload.get("created_at"):
            payload["created_at"] = _utcnow_iso()

        # Validate through the Pydantic model before touching ES.
        model = DriverReport(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._reports_index, model.report_id, doc
        )
        return model

    # ------------------------------------------------------------------
    # Get (single report)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, report_id: str
    ) -> Optional[DriverReport]:
        """Return the report or ``None`` if it does not exist / is not owned.

        Cross-tenant fetches degrade to ``None`` so the REST layer can
        return a uniform HTTP 404 without leaking existence.
        """
        self._require_tenant(tenant_id)
        if not report_id or not report_id.strip():
            raise ValueError("report_id must be a non-empty string")

        query = inject_tenant_filter(
            {"query": {"term": {"report_id": report_id}}},
            tenant_id,
        )
        query["size"] = 1

        try:
            resp = await self._es.search_documents(
                self._reports_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DriverReportRepository.get: search failed for report=%s: %s",
                report_id,
                exc,
            )
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        source = sources[0]
        # Defense-in-depth: re-validate tenant ownership.
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "DriverReportRepository.get: suppressing cross-tenant hit "
                "for report=%s (owner=%s, requester=%s)",
                report_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None

        return _safe_report_load(source)

    # ------------------------------------------------------------------
    # List for assignment
    # ------------------------------------------------------------------

    async def list_for_assignment(
        self,
        tenant_id: str,
        assignment_id: str,
        *,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[DriverReport]:
        """List a tenant's reports for an assignment, most recent first.

        Cross-tenant results are silently dropped (empty list).
        """
        self._require_tenant(tenant_id)
        if not assignment_id or not assignment_id.strip():
            raise ValueError("assignment_id must be a non-empty string")
        if size <= 0:
            raise ValueError("size must be a positive integer")

        query = inject_tenant_filter(
            {"query": {"term": {"assignment_id": assignment_id}}},
            tenant_id,
        )
        query["size"] = size
        query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._reports_index, query, size
        )
        sources = _extract_sources(resp)

        out: List[DriverReport] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "DriverReportRepository.list_for_assignment: dropping "
                    "doc with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_report_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _order_matches_driver(order: Any, driver_id: str) -> bool:
        """Return ``True`` if ``order`` is assigned to ``driver_id``.

        Accepts either a model with an ``assigned_driver_id`` attribute or a
        raw dict source, so the check works regardless of the order
        repository's return shape.
        """
        if hasattr(order, "assigned_driver_id"):
            assigned = getattr(order, "assigned_driver_id")
        elif isinstance(order, dict):
            assigned = order.get("assigned_driver_id")
        else:  # pragma: no cover - defensive
            assigned = None
        return assigned is not None and str(assigned) == driver_id

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        """Validate that tenant_id is a non-empty string."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_report_to_dict(
        report: DriverReport | Dict[str, Any],
    ) -> Dict[str, Any]:
        """Coerce a DriverReport or dict into a mutable dict."""
        if isinstance(report, DriverReport):
            return report.model_dump(mode="python")
        if isinstance(report, dict):
            return dict(report)
        raise TypeError(
            f"report must be a DriverReport or dict, got "
            f"{type(report).__name__}"
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "DriverReportRepository",
    "DriverReportCrossTenantAccessError",
    "DriverAssignmentNotFoundError",
]
