"""
Unit tests for bootstrap/agents.py.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from bootstrap.container import ServiceContainer


def _make_agent_mock():
    agent = MagicMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.agent_id = "mock-agent"
    return agent


@pytest.fixture(autouse=True)
def _mock_external():
    """Mock all external modules that bootstrap/agents.py imports."""
    modules_to_mock = [
        "services.elasticsearch_service",
        "Agents.tools",
        "Agents.tools.mutation_tools",
        "Agents.tools.ops_feature_guard",
        "Agents.tools.ops_search_tools",
        "Agents.tools.ops_report_tools",
        "strands",
        "strands.models",
        "strands.models.litellm",
    ]
    saved = {}
    for name in modules_to_mock:
        saved[name] = sys.modules.get(name)
        sys.modules[name] = MagicMock()

    yield

    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    sys.modules.pop("bootstrap.agents", None)


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock(
        redis_url="redis://localhost:6379",
        google_cloud_project="test-project-123456",
        google_cloud_location="us-central1",
    )
    c.es_service = MagicMock()
    c.ops_feature_flags = MagicMock()
    return c


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.state = MagicMock()
    return app


class TestAgentsBootstrap:
    """Tests for bootstrap/agents.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_agent_services(self, mock_app, container):
        """Verify all agent services are registered in the container."""
        mock_redis = MagicMock()

        patches = [
            patch("redis.asyncio.from_url", return_value=mock_redis),
            patch("Agents.agent_ws_manager.AgentActivityWSManager", return_value=MagicMock()),
            patch("Agents.agent_ws_manager.bind_container"),
            patch("Agents.risk_registry.RiskRegistry", return_value=MagicMock()),
            patch("Agents.business_validator.BusinessValidator", return_value=MagicMock()),
            patch("Agents.activity_log_service.ActivityLogService", return_value=MagicMock()),
            patch("Agents.autonomy_config_service.AutonomyConfigService", return_value=MagicMock()),
            patch("Agents.approval_queue_service.ApprovalQueueService", return_value=MagicMock()),
            patch("Agents.confirmation_protocol.ConfirmationProtocol", return_value=MagicMock()),
            patch("Agents.memory_service.MemoryService", return_value=MagicMock()),
            patch("Agents.feedback_service.FeedbackService", return_value=MagicMock()),
            patch("agent_endpoints.configure_agent_endpoints"),
            patch("Agents.specialists.FleetAgent", return_value=MagicMock()),
            patch("Agents.specialists.SchedulingAgent", return_value=MagicMock()),
            patch("Agents.specialists.FuelAgent", return_value=MagicMock()),
            patch("Agents.specialists.OpsIntelligenceAgent", return_value=MagicMock()),
            patch("Agents.specialists.ReportingAgent", return_value=MagicMock()),
            patch("Agents.execution_planner.ExecutionPlanner", return_value=MagicMock()),
            patch("Agents.orchestrator.AgentOrchestrator", return_value=MagicMock()),
            patch("Agents.autonomous.DelayResponseAgent", return_value=_make_agent_mock()),
            patch("Agents.autonomous.FuelManagementAgent", return_value=_make_agent_mock()),
            patch("Agents.autonomous.SLAGuardianAgent", return_value=_make_agent_mock()),
            patch("Agents.mainagent.configure_orchestrator"),
            patch("Agents.agent_es_mappings.setup_agent_indices"),
        ]

        for p in patches:
            p.start()

        try:
            sys.modules.pop("bootstrap.agents", None)
            from bootstrap.agents import initialize
            await initialize(mock_app, container)

            assert container.has("agent_ws_manager")
            assert container.has("risk_registry")
            assert container.has("business_validator")
            assert container.has("activity_log_service")
            assert container.has("autonomy_config_service")
            assert container.has("approval_queue_service")
            assert container.has("confirmation_protocol")
            assert container.has("memory_service")
            assert container.has("feedback_service")
            assert container.has("agent_orchestrator")
            assert container.has("redis_client")
        finally:
            for p in patches:
                p.stop()

    @pytest.mark.asyncio
    async def test_shutdown_stops_agents(self):
        """Verify shutdown stops autonomous agents and closes Redis."""
        sys.modules.pop("bootstrap.agents", None)
        import bootstrap.agents as agents_mod

        mock_agent = MagicMock()
        mock_agent.stop = AsyncMock()
        mock_agent.agent_id = "test-agent"
        agents_mod._autonomous_agents = [mock_agent]

        mock_redis = MagicMock()
        mock_redis.close = AsyncMock()
        agents_mod._agent_redis_client = mock_redis

        c = ServiceContainer()
        mock_ws = MagicMock()
        mock_ws.shutdown = AsyncMock()
        c.agent_ws_manager = mock_ws

        await agents_mod.shutdown(MagicMock(), c)

        mock_agent.stop.assert_awaited_once()
        mock_ws.shutdown.assert_awaited_once()
        mock_redis.close.assert_awaited_once()

        # Cleanup
        agents_mod._autonomous_agents = []
        agents_mod._agent_redis_client = None
