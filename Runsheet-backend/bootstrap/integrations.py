"""
Integrations domain bootstrap module.

Initializes: IntakeChannelRepository, intake channel endpoints router,
and registers Prometheus metrics for the intake channel admin surface.

Requirements: 2.1
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register integrations domain services.

    Wires the :class:`IntakeChannelRepository` with the shared
    ``es_service`` and ``credentials_vault`` from the container, then
    configures the intake channel admin endpoints router.

    The ``credentials_vault`` is registered by ``bootstrap/agents.py``
    which runs before this module in the boot order. If the vault is
    unavailable (e.g. in test environments without KMS), the intake
    channel endpoints will not be configured and a warning is logged.
    """
    from fuel.intake_channel_repository import IntakeChannelRepository
    from integrations.api.intake_channel_endpoints import (
        configure_intake_channel_endpoints,
        router as intake_channel_router,
    )
    from fuel.services.order_intake_metrics import (
        orders_intake_channel_rotations_total,  # noqa: F401 — ensures metric is registered
    )

    es_service = container.es_service

    # credentials_vault is registered by bootstrap/agents.py
    if not container.has("credentials_vault"):
        logger.warning(
            "credentials_vault not available — intake channel endpoints "
            "will not be configured. Ensure bootstrap/agents.py runs "
            "before bootstrap/integrations.py in the boot order."
        )
        return

    credentials_vault = container.credentials_vault

    # Build the IntakeChannelRepository
    try:
        intake_channel_repo = IntakeChannelRepository(
            es_service=es_service,
            credentials_vault=credentials_vault,
        )
        container.intake_channel_repository = intake_channel_repo
        logger.info("IntakeChannelRepository registered")
    except Exception as exc:
        logger.warning("IntakeChannelRepository creation failed: %s", exc)
        return

    # Wire the intake channel admin endpoints
    try:
        configure_intake_channel_endpoints(repository=intake_channel_repo)
        logger.info("Intake channel endpoints configured")
    except Exception as exc:
        logger.warning("configure_intake_channel_endpoints() failed: %s", exc)

    # Include the router on the app (if not already included via main.py)
    # The router is included in main.py at import time; this call ensures
    # the configure step has run so the handlers don't raise 500.
    logger.info("Integrations bootstrap complete")
