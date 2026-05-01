"""
Core infrastructure bootstrap module.

Initializes: Settings, Elasticsearch client, Redis client, Telemetry,
DataSeeder, HealthCheckService, DataIngestionService, fleet ConnectionManager.

Requirements: 1.1, 1.2
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register core infrastructure services."""
    from config.settings import get_settings
    from services.elasticsearch_service import elasticsearch_service
    from services.data_seeder import data_seeder
    from telemetry.service import initialize_telemetry
    from health.service import HealthCheckService
    from ingestion.service import DataIngestionService
    from websocket.connection_manager import ConnectionManager, bind_container as bind_fleet
    from errors.handlers import register_exception_handlers

    # Settings
    settings = get_settings()
    container.settings = settings

    # Telemetry
    telemetry_service = initialize_telemetry(settings)
    container.telemetry_service = telemetry_service

    # Elasticsearch (module-level singleton)
    container.es_service = elasticsearch_service

    # Seed baseline data
    try:
        logger.info("Seeding Elasticsearch with baseline morning data...")
        await data_seeder.seed_baseline_data(operational_time="09:00")
        logger.info("Baseline data seeding completed.")
    except Exception as e:
        logger.error("Failed to seed Elasticsearch data: %s", e)

    # Health check service
    health_check_service = HealthCheckService(
        es_service=elasticsearch_service,
        session_store=None,
        check_timeout=5.0,
    )
    container.health_check_service = health_check_service

    # Data ingestion service
    data_ingestion_service = DataIngestionService(
        es_service=elasticsearch_service,
        telemetry=telemetry_service,
    )
    container.data_ingestion_service = data_ingestion_service

    # Fleet WebSocket manager
    fleet_ws_manager = ConnectionManager()
    container.fleet_ws_manager = fleet_ws_manager
    bind_fleet(container)

    # Wire ingestion → fleet WS for live broadcasts
    data_ingestion_service.set_connection_manager(fleet_ws_manager)

    # Register structured exception handlers
    register_exception_handlers(app)

    logger.info("Core infrastructure initialized")


async def shutdown(app, container: ServiceContainer) -> None:
    """Cleanup core resources."""
    # Redis client cleanup is handled by modules that own the connection.
    logger.info("Core infrastructure shut down")
