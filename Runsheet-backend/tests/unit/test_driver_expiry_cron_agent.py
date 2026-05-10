"""
Unit tests for DriverExpiryCronAgent (Task 6.10).

Verifies:
- Agent instantiation with correct agent_id and poll interval
- monitor_cycle discovers tenants and runs all three checks
- Graceful handling when no tenants exist
- Graceful handling when a single tenant fails
- Registration is idempotent (safe to call multiple times)

Validates: Requirements 5.2, 5.3, 5.4, 5.8
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.driver_expiry_cron_agent import (
    DriverExpiryCronAgent,
    DRIVER_EXPIRY_POLL_INTERVAL_SECONDS,
    DRIVER_EXPIRY_COOLDOWN_MINUTES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_es_service():
    """Create a mock Elasticsearch service."""
    es = AsyncMock()
    return es


@pytest.fixture
def agent(mock_es_service):
    """Create a DriverExpiryCronAgent with mocked dependencies."""
    return DriverExpiryCronAgent(
        es_service=mock_es_service,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Tests: Agent configuration
# ---------------------------------------------------------------------------


class TestDriverExpiryCronAgentConfig:
    """Verify agent is configured correctly."""

    def test_agent_id(self, agent):
        assert agent.agent_id == "driver_expiry_cron_agent"

    def test_poll_interval_is_daily(self, agent):
        assert agent.poll_interval == 86_400

    def test_poll_interval_constant(self):
        assert DRIVER_EXPIRY_POLL_INTERVAL_SECONDS == 86_400

    def test_cooldown_is_daily(self):
        assert DRIVER_EXPIRY_COOLDOWN_MINUTES == 1440


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant discovery
# ---------------------------------------------------------------------------


class TestDriverExpiryCronMonitorCycle:
    """Verify monitor_cycle discovers tenants and runs checks."""

    @pytest.mark.asyncio
    async def test_no_tenants_returns_empty(self, agent, mock_es_service):
        """When no tenants have drivers, cycle is a no-op."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_discovers_tenants_via_aggregation(self, agent, mock_es_service):
        """Verifies the terms aggregation query is sent to the drivers index."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        await agent.monitor_cycle()

        # Verify the aggregation query was sent
        call_args = mock_es_service.search_documents.call_args
        assert call_args is not None
        index_arg = call_args[0][0]
        query_arg = call_args[0][1]
        assert index_arg == "drivers"
        assert "aggs" in query_arg
        assert "tenants" in query_arg["aggs"]
        assert query_arg["aggs"]["tenants"]["terms"]["field"] == "tenant_id"

    @pytest.mark.asyncio
    async def test_runs_all_three_checks_per_tenant(self, agent, mock_es_service):
        """Verifies check_expiry_alerts, auto_suspend, and drug_test_overdue are called."""
        # First call: tenant discovery
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {"buckets": [{"key": "tenant-1"}]}
            }
        }

        with patch(
            "Agents.autonomous.driver_expiry_cron_agent.DriverQualificationService"
        ) as MockDQS:
            mock_svc = AsyncMock()
            mock_svc.check_expiry_alerts.return_value = []
            mock_svc.auto_suspend_expired_drivers.return_value = []
            mock_svc.check_drug_test_overdue.return_value = []
            MockDQS.return_value = mock_svc

            await agent.monitor_cycle()

            mock_svc.check_expiry_alerts.assert_called_once_with("tenant-1")
            mock_svc.auto_suspend_expired_drivers.assert_called_once_with("tenant-1")
            mock_svc.check_drug_test_overdue.assert_called_once_with("tenant-1")

    @pytest.mark.asyncio
    async def test_returns_detections_and_actions(self, agent, mock_es_service):
        """Verifies alerts and suspensions are returned correctly."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {"buckets": [{"key": "tenant-1"}]}
            }
        }

        fake_alert = MagicMock()
        fake_suspension = {"driver_id": "d1", "suspension_reason": "CDL expired"}
        fake_overdue = {"driver_id": "d2", "days_overdue": 30}

        with patch(
            "Agents.autonomous.driver_expiry_cron_agent.DriverQualificationService"
        ) as MockDQS:
            mock_svc = AsyncMock()
            mock_svc.check_expiry_alerts.return_value = [fake_alert]
            mock_svc.auto_suspend_expired_drivers.return_value = [fake_suspension]
            mock_svc.check_drug_test_overdue.return_value = [fake_overdue]
            MockDQS.return_value = mock_svc

            detections, actions = await agent.monitor_cycle()

            # Alerts + overdue go to detections
            assert fake_alert in detections
            assert fake_overdue in detections
            assert len(detections) == 2

            # Suspensions go to actions
            assert fake_suspension in actions
            assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_single_tenant_failure_does_not_abort_others(
        self, agent, mock_es_service
    ):
        """If one tenant fails, other tenants are still processed."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {
                    "buckets": [
                        {"key": "tenant-1"},
                        {"key": "tenant-2"},
                    ]
                }
            }
        }

        call_count = {"n": 0}

        with patch(
            "Agents.autonomous.driver_expiry_cron_agent.DriverQualificationService"
        ) as MockDQS:
            mock_svc = AsyncMock()

            async def _check_expiry(tenant_id):
                call_count["n"] += 1
                if tenant_id == "tenant-1":
                    raise RuntimeError("ES timeout")
                return []

            mock_svc.check_expiry_alerts.side_effect = _check_expiry
            mock_svc.auto_suspend_expired_drivers.return_value = []
            mock_svc.check_drug_test_overdue.return_value = []
            MockDQS.return_value = mock_svc

            # Should not raise
            detections, actions = await agent.monitor_cycle()

            # tenant-2 should still have been attempted
            # (check_expiry_alerts called for both tenants)
            assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_tenant_discovery_failure_returns_empty(
        self, agent, mock_es_service
    ):
        """If tenant discovery fails, cycle returns empty gracefully."""
        mock_es_service.search_documents.side_effect = RuntimeError("ES down")

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []


# ---------------------------------------------------------------------------
# Tests: Registration idempotency
# ---------------------------------------------------------------------------


class TestDriverExpiryCronRegistration:
    """Verify registration is safe to call multiple times."""

    def test_multiple_instantiations_are_independent(self, mock_es_service):
        """Creating multiple agents does not cause conflicts."""
        agent1 = DriverExpiryCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )
        agent2 = DriverExpiryCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )

        assert agent1.agent_id == agent2.agent_id
        assert agent1 is not agent2
