"""
Fuel domain bootstrap module.

Initializes: FuelService, fuel Elasticsearch indices.

Requirements: 1.1, 1.2
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register fuel domain services."""
    from fuel.services.fuel_es_mappings import setup_fuel_indices
    from fuel.services.fuel_service import FuelService
    from fuel.api.endpoints import configure_fuel_api

    es_service = container.es_service

    # Set up fuel indices
    try:
        logger.info("Setting up fuel monitoring indices...")
        setup_fuel_indices(es_service.client, es_service=es_service)
        logger.info("Fuel monitoring indices ready")
    except Exception as e:
        logger.warning("Failed to set up fuel monitoring indices: %s", e)

    # Fuel service
    fuel_service = FuelService(es_service)
    container.fuel_service = fuel_service

    # Wire fuel API
    configure_fuel_api(fuel_service=fuel_service)
    logger.info("Fuel API configured")
