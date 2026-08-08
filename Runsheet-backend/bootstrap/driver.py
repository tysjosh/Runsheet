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
from persistence.leader_election import run_periodic

logger = logging.getLogger(__name__)

# Module-level task handle so ``shutdown`` can cancel the driver retention job.
# ``shutdown`` tolerates it staying ``None``, which is what happens when the job
# could not be scheduled.
_retention_task = None
_pod_transition_repair_task = None


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
    "redis_client": (
        "POD chain lock is process-local, work bundle cache always misses, "
        "PIN attempt lockout not enforced"
    ),
    "meter_ticket_ocr_service": "manual gallons entry only",
    "pod_bol_finalizer": "BOL generation stubbed",
    "pod_hash_chain_service": "POD hash chain not written",
    "file_storage_service": "POD artifact upload unavailable",
    "signal_bus": "exception escalations not published",
    "notification_service": "POD OTP notification not delivered",
    "driver_qualification_service": "dispatch eligibility gate not enforced",
    "credentials_vault": "driver PIN enrollment, rotation, and revocation unavailable",
    # ``inspection_service`` is deliberately absent from this list: this module
    # builds it (see the inspection block in ``initialize``) rather than
    # consuming it from an earlier one, so reporting it as a missing
    # collaborator before the wiring runs would warn on every healthy boot.
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
    from driver.api.device_endpoints import (
        configure_device_endpoints,
        router as device_router,
    )
    from driver.api.inspection_endpoints import (
        configure_inspection_endpoints,
        configured_inspection_service,
        router as inspection_router,
    )
    from driver.api.qualification_endpoints import (
        configure_qualification_endpoints,
        router as qualification_router,
    )
    from driver.api.pin_endpoints import (
        admin_router as pin_admin_router,
        router as pin_router,
    )
    from driver.api.telemetry_endpoints import (
        configure_telemetry_endpoints,
        configured_telemetry_service,
        router as telemetry_router,
    )
    from driver.api.transition_endpoints import router as transition_router
    from driver.api.duty_status_endpoints import (
        configure_duty_status_endpoints,
        configured_duty_status_service,
        router as duty_status_router,
    )
    from driver.api.hos_endpoints import (
        configure_hos_endpoints,
        configured_hos_advisory_service,
        router as hos_router,
    )
    from driver.services.driver_pin_service import configure_pin_endpoints
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
    feature_flag_service = _optional(container, "ops_feature_flags")

    # ``Inspection_Service`` is built by this module rather than resolved from
    # the container — no earlier module wires it — so the local starts empty and
    # the inspection block below is what fills it. The transition gate stack is
    # wired *after* that block for exactly this reason.
    inspection_service = _optional(container, "inspection_service")

    # Report the remaining collaborators once, so a degraded boot is visible in
    # a single log line instead of being discovered one endpoint at a time.
    _report_degradations(container)

    # ------------------------------------------------------------------
    # Index setup and the mapping-validator pass are gone with Elasticsearch
    # (R15.12).
    #
    # Both existed for the same reason: a deployment that skipped the seeder had
    # driver indices the cluster auto-created on first write with ``dynamic: true``,
    # so the strict declarations were not in force, and the validator then added
    # the missing fields. Neither failure mode exists against one Postgres table
    # created by a migration.
    #
    # What ``dynamic: strict`` did enforce and jsonb does not is rejection of an
    # undeclared field. That protection now rests entirely on ``extra="forbid"`` in
    # the driver-surface Pydantic models, which is upstream of the store and
    # therefore applies to every write path — including the ones that used to reach
    # the raw client.
    # ------------------------------------------------------------------

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
    # Device_Registry — one record per (tenant_id, driver_id, device_id),
    # written by ``PUT /api/driver/devices/{device_id}`` and removed by the
    # sign-out ``DELETE`` (R9.1-R9.3). Wired before the push notifier that
    # reads it, and ``es_service`` is its only collaborator: the composite
    # document id is what makes a re-registration replace rather than duplicate,
    # so no repository or cache sits in front of it.
    # ------------------------------------------------------------------
    try:
        configure_device_endpoints(es_service=es_service)
        mount_router(app, device_router)
        logger.info("Device registry endpoints configured and router registered")
    except Exception as exc:
        logger.warning(
            "Failed to configure device registry endpoints: %s", exc
        )

    # ------------------------------------------------------------------
    # Driver_PIN_Service — the human-session surface over the vault-backed
    # ``DriverPinVault``: enrollment, rotation, the enrollment-state read, and
    # the administrator's revocation (R2.1-R2.7, R2.9, R2.10).
    #
    # ``credentials_vault`` is registered by ``bootstrap/agents.py``, which runs
    # immediately before this module, so this is the earliest point the vault
    # exists. Absent it — a boot without KMS — no service is built and every PIN
    # handler answers 500 rather than accepting a PIN nothing persists.
    #
    # ``DriverPinVault`` is constructed here rather than taken from the
    # container: it is a stateless wrapper whose ref template is derived from
    # ``(tenant_id, driver_id)``, so the instance
    # ``bootstrap/integrations.py`` builds for the voice read surface and this
    # one address exactly the same records.
    #
    # ``telemetry_service`` is the audit sink the revocation writes to (R2.10);
    # absent it the event still reaches the application log with
    # ``audit_event: True``.
    #
    # ``redis_client`` backs the R2.8 attempt lockout — five consecutive failed
    # verifications inside 15 minutes lock a ``driver_id`` for 15 minutes. It is
    # the shared client ``bootstrap/agents.py`` registers, so the counter is
    # visible to every replica and the lockout is fleet-wide rather than
    # per-process. Unlike ``credentials_vault`` its absence does not withhold the
    # service: the lockout is simply not enforced and a rotation is bounded by
    # the per-driver route rate limit alone. That fail-open posture, and why it
    # is preferred to answering 429 to every rotation during a Redis outage, is
    # argued in ``PinAttemptLimiter``'s docstring.
    # ------------------------------------------------------------------
    try:
        credentials_vault = _optional(container, "credentials_vault")
        if credentials_vault is not None:
            from fuel.voice.driver_pin import DriverPinVault

            configure_pin_endpoints(
                pin_vault=DriverPinVault(credentials_vault),
                telemetry_service=telemetry_service,
                redis_client=redis_client,
            )
            mount_router(app, pin_router)
            mount_router(app, pin_admin_router)
            if telemetry_service is None:
                logger.warning(
                    "Driver PIN endpoints wired without a telemetry_service — "
                    "a revocation is still audited, to the application log only"
                )
            if redis_client is None:
                logger.warning(
                    "Driver PIN endpoints wired without a redis_client — the "
                    "R2.8 attempt lockout is not enforced; a failed rotation is "
                    "bounded by the per-driver route rate limit alone"
                )
            logger.info("Driver PIN endpoints configured and routers registered")
        else:
            configure_pin_endpoints(
                pin_vault=None, telemetry_service=None, redis_client=None
            )
            logger.warning(
                "Driver PIN endpoints not configured — credentials_vault "
                "unavailable, so there is no encrypted store to hold a PIN hash"
            )
    except Exception as exc:
        logger.warning("Failed to configure driver PIN endpoints: %s", exc)

    # ------------------------------------------------------------------
    # Duty_Status_Service — the append-only duty-status event log and the sole
    # writer of the ``drivers_current.status`` projection (R13.16).
    #
    # Wired **before** the ops driver surface below, because that surface needs
    # this service: an administrator changing a driver's duty status through
    # ``PATCH /api/ops/drivers/{driver_id}`` is routed through it so the
    # transition is appended to ``duty_status_events`` before the projection
    # moves (R13.19). The service instance is shared rather than rebuilt, so one
    # set of collaborators sits behind the field.
    #
    # ``driver_repository`` is the preferred projection writer, because it
    # validates tenant ownership and round-trips the document through the
    # ``Driver`` model; absent it the service falls back to a partial update
    # against ``drivers_current``. ``order_repository`` is what the R13.6 gate
    # reads — the gate fails **closed** without it, rejecting a driver-submitted
    # ``off_duty`` rather than letting a driver walk away from a delivery in
    # progress, so it is passed here even though the design's wiring sketch
    # names only the first two collaborators.
    # ------------------------------------------------------------------
    duty_status_service = None
    try:
        configure_duty_status_endpoints(
            es_service=es_service,
            driver_repository=driver_repository,
            order_repository=order_repository,
        )
        mount_router(app, duty_status_router)
        duty_status_service = configured_duty_status_service()
        if order_repository is None:
            logger.warning(
                "Duty-status endpoints wired without an order_repository — a "
                "driver-submitted off_duty will be rejected with "
                "ACTIVE_DELIVERY_IN_PROGRESS because the R13.6 gate cannot be "
                "evaluated"
            )
        logger.info("Duty-status endpoints configured and router registered")
    except Exception as exc:
        logger.warning("Failed to configure duty-status endpoints: %s", exc)

    # ------------------------------------------------------------------
    # HOS_Advisory_Service — ``GET /api/driver/hos`` (R17.1-R17.14, R17.32) and
    # ``POST /api/driver/hos/override`` (R17.23, R17.24).
    #
    # Read-only against ``truck_telemetry``: nothing here writes back to a
    # telematics vendor and nothing writes to any ELD. The carrier's ELD stays
    # the authoritative record of Hours of Service and every figure the surface
    # returns is labelled advisory (R17.1). The one write on the router is the
    # dispatcher override, which lands in ``hos_gate_overrides`` and nowhere near
    # the telematics feed.
    #
    # ``integration_instance_repository`` is registered by ``bootstrap/agents.py``,
    # which runs before this module, and supplies three values — the tenant's
    # ``hos_freshness_seconds`` override of the 300-second window and the
    # provider name for the advisory (R17.9, R17.11), and ``enabled`` for the
    # gate (R17.20). Absent it the window is the default, the provider name falls
    # back, and gating is treated as disabled in every tenant.
    #
    # ``feature_flag_service`` is read by the *gate* alone, for the overlay key
    # ``driver.hos_gating``; the advisory read consults no flag. Absent it, or
    # with Redis unreachable, the toggle reads as disabled — which is the
    # fail-open answer, because a tenant with gating disabled gets no gate at all
    # (R17.19).
    #
    # Wired **before** the transition gate stack below, because the stack's
    # Hours-of-Service gate reads its verdict off the very instance this call
    # builds, and ``configure_transition_endpoints`` assigns its collaborators
    # unconditionally — a service built after that call would arrive one boot
    # too late.
    # ------------------------------------------------------------------
    hos_advisory_service = None
    try:
        configure_hos_endpoints(
            es_service=es_service,
            driver_repository=driver_repository,
            integration_instance_repository=_optional(
                container, "integration_instance_repository"
            ),
            feature_flag_service=feature_flag_service,
        )
        mount_router(app, hos_router)
        hos_advisory_service = configured_hos_advisory_service()
        if hos_advisory_service is not None:
            container.hos_advisory_service = hos_advisory_service
        if not container.has("integration_instance_repository"):
            logger.warning(
                "HOS endpoints wired without an integration_instance_repository "
                "— the freshness window is the 300-second default in every "
                "tenant, the provider name falls back, and the HOS gate stays "
                "disabled because IntegrationInstance.enabled cannot be read"
            )
        if feature_flag_service is None:
            logger.warning(
                "HOS endpoints wired without a feature_flag_service — the "
                "driver.hos_gating overlay toggle reads as disabled, so the HOS "
                "gate is a recorded skip in every tenant"
            )
        logger.info("HOS advisory endpoints configured and router registered")
    except Exception as exc:
        logger.warning("Failed to configure HOS advisory endpoints: %s", exc)

    # ------------------------------------------------------------------
    # Authoritative re-pass over the two ``driver_endpoints`` modules.
    #
    # ``fuel``: same argument set ``bootstrap/fuel.py`` passes, plus the
    # compliance qualification service and the duty-status service, neither of
    # which exists yet when ``fuel`` runs. ``scheduling``: same argument set
    # ``bootstrap/scheduling.py``
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
                duty_status_service=duty_status_service,
            )
            if duty_status_service is None:
                logger.warning(
                    "Driver ops endpoints wired without a duty_status_service — "
                    "an administrator PATCH carrying status will be refused "
                    "rather than writing drivers_current.status outside the "
                    "duty-status event log"
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
    # Driver qualification read — ``GET /api/driver/qualifications`` (R12.1,
    # R12.2, R12.6).
    #
    # No new service: the read is a projection over the existing
    # ``compliance/services/driver_qualification_service.py``, and it is handed
    # the very instance the transition gate stack below receives so the
    # eligibility a driver sees on the profile screen is the one the gate
    # enforces. ``bootstrap/compliance.py`` runs before ``driver`` in
    # ``_BOOT_ORDER``, so this is the earliest point the collaborator exists —
    # the same reason the gate stack cannot be wired any sooner.
    #
    # Absent the service the router is still mounted and the read fails closed
    # with 500 rather than reporting an unverified eligibility.
    # ------------------------------------------------------------------
    try:
        configure_qualification_endpoints(
            driver_qualification_service=driver_qualification_service,
        )
        mount_router(app, qualification_router)
        if driver_qualification_service is None:
            logger.warning(
                "Driver qualification read wired without a "
                "driver_qualification_service — GET /api/driver/qualifications "
                "will fail closed rather than report an unverified eligibility"
            )
        logger.info(
            "Driver qualification read configured and router registered"
        )
    except Exception as exc:
        logger.warning(
            "Failed to configure the driver qualification read: %s", exc
        )

    # ------------------------------------------------------------------
    # Inspection intake — ``POST /api/driver/inspections`` (R8.3, R8.4, R8.10)
    # and the unconditional out-of-service effect (R8.5, R8.9).
    #
    # Wired **before** the transition gate stack below, because the stack's
    # out-of-service gate reads inspection-derived state through this exact
    # instance: ``configure_transition_endpoints`` assigns its collaborators
    # unconditionally, so a service registered after that call would arrive one
    # boot too late and the gate would sit permanently skipped.
    #
    # ``file_storage_service`` is what enforces the tenant prefix on every
    # submitted photo ``file_ref`` (R15.8); it is the same validator the POD and
    # exception surfaces use, and a report carrying refs is refused rather than
    # persisted with references nothing checked.
    #
    # ``scheduling_ws_manager`` is the dispatcher channel the out-of-service
    # escalation is broadcast on (R8.5) — the same manager the exception surface
    # escalates over.
    #
    # ``feature_flag_service`` decides exactly one question inside the service:
    # whether an ``inspection_type: post_trip`` submission is accepted (R8.8).
    # Pre-trip intake, the out-of-service effect, and the retention stamp read
    # it nowhere and are in force in every tenant regardless of
    # ``driver.pretrip_inspection_required``, which defaults to disabled
    # (R8.11, R8.12, R8.13). Absent, post-trip intake stays closed and nothing
    # else changes.
    # ------------------------------------------------------------------
    try:
        configure_inspection_endpoints(
            es_service=es_service,
            file_storage_service=_optional(container, "file_storage_service"),
            feature_flag_service=feature_flag_service,
            scheduling_ws_manager=scheduling_ws_manager,
        )
        mount_router(app, inspection_router)
        built_inspection_service = configured_inspection_service()
        if built_inspection_service is not None:
            container.inspection_service = built_inspection_service
            inspection_service = built_inspection_service
        if not container.has("file_storage_service"):
            logger.warning(
                "Inspection endpoints wired without a file_storage_service — "
                "a report carrying photo file_refs will be refused because the "
                "tenant-prefix check cannot be performed"
            )
        if scheduling_ws_manager is None:
            logger.warning(
                "Inspection endpoints wired without a scheduling_ws_manager — "
                "an out-of-service defect still stops the asset and still gates "
                "its transitions, but no escalation reaches the dispatcher "
                "channel"
            )
        logger.info("Inspection endpoints configured and router registered")
    except Exception as exc:
        logger.warning("Failed to configure inspection endpoints: %s", exc)

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
    # ``inspection_service`` is the instance the inspection block above built,
    # which is what arms the unconditional out-of-service gate in this same
    # boot. ``hos_advisory_service`` is the instance the HOS block above built,
    # which arms the Hours-of-Service gate — arming the *seam*, not the gate
    # itself: the verdict still requires the ``driver.hos_gating`` overlay
    # toggle **and** an enabled ``gps_eld`` instance, both of which default to
    # false, so no tenant is newly gated by this wiring (R17.19, R17.20). The
    # Geotab connector as built supplies no remaining-drive-time figure, so even
    # a tenant that switches both on gets a recorded skip rather than a block
    # (R17.13) — and R17.21 refuses the request to switch gating on at all.
    # Note that the out-of-service gate itself consults no feature flag in any
    # tenant (R8.5, R8.6); ``feature_flag_service`` is passed for the pre-trip
    # gate alone, and that flag defaults to disabled.
    #
    # ``scheduling_ws_manager`` is the dispatcher channel the ``hos_block`` event
    # is broadcast on when the HOS gate rejects a transition (R17.22) — the same
    # manager the inspection escalation and the exception surface use. Absent it
    # the rejection is unchanged and only the dispatcher-side frame is lost.
    # ------------------------------------------------------------------
    try:
        configure_transition_endpoints(
            order_repository=order_repository,
            order_service=order_service,
            driver_qualification_service=driver_qualification_service,
            inspection_service=inspection_service,
            feature_flag_service=feature_flag_service,
            hos_advisory_service=hos_advisory_service,
            scheduling_ws_manager=scheduling_ws_manager,
        )
        mount_router(app, transition_router)
        if inspection_service is None:
            logger.warning(
                "Driver transition gate stack wired without an "
                "inspection_service — the unconditional out-of-service gate has "
                "nothing to read"
            )
        if order_service is None:
            logger.warning(
                "Driver transition gate stack wired without an order_service — "
                "driver-initiated status transitions will be unavailable"
            )
        if hos_advisory_service is None:
            logger.warning(
                "Driver transition gate stack wired without an "
                "hos_advisory_service — the Hours-of-Service gate is a recorded "
                "skip in every tenant"
            )
        if scheduling_ws_manager is None:
            logger.warning(
                "Driver transition gate stack wired without a "
                "scheduling_ws_manager — an Hours-of-Service block still rejects "
                "the transition, but no hos_block event reaches the dispatcher "
                "channel"
            )
        logger.info(
            "Driver transition gate stack configured and router registered"
        )
    except Exception as exc:
        logger.warning(
            "Failed to configure the driver transition gate stack: %s", exc
        )

    # ------------------------------------------------------------------
    # Driver_Telemetry_Service — breadcrumb batch ingestion on
    # ``POST /api/driver/telemetry/breadcrumbs`` (R10.1-R10.8).
    #
    # ``es_service`` is its only collaborator, and it is both halves of the
    # write: the ``driver_breadcrumbs`` track, whose composite document id
    # ``{tenant_id}:{driver_id}:{sample_timestamp_epoch_ms}`` is what makes a
    # redrained sample a create conflict rather than a duplicate (R10.8), and
    # the ``driver_presence`` merge that refreshes ``last_location`` from the
    # newest retained sample (R10.4).
    #
    # Note the name: the container's existing ``telemetry_service`` is the
    # truck/telematics one the ops driver surface reads, so the driver-app
    # service is registered as ``driver_telemetry_service`` rather than
    # overwriting it.
    #
    # Wiring order does not matter here — nothing else reads this service, and
    # it reads nothing but Elasticsearch.
    # ------------------------------------------------------------------
    try:
        configure_telemetry_endpoints(es_service=es_service)
        mount_router(app, telemetry_router)
        built_telemetry_service = configured_telemetry_service()
        if built_telemetry_service is not None:
            container.driver_telemetry_service = built_telemetry_service
        logger.info(
            "Driver telemetry endpoints configured and router registered"
        )
    except Exception as exc:
        logger.warning(
            "Failed to configure driver telemetry endpoints: %s", exc
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

    # ------------------------------------------------------------------
    # Driver_Push_Service — the four emission points (R9.5, R9.6, R9.7, R7.11)
    # and the R13.8 duty-status suppression rule.
    #
    # This is the earliest module where the notifier can be built: it needs the
    # Device_Registry wired above and the Notification_Pipeline, which
    # ``bootstrap/notifications.py`` registers before ``driver`` runs. The
    # notifier resolves its dispatcher by the channel identifier ``push`` and
    # names no provider (R9.15).
    #
    # ``bootstrap/scheduling.py`` already looks the notifier up on the container
    # when it wires the field routers, but it runs before this module, so that
    # lookup always yields ``None``. The re-pass below is what actually arms the
    # escalation and thread-message emission points — and it carries the full
    # argument set, because both ``configure_*`` functions reset an omitted
    # argument to ``None``.
    # ------------------------------------------------------------------
    driver_push_notifier = None
    try:
        from driver.api.device_endpoints import configured_device_registry
        from driver.services.driver_push_notifier import DriverPushNotifier

        driver_push_notifier = DriverPushNotifier(
            es_service=es_service,
            # The one registry the device router built, not a second one over
            # the same index.
            device_registry=configured_device_registry(),
            notification_service=_optional(container, "notification_service"),
            driver_repository=driver_repository,
            # Read-only, and only for ``is_driver_connected`` — the R7.11 gate.
            driver_ws_manager=driver_ws_manager,
        )
        container.driver_push_notifier = driver_push_notifier
        logger.info("Driver push notifier registered")
    except Exception as exc:
        logger.warning("Failed to build the driver push notifier: %s", exc)

    try:
        from driver.api.message_endpoints import configure_message_endpoints

        configure_message_endpoints(
            es_service=es_service,
            job_service=job_service,
            order_repository=order_repository,
            scheduling_ws_manager=scheduling_ws_manager,
            driver_ws_manager=driver_ws_manager,
            push_notifier=driver_push_notifier,
        )
        logger.info(
            "Driver message endpoints re-wired with the push notifier "
            "(authoritative pass)"
        )
    except Exception as exc:
        logger.warning("Failed to re-wire driver message endpoints: %s", exc)

    try:
        from driver.api.exception_endpoints import configure_exception_endpoints

        configure_exception_endpoints(
            es_service=es_service,
            job_service=job_service,
            order_repository=order_repository,
            signal_bus=_optional(container, "signal_bus"),
            scheduling_ws_manager=scheduling_ws_manager,
            driver_ws_manager=driver_ws_manager,
            push_notifier=driver_push_notifier,
        )
        logger.info(
            "Driver exception endpoints re-wired with the push notifier "
            "(authoritative pass)"
        )
    except Exception as exc:
        logger.warning("Failed to re-wire driver exception endpoints: %s", exc)

    # The assignment emission points. ``order.dispatched`` is the moment a fuel
    # order becomes a driver's work, and ``JobService.reassign_asset`` is the
    # assignment-revocation path — it already emits the realtime pair, so the
    # push sits beside it rather than in a second place.
    if driver_push_notifier is not None:
        if order_service is not None:
            try:
                order_service.subscribe(
                    "order.dispatched", driver_push_notifier.on_order_dispatched
                )
                logger.info(
                    "Driver assignment push registered on order.dispatched"
                )
            except Exception as exc:
                logger.warning(
                    "Failed to register the assignment push on "
                    "order.dispatched: %s",
                    exc,
                )
        else:
            logger.warning(
                "Assignment push not registered — order_service unavailable, "
                "so no order.dispatched event is published"
            )

        if job_service is not None:
            try:
                job_service.set_push_notifier(driver_push_notifier)
                logger.info(
                    "Driver push notifier wired into JobService for the "
                    "assignment-revocation path"
                )
            except Exception as exc:
                logger.warning(
                    "Failed to wire the push notifier into JobService: %s", exc
                )

    # Final authoritative POD pass. At this point agents has registered the
    # file store, OCR, BOL, reconciliation, Redis, and hash-chain services, so
    # no later partial configure call can silently un-wire one of them.
    try:
        from driver.api.pod_endpoints import configure_pod_endpoints

        configure_pod_endpoints(
            es_service=es_service,
            job_service=job_service,
            order_repository=order_repository,
            order_service=order_service,
            scheduling_ws_manager=scheduling_ws_manager,
            driver_ws_manager=driver_ws_manager,
            file_storage_service=_optional(container, "file_storage_service"),
            redis_client=redis_client,
            pod_bol_finalizer=_optional(container, "pod_bol_finalizer"),
            ocr_service=_optional(container, "meter_ticket_ocr_service"),
            reconciliation_service=_optional(
                container, "reconciliation_service"
            ),
            pod_hash_chain_writer=_optional(
                container, "pod_hash_chain_service"
            ),
        )
        logger.info("POD endpoints configured (authoritative driver pass)")
    except Exception as exc:
        logger.warning("Failed to complete authoritative POD wiring: %s", exc)

    # Repair any POD that was committed before its order transition completed.
    global _pod_transition_repair_task
    if order_repository is not None and order_service is not None:
        try:
            from driver.services.pod_transition_reconciler import (
                POD_TRANSITION_REPAIR_INTERVAL_SECONDS,
                PODTransitionReconciler,
            )

            pod_transition_reconciler = PODTransitionReconciler(
                es_service=es_service,
                order_repository=order_repository,
                order_service=order_service,
                redis_client=redis_client,
            )
            container.pod_transition_reconciler = pod_transition_reconciler

            async def _pod_transition_repair_cycle() -> None:
                """One pass repairing PODs whose order transition never landed."""
                counts = await pod_transition_reconciler.repair_pending()
                if counts["examined"]:
                    logger.info("POD transition repair cycle: %s", counts)

            # The original loop repaired before its first sleep, so a repair
            # pass ran at boot. ``run_immediately`` preserves that.
            _pod_transition_repair_task = asyncio.create_task(
                run_periodic(
                    "driver.pod-transition-repair",
                    POD_TRANSITION_REPAIR_INTERVAL_SECONDS,
                    _pod_transition_repair_cycle,
                    run_immediately=True,
                )
            )
            logger.info(
                "POD transition repair started (interval: %ds)",
                POD_TRANSITION_REPAIR_INTERVAL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Failed to start POD transition repair: %s", exc)
    else:
        logger.warning(
            "POD transition repair not started — order service unavailable"
        )

    # ------------------------------------------------------------------
    # DriverRetentionJob — one ``delete_by_query`` per data class, at least
    # once every 24 hours (R10.13), each emitting one log record naming its
    # ``data_class`` (R10.20). The periods are platform policy, one per class
    # (R10.16, R10.17, R10.18), and live in
    # ``driver/services/driver_retention_job.py``.
    #
    # The loop shape follows ``DriverDailyResetJob``
    # (``bootstrap/scheduling.py``) verbatim: a module-global task handle, one
    # ``asyncio.create_task`` around ``run_periodic``, which keeps a failed
    # sweep from killing the task and runs only on the sweep leader, and
    # cancellation in this module's ``shutdown``. Sleep-then-sweep, so boot is
    # never delayed by a retention pass.
    # ------------------------------------------------------------------
    global _retention_task

    try:
        from driver.services.driver_retention_job import (
            DriverRetentionJob,
            RETENTION_INTERVAL_SECONDS,
            run_retention_cycle,
        )

        retention_job = DriverRetentionJob(es_service=es_service)
        container.driver_retention_job = retention_job

        async def _driver_retention_cycle() -> None:
            """One sweep of each driver data class."""
            await run_retention_cycle(retention_job)

        _retention_task = asyncio.create_task(
            run_periodic(
                "driver.retention",
                RETENTION_INTERVAL_SECONDS,
                _driver_retention_cycle,
            )
        )
        logger.info(
            "Driver retention job started (interval: %ds)",
            RETENTION_INTERVAL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Failed to start the driver retention job: %s", exc)

    logger.info("Driver domain initialized")


async def shutdown(app, container: ServiceContainer) -> None:
    """Cancel driver-domain background tasks."""
    global _retention_task, _pod_transition_repair_task

    if (
        _pod_transition_repair_task is not None
        and not _pod_transition_repair_task.done()
    ):
        _pod_transition_repair_task.cancel()
        try:
            await _pod_transition_repair_task
        except asyncio.CancelledError:
            pass
        logger.info("POD transition repair task stopped")

    if _retention_task is not None and not _retention_task.done():
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass
        logger.info("Driver retention task stopped")

    logger.info("Driver domain shut down")
