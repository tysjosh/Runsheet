"""
Unit tests for MeterCalibrationCronAgent (Task 10.10).

Verifies:
- Agent instantiation with correct agent_id and poll interval
- monitor_cycle discovers tenants and runs check_calibration_alerts
- Graceful handling when no tenants exist
- Graceful handling when a single tenant fails
- Registration is idempotent (safe to call multiple times)

Validates: Requirement 8.4
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.meter_calibration_cron_agent import (
    MeterCalibrationCronAgent,
    METER_CALIBRATION_POLL_INTERVAL_SECONDS,
    METER_CALIBRATION_COOLDOWN_MINUTES,
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
    """Create a MeterCalibrationCronAgent with mocked dependencies."""
    return MeterCalibrationCronAgent(
        es_service=mock_es_service,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Tests: Agent configuration
# ---------------------------------------------------------------------------


class TestMeterCalibrationCronAgentConfig:
    """Verify agent is configured correctly."""

    def test_agent_id(self, agent):
        assert agent.agent_id == "meter_calibration_cron_agent"

    def test_poll_interval_is_daily(self, agent):
        assert agent.poll_interval == 86_400

    def test_poll_interval_constant(self):
        assert METER_CALIBRATION_POLL_INTERVAL_SECONDS == 86_400

    def test_cooldown_is_daily(self):
        assert METER_CALIBRATION_COOLDOWN_MINUTES == 1440


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant discovery
# ---------------------------------------------------------------------------


class TestMeterCalibrationCronMonitorCycle:
    """Verify monitor_cycle discovers tenants and runs checks."""

    @pytest.mark.asyncio
    async def test_no_tenants_returns_empty(self, agent, mock_es_service):
        """When no tenants have meters, cycle is a no-op."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_discovers_tenants_via_aggregation(self, agent, mock_es_service):
        """Verifies the terms aggregation query is sent to the meter_registry index."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        await agent.monitor_cycle()

        # Verify the aggregation query was sent
        call_args = mock_es_service.search_documents.call_args
        assert call_args is not None
        index_arg = call_args[0][0]
        query_arg = call_args[0][1]
        assert index_arg == "meter_registry"
        assert "aggs" in query_arg
        assert "tenants" in query_arg["aggs"]
        assert query_arg["aggs"]["tenants"]["terms"]["field"] == "tenant_id"

    @pytest.mark.asyncio
    async def test_runs_check_calibration_alerts_per_tenant(self, agent, mock_es_service):
        """Verifies check_calibration_alerts is called for each discovered tenant."""
        # First call: tenant discovery
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {"buckets": [{"key": "tenant-1"}]}
            }
        }

        with patch(
            "Agents.autonomous.meter_calibration_cron_agent.MeterAuditService"
        ) as MockMAS:
            mock_svc = AsyncMock()
            mock_svc.check_calibration_alerts.return_value = []
            MockMAS.return_value = mock_svc

            await agent.monitor_cycle()

            mock_svc.check_calibration_alerts.assert_called_once_with("tenant-1")

    @pytest.mark.asyncio
    async def test_returns_detections_from_alerts(self, agent, mock_es_service):
        """Verifies alerts are returned as detections."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {"buckets": [{"key": "tenant-1"}]}
            }
        }

        fake_alert = MagicMock()

        with patch(
            "Agents.autonomous.meter_calibration_cron_agent.MeterAuditService"
        ) as MockMAS:
            mock_svc = AsyncMock()
            mock_svc.check_calibration_alerts.return_value = [fake_alert]
            MockMAS.return_value = mock_svc

            detections, actions = await agent.monitor_cycle()

            assert fake_alert in detections
            assert len(detections) == 1
            # Actions are empty — alerts are informational
            assert actions == []

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
            "Agents.autonomous.meter_calibration_cron_agent.MeterAuditService"
        ) as MockMAS:
            mock_svc = AsyncMock()

            async def _check_calibration(tenant_id):
                call_count["n"] += 1
                if tenant_id == "tenant-1":
                    raise RuntimeError("ES timeout")
                return []

            mock_svc.check_calibration_alerts.side_effect = _check_calibration
            MockMAS.return_value = mock_svc

            # Should not raise
            detections, actions = await agent.monitor_cycle()

            # tenant-2 should still have been attempted
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

    @pytest.mark.asyncio
    async def test_multiple_tenants_aggregates_alerts(
        self, agent, mock_es_service
    ):
        """Alerts from multiple tenants are aggregated in detections."""
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

        alert_1 = MagicMock()
        alert_2 = MagicMock()

        with patch(
            "Agents.autonomous.meter_calibration_cron_agent.MeterAuditService"
        ) as MockMAS:
            mock_svc = AsyncMock()

            async def _check_calibration(tenant_id):
                if tenant_id == "tenant-1":
                    return [alert_1]
                return [alert_2]

            mock_svc.check_calibration_alerts.side_effect = _check_calibration
            MockMAS.return_value = mock_svc

            detections, actions = await agent.monitor_cycle()

            assert len(detections) == 2
            assert alert_1 in detections
            assert alert_2 in detections


# ---------------------------------------------------------------------------
# Tests: Registration idempotency
# ---------------------------------------------------------------------------


class TestMeterCalibrationCronRegistration:
    """Verify registration is safe to call multiple times."""

    def test_multiple_instantiations_are_independent(self, mock_es_service):
        """Creating multiple agents does not cause conflicts."""
        agent1 = MeterCalibrationCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )
        agent2 = MeterCalibrationCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )

        assert agent1.agent_id == agent2.agent_id
        assert agent1 is not agent2
