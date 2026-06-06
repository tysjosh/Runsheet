"""
Consistency-preserving order→job assignment (cross-module-entity-linkage).

This module implements the *single* application-service method that links a
fuel order to a scheduling job. Per the design's Referential Integrity Strategy
(§Consistency on assignment, Req 3.4) the linkage is written as **one
operation, not two independent writes**: it sets the order's
``assigned_asset_id`` / ``assigned_driver_id`` AND the job's ``order_id`` /
``customer_id`` / ``driver_id`` together so the two sides can never diverge
(order pointing at one asset while the job points at another).

Referential integrity is enforced at write time (Req 2.3, 3.3) using the shared
``RefResolver`` (task 1 / 1.1): the referenced asset must exist in the same
tenant and be type-compatible with the job, and a referenced driver must exist
in the same tenant. Invalid references are rejected with ``validation_error``
(HTTP 400) carrying a stable ``details.reason`` (``asset_required``,
``asset_not_found``, ``asset_type_incompatible``, ``driver_not_found``,
``order_not_found``). Because the resolver's loaders are tenant-scoped, a
cross-tenant id resolves to ``None`` and is rejected as ``*_not_found`` — never
linked across tenants (Req 5.3).

Partial-write protection (design §Error Handling): the job side is written
first, then the order side. If the order write fails, the job-side link is
rolled back to its prior values so the references do not diverge, and the
original error is re-raised — the operation fails as a unit.

Validates: Requirements 2.2, 3.2, 3.3, 3.4
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from errors.exceptions import validation_error
from scheduling.models import JOB_ASSET_COMPATIBILITY, Job, JobType
from services.ref_resolver import RefResolver, get_ref_resolver
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = ["OrderJobAssignmentService"]


class OrderJobAssignmentService:
    """Links an order and a job atomically, preserving referential consistency.

    Dependencies are injected so the service is trivially testable with
    in-memory fakes:

        job_service: A :class:`scheduling.services.job_service.JobService`
            (uses ``get_job_doc`` + ``set_link_fields``).
        order_repository: A :class:`fuel.order_repository.FuelOrderRepository`
            (uses ``get`` + ``upsert_with_last_event_timestamp``).
        ref_resolver: The shared :class:`RefResolver` used for write-time
            reference validation. Defaults to the process-wide resolver.

    Validates: Requirements 2.2, 3.2, 3.3, 3.4
    """

    def __init__(
        self,
        *,
        job_service: Any,
        order_repository: Any,
        ref_resolver: Optional[RefResolver] = None,
    ) -> None:
        self._jobs = job_service
        self._orders = order_repository
        self._resolver = ref_resolver or get_ref_resolver()

    async def assign_order_to_job(
        self,
        *,
        tenant_id: str,
        order_id: str,
        job_id: str,
        driver_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict:
        """Link ``order_id`` to ``job_id``, setting both sides consistently.

        Sets, in a single operation:
            * order ``assigned_asset_id`` ← job's ``asset_assigned`` (Req 2.2)
            * order ``assigned_driver_id`` ← effective driver (Req 2.2)
            * job   ``order_id``           ← order's id            (Req 3.2)
            * job   ``customer_id``        ← order's customer_id   (Req 3.2)
            * job   ``driver_id``          ← effective driver      (Req 3.1)

        The effective driver is, in priority order: the explicit ``driver_id``
        argument, then the job's existing ``driver_id``, then the order's
        existing ``assigned_driver_id``. A null effective driver is allowed
        (the link is asset-only) since the references are nullable (Req 6.1).

        Args:
            tenant_id: Tenant scope from the verified session.
            order_id: The order to link.
            job_id: The job that fulfills the order.
            driver_id: Optional explicit driver to assign to both sides.
            actor_id: Optional operator performing the assignment.

        Returns:
            ``{"job": Job, "order": FuelOrder}`` reflecting the linked state.

        Raises:
            AppException: ``validation_error`` (400) with ``details.reason`` for
                an invalid asset/driver/order reference; 404 if the job itself
                does not exist for this tenant.
        """
        # --- Load both entities (tenant-scoped) ---
        order = await self._orders.get(tenant_id, order_id)
        if order is None:
            raise validation_error(
                f"Order '{order_id}' does not exist in this tenant",
                details={"reason": "order_not_found", "id": order_id},
            )

        # The job is the assignment target addressed by id; a missing job is a
        # 404 (resource_not_found) raised by the repository, not a reference
        # validation failure.
        job_doc = await self._jobs.get_job_doc(job_id, tenant_id)
        job_type = JobType(job_doc["job_type"])

        # --- Write-time reference validation (Req 2.3, 3.3) ---
        asset_id = job_doc.get("asset_assigned")
        await self._validate_asset(tenant_id, asset_id, job_type)

        effective_driver = (
            driver_id
            if driver_id is not None
            else (job_doc.get("driver_id") or order.assigned_driver_id)
        )
        # Validates driver existence in-tenant; a null driver is permitted
        # (the reference is optional). Uses the task-1.1 write-time helper,
        # which raises validation_error(reason="driver_not_found").
        await self._resolver.validate_ref(
            tenant_id, "driver", effective_driver, required=False
        )

        # --- Capture prior job link state for rollback (partial-write guard) ---
        prior_links = {
            "order_id": job_doc.get("order_id"),
            "customer_id": job_doc.get("customer_id"),
            "driver_id": job_doc.get("driver_id"),
        }

        # --- Write 1: job side (order_id / customer_id / driver_id) ---
        new_links = {
            "order_id": order.order_id,
            "customer_id": order.customer_id,
            "driver_id": effective_driver,
        }
        updated_job_doc = await self._jobs.set_link_fields(
            job_id, tenant_id, new_links, job_doc=job_doc
        )

        # --- Write 2: order side (assigned_asset_id / assigned_driver_id) ---
        # If this fails, the two sides would diverge — so roll the job side
        # back to its prior references and re-raise (Req 3.4, fail-as-a-unit).
        now = utcnow()
        updated_order = order.model_copy(
            update={
                "assigned_asset_id": asset_id,
                "assigned_driver_id": effective_driver,
                "updated_at": now,
                "last_event_timestamp": now,
            }
        )
        try:
            await self._orders.upsert_with_last_event_timestamp(
                tenant_id, updated_order
            )
        except Exception:
            logger.exception(
                "Order write failed linking order=%s to job=%s; rolling back "
                "job-side references to avoid divergence",
                order_id,
                job_id,
            )
            try:
                await self._jobs.set_link_fields(
                    job_id, tenant_id, prior_links, job_doc=updated_job_doc
                )
            except Exception:  # noqa: BLE001 - best-effort compensation
                logger.exception(
                    "Rollback of job-side references failed for job=%s; "
                    "order=%s and job links may have diverged",
                    job_id,
                    order_id,
                )
            raise

        return {
            "job": Job(**self._jobs._normalize_job_doc(updated_job_doc)),
            "order": updated_order,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _validate_asset(
        self, tenant_id: str, asset_id: Optional[str], job_type: JobType
    ) -> None:
        """Validate the job's asset exists in-tenant and is type-compatible.

        Uses the shared resolver so the existence check is tenant-scoped
        (cross-tenant ids resolve to ``None`` → ``asset_not_found``). Type
        compatibility is read from the resolved asset summary's ``asset_type``
        (logistics-scheduling Req 3.3).

        Raises:
            AppException: ``validation_error`` (400) with ``details.reason`` of
                ``asset_required`` / ``asset_not_found`` /
                ``asset_type_incompatible``.
        """
        if not asset_id:
            raise validation_error(
                "The job has no asset assigned; an asset is required to link "
                "an order to a job",
                details={"reason": "asset_required"},
            )

        asset_ref = await self._resolver.resolve(tenant_id, "asset", asset_id)
        if not asset_ref.is_resolved:
            raise validation_error(
                f"Referenced asset '{asset_id}' does not exist in this tenant",
                details={"reason": "asset_not_found", "id": asset_id},
            )

        asset_type = (asset_ref.summary or {}).get("asset_type", "vehicle")
        compatible_types = JOB_ASSET_COMPATIBILITY.get(job_type, [])
        if asset_type not in compatible_types:
            raise validation_error(
                f"Asset type '{asset_type}' is not compatible with job type "
                f"'{job_type.value}'. Compatible types: {compatible_types}",
                details={
                    "reason": "asset_type_incompatible",
                    "id": asset_id,
                    "asset_type": asset_type,
                    "job_type": job_type.value,
                    "compatible_types": compatible_types,
                },
            )
