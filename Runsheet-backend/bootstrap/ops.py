"""
Ops domain bootstrap module.

Initializes: OpsElasticsearchService, OpsAdapter, IdempotencyService,
PoisonQueueService, FeatureFlagService, WebhookReceiver, DriftDetector,
OpsWebSocketManager, ReplayService, and ops AI tool wiring.

Requirements: 1.1, 1.2
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register ops domain services."""
    from ops.services.ops_es_service import OpsElasticsearchService
    from ops.ingestion.adapter import AdapterTransformer
    from ops.ingestion.handlers.v1_0 import V1SchemaHandler
    from ops.ingestion.idempotency import IdempotencyService
    from ops.ingestion.poison_queue import PoisonQueueService
    from ops.ingestion.replay import configure_replay_service
    from ops.services.feature_flags import FeatureFlagService
    from ops.webhooks.receiver import configure_webhook_receiver
    from ops.api.endpoints import configure_ops_api
    from ops.services.drift_detector import configure_drift_detector
    from ops.websocket.ops_ws import OpsWebSocketManager, bind_container as bind_ops_ws
    from Agents.tools.ops_feature_guard import configure_ops_feature_guard
    from Agents.tools.ops_search_tools import configure_ops_search_tools
    from Agents.tools.ops_report_tools import configure_ops_report_tools

    settings = container.settings
    es_service = container.es_service

    # Ops Elasticsearch service
    ops_es_service = OpsElasticsearchService(es_service)
    container.ops_es_service = ops_es_service

    # Set up ops indices
    try:
        logger.info("Setting up ops intelligence indices...")
        ops_es_service.setup_ops_indices()
        ops_es_service.setup_ops_ilm_policies()
        ops_es_service.verify_ops_ilm_policies()
        ops_es_service.validate_ops_index_schemas()
        logger.info("Ops intelligence indices ready")
    except Exception as e:
        logger.error("Failed to set up ops intelligence indices: %s", e)

    # Adapter
    ops_adapter = AdapterTransformer()
    ops_adapter.register_handler("1.0", V1SchemaHandler())

    # Poison queue
    ops_poison_queue = PoisonQueueService(ops_es_service)
    container.ops_poison_queue = ops_poison_queue

    # Idempotency service (Redis-backed)
    redis_url = settings.redis_url or "redis://localhost:6379"
    ops_idempotency = IdempotencyService(
        redis_url=redis_url,
        ttl_hours=settings.dinee_idempotency_ttl_hours,
    )
    container.ops_idempotency = ops_idempotency

    # Feature flag service (Redis-backed)
    ops_feature_flags = FeatureFlagService(
        redis_url=redis_url,
        ops_es_service=ops_es_service,
    )
    container.ops_feature_flags = ops_feature_flags

    # Ops WebSocket manager
    ops_ws_manager = OpsWebSocketManager()
    container.ops_ws_manager = ops_ws_manager
    bind_ops_ws(container)

    # Connect Redis-backed services
    await ops_idempotency.connect()
    await ops_feature_flags.connect()

    # Wire webhook receiver
    configure_webhook_receiver(
        adapter=ops_adapter,
        idempotency_service=ops_idempotency,
        poison_queue_service=ops_poison_queue,
        ops_es_service=ops_es_service,
        ws_manager=ops_ws_manager,
        feature_flag_service=ops_feature_flags,
        webhook_secret=settings.dinee_webhook_secret,
        webhook_tenant_id=settings.dinee_webhook_tenant_id,
        idempotency_ttl_hours=settings.dinee_idempotency_ttl_hours,
    )
    logger.info("Webhook receiver configured")

    # Wire ops API
    configure_ops_api(
        ops_es_service=ops_es_service,
        feature_flag_service=ops_feature_flags,
    )
    logger.info("Ops API configured")

    # Wire replay service
    configure_replay_service(
        adapter=ops_adapter,
        idempotency=ops_idempotency,
        ops_es=ops_es_service,
        settings=settings,
    )
    logger.info("Replay service configured")

    # Wire drift detector
    configure_drift_detector(
        ops_es=ops_es_service,
        settings=settings,
        threshold_pct=settings.drift_threshold_pct,
        schedule_interval_hours=settings.drift_schedule_interval_hours,
    )
    logger.info("Drift detector configured")

    # Wire feature flag into WS manager
    ops_ws_manager.set_feature_flag_service(ops_feature_flags)
    logger.info("Ops WebSocket feature flag integration configured")

    # Wire ops AI tools
    configure_ops_feature_guard(ops_feature_flags)
    configure_ops_search_tools(ops_es_service)
    configure_ops_report_tools(ops_es_service)
    logger.info("Ops AI tools configured")


async def shutdown(app, container: ServiceContainer) -> None:
    """Gracefully shut down ops services."""
    if container.has("ops_ws_manager"):
        try:
            await container.ops_ws_manager.shutdown()
        except Exception:
            pass

    if container.has("ops_idempotency"):
        try:
            await container.ops_idempotency.disconnect()
        except Exception:
            pass

    if container.has("ops_feature_flags"):
        try:
            await container.ops_feature_flags.disconnect()
        except Exception:
            pass

    logger.info("Ops domain shut down")
