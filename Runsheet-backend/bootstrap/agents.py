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


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register all agentic AI services."""
    global _autonomous_agents, _agent_scheduler, _agent_redis_client

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

    # Agent WebSocket manager
    agent_ws_manager = AgentActivityWSManager()
    container.agent_ws_manager = agent_ws_manager
    bind_agent_ws(container)

    # Redis client for agentic services
    redis_url = settings.redis_url or "redis://localhost:6379"
    _agent_redis_client = aioredis.from_url(redis_url, decode_responses=False)
    container.redis_client = _agent_redis_client
    logger.info("Agent Redis client connected")

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

    configure_mvp_endpoints(
        pipeline=mvp_pipeline,
        es_service=es_service,
        exception_replanning_agent=exception_replanning_agent,
    )
    app.include_router(mvp_router)
    logger.info("MVP endpoints configured and router registered")


async def shutdown(app, container: ServiceContainer) -> None:
    """Stop agents in order: L2 → L1 → L0, then close resources (Req 10.5)."""
    global _autonomous_agents, _agent_scheduler, _agent_redis_client

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
                "dispatch_optimizer", "exception_commander",
                "revenue_guard", "customer_promise",
            ]
            l0_agents = [
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
            except Exception:
                pass
    else:
        # Fallback if scheduler was never created
        for agent in _autonomous_agents:
            try:
                await agent.stop()
                logger.info("Stopped autonomous agent: %s", agent.agent_id)
            except Exception:
                pass

    # Shut down agent WS manager
    if container.has("agent_ws_manager"):
        try:
            await container.agent_ws_manager.shutdown()
        except Exception:
            pass

    # Close Redis client
    if _agent_redis_client is not None:
        try:
            await _agent_redis_client.close()
            logger.info("Agent Redis client closed")
        except Exception:
            pass

    logger.info("Agentic AI domain shut down")
