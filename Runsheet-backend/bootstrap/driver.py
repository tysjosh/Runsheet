"""
Driver domain bootstrap module — the last writer of the driver surface.

Position in ``_BOOT_ORDER``: after ``agents``, before ``integrations``. That is
not cosmetic. Every collaborator the driver surface needs is registered by an
earlier module, and the last one to arrive is Redis (``bootstrap/agents.py``
registers ``redis_client``). ``bootstrap/scheduling.py`` reads
``container.redis_client if container.has("redis_client") else None`` at a point
where the client does not exist yet, so its ``configure_pod_endpoints`` pass has
always wired the POD hash-chain writer with ``redis_client=None``. A module
positioned after ``agents`` is therefore the earliest point at which the driver
surface can be wired **completely and once**.

Because the ``configure_*`` functions on this surface assign their module
globals unconditionally — every argument not passed is reset to ``None`` — being
the last writer is what makes that hazard harmless: earlier passes in
``bootstrap/scheduling.py`` and ``bootstrap/agents.py`` stay as they are and are
superseded by the authoritative pass here.

Note the name collision this module has to navigate:
``fuel/api/driver_endpoints.py::configure_driver_endpoints`` and
``scheduling/api/driver_endpoints.py::configure_driver_endpoints`` are two
different functions with the same name, wiring two different routers
(``/api/ops/drivers`` and ``/api/scheduling``). They are imported here under
distinct local names so the two can never be confused.

Requirements: 4.1, 15.12
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bootstrap.container import ServiceContainer
from bootstrap.routing import mount_router

logger = logging.getLogger(__name__)

# Module-level task handles so ``shutdown`` can cancel them. Populated by the
# background-job registrations that land in later tasks (the driver retention
# job); ``shutdown`` tolerates them staying ``None``.
_retention_task = None


def _optional(container: ServiceContainer, name: str) -> Optional[Any]:
    """Return ``container.<name>`` when registered, else ``None``.

    The driver surface degrades rather than fails when an optional collaborator
    is missing (no Redis → cache miss and a process-local POD chain lock, no OCR
    → manual gallons entry), so every lookup here is guarded.
    """
    return container.get(name) if container.has(name) else None


#: Collaborators the driver surface consumes, each with the stated consequence
#: of its absence. Reported in one log line so a degraded boot is visible before
#: the first request rather than one endpoint at a time.
_DEGRADATIONS = {
    "order_repository": "order-keyed driver reads unavailable",
    "order_service": "driver-initiated status transitions unavailable",
    "redis_client": "POD chain lock is process-local, work bundle cache always misses",
    "meter_ticket_ocr_service": "manual gallons entry only",
    "pod_bol_finalizer": "BOL generation stubbed",
    "pod_hash_chain_service": "POD hash chain not written",
    "file_storage_service": "POD artifact upload unavailable",
    "signal_bus": "exception escalations not published",
    "notification_service": "POD OTP notification not delivered",
    "driver_qualification_service": "dispatch eligibility gate not enforced",
    "inspection_service": (
        "no inspection-derived asset state exists, so the out-of-service and "
        "pre-trip gates have nothing to read"
    ),
    "ops_feature_flags": "flag-gated driver behaviour falls back to its default",
}


def _report_degradations(container: ServiceContainer) -> None:
    """Log which optional driver collaborators are absent, and what that costs."""
    missing = [
        f"{name} ({effect})"
        for name, effect in _DEGRADATIONS.items()
        if not container.has(name)
    ]
    if missing:
        logger.warning(
            "Driver surface degraded — %d collaborator(s) absent: %s",
            len(missing),
            "; ".join(missing),
        )
    else:
        logger.info("Driver surface collaborators all present")


async def initialize(app, container: ServiceContainer) -> None:
    """Wire the driver surface: index setup, routers, and collaborators.

    Ordering inside this function follows the dependency chain rather than the
    requirement numbering: collaborator resolution, then index setup (so a
    strict mapping is in force before the first write), then the routers.
    Every step is guarded — one failure degrades its own surface and leaves the
    rest of the module's wiring intact, matching ``initialize_all``'s fail-open
    posture.
    """
    from driver.api.session_endpoints import (
        configure_session_endpoints,
        router as session_router,
    )
    from driver.api.work_endpoints import (
        configure_work_endpoints,
        router as work_router,
    )
    from driver.api.transition_endpoints import router as transition_router
    from driver.services.order_transition_service import (
        configure_transition_endpoints,
    )

    # The two same-named wiring functions, under distinct local names.
    from fuel.api.driver_endpoints import (
        configure_driver_endpoints as configure_fuel_driver_endpoints,
    )
    from scheduling.api.driver_endpoints import (
        configure_driver_endpoints as configure_scheduling_driver_endpoints,
    )

    es_service = container.es_service

    # ------------------------------------------------------------------
    # Collaborators, resolved once in dependency order. Everything except
    # ``es_service`` is optional at this point in boot.
    # ------------------------------------------------------------------
    job_service = _optional(container, "job_service")
    order_repository = _optional(container, "order_repository")
    redis_client = _optional(container, "redis_client")
    driver_repository = _optional(container, "driver_repository")
    scheduling_ws_manager = _optional(container, "scheduling_ws_manager")
    driver_ws_manager = _optional(container, "driver_ws_manager")
    driver_qualification_service = _optional(
        container, "driver_qualification_service"
    )
    telemetry_service = _optional(container, "telemetry_service")
    order_service = _optional(container, "order_service")
    inspection_service = _optional(container, "inspection_service")
    feature_flag_service = _optional(container, "ops_feature_flags")

    # Report the remaining collaborators once, so a degraded boot is visible in
    # a single log line instead of being discovered one endpoint at a time.
    _report_degradations(container)

    # ------------------------------------------------------------------
    # Index setup, then the mapping-validator pass — in that order, in this
    # module (R15.12).
    #
    # ``setup_driver_indices`` is invoked only by the seeder today
    # (``seed_all_data.py:213``, ``:247``), so a deployment that skipped the
    # seeder has driver indices Elasticsearch auto-created on first write with
    # ``dynamic: true`` — the strict declarations are simply not in force.
    # Calling it here creates what is absent and tightens ``dynamic`` on what
    # already exists.
    #
    # The validator runs immediately after, in the same module, rather than
    # relying on the earlier pass in ``bootstrap/agents.py``:
    # ``validate_all`` skips an index that does not exist, so an index this
    # boot has just created would go unvalidated until the next boot.
    # Create-then-remediate in one place closes that window. Both steps are
    # guarded separately — a failure to create one index should not cost the
    # remediation of the others.
    # ------------------------------------------------------------------
    try:
        from driver.services.driver_es_mappings import setup_driver_indices

        logger.info("Setting up driver indices...")
        setup_driver_indices(es_service)
        logger.info("Driver indices ready")
    except Exception as exc:
        logger.error("Driver index setup failed (non-blocking): %s", exc)

    try:
        from services.mapping_validator import MappingValidator

        mapping_validator = MappingValidator(es_service=es_service)
        drift_items = await mapping_validator.validate_all()
        await mapping_validator.remediate(drift_items)
        logger.info("Driver mapping validation pass complete")
    except Exception as exc:
        logger.error(
            "Driver mapping validation failed (non-blocking): %s", exc
        )

    # ------------------------------------------------------------------
    # Mobile_Session — reads ``drivers_current`` so sign-in can fail with
    # DRIVER_RECORD_NOT_PROVISIONED (R1.15) before it issues a session. The
    # router is mounted here rather than in ``main.py`` so the collaborator
    # wiring and the mount stay in one place and cannot drift apart.
    # ------------------------------------------------------------------
    try:
        configure_session_endpoints(
            driver_repository=driver_repository,
            es_service=es_service,
        )
        mount_router(app, session_router)
        if driver_repository is None:
            logger.warning(
                "Driver session endpoints wired without a driver_repository — "
                "the DriverRepository is constructed from es_service"
            )
        logger.info("Driver session endpoints configured and router registered")
    except Exception as exc:
        logger.warning("Failed to configure driver session endpoints: %s", exc)

    # ------------------------------------------------------------------
    # Authoritative re-pass over the two ``driver_endpoints`` modules.
    #
    # ``fuel``: same argument set ``bootstrap/fuel.py`` passes, plus the
    # compliance qualification service, which does not exist yet when ``fuel``
    # runs. ``scheduling``: same argument set ``bootstrap/scheduling.py``
    # passes. Both functions reset any omitted argument to ``None``, so each
    # call carries its full argument set.
    # ------------------------------------------------------------------
    if driver_repository is not None:
        try:
            ref_resolver = None
            try:
                from services.ref_resolver import get_ref_resolver

                ref_resolver = get_ref_resolver()
            except Exception as exc:
                logger.warning("Reference resolver unavailable: %s", exc)

            configure_fuel_driver_endpoints(
                driver_repository=driver_repository,
                ref_resolver=ref_resolver,
                driver_qualification_service=driver_qualification_service,
                telemetry_service=telemetry_service,
            )
            logger.info("Driver ops endpoints re-wired (authoritative pass)")
        except Exception as exc:
            logger.warning("Failed to re-wire driver ops endpoints: %s", exc)
    else:
        logger.warning(
            "Driver ops endpoints not re-wired — driver_repository unavailable"
        )

    if job_service is not None:
        try:
            configure_scheduling_driver_endpoints(
                job_service=job_service,
                scheduling_ws_manager=scheduling_ws_manager,
                driver_ws_manager=driver_ws_manager,
            )
            logger.info(
                "Driver scheduling endpoints re-wired (authoritative pass)"
            )
        except Exception as exc:
            logger.warning(
                "Failed to re-wire driver scheduling endpoints: %s", exc
            )
    else:
        logger.warning(
            "Driver scheduling endpoints not re-wired — job_service unavailable"
        )

    # ------------------------------------------------------------------
    # Driver_Work_API — the assigned-order list, the single-order detail, and
    # the caller's own identity. ``redis_client`` is the detail read's bundle
    # cache; absent it every read is a cache miss rather than an error, which is
    # exactly why this module runs after ``agents`` registers the client.
    # ------------------------------------------------------------------
    try:
        configure_work_endpoints(
            es_service=es_service,
            order_repository=order_repository,
            job_service=job_service,
            redis_client=redis_client,
        )
        mount_router(app, work_router)
        if order_repository is None:
            logger.warning(
                "Driver work endpoints wired without an order_repository — "
                "the assigned-work list and single-order read will fail"
            )
        logger.info("Driver work endpoints configured and router registered")
    except Exception as exc:
        logger.warning("Failed to configure driver work endpoints: %s", exc)

    # ------------------------------------------------------------------
    # Driver transition gate stack. This is the earliest module that can wire
    # it: ``Dispatch_Eligibility`` is
    # ``compliance/services/driver_qualification_service.py::is_dispatch_eligible``
    # and ``bootstrap/compliance.py`` runs before ``driver`` in ``_BOOT_ORDER``,
    # so the qualification service does not exist any earlier.
    #
    # The gates are applied only on the driver path, before
    # ``OrderService.apply_status_transition`` — never inside it, because
    # ``OrderService`` is shared with the agent mutation tools and with
    # dispatcher-initiated transitions.
    #
    # Two arguments are deliberately absent in Phase 1. ``inspection_service``
    # arrives with the inspection-intake task; until then the out-of-service
    # gate has nothing to read, which is sound rather than a hole, because
    # ``Inspection_Service`` is the only writer of that state.
    # ``hos_advisory_service`` stays ``None`` — Phase 2 arms the HOS gate.
    # Note that the out-of-service gate itself consults no feature flag in any
    # tenant (R8.5, R8.6); ``feature_flag_service`` is passed for the pre-trip
    # gate alone, and that flag defaults to disabled.
    # ------------------------------------------------------------------
    try:
        configure_transition_endpoints(
            order_repository=order_repository,
            order_service=order_service,
            driver_qualification_service=driver_qualification_service,
            inspection_service=inspection_service,
            feature_flag_service=feature_flag_service,
            hos_advisory_service=None,
        )
        mount_router(app, transition_router)
        if order_service is None:
            logger.warning(
                "Driver transition gate stack wired without an order_service — "
                "driver-initiated status transitions will be unavailable"
            )
        logger.info(
            "Driver transition gate stack configured and router registered"
        )
    except Exception as exc:
        logger.warning(
            "Failed to configure the driver transition gate stack: %s", exc
        )

    # ------------------------------------------------------------------
    # POD_OTP_Service ← Notification_Pipeline (R5.27).
    #
    # ``bootstrap/fuel.py`` constructs PODOTPService and subscribes it to
    # ``order.dispatched``, but ``notifications`` runs after ``fuel`` in
    # ``_BOOT_ORDER``, so the pipeline does not exist at that point. This is
    # the setter injection that completes the wiring — the same late-binding
    # pattern ``bootstrap/compliance.py`` uses for the InvoiceService.
    #
    # Absent either half the surface degrades rather than fails: the code is
    # still generated and persisted, the customer just never receives it, and
    # the submission then fails closed. ``_report_degradations`` above already
    # names ``notification_service`` with exactly that consequence.
    # ------------------------------------------------------------------
    try:
        pod_otp_service = _optional(container, "pod_otp_service")
        notification_service = _optional(container, "notification_service")
        if pod_otp_service is not None and notification_service is not None:
            pod_otp_service.set_notification_service(notification_service)
            logger.info(
                "Notification_Pipeline wired into PODOTPService for pod_otp "
                "delivery"
            )
        else:
            missing = [
                name
                for name, value in (
                    ("pod_otp_service", pod_otp_service),
                    ("notification_service", notification_service),
                )
                if value is None
            ]
            logger.warning(
                "pod_otp notification delivery not wired — missing: %s",
                ", ".join(missing),
            )
    except Exception as exc:
        logger.warning(
            "Failed to wire Notification_Pipeline into PODOTPService: %s", exc
        )

    # The final ``configure_pod_endpoints`` / ``configure_exception_endpoints``
    # / ``configure_message_endpoints`` pass, the duty-status, device-registry,
    # transition, and inspection routers, the push notifier, and the retention
    # job schedule are added by their own tasks.

    logger.info("Driver domain initialized")


async def shutdown(app, container: ServiceContainer) -> None:
    """Cancel driver-domain background tasks."""
    global _retention_task

    if _retention_task is not None and not _retention_task.done():
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass
        logger.info("Driver retention task stopped")

    logger.info("Driver domain shut down")
