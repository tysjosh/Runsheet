"""
Explicit dependency container for all backend services.

Replaces module-level singletons and configure_*() wiring with a single
registry that is created at startup, populated by bootstrap modules, and
stored on ``app.state.container``.

Requirements: 2.1, 2.2, 2.3
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from config.settings import Settings
    from services.elasticsearch_service import ElasticsearchService
    from ops.services.ops_es_service import OpsElasticsearchService
    from ops.services.feature_flags import FeatureFlagService
    from ops.ingestion.idempotency import IdempotencyService
    from ops.ingestion.poison_queue import PoisonQueueService
    from fuel.services.fuel_service import FuelService
    from scheduling.services.job_service import JobService
    from scheduling.services.cargo_service import CargoService
    from scheduling.services.delay_detection_service import DelayDetectionService
    from health.service import HealthCheckService
    from telemetry.service import TelemetryService
    from ingestion.service import DataIngestionService
    from websocket.connection_manager import ConnectionManager
    from ops.websocket.ops_ws import OpsWebSocketManager
    from scheduling.websocket.scheduling_ws import SchedulingWebSocketManager
    from Agents.agent_ws_manager import AgentActivityWSManager
    from Agents.risk_registry import RiskRegistry
    from Agents.business_validator import BusinessValidator
    from Agents.activity_log_service import ActivityLogService
    from Agents.autonomy_config_service import AutonomyConfigService
    from Agents.approval_queue_service import ApprovalQueueService
    from Agents.confirmation_protocol import ConfirmationProtocol
    from Agents.memory_service import MemoryService
    from Agents.feedback_service import FeedbackService
    from Agents.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)


class ServiceContainer:
    """Typed registry for all backend service instances.

    Services are set as attributes during bootstrap and accessed via
    attribute lookup or the ``get()`` method.

    Usage::

        container = ServiceContainer()
        container.settings = get_settings()
        container.es_service = ElasticsearchService(...)

        # Later, in endpoint handlers or other modules:
        es = container.get("es_service")
        # or
        es = container.es_service

    Requirements: 2.1, 2.2, 2.3
    """

    # -- Core --
    settings: Settings
    es_service: ElasticsearchService
    redis_client: Any  # redis.asyncio client
    telemetry_service: TelemetryService
    health_check_service: HealthCheckService
    data_ingestion_service: DataIngestionService

    # -- WebSocket Managers --
    fleet_ws_manager: ConnectionManager
    ops_ws_manager: OpsWebSocketManager
    scheduling_ws_manager: SchedulingWebSocketManager
    agent_ws_manager: AgentActivityWSManager

    # -- Ops --
    ops_es_service: OpsElasticsearchService
    ops_idempotency: IdempotencyService
    ops_poison_queue: PoisonQueueService
    ops_feature_flags: FeatureFlagService

    # -- Fuel --
    fuel_service: FuelService

    # -- Scheduling --
    job_service: JobService
    cargo_service: CargoService
    delay_detection_service: DelayDetectionService

    # -- Agents --
    risk_registry: RiskRegistry
    business_validator: BusinessValidator
    activity_log_service: ActivityLogService
    autonomy_config_service: AutonomyConfigService
    approval_queue_service: ApprovalQueueService
    confirmation_protocol: ConfirmationProtocol
    memory_service: MemoryService
    feedback_service: FeedbackService
    agent_orchestrator: AgentOrchestrator
    agent_scheduler: Any  # AgentScheduler (avoids circular import)

    def __init__(self) -> None:
        self._registry: Dict[str, Any] = {}

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._registry[name] = value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._registry[name]
        except KeyError:
            raise AttributeError(
                f"Service '{name}' has not been registered in the container. "
                f"Available services: {sorted(self._registry.keys())}"
            )

    def get(self, service_name: str) -> Any:
        """Retrieve a service by name.

        Raises:
            KeyError: If the service has not been registered, with a
                descriptive message listing available services.
        """
        try:
            return self._registry[service_name]
        except KeyError:
            raise KeyError(
                f"Service '{service_name}' not found in container. "
                f"Registered services: {sorted(self._registry.keys())}"
            )

    def has(self, service_name: str) -> bool:
        """Check whether a service is registered."""
        return service_name in self._registry

    @property
    def registered_services(self) -> List[str]:
        """Return sorted list of registered service names."""
        return sorted(self._registry.keys())
