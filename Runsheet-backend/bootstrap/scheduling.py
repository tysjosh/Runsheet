"""
Scheduling domain bootstrap module.

Initializes: JobService, CargoService, DelayDetectionService,
SchedulingWebSocketManager, periodic delay detection background task.

Requirements: 1.1, 1.2
"""
import asyncio
import logging

from bootstrap.container import ServiceContainer
from persistence.leader_election import run_periodic

logger = logging.getLogger(__name__)

# Module-level reference so shutdown can cancel the task.
_delay_check_task = None
_driver_daily_reset_task = None


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register scheduling domain services."""
    global _delay_check_task

    from scheduling.services.scheduling_es_mappings import setup_scheduling_indices
    from scheduling.services.job_service import JobService
    from scheduling.services.cargo_service import CargoService
    from scheduling.services.delay_detection_service import DelayDetectionService
    from scheduling.api.endpoints import configure_scheduling_api
    from scheduling.api.driver_endpoints import configure_driver_endpoints
    from driver.api.message_endpoints import configure_message_endpoints
    from driver.api.exception_endpoints import configure_exception_endpoints
    from driver.api.pod_endpoints import configure_pod_endpoints
    from driver.middleware.idempotency import configure_idempotency_middleware
    from scheduling.websocket.scheduling_ws import (
        SchedulingWebSocketManager,
        bind_container as bind_sched_ws,
    )
    from driver.ws.driver_ws_manager import (
        DriverWSManager,
        bind_container as bind_driver_ws,
    )

    settings = container.settings
    es_service = container.es_service

    # Set up scheduling indices
    try:
        logger.info("Setting up scheduling indices...")
        setup_scheduling_indices(es_service)
        logger.info("Scheduling indices ready")
    except Exception as e:
        logger.warning("Failed to set up scheduling indices: %s", e)

    # Scheduling WebSocket manager
    scheduling_ws_manager = SchedulingWebSocketManager()
    container.scheduling_ws_manager = scheduling_ws_manager
    bind_sched_ws(container)

    # Driver WebSocket manager
    driver_ws_manager = DriverWSManager(es_service=es_service)
    container.driver_ws_manager = driver_ws_manager
    bind_driver_ws(container)

    # Idempotency middleware (ES-backed, Req 14.1–14.4)
    configure_idempotency_middleware(es_service=es_service)
    logger.info("Idempotency middleware configured")

    # Services
    redis_url = settings.redis_url or "redis://localhost:6379"
    job_service = JobService(es_service, redis_url=redis_url)
    cargo_service = CargoService(es_service)
    delay_service = DelayDetectionService(es_service, ws_manager=scheduling_ws_manager)

    container.job_service = job_service
    container.cargo_service = cargo_service
    container.delay_detection_service = delay_service

    # Wire WS manager into services for real-time broadcasts
    job_service._ws_manager = scheduling_ws_manager
    job_service._driver_ws_manager = driver_ws_manager
    cargo_service._ws_manager = scheduling_ws_manager

    # Wire scheduling API
    configure_scheduling_api(
        job_service=job_service,
        cargo_service=cargo_service,
        delay_service=delay_service,
    )

    # Wire driver acknowledgment endpoints
    configure_driver_endpoints(
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws_manager,
        driver_ws_manager=driver_ws_manager,
    )

    # Wire driver messaging endpoints. ``order_repository`` / ``push_notifier``
    # are looked up rather than omitted: every ``configure_*`` call on the
    # driver surface assigns each module global unconditionally, so an omitted
    # argument resets it to ``None`` and un-wires the order-keyed path.
    order_repository = (
        container.get("order_repository")
        if container.has("order_repository")
        else None
    )
    driver_push_notifier = (
        container.get("driver_push_notifier")
        if container.has("driver_push_notifier")
        else None
    )
    configure_message_endpoints(
        es_service=es_service,
        job_service=job_service,
        order_repository=order_repository,
        scheduling_ws_manager=scheduling_ws_manager,
        driver_ws_manager=driver_ws_manager,
        push_notifier=driver_push_notifier,
    )

    # Wire driver exception reporting endpoints
    signal_bus = container.get("signal_bus") if container.has("signal_bus") else None
    configure_exception_endpoints(
        es_service=es_service,
        job_service=job_service,
        order_repository=order_repository,
        signal_bus=signal_bus,
        scheduling_ws_manager=scheduling_ws_manager,
        driver_ws_manager=driver_ws_manager,
        push_notifier=driver_push_notifier,
    )

    # Wire driver POD endpoints. The ``ocr_service`` is optional: when the
    # container hasn't wired a MeterTicketOCRService (e.g. in local dev
    # without AWS creds or in unit tests), the POD flow silently skips
    # OCR and the driver must enter ``delivered_gallons`` manually.
    ocr_service = (
        container.meter_ticket_ocr_service
        if container.has("meter_ticket_ocr_service")
        else None
    )
    # Redis client is used to serialize the per-tenant POD hash-chain lock
    # (``pod_chain_lock:{tenant_id}``, Requirement 4.5.2). Multi-replica
    # deployments MUST have ``redis_client`` wired in the container; when
    # absent, the hash-chain writer falls back to a process-local lock so
    # single-replica deployments / unit tests still run correctly.
    redis_client = (
        container.redis_client if container.has("redis_client") else None
    )
    order_service = (
        container.get("order_service") if container.has("order_service") else None
    )
    configure_pod_endpoints(
        es_service=es_service,
        job_service=job_service,
        order_repository=order_repository,
        order_service=order_service,
        scheduling_ws_manager=scheduling_ws_manager,
        driver_ws_manager=driver_ws_manager,
        ocr_service=ocr_service,
        reconciliation_service=(
            container.reconciliation_service
            if container.has("reconciliation_service")
            else None
        ),
        redis_client=redis_client,
    )
    logger.info("Scheduling API configured")

    # Start periodic delay detection background task
    interval = settings.scheduling_delay_check_interval_seconds

    async def _delay_check_cycle() -> None:
        """One pass checking for delayed jobs."""
        newly_delayed = await delay_service.check_delays(tenant_id=None)
        if newly_delayed:
            logger.info(
                "Periodic delay check: %d job(s) newly delayed",
                len(newly_delayed),
            )

    _delay_check_task = asyncio.create_task(
        run_periodic("scheduling.delay-check", interval, _delay_check_cycle)
    )
    logger.info("Periodic delay check started (interval: %ds)", interval)

    # ---------------------------------------------------------------
    # Driver daily reset cron — Requirement 3.2.4
    # Fires at 00:00 in each tenant's configured timezone (default
    # America/Chicago). Failures log logger.exception and increment
    # fuelops_driver_daily_reset_errors_total{tenant_id}.
    # ---------------------------------------------------------------
    global _driver_daily_reset_task

    try:
        from fuel.services.driver_daily_reset import (
            DriverDailyResetJob,
            run_daily_reset_cycle,
            RESET_CHECK_INTERVAL_SECONDS,
        )

        driver_repository = (
            container.driver_repository
            if container.has("driver_repository")
            else None
        )
        tenant_settings_service = (
            container.tenant_settings_service
            if container.has("tenant_settings_service")
            else None
        )

        if driver_repository is not None:
            daily_reset_job = DriverDailyResetJob(
                es_service=es_service,
                driver_repository=driver_repository,
                tenant_settings_service=tenant_settings_service,
            )
            container.driver_daily_reset_job = daily_reset_job

            async def _driver_daily_reset_cycle() -> None:
                """One pass checking for midnight and resetting counters."""
                await run_daily_reset_cycle(daily_reset_job)

            _driver_daily_reset_task = asyncio.create_task(
                run_periodic(
                    "driver.daily-reset",
                    RESET_CHECK_INTERVAL_SECONDS,
                    _driver_daily_reset_cycle,
                )
            )
            logger.info(
                "Driver daily reset cron started (check interval: %ds)",
                RESET_CHECK_INTERVAL_SECONDS,
            )
        else:
            logger.warning(
                "Driver daily reset cron not started — "
                "driver_repository unavailable"
            )
    except Exception as e:
        logger.warning("Failed to start driver daily reset cron: %s", e)


async def shutdown(app, container: ServiceContainer) -> None:
    """Cancel periodic task and shut down scheduling WS manager."""
    global _delay_check_task, _driver_daily_reset_task

    if _delay_check_task is not None and not _delay_check_task.done():
        _delay_check_task.cancel()
        try:
            await _delay_check_task
        except asyncio.CancelledError:
            pass
        logger.info("Periodic delay check task stopped")

    if _driver_daily_reset_task is not None and not _driver_daily_reset_task.done():
        _driver_daily_reset_task.cancel()
        try:
            await _driver_daily_reset_task
        except asyncio.CancelledError:
            pass
        logger.info("Driver daily reset task stopped")

    if container.has("scheduling_ws_manager"):
        try:
            await container.scheduling_ws_manager.shutdown()
        except Exception as exc:
            # Don't propagate — shutdown continues for the other
            # components — but surface the failure so we can see stuck
            # shutdown paths in logs.
            logger.exception(
                "Scheduling WS manager shutdown failed: %s", exc
            )

    if container.has("driver_ws_manager"):
        try:
            await container.driver_ws_manager.shutdown()
        except Exception as exc:
            logger.exception(
                "Driver WS manager shutdown failed: %s", exc
            )

    logger.info("Scheduling domain shut down")
