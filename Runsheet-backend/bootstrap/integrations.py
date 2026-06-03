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

    # Late-inject the repo + vault into the OrderIntakePipeline. The pipeline
    # is constructed in bootstrap/fuel.py (boot order #5), BEFORE this module
    # (#11) builds the IntakeChannelRepository and before agents (#10) registers
    # the credentials_vault — so the pipeline was created with both set to
    # ``None``. Without this re-injection every dispatcher order create / webhook
    # ingest 500s in ``_resolve_dispatcher_channel`` (NoneType has no attribute
    # 'get_dispatcher_channel'). Re-inject here now that both deps exist.
    try:
        if container.has("order_intake_pipeline"):
            pipeline = container.order_intake_pipeline
            if pipeline is not None:
                pipeline.set_intake_channel_repo(intake_channel_repo)
                if container.has("credentials_vault"):
                    pipeline.set_credentials_vault(container.credentials_vault)
                logger.info(
                    "OrderIntakePipeline: intake_channel_repo + credentials_vault "
                    "late-injected (boot-order fix)"
                )
        else:
            logger.warning(
                "order_intake_pipeline not in container — cannot late-inject "
                "intake_channel_repo; dispatcher order intake will fail"
            )
    except Exception as exc:
        logger.warning(
            "Failed to late-inject intake_channel_repo into pipeline: %s", exc
        )

    # The order webhook receiver (ops/webhooks/receiver.py) is configured in
    # bootstrap/fuel.py too; re-point it at the now-available repo/vault so
    # webhook-based intake resolves channels correctly.
    try:
        from ops.webhooks import receiver as _webhook_receiver

        if getattr(_webhook_receiver, "_order_intake_pipeline", None) is not None:
            _webhook_receiver._intake_channel_repo = intake_channel_repo
            if container.has("credentials_vault"):
                _webhook_receiver._credentials_vault = container.credentials_vault
            logger.info("Webhook receiver: intake_channel_repo re-pointed (boot-order fix)")
    except Exception as exc:
        logger.debug("Webhook receiver re-point skipped: %s", exc)

    # Include the router on the app (if not already included via main.py)
    # The router is included in main.py at import time; this call ensures
    # the configure step has run so the handlers don't raise 500.
    logger.info("Integrations bootstrap complete")
