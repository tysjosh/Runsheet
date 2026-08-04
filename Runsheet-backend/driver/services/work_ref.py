"""
The ``Work_Ref`` seam — one resolved unit of work behind two routers.

Every driver field operation (POD submission, exception report, thread
message) acts on a single unit of work that the caller may name either by
``job_id`` or by ``order_id``. This module resolves a path parameter plus a
verified :class:`~ops.middleware.tenant_guard.TenantContext` into the triple
``(tenant_id, driver_id, work_ref)`` carried by :class:`WorkRef`, so the
router handler above it does nothing but resolve and delegate, and the
service below it holds the whole rule exactly once.

Because both the job-keyed and the order-keyed handler resolve through this
module and then call the same service, the two paths cannot diverge on a
validation rule or on an error code (R7.18, R7.19) — there is only one code
path that can produce either.

Collaborators (``job_service``, ``order_repository``) arrive through the
constructor from the existing ``configure_*`` functions. There is no
dependency-injection container, no service locator, and no FastAPI
``Depends`` for collaborators here (R5.24, R7.20).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §The ``Work_Ref`` Seam.

Validates: Requirements 1.5, 1.6, 1.12, 7.18, 7.21, 15.11
- 1.5, 1.6: every resolution begins with ``require_driver_identity``
- 1.12: dual acceptance on the job-keyed path — ``asset_assigned`` against
  ``TenantContext.user_id`` OR ``assigned_driver_id`` against
  ``TenantContext.driver_id``
- 7.18: the router resolves the triple; every rule lives below the resolution
- 7.21: the order-keyed path confirms tenant membership and
  ``assigned_driver_id`` equality, rejecting a mismatch with 403 ``FORBIDDEN``
- 15.11: the ``tenant_id`` on every fetched document is re-validated before
  that document is used

Rejection details name the work reference only: never the caller's held roles
and never the identity of the driver a resource is assigned to (R15.14).
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from auth.authorization import require_driver_identity
from errors.exceptions import AppException, forbidden, resource_not_found
from ops.middleware.tenant_guard import TenantContext

logger = logging.getLogger(__name__)

#: Which identifier namespace the caller used to name the unit of work.
WorkKind = Literal["job", "order"]


# ---------------------------------------------------------------------------
# The triple
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkRef:
    """The resolved unit of work a driver field operation acts on.

    ``kind`` records which identifier namespace the caller used, so a service
    can pick the right authorization rule and the right document to read
    without the router having done either.

    Attributes:
        tenant_id: The verified tenant scope for the request.
        driver_id: The caller's canonical ``drivers_current.driver_id``, from
            the verified session claim rather than from the request.
        kind: ``"job"`` or ``"order"``.
        work_id: The ``job_id`` or the ``order_id``, per ``kind``.
        job_doc: The fetched job document when ``kind == "job"``, and ``None``
            when the job could not be read (the job-keyed path stays
            non-blocking on a lookup failure, as it is today).
        order_doc: The fetched fuel-order document when ``kind == "order"``.
    """

    tenant_id: str
    driver_id: str
    kind: WorkKind
    work_id: str
    job_doc: Optional[dict] = None
    order_doc: Optional[dict] = None

    @property
    def job_id(self) -> Optional[str]:
        """The job identifier, or ``None`` on the order-keyed path."""
        return self.work_id if self.kind == "job" else None

    @property
    def order_id(self) -> Optional[str]:
        """The fuel-order identifier.

        On the order-keyed path this is ``work_id``. On the job-keyed path it
        is the ``order_id`` carried by the job document, when the job carries
        one, so a POD resolved from a job still names its order.
        """
        if self.kind == "order":
            return self.work_id
        return (self.job_doc or {}).get("order_id") or None


# ---------------------------------------------------------------------------
# Job-keyed authorization — the dual-acceptance disjunction (R1.12)
# ---------------------------------------------------------------------------


def authorizes_job(job_doc: dict, tenant: TenantContext) -> bool:
    """Decide whether ``tenant`` may act on ``job_doc``.

    Exactly a three-way disjunction:

    1. the job names no driver in **either** identifier namespace — allow.
       This preserves the existing permissive behaviour of the job-keyed
       endpoints, which gate only when an assignment value is present
       (``pod_endpoints.py:720``); tightening it would change the job-keyed
       contract, which R7.15 forbids.
    2. ``asset_assigned`` equals ``TenantContext.user_id`` — allow. The
       pre-migration namespace.
    3. ``assigned_driver_id`` equals a non-null ``TenantContext.driver_id`` —
       allow. The canonical namespace R1.13 began writing.

    Anything else is a denial: a job assigned in at least one namespace to
    somebody who is not the caller.

    Args:
        job_doc: The raw ``jobs_current`` document.
        tenant: The verified Auth_Context for the request.

    Returns:
        ``True`` when the caller is authorized for this job.

    Validates: Requirements 1.12
    """
    assigned_user = (job_doc or {}).get("asset_assigned") or ""
    assigned_driver = (job_doc or {}).get("assigned_driver_id") or ""

    if not assigned_user and not assigned_driver:
        return True
    if assigned_user and assigned_user == tenant.user_id:
        return True
    if assigned_driver and tenant.driver_id and assigned_driver == tenant.driver_id:
        return True
    return False


# ---------------------------------------------------------------------------
# Resolution — the only thing a router handler does
# ---------------------------------------------------------------------------


class WorkRefResolver:
    """Resolves a path parameter + verified ``TenantContext`` into a WorkRef.

    Collaborators arrive through the constructor from the existing
    module-global wiring: each ``configure_*`` function builds one resolver
    from the same ``job_service`` / ``order_repository`` references it already
    receives (R5.24, R7.20).

    Args:
        job_service: Anything exposing ``_get_job_doc(job_id, tenant_id)``.
            Optional: when absent, the job-keyed path resolves without a job
            document, matching today's behaviour when the scheduling job
            service is not wired.
        order_repository: Anything exposing ``get(tenant_id, order_id)``
            returning a ``FuelOrder`` (or ``None``). Required for
            :meth:`resolve_order`.
    """

    def __init__(self, *, job_service=None, order_repository=None) -> None:
        self._job_service = job_service
        self._order_repository = order_repository

    # -- job-keyed ------------------------------------------------------

    async def resolve_job(self, job_id: str, tenant: TenantContext) -> "WorkRef":
        """Resolve the job-keyed path parameter into a :class:`WorkRef`.

        Args:
            job_id: The ``job_id`` path parameter.
            tenant: The verified Auth_Context for the request.

        Returns:
            The resolved work reference, carrying the job document when it
            could be read.

        Raises:
            AppException: ``insufficient_role`` / ``driver_identity_missing``
                (403) from :func:`require_driver_identity` (R1.5, R1.6);
                ``resource_not_found`` (404) when the job service reports the
                job absent for this tenant; ``forbidden`` (403) when the job
                is assigned to somebody who is not the caller in both
                namespaces (R1.12).

        Validates: Requirements 1.5, 1.6, 1.12, 7.18, 15.11
        """
        driver_id = require_driver_identity(tenant)
        job_doc = await self._fetch_job(job_id, tenant.tenant_id)
        if job_doc is not None and not authorizes_job(job_doc, tenant):
            # Details name the work reference only — never the caller's roles
            # and never the assigned driver's identity (R15.14).
            raise forbidden(
                message="Assignment revoked",
                details={"job_id": job_id},
            )
        return WorkRef(
            tenant_id=tenant.tenant_id,
            driver_id=driver_id,
            kind="job",
            work_id=job_id,
            job_doc=job_doc,
        )

    async def _fetch_job(self, job_id: str, tenant_id: str) -> Optional[dict]:
        """Read the job document, or ``None`` when it cannot be read.

        Preserves the established job-keyed behaviour exactly: a structured
        ``AppException`` (notably the 404 raised by ``_get_job_doc`` for a job
        outside the tenant) propagates, while any other failure is logged and
        treated as "no document", leaving the operation non-blocking.

        The tenant scope is applied by ``_get_job_doc`` itself, and the
        returned document's ``tenant_id`` is re-validated here before it is
        used for anything (R15.11).
        """
        if self._job_service is None:
            return None
        try:
            job_doc = await self._job_service._get_job_doc(job_id, tenant_id)
        except AppException:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to fetch job %s for driver access checks tenant=%s: %s",
                job_id,
                tenant_id,
                exc,
            )
            return None

        if not isinstance(job_doc, dict):
            return None
        if job_doc.get("tenant_id") not in (None, tenant_id):
            # Per-document tenant re-validation (R15.11): a document that
            # names another tenant is treated as absent.
            raise resource_not_found(
                message="Job not found",
                details={"job_id": job_id},
            )
        return job_doc

    # -- order-keyed ----------------------------------------------------

    async def resolve_order(self, order_id: str, tenant: TenantContext) -> "WorkRef":
        """Resolve the order-keyed path parameter into a :class:`WorkRef`.

        There is no dual acceptance here: the order-keyed surface is new, so
        it authorizes on the canonical ``assigned_driver_id`` alone (R7.21).

        Args:
            order_id: The ``order_id`` path parameter.
            tenant: The verified Auth_Context for the request.

        Returns:
            The resolved work reference, carrying the order document.

        Raises:
            AppException: ``insufficient_role`` / ``driver_identity_missing``
                (403) from :func:`require_driver_identity`;
                ``resource_not_found`` (404) when no order with that
                identifier exists in the caller's tenant; ``forbidden`` (403)
                when the order's ``assigned_driver_id`` is not the caller.

        Validates: Requirements 1.5, 1.6, 7.18, 7.21, 15.11
        """
        driver_id = require_driver_identity(tenant)
        if self._order_repository is None:
            raise RuntimeError(
                "WorkRefResolver has no order_repository. Pass one from the "
                "configure_* function that constructs the resolver."
            )

        order = await self._order_repository.get(tenant.tenant_id, order_id)
        if order is None:
            raise resource_not_found(
                message="Order not found",
                details={"order_id": order_id},
            )

        doc = _as_document(order)
        if doc.get("tenant_id") != tenant.tenant_id:
            # Per-document tenant re-validation (R15.11). A cross-tenant hit
            # is indistinguishable from an absent order to the caller.
            raise resource_not_found(
                message="Order not found",
                details={"order_id": order_id},
            )
        if (doc.get("assigned_driver_id") or "") != driver_id:
            raise forbidden(
                message="Assignment revoked",
                details={"order_id": order_id},
            )

        return WorkRef(
            tenant_id=tenant.tenant_id,
            driver_id=driver_id,
            kind="order",
            work_id=order_id,
            order_doc=doc,
        )


def _as_document(order: Any) -> dict:
    """Normalize a repository result into a plain dict.

    ``FuelOrderRepository.get`` returns a ``FuelOrder``; fakes and cutover
    read paths may hand back a raw document. Both are accepted so the
    resolver depends on the shape of the data, not on the class.
    """
    if isinstance(order, dict):
        return order
    model_dump = getattr(order, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return dict(order)
