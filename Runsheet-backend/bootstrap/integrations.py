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

    # NB: a block here used to late-inject ``intake_channel_repo`` and
    # ``credentials_vault`` into ``ops/webhooks/receiver.py``. It was guarded on
    # that module's ``_order_intake_pipeline`` being non-None, which
    # ``bootstrap/ops.py`` never supplied — so the body never ran, and its
    # failure was logged at debug. Both the guard and the module are gone.
    # Webhook intake now reaches the pipeline only through
    # ``fuel/api/order_webhook_endpoints.py``, which is wired directly in
    # ``bootstrap/fuel.py``.

    # Canonical CSV / Sheets imports are wired last because they need the
    # fully-injected OrderIntakePipeline plus the customer-tank repository.
    try:
        from fuel.customer_tank_models import CustomerTankRepository
        from fuel.services.tank_import_service import TankImportService
        from import_endpoints import configure_import_endpoints

        customer_tank_repo = CustomerTankRepository(es_service)
        container.customer_tank_repository = customer_tank_repo
        tank_import_service = TankImportService(
            es_service=es_service,
            customer_tank_repository=customer_tank_repo,
        )
        container.tank_import_service = tank_import_service
        if container.has("order_intake_pipeline"):
            pipeline = container.order_intake_pipeline
            pipeline.set_customer_tank_repo(customer_tank_repo)
            configure_import_endpoints(
                order_intake_pipeline=pipeline,
                tank_import_service=tank_import_service,
            )
            logger.info("Canonical order/tank import workflow configured")
        else:
            logger.warning(
                "Canonical imports not configured: OrderIntakePipeline unavailable"
            )
    except Exception as exc:
        logger.warning("Canonical import wiring failed: %s", exc)

    # ------------------------------------------------------------------
    # Dinee voice integration (Surface A submission bridge + Surface B
    # read/driver endpoints). Wired here — the last bootstrap module — so the
    # OrderIntakePipeline is fully injected (intake_channel_repo + vault above)
    # and every dependency the voice surfaces need is available on the
    # container. Requirements: 1.1, 1.3, 3.5, 3.6.
    # ------------------------------------------------------------------
    await _initialize_voice_integration(container, es_service, credentials_vault, intake_channel_repo)

    # Wire the intake channel admin endpoints. Done AFTER voice integration so
    # the already-constructed VoiceApiKeyRepository (registered on the container
    # by _initialize_voice_integration) can be injected — enabling the create
    # endpoint to mint a Surface B voice API key for ``channel_type="voice"``.
    # When the voice repo is unavailable (e.g. empty salt), the endpoints still
    # configure and voice-channel creates simply return no voice_api_key.
    try:
        voice_api_key_repo = (
            container.voice_api_key_repository
            if container.has("voice_api_key_repository")
            else None
        )
        configure_intake_channel_endpoints(
            repository=intake_channel_repo,
            voice_api_key_repository=voice_api_key_repo,
        )
        logger.info(
            "Intake channel endpoints configured (voice_api_key_repository=%s)",
            "wired" if voice_api_key_repo is not None else "absent",
        )
    except Exception as exc:
        logger.warning("configure_intake_channel_endpoints() failed: %s", exc)

    # Include the router on the app (if not already included via main.py)
    # The router is included in main.py at import time; this call ensures
    # the configure step has run so the handlers don't raise 500.
    logger.info("Integrations bootstrap complete")


async def _initialize_voice_integration(
    container: ServiceContainer,
    es_service,
    credentials_vault,
    intake_channel_repo,
) -> None:
    """Wire the Dinee voice Surface A bridge and Surface B read/driver router.

    Constructs the :class:`DineeVoiceBridge` (pipeline + intake-channel repo +
    :class:`VoiceSubmissionLedger`) and the Surface B repositories/services, then
    calls the routers' ``configure_*`` seams. Also provisions the
    ``voice_api_keys`` reverse-lookup index and wires ``get_voice_tenant``.

    Every step degrades gracefully (logged warning) so a voice-wiring failure
    never blocks the rest of integrations bootstrap.
    """
    settings = container.settings if container.has("settings") else None

    # ── Surface B — API-key auth reverse lookup + index ────────────────
    try:
        from fuel.voice.voice_auth import (
            VoiceApiKeyRepository,
            configure_voice_auth,
        )
        from fuel.voice.voice_es_mappings import setup_voice_indices

        # Create the voice_api_keys index (idempotent).
        try:
            setup_voice_indices(es_service)
            logger.info("Voice ES indices ready")
        except Exception as exc:
            logger.warning("Failed to set up voice ES indices: %s", exc)

        salt = getattr(settings, "voice_api_key_salt", "") if settings else ""
        if salt:
            voice_api_key_repo = VoiceApiKeyRepository(
                es_service=es_service,
                credentials_vault=credentials_vault,
                salt=salt,
            )
            container.voice_api_key_repository = voice_api_key_repo
            configure_voice_auth(voice_api_key_repo)
            logger.info("Voice Surface B authentication configured")
        else:
            logger.warning(
                "voice_api_key_salt is empty — Surface B voice authentication "
                "not configured (get_voice_tenant will fail closed)"
            )
    except Exception as exc:
        logger.warning("Voice Surface B authentication wiring failed: %s", exc)

    # ── Surface A — submission bridge + ledger ─────────────────────────
    try:
        from fuel.voice.dinee_voice_bridge import DineeVoiceBridge
        from fuel.voice.voice_submission_ledger import VoiceSubmissionLedger
        from fuel.voice.voice_submission_router import (
            configure_voice_submission_router,
        )

        pipeline = (
            container.order_intake_pipeline
            if container.has("order_intake_pipeline")
            else None
        )

        # Reuse the exact Redis client the IdempotencyService already connected
        # so the ledger's TTL and connection track the pipeline's idempotency
        # markers (design A4).
        idempotency_service = (
            container.ops_idempotency if container.has("ops_idempotency") else None
        )
        redis_client = getattr(idempotency_service, "client", None)
        ttl_hours = int(
            getattr(settings, "dinee_idempotency_ttl_hours", 72) if settings else 72
        )

        if pipeline is not None and redis_client is not None:
            ledger = VoiceSubmissionLedger(ttl_hours=ttl_hours, client=redis_client)
            container.voice_submission_ledger = ledger

            replay_window = int(
                getattr(settings, "voice_replay_window_seconds", 300)
                if settings
                else 300
            )
            bridge = DineeVoiceBridge(
                pipeline=pipeline,
                intake_channel_repo=intake_channel_repo,
                ledger=ledger,
                replay_window_seconds=replay_window,
            )
            container.dinee_voice_bridge = bridge
            configure_voice_submission_router(bridge=bridge)
            logger.info("Voice submission bridge configured")
        else:
            missing = []
            if pipeline is None:
                missing.append("order_intake_pipeline")
            if redis_client is None:
                missing.append("ops_idempotency.client")
            logger.warning(
                "Voice submission bridge not configured — missing: %s",
                ", ".join(missing),
            )
    except Exception as exc:
        logger.warning("Voice submission bridge wiring failed: %s", exc)

    # ── Surface B — read/driver repositories + router ──────────────────
    try:
        from commerce.services.customer_service import CustomerService
        from fuel.customer_tank_models import CustomerTankRepository
        from fuel.driver_report_repository import DriverReportRepository
        from fuel.services.delivery_destination_service import (
            DeliveryDestinationService,
        )
        from fuel.services import fuel_product_catalog
        from fuel.voice.driver_pin import DriverPinVault
        from fuel.voice.voice_read_driver_router import (
            configure_voice_read_driver_router,
        )

        order_repository = (
            container.order_repository if container.has("order_repository") else None
        )
        driver_repository = (
            container.driver_repository if container.has("driver_repository") else None
        )

        customer_service = CustomerService(es_service=es_service)
        delivery_destination_service = DeliveryDestinationService(es_service)
        customer_tank_repository = CustomerTankRepository(es_service)
        driver_pin_vault = DriverPinVault(credentials_vault)

        driver_report_repository = None
        if order_repository is not None:
            driver_report_repository = DriverReportRepository(
                es_service, order_repository=order_repository
            )
        else:
            logger.warning(
                "order_repository unavailable — voice driver reports endpoint "
                "will not persist (DriverReportRepository not built)"
            )

        configure_voice_read_driver_router(
            customer_service=customer_service,
            delivery_destination_service=delivery_destination_service,
            customer_tank_repository=customer_tank_repository,
            fuel_order_repository=order_repository,
            driver_repository=driver_repository,
            driver_report_repository=driver_report_repository,
            driver_pin_vault=driver_pin_vault,
            product_catalog=fuel_product_catalog,
        )
        logger.info("Voice read/driver router configured")
    except Exception as exc:
        logger.warning("Voice read/driver router wiring failed: %s", exc)
