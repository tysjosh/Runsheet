"""
Unit tests for AssetCertExpiryCronAgent (Task 8.11).

Verifies:
- Agent instantiation with correct agent_id and poll interval
- monitor_cycle discovers tenants and runs check_expiry_alerts
- Graceful handling when no tenants exist
- Graceful handling when a single tenant fails
- Registration is idempotent (safe to call multiple times)

Validates: Requirements 13.2, 13.3, 13.4
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.asset_cert_expiry_cron_agent import (
    AssetCertExpiryCronAgent,
    ASSET_CERT_EXPIRY_POLL_INTERVAL_SECONDS,
    ASSET_CERT_EXPIRY_COOLDOWN_MINUTES,
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
    """Create an AssetCertExpiryCronAgent with mocked dependencies."""
    return AssetCertExpiryCronAgent(
        es_service=mock_es_service,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Tests: Agent configuration
# ---------------------------------------------------------------------------


class TestAssetCertExpiryCronAgentConfig:
    """Verify agent is configured correctly."""

    def test_agent_id(self, agent):
        assert agent.agent_id == "asset_cert_expiry_cron_agent"

    def test_poll_interval_is_daily(self, agent):
        assert agent.poll_interval == 86_400

    def test_poll_interval_constant(self):
        assert ASSET_CERT_EXPIRY_POLL_INTERVAL_SECONDS == 86_400

    def test_cooldown_is_daily(self):
        assert ASSET_CERT_EXPIRY_COOLDOWN_MINUTES == 1440


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant discovery
# ---------------------------------------------------------------------------


class TestAssetCertExpiryCronMonitorCycle:
    """Verify monitor_cycle discovers tenants and runs checks."""

    @pytest.mark.asyncio
    async def test_no_tenants_returns_empty(self, agent, mock_es_service):
        """When no tenants have asset certifications, cycle is a no-op."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_discovers_tenants_via_aggregation(self, agent, mock_es_service):
        """Verifies the terms aggregation query is sent to the asset_certifications index."""
        mock_es_service.search_documents.return_value = {
            "aggregations": {"tenants": {"buckets": []}}
        }

        await agent.monitor_cycle()

        # Verify the aggregation query was sent
        call_args = mock_es_service.search_documents.call_args
        assert call_args is not None
        index_arg = call_args[0][0]
        query_arg = call_args[0][1]
        assert index_arg == "asset_certifications"
        assert "aggs" in query_arg
        assert "tenants" in query_arg["aggs"]
        assert query_arg["aggs"]["tenants"]["terms"]["field"] == "tenant_id"

    @pytest.mark.asyncio
    async def test_runs_check_expiry_alerts_per_tenant(self, agent, mock_es_service):
        """Verifies check_expiry_alerts is called for each discovered tenant."""
        # First call: tenant discovery
        mock_es_service.search_documents.return_value = {
            "aggregations": {
                "tenants": {"buckets": [{"key": "tenant-1"}]}
            }
        }

        with patch(
            "Agents.autonomous.asset_cert_expiry_cron_agent.AssetCertificationService"
        ) as MockACS:
            mock_svc = AsyncMock()
            mock_svc.check_expiry_alerts.return_value = []
            MockACS.return_value = mock_svc

            await agent.monitor_cycle()

            mock_svc.check_expiry_alerts.assert_called_once_with("tenant-1")

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
            "Agents.autonomous.asset_cert_expiry_cron_agent.AssetCertificationService"
        ) as MockACS:
            mock_svc = AsyncMock()
            mock_svc.check_expiry_alerts.return_value = [fake_alert]
            MockACS.return_value = mock_svc

            detections, actions = await agent.monitor_cycle()

            assert fake_alert in detections
            assert len(detections) == 1
            # Actions are empty — status transitions are internal
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
            "Agents.autonomous.asset_cert_expiry_cron_agent.AssetCertificationService"
        ) as MockACS:
            mock_svc = AsyncMock()

            async def _check_expiry(tenant_id):
                call_count["n"] += 1
                if tenant_id == "tenant-1":
                    raise RuntimeError("ES timeout")
                return []

            mock_svc.check_expiry_alerts.side_effect = _check_expiry
            MockACS.return_value = mock_svc

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
            "Agents.autonomous.asset_cert_expiry_cron_agent.AssetCertificationService"
        ) as MockACS:
            mock_svc = AsyncMock()

            async def _check_expiry(tenant_id):
                if tenant_id == "tenant-1":
                    return [alert_1]
                return [alert_2]

            mock_svc.check_expiry_alerts.side_effect = _check_expiry
            MockACS.return_value = mock_svc

            detections, actions = await agent.monitor_cycle()

            assert len(detections) == 2
            assert alert_1 in detections
            assert alert_2 in detections


# ---------------------------------------------------------------------------
# Tests: Registration idempotency
# ---------------------------------------------------------------------------


class TestAssetCertExpiryCronRegistration:
    """Verify registration is safe to call multiple times."""

    def test_multiple_instantiations_are_independent(self, mock_es_service):
        """Creating multiple agents does not cause conflicts."""
        agent1 = AssetCertExpiryCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )
        agent2 = AssetCertExpiryCronAgent(
            es_service=mock_es_service,
            activity_log_service=MagicMock(),
            ws_manager=MagicMock(),
            confirmation_protocol=MagicMock(),
        )

        assert agent1.agent_id == agent2.agent_id
        assert agent1 is not agent2
