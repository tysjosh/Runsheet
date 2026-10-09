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
    from inventory.service import InventoryService
    from inventory.tenant_config import TenantInventoryConfigService
    from inventory.api.endpoints import configure_inventory_api

    es_service = container.es_service

    # Set up inventory indices

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
