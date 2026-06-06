"""
Cross-module linkage backfill (cross-module-entity-linkage task 5.1).

This backfill is **defined and runnable on demand** — it is *not* wired into
bootstrap or any Alembic/ES migration and never runs automatically. Operators
invoke it via ``scripts/linkage_backfill.py`` once the additive schema changes
(tasks 2 / 3) are deployed.

What it does, for *in-flight* records (an order already tied to a run/job via
``assigned_run_id``):

* derives the order's ``assigned_asset_id`` from the linked job's
  ``asset_assigned`` (Req 6.4), and
* derives the job's ``order_id`` / ``customer_id`` from the linked order
  (Req 6.4).

The linkage is written through the consistency-preserving
:class:`scheduling.services.order_job_assignment_service.OrderJobAssignmentService`
so the backfill produces *exactly* the same guaranteed-consistent linkage the
live assignment path does — order and job can never diverge (Req 3.4), and
write-time reference validation still applies.

**Unlinked records stay unlinked, never fail (Req 6.2).** A record whose link
cannot be derived — no ``assigned_run_id``, the run no longer resolves to a
job, the job carries no asset, or the assignment service rejects the reference
(e.g. the asset was retired / is type-incompatible / crosses tenants) — is
simply skipped and counted as "unlinked". The backfill does not raise on these;
historical/complete records are expected to remain unlinked.

**Tenant-scoped (Req 5.3).** Every read and write goes through the
tenant-scoped repositories/services, so a run for ``tenant_id`` can never read
or mutate another tenant's orders or jobs.

**Idempotent.** Re-running is safe: an order/job pair that is already linked
consistently is detected and skipped, so no redundant writes (and no
timestamp churn) occur.

Validates: Requirements 6.2, 6.4
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from errors.exceptions import AppException
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "OrderBackfillPlan",
    "plan_order_backfill",
    "LinkageBackfill",
    "build_linkage_backfill",
    "REASON_NO_RUN_LINK",
    "REASON_JOB_NOT_FOUND",
    "REASON_JOB_MISSING_ASSET",
    "REASON_ALREADY_LINKED",
    "REASON_LINKABLE",
]

# --- Plan classifications -------------------------------------------------

#: Order is not tied to any run/job — nothing to derive (skip).
REASON_NO_RUN_LINK = "no_run_link"
#: The order's ``assigned_run_id`` does not resolve to a job in this tenant
#: (historical/dangling reference) — remains unlinked.
REASON_JOB_NOT_FOUND = "job_not_found"
#: The linked job carries no ``asset_assigned`` — the order's asset cannot be
#: derived — remains unlinked.
REASON_JOB_MISSING_ASSET = "job_missing_asset"
#: Both sides are already linked consistently — skip (idempotent).
REASON_ALREADY_LINKED = "already_linked"
#: The link can be derived and will be written.
REASON_LINKABLE = "linkable"

#: Actions a plan can carry.
ACTION_LINK = "link"
ACTION_SKIP = "skip"


@dataclass(frozen=True)
class OrderBackfillPlan:
    """The derivation decision for a single order (pure, side-effect free).

    ``action`` is :data:`ACTION_LINK` (the link is derivable and should be
    written) or :data:`ACTION_SKIP` (nothing to do / not derivable). ``reason``
    carries the classification for reporting. ``asset_id`` / ``customer_id``
    are the values that *would* be written on a ``link`` plan (informational —
    the actual consistent write is performed by the assignment service).
    """

    order_id: str
    action: str
    reason: str
    job_id: Optional[str] = None
    asset_id: Optional[str] = None
    customer_id: Optional[str] = None

    @property
    def is_link(self) -> bool:
        return self.action == ACTION_LINK


def plan_order_backfill(
    order: Any, job_doc: Optional[Dict[str, Any]]
) -> OrderBackfillPlan:
    """Decide what (if anything) to backfill for ``order`` given its linked job.

    Pure function — performs no I/O — so the derivation logic is unit-testable
    in isolation from Elasticsearch / repositories.

    Derivation rules (Req 6.4) and unlinked handling (Req 6.2):

    * No ``assigned_run_id`` → :data:`REASON_NO_RUN_LINK` (skip).
    * ``job_doc is None`` (run id did not resolve to a job for this tenant) →
      :data:`REASON_JOB_NOT_FOUND` (skip, remains unlinked).
    * Job has no ``asset_assigned`` → :data:`REASON_JOB_MISSING_ASSET` (skip,
      remains unlinked — the asset is not derivable).
    * Order already carries the job's asset AND the job already carries this
      order's ``order_id`` / ``customer_id`` → :data:`REASON_ALREADY_LINKED`
      (skip, idempotent).
    * Otherwise → :data:`REASON_LINKABLE` (link).

    Args:
        order: A :class:`fuel.order_models.FuelOrder` (or any object exposing
            ``order_id`` / ``customer_id`` / ``assigned_asset_id`` /
            ``assigned_run_id``).
        job_doc: The raw job document linked by ``order.assigned_run_id``, or
            ``None`` when the run id does not resolve to a job in this tenant.

    Returns:
        An :class:`OrderBackfillPlan`.
    """
    order_id = getattr(order, "order_id", None)
    run_id = getattr(order, "assigned_run_id", None)

    if not run_id:
        return OrderBackfillPlan(
            order_id=order_id, action=ACTION_SKIP, reason=REASON_NO_RUN_LINK
        )

    if job_doc is None:
        return OrderBackfillPlan(
            order_id=order_id,
            action=ACTION_SKIP,
            reason=REASON_JOB_NOT_FOUND,
            job_id=run_id,
        )

    asset_id = job_doc.get("asset_assigned")
    if not asset_id:
        return OrderBackfillPlan(
            order_id=order_id,
            action=ACTION_SKIP,
            reason=REASON_JOB_MISSING_ASSET,
            job_id=run_id,
        )

    order_customer_id = getattr(order, "customer_id", None)
    already_linked = (
        getattr(order, "assigned_asset_id", None) == asset_id
        and job_doc.get("order_id") == order_id
        and job_doc.get("customer_id") == order_customer_id
    )
    if already_linked:
        return OrderBackfillPlan(
            order_id=order_id,
            action=ACTION_SKIP,
            reason=REASON_ALREADY_LINKED,
            job_id=run_id,
            asset_id=asset_id,
            customer_id=order_customer_id,
        )

    return OrderBackfillPlan(
        order_id=order_id,
        action=ACTION_LINK,
        reason=REASON_LINKABLE,
        job_id=run_id,
        asset_id=asset_id,
        customer_id=order_customer_id,
    )


class LinkageBackfill:
    """Orchestrates the on-demand cross-module linkage backfill for a tenant.

    Dependencies are injected so the orchestrator is trivially testable with
    in-memory fakes:

        order_repository: Provides ``list_for_tenant(tenant_id, size=...)``.
        job_service: Provides ``get_job_doc(job_id, tenant_id)`` (raises an
            ``AppException`` 404 when the job does not exist for the tenant).
        assignment_service: Provides the consistency-preserving
            ``assign_order_to_job(...)`` coroutine used to write both sides.

    Validates: Requirements 6.2, 6.4
    """

    def __init__(
        self,
        *,
        order_repository: Any,
        job_service: Any,
        assignment_service: Any,
    ) -> None:
        self._orders = order_repository
        self._jobs = job_service
        self._assign = assignment_service

    async def run(
        self,
        tenant_id: str,
        *,
        dry_run: bool = False,
        batch_size: int = 500,
    ) -> Dict[str, Any]:
        """Backfill derivable links for one tenant's in-flight orders.

        Args:
            tenant_id: Target tenant (every read/write is scoped to it).
            dry_run: When ``True``, classify and count only — write nothing.
            batch_size: Max orders to load in one listing pass.

        Returns:
            A report dict with per-classification counts and a capped detail
            list. ``status`` is always ``"success"`` unless an unexpected,
            non-reference error occurs (reference rejections are expected and
            counted as ``unlinked``, not failures — Req 6.2).
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

        report: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "dry_run": dry_run,
            "started_at": utcnow().isoformat(),
            "scanned": 0,
            "linked": 0,
            "skipped": 0,
            "unlinked": 0,
            "reasons": {},
            "details": [],
            "errors": [],
            "status": "success",
        }

        def _bump(reason: str) -> None:
            report["reasons"][reason] = report["reasons"].get(reason, 0) + 1

        try:
            orders = await self._orders.list_for_tenant(
                tenant_id, size=batch_size
            )
        except Exception as exc:  # noqa: BLE001 — surface listing failure
            logger.exception(
                "LinkageBackfill: failed to list orders for tenant=%s", tenant_id
            )
            report["status"] = "failed"
            report["errors"].append(f"list_orders_failed: {exc}")
            report["completed_at"] = utcnow().isoformat()
            return report

        for order in orders:
            report["scanned"] += 1
            run_id = getattr(order, "assigned_run_id", None)

            job_doc: Optional[Dict[str, Any]] = None
            if run_id:
                try:
                    job_doc = await self._jobs.get_job_doc(run_id, tenant_id)
                except AppException:
                    # 404 / not in this tenant — dangling run reference. The
                    # order stays unlinked (Req 6.2); not an error.
                    job_doc = None
                except Exception as exc:  # noqa: BLE001 — defensive, keep going
                    logger.warning(
                        "LinkageBackfill: job lookup failed (order=%s run=%s): %s",
                        getattr(order, "order_id", None),
                        run_id,
                        exc,
                    )
                    job_doc = None

            plan = plan_order_backfill(order, job_doc)
            _bump(plan.reason)

            if not plan.is_link:
                # Already linked / no run link counts as "skipped"; the
                # not-derivable classifications count as "unlinked".
                if plan.reason == REASON_ALREADY_LINKED or (
                    plan.reason == REASON_NO_RUN_LINK
                ):
                    report["skipped"] += 1
                else:
                    report["unlinked"] += 1
                self._record_detail(report, plan, outcome="skipped")
                continue

            if dry_run:
                report["linked"] += 1
                self._record_detail(report, plan, outcome="would_link")
                continue

            try:
                await self._assign.assign_order_to_job(
                    tenant_id=tenant_id,
                    order_id=plan.order_id,
                    job_id=plan.job_id,
                )
                report["linked"] += 1
                self._record_detail(report, plan, outcome="linked")
            except AppException as exc:
                # Write-time reference validation rejected the derived link
                # (asset retired / type-incompatible / cross-tenant / order
                # gone). Expected for some in-flight records — they remain
                # unlinked rather than failing the run (Req 6.2).
                reason = self._reason_from_exception(exc)
                report["unlinked"] += 1
                _bump(reason)
                # The optimistic REASON_LINKABLE bump no longer reflects the
                # outcome; decrement it so counts stay consistent.
                report["reasons"][REASON_LINKABLE] = max(
                    0, report["reasons"].get(REASON_LINKABLE, 1) - 1
                )
                self._record_detail(
                    report, plan, outcome="unlinked", reason=reason
                )
                logger.info(
                    "LinkageBackfill: order=%s run=%s not linkable (%s)",
                    plan.order_id,
                    plan.job_id,
                    reason,
                )
            except Exception as exc:  # noqa: BLE001 — unexpected, keep going
                logger.exception(
                    "LinkageBackfill: unexpected error linking order=%s run=%s",
                    plan.order_id,
                    plan.job_id,
                )
                report["unlinked"] += 1
                report["errors"].append(
                    f"link_failed order={plan.order_id}: {exc}"
                )
                self._record_detail(
                    report, plan, outcome="error", reason=str(exc)
                )

        report["completed_at"] = utcnow().isoformat()
        return report

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    #: Cap on the per-run detail list so a huge tenant does not balloon the
    #: report. Counts are always exact; only the per-order detail is capped.
    _DETAIL_CAP = 1000

    def _record_detail(
        self,
        report: Dict[str, Any],
        plan: OrderBackfillPlan,
        *,
        outcome: str,
        reason: Optional[str] = None,
    ) -> None:
        """Append a capped per-order detail entry to the report."""
        if len(report["details"]) >= self._DETAIL_CAP:
            return
        report["details"].append(
            {
                "order_id": plan.order_id,
                "job_id": plan.job_id,
                "outcome": outcome,
                "reason": reason or plan.reason,
                "asset_id": plan.asset_id,
                "customer_id": plan.customer_id,
            }
        )

    @staticmethod
    def _reason_from_exception(exc: AppException) -> str:
        """Extract a stable ``details.reason`` from a validation rejection."""
        details = getattr(exc, "details", None)
        if isinstance(details, dict) and details.get("reason"):
            return str(details["reason"])
        return "reference_rejected"


# ---------------------------------------------------------------------------
# Wiring factory (used by the CLI; never auto-run)
# ---------------------------------------------------------------------------


def build_linkage_backfill(es_service: Any) -> LinkageBackfill:
    """Construct a fully-wired :class:`LinkageBackfill` for a live ES service.

    Mirrors the bootstrap wiring (``bootstrap/fuel.py``): builds the order
    repository, the job service, a :class:`RefResolver` with the cross-module
    loaders registered, and the consistency-preserving assignment service.

    This is invoked only by the on-demand CLI — importing this module does not
    run anything.
    """
    from fuel.order_repository import FuelOrderRepository
    from scheduling.services.job_service import JobService
    from scheduling.services.order_job_assignment_service import (
        OrderJobAssignmentService,
    )
    from services.ref_loaders import register_order_link_loaders
    from services.ref_resolver import RefResolver

    order_repository = FuelOrderRepository(es_service)
    job_service = JobService(es_service)

    resolver = RefResolver()
    register_order_link_loaders(
        resolver,
        es_service=es_service,
        order_repository=order_repository,
    )

    assignment_service = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repository,
        ref_resolver=resolver,
    )

    return LinkageBackfill(
        order_repository=order_repository,
        job_service=job_service,
        assignment_service=assignment_service,
    )
