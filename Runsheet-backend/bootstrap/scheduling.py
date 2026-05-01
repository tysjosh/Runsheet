"""
Scheduling domain bootstrap module.

Initializes: JobService, CargoService, DelayDetectionService,
SchedulingWebSocketManager, periodic delay detection background task.

Requirements: 1.1, 1.2
"""
import asyncio
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)

# Module-level reference so shutdown can cancel the task.
_delay_check_task = None


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register scheduling domain services."""
    global _delay_check_task

    from scheduling.services.scheduling_es_mappings import setup_scheduling_indices
    from scheduling.services.job_service import JobService
    from scheduling.services.cargo_service import CargoService
    from scheduling.services.delay_detection_service import DelayDetectionService
    from scheduling.api.endpoints import configure_scheduling_api
    from scheduling.websocket.scheduling_ws import (
        SchedulingWebSocketManager,
        bind_container as bind_sched_ws,
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
    cargo_service._ws_manager = scheduling_ws_manager

    # Wire scheduling API
    configure_scheduling_api(
        job_service=job_service,
        cargo_service=cargo_service,
        delay_service=delay_service,
    )
    logger.info("Scheduling API configured")

    # Start periodic delay detection background task
    interval = settings.scheduling_delay_check_interval_seconds

    async def _periodic_delay_check() -> None:
        """Background task that periodically checks for delayed jobs."""
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    newly_delayed = await delay_service.check_delays(tenant_id=None)
                    if newly_delayed:
                        logger.info(
                            "Periodic delay check: %d job(s) newly delayed",
                            len(newly_delayed),
                        )
                except Exception as exc:
                    logger.error("Periodic delay check failed: %s", exc)
        except asyncio.CancelledError:
            logger.info("Periodic delay check task cancelled")

    _delay_check_task = asyncio.create_task(_periodic_delay_check())
    logger.info("Periodic delay check started (interval: %ds)", interval)


async def shutdown(app, container: ServiceContainer) -> None:
    """Cancel periodic task and shut down scheduling WS manager."""
    global _delay_check_task

    if _delay_check_task is not None and not _delay_check_task.done():
        _delay_check_task.cancel()
        try:
            await _delay_check_task
        except asyncio.CancelledError:
            pass
        logger.info("Periodic delay check task stopped")

    if container.has("scheduling_ws_manager"):
        try:
            await container.scheduling_ws_manager.shutdown()
        except Exception:
            pass

    logger.info("Scheduling domain shut down")
