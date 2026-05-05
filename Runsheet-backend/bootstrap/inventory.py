"""
Inventory domain bootstrap module.

Initializes: InventoryService, TenantInventoryConfigService,
inventory Elasticsearch indices.
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register inventory domain services."""
    from inventory.es_mappings import setup_inventory_indices
    from inventory.service import InventoryService
    from inventory.tenant_config import TenantInventoryConfigService
    from inventory.api.endpoints import configure_inventory_api

    es_service = container.es_service

    # Set up inventory indices
    try:
        logger.info("Setting up inventory indices...")
        setup_inventory_indices(es_service)
        logger.info("Inventory indices ready")
    except Exception as e:
        logger.warning("Failed to set up inventory indices: %s", e)

    # Get the fleet WS manager for broadcasting alerts (optional)
    ws_manager = None
    if container.has("fleet_ws_manager"):
        ws_manager = container.fleet_ws_manager

    # Inventory service
    inventory_service = InventoryService(es_service, ws_manager=ws_manager)
    container.inventory_service = inventory_service

    # Tenant inventory config service (uses Redis when available)
    redis_client = container.redis_client if container.has("redis_client") else None
    tenant_inventory_config = TenantInventoryConfigService(redis_client=redis_client)
    container.tenant_inventory_config = tenant_inventory_config

    # Wire inventory API
    configure_inventory_api(inventory_service=inventory_service)
    logger.info("Inventory API configured")
