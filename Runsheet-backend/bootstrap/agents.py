"""
Agentic AI bootstrap module.

Initializes: RiskRegistry, BusinessValidator, ActivityLogService,
AutonomyConfigService, ApprovalQueueService, ConfirmationProtocol,
MemoryService, FeedbackService, specialist agents, ExecutionPlanner,
AgentOrchestrator, autonomous agents via AgentScheduler, and agent ES indices.

Requirements: 1.1, 1.2, 7.6
"""
import logging
import os

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)

# Module-level references for shutdown.
_autonomous_agents = []
_agent_scheduler = None
_agent_redis_client = None
# Fuel-Ops Hardening (Task 12.1) — background services with their own
# lifecycles that live outside the AgentScheduler.
_storm_mode_evaluator = None
_integration_scheduler = None


# ---------------------------------------------------------------------------
# Fuel-Ops Hardening helpers (Task 12.1)
# ---------------------------------------------------------------------------


#: Overlay feature flags introduced by the fuel-ops hardening spec.
#: Every flag is seeded to ``disabled`` for new deployments so tenants
#: must explicitly opt in via the admin UI before the corresponding
#: capability becomes active. Integration Marketplace visibility is
#: controlled via ``overlay.integration.{provider_name}`` so every
#: connector surfaces to the UI only after the tenant enables it.
#: Req 10.2.2.
_FUEL_OPS_FEATURE_FLAG_DEFAULTS = (
    "overlay.weather_provider",
    "overlay.traffic_aware_routing",
    "overlay.bol_generation",
    "overlay.qbo_invoice_push",
    "overlay.stripe_autocharge",
    "overlay.rack_price_provider",
    "overlay.storm_trigger",
    "overlay.auto_storm_mode",
    "overlay.terminal_sourcing",
    "overlay.contamination_enforcement",
    "overlay.integration.quickbooks_online",
    "overlay.integration.veeder_root",
    "overlay.integration.geotab",
    "overlay.integration.stripe",
)


def _resolve_fuel_ops_settings(settings) -> dict:
    """Resolve the fuel-ops hardening platform settings.

    These values are not yet top-level pydantic fields on
    :class:`config.settings.Settings` (they are optional deployment
    parameters — a dev stack happily runs without S3 / KMS). The helper
    reads them from the process environment with a stable set of names
    so bootstrap wires real services when they are available and falls
    back to ``None`` (service skipped) otherwise.
    """

    return {
        "s3_bucket": os.environ.get("FUEL_OPS_S3_BUCKET"),
        "s3_region": (
            os.environ.get("FUEL_OPS_S3_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        ),
        "kms_key_id": os.environ.get("FUEL_OPS_KMS_KEY_ID"),
    }


async def _seed_fuel_ops_feature_flag_defaults(
    container,
    redis_client,
) -> None:
    """Seed every fuel-ops overlay feature flag to ``disabled`` by default.

    Uses the shared :class:`ops.services.feature_flags.FeatureFlagService`
    Redis key layout (``overlay_ff:{flag_key}:{tenant_id}``). We only
    set the key when it is absent so existing tenant overrides are
    preserved across redeploys. Missing Redis simply logs a warning.

    Validates: Requirement 10.2.2.
    """

    if redis_client is None:
        return

    # Feature flag defaults are keyed per tenant. Without a tenant
    # enumeration we seed a ``default`` placeholder so the
    # feature-flag admin UI sees the flags even before any tenant
    # opts in. Per-tenant seeding is handled by the migration
    # scripts referenced by Task 12.4.
    placeholder_tenant = "__default__"
    from ops.services.feature_flags import OVERLAY_PREFIX

    seeded = []
    for flag_key in _FUEL_OPS_FEATURE_FLAG_DEFAULTS:
        redis_key = f"{OVERLAY_PREFIX}{flag_key}:{placeholder_tenant}"
        try:
            # SET NX so we never clobber an existing default.
            existing = await redis_client.get(redis_key)
            if existing is None:
                await redis_client.set(redis_key, "disabled", ex=90 * 24 * 60 * 60)
                seeded.append(flag_key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to seed fuel-ops feature flag %s: %s",
                flag_key,
                exc,
            )

    if seeded:
        logger.info(
            "Seeded fuel-ops overlay feature flags (defaults=OFF): %s",
            ", ".join(seeded),
        )
    else:
        logger.debug(
            "Fuel-ops overlay feature flags already seeded; no changes made"
        )


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register all agentic AI services."""
    global _autonomous_agents, _agent_scheduler, _agent_redis_client
    global _storm_mode_evaluator, _integration_scheduler

    import redis.asyncio as aioredis

    from Agents.risk_registry import RiskRegistry
    from Agents.business_validator import BusinessValidator
    from Agents.activity_log_service import ActivityLogService
    from Agents.autonomy_config_service import AutonomyConfigService
    from Agents.approval_queue_service import ApprovalQueueService
    from Agents.confirmation_protocol import ConfirmationProtocol
    from Agents.memory_service import MemoryService
    from Agents.feedback_service import FeedbackService
    from Agents.tools.mutation_tools import configure_mutation_tools
    from Agents.agent_es_mappings import setup_agent_indices
    from Agents.specialists import (
        FleetAgent,
        SchedulingAgent,
        FuelAgent,
        OpsIntelligenceAgent,
        ReportingAgent,
    )
    from Agents.execution_planner import ExecutionPlanner
    from Agents.orchestrator import AgentOrchestrator
    from Agents.autonomous import (
        DelayResponseAgent,
        FuelManagementAgent,
        SLAGuardianAgent,
    )
    from Agents.agent_ws_manager import (
        AgentActivityWSManager,
        bind_container as bind_agent_ws,
    )
    from Agents.mainagent import configure_orchestrator
    from agent_endpoints import configure_agent_endpoints

    settings = container.settings
    es_service = container.es_service

    # ---- Startup Mapping Validation (Req 5.1, 5.5) ----
    try:
        from services.mapping_validator import MappingValidator

        mapping_validator = MappingValidator(es_service=es_service)
        drift_items = await mapping_validator.validate_all()
        await mapping_validator.remediate(drift_items)
    except Exception as exc:
        logger.error("Mapping validation failed (non-blocking): %s", exc)

    # Agent WebSocket manager
    agent_ws_manager = AgentActivityWSManager()
    container.agent_ws_manager = agent_ws_manager
    bind_agent_ws(container)

    # Redis client for agentic services
    redis_url = settings.redis_url or "redis://localhost:6379"
    _agent_redis_client = aioredis.from_url(redis_url, decode_responses=False)
    container.redis_client = _agent_redis_client
    logger.info("Agent Redis client connected")

    # ---- Fuel-Ops Hardening infrastructure (Task 12.1) ------------------
    # Build the shared platform services introduced by the fuel-ops
    # hardening spec before any downstream wiring that consumes them
    # (POD endpoints, fuel-ops endpoints, overlay agents). Services are
    # constructed with best-effort configuration: every dependency that
    # has not yet been wired (AWS credentials, S3 bucket, KMS key,
    # Textract client) falls back to ``None`` so the bootstrap still
    # runs in local-dev / CI environments. Downstream consumers already
    # tolerate ``None`` by degrading gracefully (see the test suites in
    # ``tests/unit/test_fuel_ops_*``).
    #
    # Req 10.2.1, 10.2.2.
    fuel_ops_settings = _resolve_fuel_ops_settings(settings)

    # 1. FuelProductCatalog is a module-level catalog — nothing to
    #    instantiate, but publish a reference on the container so
    #    downstream callers can retrieve it uniformly.
    try:
        from fuel.services import fuel_product_catalog as _fuel_product_catalog
        container.fuel_product_catalog = _fuel_product_catalog
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("FuelProductCatalog import failed: %s", exc)

    # 2. UnitConversion is a set of module-level helpers — no
    #    registration required, but expose on the container for
    #    symmetry with the other fuel-ops services.
    try:
        from services import unit_conversion as _unit_conversion
        container.unit_conversion = _unit_conversion
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("UnitConversion import failed: %s", exc)

    # 3. TenantCredentialsVault — AWS KMS-backed credential store.
    try:
        from services.credentials_vault import TenantCredentialsVault

        credentials_vault = TenantCredentialsVault(
            es_service=es_service,
            kms_key_id=fuel_ops_settings.get("kms_key_id"),
        )
        container.credentials_vault = credentials_vault
        logger.info("TenantCredentialsVault registered")
    except Exception as exc:
        credentials_vault = None
        logger.warning("TenantCredentialsVault wiring failed: %s", exc)

    # 4. FileStorageService — S3-backed object store with tenant
    #    prefixes and presigned URLs. Only constructed when a bucket is
    #    configured; local-dev / CI skip the S3 path entirely.
    file_storage_service = None
    try:
        from services.file_storage_service import FileStorageService

        _bucket = fuel_ops_settings.get("s3_bucket")
        _region = fuel_ops_settings.get("s3_region")
        if _bucket and _region:
            file_storage_service = FileStorageService(
                bucket=_bucket,
                region=_region,
            )
            container.file_storage_service = file_storage_service
            logger.info(
                "FileStorageService registered (bucket=%s region=%s)",
                _bucket,
                _region,
            )
        else:
            logger.info(
                "FileStorageService not registered — FUEL_OPS_S3_BUCKET / "
                "FUEL_OPS_S3_REGION not configured"
            )
    except Exception as exc:
        logger.warning("FileStorageService wiring failed: %s", exc)

    # 5. MeterTicketOCRService — AWS Textract wrapper. Requires a
    #    FileStorageService to fetch the meter-ticket bytes. When S3 is
    #    not configured, the OCR service is simply skipped.
    meter_ticket_ocr_service = None
    if file_storage_service is not None:
        try:
            from services.meter_ticket_ocr_service import MeterTicketOCRService

            meter_ticket_ocr_service = MeterTicketOCRService(
                file_storage=file_storage_service,
                es_service=es_service,
                redis_client=_agent_redis_client,
                region=fuel_ops_settings.get("s3_region") or "us-east-1",
            )
            container.meter_ticket_ocr_service = meter_ticket_ocr_service
            logger.info("MeterTicketOCRService registered")
        except Exception as exc:
            logger.warning("MeterTicketOCRService wiring failed: %s", exc)

    # 6. ReconciliationService — ordered/loaded/delivered variance
    #    persister. Always constructable because it only needs the ES
    #    service + optional Redis client.
    reconciliation_service = None
    try:
        from services.reconciliation_service import ReconciliationService

        reconciliation_service = ReconciliationService(
            es_service=es_service,
            redis_client=_agent_redis_client,
        )
        container.reconciliation_service = reconciliation_service
        logger.info("ReconciliationService registered")
    except Exception as exc:
        logger.warning("ReconciliationService wiring failed: %s", exc)

    # 7. BOLService — Bill-of-Lading generator. Requires both S3 and
    #    reportlab (imported lazily inside the service).
    bol_service = None
    if file_storage_service is not None:
        try:
            from services.bol_service import BOLService

            bol_service = BOLService(
                file_storage=file_storage_service,
                es_service=es_service,
            )
            container.bol_service = bol_service
            logger.info("BOLService registered")
        except Exception as exc:
            logger.warning("BOLService wiring failed: %s", exc)

    # 8. PodHashChainWriter — atomic hash-chain persistence for POD
    #    records. Expose on the container as ``pod_hash_chain_service``
    #    to match the task description (``services.pod_hash_chain`` is
    #    the pure-function helper module; the writer is the stateful
    #    service). The writer is always constructable.
    pod_hash_chain_writer = None
    try:
        from services.pod_hash_chain_writer import PodHashChainWriter

        pod_hash_chain_writer = PodHashChainWriter(
            es_service=es_service,
            redis_client=_agent_redis_client,
        )
        container.pod_hash_chain_service = pod_hash_chain_writer
        logger.info("PodHashChainWriter registered")
    except Exception as exc:
        logger.warning("PodHashChainWriter wiring failed: %s", exc)

    # 9. DeliveryDestinationService — unified reader over fuel_stations
    #    and customer_tanks.
    try:
        from fuel.services.delivery_destination_service import (
            DeliveryDestinationService,
        )

        delivery_destination_service = DeliveryDestinationService(
            es_service=es_service
        )
        container.delivery_destination_service = delivery_destination_service
        logger.info("DeliveryDestinationService registered")
    except Exception as exc:
        delivery_destination_service = None
        logger.warning("DeliveryDestinationService wiring failed: %s", exc)

    # ---- Fuel-Ops Hardening ES indices (Task 12.2 prerequisite) --------
    # Create the 21 new indices introduced by this spec. The helper is
    # idempotent — existing indices are left untouched.
    try:
        from fuel.services.fuel_ops_es_mappings import setup_fuel_ops_indices

        setup_fuel_ops_indices(es_service)
        logger.info("Fuel-ops ES indices ready")
    except Exception as exc:
        logger.warning("Fuel-ops ES index setup failed: %s", exc)

    # ---- Fuel-Ops feature-flag defaults (Task 12.1, 12.7) -------------
    # Seed every overlay feature flag introduced by this spec to
    # ``disabled`` for every existing tenant when the key does not
    # already exist, so freshly-deployed tenants pick up the new
    # capabilities only after an explicit opt-in.
    await _seed_fuel_ops_feature_flag_defaults(container, _agent_redis_client)


    # Core agent services (order matters — later services depend on earlier ones)
    risk_registry = RiskRegistry(redis_client=_agent_redis_client)
    container.risk_registry = risk_registry

    business_validator = BusinessValidator(es_service=es_service)
    container.business_validator = business_validator

    activity_log_service = ActivityLogService(
        es_service=es_service, ws_manager=agent_ws_manager
    )
    container.activity_log_service = activity_log_service

    autonomy_config_service = AutonomyConfigService(redis_client=_agent_redis_client)
    container.autonomy_config_service = autonomy_config_service

    # Tenant settings service (Region + measurement_units) — wired into the
    # tenant guard middleware so every request's TenantContext carries the
    # tenant's Region and display units.  Req 6.1.5, 6.3.1.
    from services.tenant_settings import TenantSettingsService
    from ops.middleware.tenant_guard import configure_tenant_guard

    tenant_settings_service = TenantSettingsService(redis_client=_agent_redis_client)
    container.tenant_settings_service = tenant_settings_service
    configure_tenant_guard(tenant_settings_service)
    logger.info("Tenant settings service wired into tenant guard")

    # Approval queue
    approval_queue_service = ApprovalQueueService(
        es_service=es_service,
        ws_manager=agent_ws_manager,
        activity_log_service=activity_log_service,
    )
    container.approval_queue_service = approval_queue_service

    # Confirmation protocol
    confirmation_protocol = ConfirmationProtocol(
        risk_registry=risk_registry,
        approval_queue_service=approval_queue_service,
        autonomy_config_service=autonomy_config_service,
        activity_log_service=activity_log_service,
        business_validator=business_validator,
        es_service=es_service,
        notification_service=container.notification_service if container.has("notification_service") else None,
    )
    container.confirmation_protocol = confirmation_protocol

    # Wire back-reference
    approval_queue_service._confirmation_protocol = confirmation_protocol

    # Memory and Feedback
    memory_service = MemoryService(es_service=es_service)
    container.memory_service = memory_service

    feedback_service = FeedbackService(es_service=es_service)
    container.feedback_service = feedback_service

    # Wire mutation tools
    configure_mutation_tools(confirmation_protocol, es_service)
    logger.info("Mutation tools configured")

    # Wire agent REST endpoints
    configure_agent_endpoints(
        approval_queue_service=approval_queue_service,
        activity_log_service=activity_log_service,
        autonomy_config_service=autonomy_config_service,
        memory_service=memory_service,
        feedback_service=feedback_service,
    )
    logger.info("Agent endpoints configured")

    # Specialist agents
    from strands.models.litellm import LiteLLMModel

    # Set the env var litellm reads for Gemini API key auth
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key
    specialist_model = LiteLLMModel(
        model_id="gemini/gemini-2.5-flash",
        client_args={
            "api_key": gemini_key,
        },
        params={
            "max_tokens": 8000,
            "temperature": 0.7,
        },
    )

    specialists = {
        "fleet": FleetAgent(model=specialist_model),
        "scheduling": SchedulingAgent(model=specialist_model),
        "fuel": FuelAgent(model=specialist_model),
        "ops": OpsIntelligenceAgent(model=specialist_model),
        "reporting": ReportingAgent(model=specialist_model),
    }
    logger.info("Specialist agents initialized")

    # Execution planner and orchestrator
    execution_planner = ExecutionPlanner(
        activity_log_service=activity_log_service,
        confirmation_protocol=confirmation_protocol,
    )
    agent_orchestrator = AgentOrchestrator(
        specialists=specialists,
        execution_planner=execution_planner,
        activity_log_service=activity_log_service,
    )
    container.agent_orchestrator = agent_orchestrator
    logger.info("Agent orchestrator initialized")

    # Autonomous agents — managed by AgentScheduler (Req 7.6)
    from bootstrap.agent_scheduler import AgentScheduler, RestartPolicy

    ops_feature_flags = container.ops_feature_flags

    delay_response_agent = DelayResponseAgent(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
    )
    fuel_management_agent = FuelManagementAgent(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
    )
    sla_guardian_agent = SLAGuardianAgent(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
    )

    # Create AgentScheduler and register agents with restart policies
    telemetry_service = container.get("telemetry_service") if container.has("telemetry_service") else None
    scheduler = AgentScheduler(
        telemetry_service=telemetry_service,
        activity_log_service=activity_log_service,
        shutdown_timeout=10.0,
    )
    scheduler.register(delay_response_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(fuel_management_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(sla_guardian_agent, RestartPolicy.ALWAYS)

    await scheduler.start_all()
    _agent_scheduler = scheduler
    container.agent_scheduler = scheduler

    _autonomous_agents = [
        delay_response_agent,
        fuel_management_agent,
        sla_guardian_agent,
    ]
    logger.info("Autonomous agents started via AgentScheduler")

    # Store references on app.state for health/pause/resume endpoints
    app.state.autonomous_agents = {
        "delay_response_agent": delay_response_agent,
        "fuel_management_agent": fuel_management_agent,
        "sla_guardian_agent": sla_guardian_agent,
    }
    app.state.agent_orchestrator = agent_orchestrator

    # Wire orchestrator into mainagent for multi-agent routing
    configure_orchestrator(agent_orchestrator)

    # Set up agent ES indices
    setup_agent_indices(es_service)
    logger.info("Agent ES indices ready")

    # ---- Overlay Infrastructure (Phase 2) ----
    # Imports inside function to avoid circular imports
    from Agents.overlay.signal_bus import SignalBus
    from Agents.overlay.outcome_tracker import OutcomeTracker
    from Agents.overlay.overlay_es_mappings import setup_overlay_indices
    from Agents.overlay.dispatch_optimizer import DispatchOptimizer
    from Agents.overlay.exception_commander import ExceptionCommander
    from Agents.overlay.revenue_guard import RevenueGuard
    from Agents.overlay.customer_promise import CustomerPromise
    from Agents.overlay.learning_policy_agent import LearningPolicyAgent
    from Agents.overlay.driver_nudge_agent import DriverNudgeAgent

    # Create SignalBus wired to ES (Req 2.1)
    signal_bus = SignalBus(es_service=es_service)
    container.signal_bus = signal_bus
    logger.info("SignalBus initialized")

    # Create OutcomeTracker wired to SignalBus and ES (Req 11.1)
    outcome_tracker = OutcomeTracker(
        signal_bus=signal_bus,
        es_service=es_service,
    )
    container.outcome_tracker = outcome_tracker

    # Wire Layer 0 agents to publish RiskSignals (Req 2.2)
    for agent_name, agent in app.state.autonomous_agents.items():
        agent._signal_bus = signal_bus

    # Set up overlay ES indices
    setup_overlay_indices(es_service)
    logger.info("Overlay ES indices ready")

    # Shared dependencies for overlay agents (Req 10.1, 10.4)
    overlay_common_args = dict(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=autonomy_config_service,
        feature_flag_service=ops_feature_flags,
    )

    # Instantiate overlay agents
    dispatch_optimizer = DispatchOptimizer(
        **overlay_common_args,
        execution_planner=execution_planner,
    )
    exception_commander = ExceptionCommander(**overlay_common_args)
    revenue_guard = RevenueGuard(**overlay_common_args)
    customer_promise = CustomerPromise(**overlay_common_args)
    learning_policy_agent = LearningPolicyAgent(
        **overlay_common_args,
        feedback_service=feedback_service,
    )

    # Driver Nudge Agent — monitors unacknowledged assignments (Req 15.1–15.4)
    driver_nudge_agent = DriverNudgeAgent(**overlay_common_args)

    # Register with scheduler — Layer 1 first, then Layer 2 (Req 10.2)
    scheduler.register(dispatch_optimizer, RestartPolicy.ON_FAILURE)
    scheduler.register(exception_commander, RestartPolicy.ON_FAILURE)
    scheduler.register(revenue_guard, RestartPolicy.ON_FAILURE)
    scheduler.register(customer_promise, RestartPolicy.ON_FAILURE)
    scheduler.register(driver_nudge_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(learning_policy_agent, RestartPolicy.ON_FAILURE)

    # Start overlay agents (only newly registered ones)
    await scheduler.start_all()
    logger.info("Overlay agents started via AgentScheduler")

    # Store overlay references on app.state (Req 10.8)
    app.state.overlay_agents = {
        "dispatch_optimizer": dispatch_optimizer,
        "exception_commander": exception_commander,
        "revenue_guard": revenue_guard,
        "customer_promise": customer_promise,
        "driver_nudge_agent": driver_nudge_agent,
        "learning_policy_agent": learning_policy_agent,
    }

    # ---- Fuel Distribution MVP Agents (Phase 3) ----
    from Agents.overlay.tank_forecasting_agent import TankForecastingAgent
    from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
    from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
    from Agents.overlay.route_planning_agent import RoutePlanningAgent
    from Agents.overlay.exception_replanning_agent import ExceptionReplanningAgent
    from Agents.support.mvp_es_mappings import setup_mvp_indices
    from Agents.support.fuel_distribution_pipeline import FuelDistributionPipeline

    # Set up MVP ES indices (Req 7.9)
    setup_mvp_indices(es_service)
    logger.info("MVP ES indices ready")

    # Instantiate MVP agents with shared dependencies (Req 11.1–11.6)
    tank_forecasting_agent = TankForecastingAgent(**overlay_common_args)
    delivery_prioritization_agent = DeliveryPrioritizationAgent(
        **overlay_common_args,
        redis_client=_agent_redis_client,
    )
    compartment_loading_agent = CompartmentLoadingAgent(**overlay_common_args)
    route_planning_agent = RoutePlanningAgent(**overlay_common_args)
    exception_replanning_agent = ExceptionReplanningAgent(**overlay_common_args)

    # Register MVP agents with AgentScheduler (Req 11.2)
    scheduler.register(tank_forecasting_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(delivery_prioritization_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(compartment_loading_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(route_planning_agent, RestartPolicy.ON_FAILURE)
    scheduler.register(exception_replanning_agent, RestartPolicy.ON_FAILURE)

    # Start MVP agents (only newly registered ones)
    await scheduler.start_all()
    logger.info("MVP agents started via AgentScheduler")

    # Store MVP agent references on app.state
    app.state.mvp_agents = {
        "tank_forecasting": tank_forecasting_agent,
        "delivery_prioritization": delivery_prioritization_agent,
        "compartment_loading": compartment_loading_agent,
        "route_planning": route_planning_agent,
        "exception_replanning": exception_replanning_agent,
    }

    # Create FuelDistributionPipeline instance (Req 6.1–6.6)
    mvp_pipeline = FuelDistributionPipeline(
        agents=app.state.mvp_agents,
        ws_manager=agent_ws_manager,
        signal_bus=signal_bus,
    )
    app.state.mvp_pipeline = mvp_pipeline
    logger.info("FuelDistributionPipeline initialized")

    # Wire MVP REST endpoints (Req 8.1–8.6)
    from Agents.support.mvp_endpoints import configure_mvp_endpoints, router as mvp_router
    from fuel.services.fleet_registration_service import FleetRegistrationService

    # Create FleetRegistrationService for fuel tanker fleet integration (Req 6.1, 6.3)
    fleet_registration_service = FleetRegistrationService(es_service=es_service)

    # Create PlanExecutionService and get WebSocket manager (Req 3.1–3.9, 4.1–4.7, 5.1–5.6)
    from Agents.support.plan_execution_service import PlanExecutionService
    from Agents.support.plan_execution_ws_manager import get_plan_execution_ws_manager

    plan_execution_service = PlanExecutionService(es_service=es_service)
    plan_execution_ws_manager = get_plan_execution_ws_manager()

    configure_mvp_endpoints(
        pipeline=mvp_pipeline,
        es_service=es_service,
        exception_replanning_agent=exception_replanning_agent,
        fleet_registration_service=fleet_registration_service,
        plan_execution_service=plan_execution_service,
        plan_execution_ws_manager=plan_execution_ws_manager,
    )
    app.include_router(mvp_router)
    logger.info("MVP endpoints configured and router registered")

    # ---- Fuel Ops Hardening endpoints (Phase 3 Task 3.6 et al.) ----
    # Register the fuel-domain router that owns the customer-tanks CRUD
    # surface (Req 1.6.2, 1.6.3) and the destination/product endpoints
    # added in Capability 6. Wired here so the Customer_Tank repository
    # shares the application-scoped ES service.
    from fuel.api.fuel_ops_endpoints import (
        configure_fuel_ops_endpoints,
        router as fuel_ops_router,
        mvp_router as fuel_ops_mvp_router,
    )

    configure_fuel_ops_endpoints(es_service=es_service)
    app.include_router(fuel_ops_router)
    app.include_router(fuel_ops_mvp_router)
    logger.info("Fuel-ops endpoints configured and routers registered")

    # Wire the fuel-planning WebSocket manager into the Tank Forecasting
    # Agent so it emits ``customer_tank_forecast_ready`` (Req 1.6.4) on
    # ``/ws/fuel-planning`` whenever a per-tank forecast completes.
    from fuel.services.fuel_planning_ws_manager import (
        get_fuel_planning_ws_manager,
    )
    fuel_planning_ws_manager = get_fuel_planning_ws_manager()
    container.fuel_planning_ws_manager = fuel_planning_ws_manager
    tank_forecasting_agent.set_fuel_planning_ws_manager(fuel_planning_ws_manager)
    # Task 4.10: wire the same manager into the Exception_Replanning_Agent
    # so every replan broadcasts ``replan_diff_ready`` on /ws/fuel-planning
    # (Req 2.5.4) alongside persisting the structured diff to
    # ``mvp_replan_events``.
    exception_replanning_agent.set_fuel_planning_ws_manager(
        fuel_planning_ws_manager
    )
    logger.info(
        "Fuel-planning WebSocket manager wired into Tank_Forecasting_Agent"
        " and Exception_Replanning_Agent"
    )

    # Task 14.4 (Req 12.5): Wire NotificationService into the
    # TankForecastingAgent so it fires ``low_tank_autofill_alert`` when a
    # customer tank's predicted level drops below the reorder point.
    if container.has("notification_service"):
        tank_forecasting_agent.set_notification_service(
            container.notification_service
        )
        logger.info(
            "NotificationService wired into Tank_Forecasting_Agent for "
            "low_tank_autofill_alert (Req 12.5)"
        )

    # Task 4.9: re-wire fuel-ops endpoints with ConfirmationProtocol and
    # the fuel-planning WS manager now that both are constructed; the
    # POST /api/fuel/mvp/routes/{route_id}/emergency-stop handler needs
    # both to route the patched route through risk classification
    # (Req 2.4.5) and broadcast ``emergency_stop_inserted`` (Req 2.4.6).
    #
    # Task 7.7: the wait-summary endpoint caches the rolling 2-hour
    # average at ``terminal_wait:{tenant_id}:{terminal_id}`` in Redis so
    # the Sourcing_Recommender (Task 7.9) can read it in O(1). Passing
    # the shared ``_agent_redis_client`` wires that cache path. When
    # Redis is unavailable the endpoint falls back to a direct ES
    # aggregation, so a Redis outage never breaks the endpoint.
    #
    # Task 7.10: build the :class:`SourcingRecommender` singleton with
    # its full dependency set (terminal + contract repositories, rack-
    # price provider, wait-time resolver, rack-price sync service) and
    # pass it through to both the sourcing endpoint and the
    # Route_Planning_Agent. Every dependency is best-effort: if the
    # rack-price CSV loader has not been configured yet (no tenant has
    # uploaded a CSV) the provider returns an empty list and the
    # recommender surfaces ``no_price_available``. The endpoint still
    # returns HTTP 503 when this block raises so a misconfigured
    # bootstrap fails loudly rather than silently skipping the
    # recommender (Req 8.5.4 / 8.5.5).
    sourcing_recommender = None
    try:
        from fuel.terminal_models import (
            SupplierContractRepository,
            TerminalRepository,
            TerminalWaitReportRepository,
        )
        from fuel.services.terminal_wait_resolver import (
            build_wait_time_resolver,
        )
        from integrations.rack_price_sync import RackPriceSyncService

        sourcing_terminal_repo = TerminalRepository(es_service=es_service)
        sourcing_contract_repo = SupplierContractRepository(
            es_service=es_service
        )
        sourcing_wait_report_repo = TerminalWaitReportRepository(
            es_service=es_service
        )
        sourcing_wait_resolver = build_wait_time_resolver(
            redis_client=_agent_redis_client,
            wait_report_repository=sourcing_wait_report_repo,
        )

        # Rack-price provider: default to the CSV-backed fallback
        # adapter. The CSV loader is a no-op async callable until a
        # tenant uploads a rack sheet via the admin UI; the provider
        # safely degrades to an empty candidate set when no CSV is
        # present, which the recommender surfaces as
        # ``no_price_available``. Swap in ``OPISRackPriceProvider`` here
        # once the tenant has subscribed.
        from integrations.rack_price_provider_base import (
            CSVFallbackRackPriceProvider,
        )

        async def _noop_csv_loader(tenant_id: str) -> bytes:
            raise FileNotFoundError(
                f"no rack-price CSV configured for tenant {tenant_id!r}"
            )

        sourcing_rack_provider = CSVFallbackRackPriceProvider(
            csv_loader=_noop_csv_loader,
            redis_client=_agent_redis_client,
        )
        sourcing_rack_sync = RackPriceSyncService(es_service=es_service)

        # Tenant-config handle — the recommender uses the same minimal
        # ``async get(key)`` contract as the Route_Planning_Agent's
        # traffic-provider lookup, so the shared Redis client suffices.
        class _RedisTenantConfig:
            def __init__(self, client):
                self._client = client

            async def get(self, key: str):
                if self._client is None:
                    return None
                try:
                    return await self._client.get(key)
                except Exception as exc:
                    logger.warning(
                        "Sourcing tenant-config Redis read failed for key=%s: %s",
                        key,
                        exc,
                    )
                    return None

        sourcing_tenant_config = _RedisTenantConfig(_agent_redis_client)

        from fuel.services.sourcing_recommender import SourcingRecommender

        sourcing_recommender = SourcingRecommender(
            terminal_repo=sourcing_terminal_repo,
            contract_repo=sourcing_contract_repo,
            rack_price_provider=sourcing_rack_provider,
            wait_time_resolver=sourcing_wait_resolver,
            tenant_config=sourcing_tenant_config,
            rack_price_sync=sourcing_rack_sync,
        )
        logger.info("SourcingRecommender singleton constructed")
    except Exception as exc:
        logger.exception(
            "SourcingRecommender construction failed — "
            "/api/fuel/sourcing/recommendations will surface HTTP 503 "
            "until bootstrap is fixed: %s",
            exc,
        )
        sourcing_recommender = None

    configure_fuel_ops_endpoints(
        es_service=es_service,
        confirmation_protocol=confirmation_protocol,
        fuel_planning_ws_manager=fuel_planning_ws_manager,
        redis_client=_agent_redis_client,
        sourcing_recommender=sourcing_recommender,
        file_storage_service=file_storage_service,
        destination_service=delivery_destination_service,
        storm_mode_evaluator=None,  # populated further below
    )

    # Task 7.10: inject the same recommender into the Route_Planning_Agent
    # so Loading_Plans that carry an external ``terminal_id`` stamp the
    # chosen terminal id + reasons on the persisted Route_Plan
    # (Req 8.5.5). Safe to call with ``None`` — the agent then skips
    # the sourcing step on every evaluation.
    route_planning_agent.set_sourcing_recommender(sourcing_recommender)

    # ---- Inventory Pipeline Integration ----
    from Agents.autonomous.inventory_monitor import InventoryMonitorAgent

    # L0: Inventory Monitor Agent (Req 2.1, 2.7)
    inventory_monitor_agent = InventoryMonitorAgent(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
        signal_bus=signal_bus,
    )
    scheduler.register(inventory_monitor_agent, RestartPolicy.ON_FAILURE)
    await scheduler.start_all()
    logger.info("InventoryMonitorAgent started via AgentScheduler")

    # Add to autonomous_agents dict for health/pause/resume endpoints
    app.state.autonomous_agents["inventory_monitor_agent"] = inventory_monitor_agent

    # Add to module-level fallback list
    _autonomous_agents.append(inventory_monitor_agent)

    # ---- Inventory Pipeline Integration — Service Wiring (Req 1.1, 5.1, 6.1) ----
    from inventory.asset_readiness import AssetReadinessChecker
    from inventory.driver_exception_handler import DriverExceptionHandler

    inventory_service = container.inventory_service if container.has("inventory_service") else None
    tenant_inventory_config = container.tenant_inventory_config if container.has("tenant_inventory_config") else None

    # AssetReadinessChecker — verifies critical parts before truck assignment
    asset_readiness_checker = AssetReadinessChecker(
        es_service=es_service,
        tenant_config_service=tenant_inventory_config,
    )

    # DriverExceptionHandler — handles inventory lookup for driver exceptions
    driver_exception_handler = DriverExceptionHandler(
        es_service=es_service,
        inventory_service=inventory_service,
        signal_bus=signal_bus,
    )
    container.driver_exception_handler = driver_exception_handler

    # Wire readiness checker into JobService (Req 1.1)
    job_service = container.job_service
    job_service.set_readiness_checker(asset_readiness_checker)

    # Wire inventory service into JobService for auto-consume (Req 5.1)
    job_service.set_inventory_service(inventory_service)

    # Wire tenant inventory config into JobService for tenant settings
    job_service.set_tenant_inventory_config(tenant_inventory_config)

    # Wire inventory service into ExceptionReplanningAgent (Req 4.5, 6.1)
    exception_replanning_agent.set_inventory_service(inventory_service)

    logger.info("Inventory pipeline services wired into JobService and ExceptionReplanningAgent")

    # ---- Cross-Domain Integration Agents ----
    from Agents.autonomous.truck_fuel_monitor import TruckFuelMonitor
    from Agents.autonomous.job_sla_monitor import JobSLAMonitor
    from Agents.overlay.job_priority_engine import JobPriorityEngine
    from scheduling.services.job_reroute_service import JobRerouteService

    # L0: Truck Fuel Monitor (Req 1.1)
    truck_fuel_monitor = TruckFuelMonitor(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
        signal_bus=signal_bus,
    )
    scheduler.register(truck_fuel_monitor, RestartPolicy.ON_FAILURE)

    # L0: Job SLA Monitor (Req 4.1)
    job_sla_monitor = JobSLAMonitor(
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=agent_ws_manager,
        confirmation_protocol=confirmation_protocol,
        feature_flag_service=ops_feature_flags,
        signal_bus=signal_bus,
    )
    scheduler.register(job_sla_monitor, RestartPolicy.ON_FAILURE)

    # L1: Job Priority Engine (Req 3.1)
    job_priority_engine = JobPriorityEngine(**overlay_common_args)
    scheduler.register(job_priority_engine, RestartPolicy.ON_FAILURE)

    # Wire delay_response_agent SignalBus explicitly (Req 5.7)
    # Note: already set by the Layer 0 loop above, but explicit for traceability
    delay_response_agent._signal_bus = signal_bus

    # Job Reroute Service (Req 2.1)
    job_reroute_service = JobRerouteService(
        es_service=es_service,
        confirmation_protocol=confirmation_protocol,
    )

    # Start cross-domain agents (only newly registered ones)
    await scheduler.start_all()
    logger.info("Cross-domain integration agents started via AgentScheduler")

    # Store cross-domain agent references on app.state
    app.state.cross_domain_agents = {
        "truck_fuel_monitor": truck_fuel_monitor,
        "job_sla_monitor": job_sla_monitor,
        "job_priority_engine": job_priority_engine,
        "job_reroute_service": job_reroute_service,
    }

    # Wire reroute REST routes (Req 2.8)
    from scheduling.routes.job_reroute_routes import (
        router as job_reroute_router,
        configure_job_reroute_routes,
    )

    configure_job_reroute_routes(job_reroute_service=job_reroute_service)
    app.include_router(job_reroute_router)
    logger.info("Cross-domain integration wiring complete")

    # ---- Fuel-Ops Hardening autonomous services (Task 12.1) -----------
    # Register the new autonomous agents and background services
    # introduced by the fuel-ops hardening spec. Each piece is
    # constructed best-effort: a missing dependency logs a warning and
    # leaves the feature disabled rather than breaking the boot.

    # WeatherAlertIngester (Task 10.2, Req 9.1.1 / 9.1.2) — 5-minute
    # NOAA/NWS poller. Registered with the AgentScheduler so lifecycle
    # tracking (restart, SLO) is handled alongside the other L0 agents.
    try:
        from Agents.autonomous.weather_alert_ingester import WeatherAlertIngester

        weather_alert_ingester = WeatherAlertIngester(
            es_service=es_service,
            activity_log_service=activity_log_service,
            ws_manager=agent_ws_manager,
            confirmation_protocol=confirmation_protocol,
            signal_bus=signal_bus,
            feature_flag_service=ops_feature_flags,
        )
        scheduler.register(weather_alert_ingester, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()
        app.state.autonomous_agents["weather_alert_ingester"] = (
            weather_alert_ingester
        )
        _autonomous_agents.append(weather_alert_ingester)
        container.weather_alert_ingester = weather_alert_ingester
        logger.info("WeatherAlertIngester started via AgentScheduler")
    except Exception as exc:
        logger.warning("WeatherAlertIngester wiring failed: %s", exc)

    # StormModeEvaluator (Task 10.3, Req 9.1.3–9.1.5) — runs its own
    # poll loop rather than living in the AgentScheduler because
    # downstream consumers (Delivery_Prioritization_Agent,
    # Route_Planning_Agent, notification resolver) query its state via
    # ``get_state(tenant_id)`` rather than via a SignalBus subscription.
    storm_mode_evaluator = None
    try:
        from fuel.services.storm_mode_evaluator import StormModeEvaluator

        storm_mode_evaluator = StormModeEvaluator(
            es_service=es_service,
            signal_bus=signal_bus,
            redis_client=_agent_redis_client,
        )
        await storm_mode_evaluator.start()
        _storm_mode_evaluator = storm_mode_evaluator
        container.storm_mode_evaluator = storm_mode_evaluator
        app.state.storm_mode_evaluator = storm_mode_evaluator
        logger.info("StormModeEvaluator started")

        # Back-wire the evaluator into the Storm_Mode notification
        # resolver that was constructed by the notifications bootstrap
        # (Task 10.9). When the resolver was created without a state
        # provider it logged a warning — re-set it now so severe-
        # weather templates start firing.
        if container.has("storm_notification_resolver"):
            resolver = container.storm_notification_resolver
            if hasattr(resolver, "_state_provider"):
                resolver._state_provider = storm_mode_evaluator
                logger.info(
                    "Storm_Mode notification resolver re-wired with active"
                    " StormModeEvaluator"
                )

        # Inject the evaluator into the overlay agents that gate
        # behaviour on Storm_Mode. Both setters tolerate ``None`` so a
        # missing evaluator keeps the pre-storm behaviour.
        if hasattr(route_planning_agent, "set_storm_mode_evaluator"):
            route_planning_agent.set_storm_mode_evaluator(storm_mode_evaluator)
        if hasattr(delivery_prioritization_agent, "_storm_mode_evaluator"):
            # The agent accepts the evaluator via its constructor; no
            # public setter exists, so wire the private attribute
            # (mirrors the pattern used for ``_signal_bus`` above).
            delivery_prioritization_agent._storm_mode_evaluator = (
                storm_mode_evaluator
            )
    except Exception as exc:
        logger.warning("StormModeEvaluator wiring failed: %s", exc)

    # IntegrationScheduler (Task 9.2, Req 5.1.5 / 5.1.6) — APScheduler-
    # backed cron orchestrator for every tenant's integration
    # instances. Constructed only when its mandatory dependencies are
    # available; a missing credentials vault or APScheduler install
    # logs a warning rather than failing the boot so development
    # tenants can keep running without integrations.
    integration_scheduler = None
    try:
        from integrations.connector_base import IntegrationInstanceRepository
        from integrations.integration_scheduler import IntegrationScheduler

        integration_instance_repository = IntegrationInstanceRepository(
            es_service=es_service
        )
        container.integration_instance_repository = (
            integration_instance_repository
        )

        # ConnectorFactory is populated lazily once we register
        # provider-specific factories. Until then the scheduler is
        # constructed but not started, so no ticks fire. A future
        # integration-bootstrap module can replace this stub with a
        # real factory.
        async def _placeholder_connector_factory(instance):
            raise RuntimeError(
                "IntegrationScheduler connector_factory is not configured; "
                "install the per-provider factory via the integrations "
                "bootstrap"
            )

        integration_scheduler = IntegrationScheduler(
            repository=integration_instance_repository,
            es_service=es_service,
            connector_factory=_placeholder_connector_factory,
            signal_bus=signal_bus,
        )
        container.integration_scheduler = integration_scheduler
        _integration_scheduler = integration_scheduler
        logger.info(
            "IntegrationScheduler registered (connector_factory pending)"
        )

        # Register the built-in connector catalog entries so
        # GET /api/integrations/providers returns the full list
        # (Task 9.10, Req 5.6.2 / 5.6.6). Each entry defaults to
        # ``overlay.integration.{provider_name}`` for Marketplace
        # visibility.
        try:
            from integrations.provider_registry import register_all_providers

            register_all_providers()
            logger.info("Integration provider catalog registered")
        except Exception as exc:
            logger.warning("Integration provider catalog wiring failed: %s", exc)
    except Exception as exc:
        logger.warning("IntegrationScheduler wiring failed: %s", exc)

    # ---- Integrations REST router wiring (Task 12.3, Req 5.1.7 / 5.1.8) ----
    # The ``integrations_router`` is mounted in ``main.py`` at import
    # time. Its handlers raise HTTP 500 until
    # :func:`configure_integrations_endpoints` installs the repository,
    # scheduler, and credentials vault references. Call it here so the
    # full dependency graph (vault, repository, scheduler, ES) is
    # available. Missing dependencies log a warning rather than
    # breaking boot — dev environments without KMS can still run.
    try:
        from integrations.api.integrations_endpoints import (
            configure_integrations_endpoints,
        )

        if (
            integration_scheduler is not None
            and container.has("integration_instance_repository")
        ):
            configure_integrations_endpoints(
                repository=container.integration_instance_repository,
                scheduler=integration_scheduler,
                credentials_vault=credentials_vault,
                es_service=es_service,
            )
            logger.info("Integrations REST endpoints configured")
        else:
            logger.warning(
                "Integrations REST endpoints NOT configured "
                "(scheduler=%s, repository=%s); "
                "/api/integrations routes will return 500 until bootstrap"
                " finishes wiring",
                integration_scheduler is not None,
                container.has("integration_instance_repository"),
            )
    except Exception as exc:
        logger.warning(
            "configure_integrations_endpoints() failed: %s", exc
        )

    # ---- Stripe REST + webhook router wiring (Task 12.3, Req 5.5.2 / 5.5.4) ----
    # The Stripe endpoints module exposes two routers (tenant-scoped
    # ``/api/integrations/stripe/*`` and the unauthenticated
    # ``/webhooks/stripe/{tenant_id}``) and needs an async
    # ``connector_factory(tenant_id) -> Optional[StripeConnector]`` so
    # every request resolves a fresh connector bound to the caller's
    # tenant credentials. The factory looks up the tenant's Stripe
    # IntegrationInstance via the repository, short-circuits to
    # ``None`` when no Stripe integration is configured (the endpoint
    # then returns HTTP 404), and otherwise hands back a
    # :class:`StripeConnector` wired with the shared vault,
    # reconciliation_service, feature_flag_service, confirmation
    # protocol, Redis client, and ES service.
    try:
        from integrations.api.stripe_endpoints import (
            configure_stripe_endpoints,
        )
        from integrations.stripe_connector import StripeConnector

        _stripe_repository = (
            container.integration_instance_repository
            if container.has("integration_instance_repository")
            else None
        )

        async def _stripe_connector_factory(tenant_id: str):
            """Resolve the Stripe connector for ``tenant_id``.

            Returns ``None`` when the tenant has no active Stripe
            integration instance so the endpoints surface HTTP 404
            ``stripe_integration_not_configured`` uniformly.
            """

            if _stripe_repository is None or credentials_vault is None:
                return None
            try:
                instances = await _stripe_repository.list_for_tenant(
                    tenant_id=tenant_id,
                    provider_name=StripeConnector.provider_name,
                    enabled=None,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Stripe connector factory: repository lookup failed "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return None

            if not instances:
                return None
            # Prefer an enabled instance; fall back to the first record
            # so a disabled integration still serves webhooks (Stripe
            # will keep delivering events until the operator removes
            # the endpoint from their dashboard).
            instance = next(
                (i for i in instances if i.enabled), instances[0]
            )

            return StripeConnector(
                tenant_id=tenant_id,
                instance_id=instance.instance_id,
                credentials_vault=credentials_vault,
                credentials_ref=instance.credentials_ref,
                reconciliation_service=reconciliation_service,
                feature_flag_service=ops_feature_flags,
                confirmation_protocol=confirmation_protocol,
                redis_client=_agent_redis_client,
                es_service=es_service,
            )

        configure_stripe_endpoints(
            connector_factory=_stripe_connector_factory,
        )
        logger.info(
            "Stripe REST endpoints + webhook router configured "
            "(connector factory ready)"
        )
    except Exception as exc:
        logger.warning(
            "configure_stripe_endpoints() failed: %s", exc
        )

    # Re-wire POD endpoints with the fuel-ops services now that they
    # exist on the container. ``bootstrap.scheduling.configure_pod_endpoints``
    # fires earlier in the boot order and does not know about
    # file_storage_service, pod_bol_finalizer, or pod_hash_chain_writer;
    # calling configure_pod_endpoints again here overlays those refs
    # without disturbing the existing wiring.
    pod_bol_finalizer = None
    try:
        if bol_service is not None:
            from driver.services.pod_bol_finalizer import PODBOLFinalizer

            pod_bol_finalizer = PODBOLFinalizer(
                bol_service=bol_service,
                es_service=es_service,
                feature_flag_service=ops_feature_flags,
            )
            container.pod_bol_finalizer = pod_bol_finalizer
            logger.info("PODBOLFinalizer registered")
    except Exception as exc:
        logger.warning("PODBOLFinalizer wiring failed: %s", exc)

    try:
        from driver.api.pod_endpoints import configure_pod_endpoints

        configure_pod_endpoints(
            es_service=es_service,
            job_service=container.job_service
                if container.has("job_service") else None,
            scheduling_ws_manager=container.scheduling_ws_manager
                if container.has("scheduling_ws_manager") else None,
            driver_ws_manager=container.driver_ws_manager
                if container.has("driver_ws_manager") else None,
            file_storage_service=file_storage_service,
            redis_client=_agent_redis_client,
            pod_bol_finalizer=pod_bol_finalizer,
            ocr_service=meter_ticket_ocr_service,
            pod_hash_chain_writer=pod_hash_chain_writer,
        )
        logger.info(
            "POD endpoints re-wired with fuel-ops services "
            "(file_storage=%s, ocr=%s, bol=%s, hash_chain=%s)",
            file_storage_service is not None,
            meter_ticket_ocr_service is not None,
            pod_bol_finalizer is not None,
            pod_hash_chain_writer is not None,
        )
    except Exception as exc:
        logger.warning("POD endpoint re-wiring failed: %s", exc)

    # Re-wire the fuel-ops endpoints one more time now that the
    # StormModeEvaluator exists so GET /api/fuel/storm-mode/status can
    # resolve state.
    if storm_mode_evaluator is not None:
        try:
            configure_fuel_ops_endpoints(
                es_service=es_service,
                confirmation_protocol=confirmation_protocol,
                fuel_planning_ws_manager=fuel_planning_ws_manager,
                redis_client=_agent_redis_client,
                sourcing_recommender=sourcing_recommender,
                file_storage_service=file_storage_service,
                destination_service=delivery_destination_service,
                storm_mode_evaluator=storm_mode_evaluator,
            )
            logger.info(
                "Fuel-ops endpoints re-wired with StormModeEvaluator"
            )
        except Exception as exc:
            logger.warning(
                "Fuel-ops endpoint Storm_Mode re-wire failed: %s", exc
            )

    logger.info("Fuel-ops hardening bootstrap complete")


async def shutdown(app, container: ServiceContainer) -> None:
    """Stop agents in order: L2 → L1 → L0, then close resources (Req 10.5)."""
    global _autonomous_agents, _agent_scheduler, _agent_redis_client
    global _storm_mode_evaluator, _integration_scheduler

    # Stop the fuel-ops hardening services FIRST (before the
    # AgentScheduler) so any in-flight tick cannot observe a
    # partially-torn-down container. ``StormModeEvaluator`` and
    # ``IntegrationScheduler`` each own their own asyncio task.
    if _integration_scheduler is not None:
        try:
            await _integration_scheduler.shutdown(wait=False)
            logger.info("IntegrationScheduler stopped")
        except Exception as exc:
            logger.warning("IntegrationScheduler shutdown failed: %s", exc)
        _integration_scheduler = None

    if _storm_mode_evaluator is not None:
        try:
            await _storm_mode_evaluator.stop()
            logger.info("StormModeEvaluator stopped")
        except Exception as exc:
            logger.warning("StormModeEvaluator shutdown failed: %s", exc)
        _storm_mode_evaluator = None

    if _agent_scheduler is not None:
        try:
            # Ordered shutdown: MVP → L2 → L1 → L0 to prevent signal consumption
            # from stopped producers (Req 10.5)
            mvp_agents = [
                "tank_forecasting", "delivery_prioritization",
                "compartment_loading", "route_planning",
                "exception_replanning",
            ]
            l2_agents = ["learning_policy_agent"]
            l1_agents = [
                "job_priority_engine",
                "dispatch_optimizer", "exception_commander",
                "revenue_guard", "customer_promise",
            ]
            l0_agents = [
                "weather_alert_ingester",
                "inventory_monitor", "truck_fuel_monitor", "job_sla_monitor",
                "delay_response_agent", "fuel_management_agent",
                "sla_guardian_agent",
            ]

            for layer_name, agent_ids in [
                ("MVP", mvp_agents),
                ("L2", l2_agents),
                ("L1", l1_agents),
                ("L0", l0_agents),
            ]:
                for agent_id in agent_ids:
                    state = _agent_scheduler._agents.get(agent_id)
                    if state:
                        try:
                            await _agent_scheduler._stop_agent(state)
                        except Exception as exc:
                            logger.error(
                                "Error stopping %s agent %s: %s",
                                layer_name, agent_id, exc,
                            )
                logger.info("Stopped %s agents", layer_name)

            logger.info("AgentScheduler stopped all agents (L2 → L1 → L0)")
        except Exception as exc:
            logger.error("AgentScheduler ordered shutdown error: %s", exc)
            # Fallback: stop all agents at once
            try:
                await _agent_scheduler.stop_all()
            except Exception as fallback_exc:
                logger.exception(
                    "AgentScheduler fallback stop_all failed: %s",
                    fallback_exc,
                )
    else:
        # Fallback if scheduler was never created
        for agent in _autonomous_agents:
            try:
                await agent.stop()
                logger.info("Stopped autonomous agent: %s", agent.agent_id)
            except Exception as exc:
                logger.exception(
                    "Autonomous agent %s stop failed: %s",
                    getattr(agent, "agent_id", "<unknown>"),
                    exc,
                )

    # Shut down agent WS manager
    if container.has("agent_ws_manager"):
        try:
            await container.agent_ws_manager.shutdown()
        except Exception as exc:
            logger.exception(
                "Agent WS manager shutdown failed: %s", exc
            )

    # Close Redis client
    if _agent_redis_client is not None:
        try:
            await _agent_redis_client.close()
            logger.info("Agent Redis client closed")
        except Exception as exc:
            logger.exception("Agent Redis client close failed: %s", exc)

    # Reset tenant guard wiring so the next boot cycle starts clean
    try:
        from ops.middleware.tenant_guard import configure_tenant_guard
        configure_tenant_guard(None)
    except Exception as exc:
        logger.exception(
            "Tenant guard reset failed during shutdown: %s", exc
        )

    logger.info("Agentic AI domain shut down")
