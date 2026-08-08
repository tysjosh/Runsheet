"""
Unit tests for order mutation tools (Task 12.2).

Tests the three mutation tools:
- update_order_status (MEDIUM risk)
- assign_driver_to_order (MEDIUM risk)
- cancel_order (HIGH risk)

Validates:
- ConfirmationProtocol routing with correct risk classifications
- suggest-only tenants return suggestion strings without executing
- Internal service routing (OrderService, DriverRepository) not HTTP
- Tenant scoping via ContextVar pattern
- Error handling for missing orders/drivers
"""

import json
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock strands and ES service BEFORE any tool imports so the module-level
# instance never touches a real cluster or requires the strands package.
#
# Real SDK first: a MagicMock in ``sys.modules["strands"]`` is not a package, so
# a later ``from strands.models.litellm import ...`` in any other test module
# would fail. setdefault only avoided that when something had already imported
# strands for real, which depends on collection order.
try:
    import strands  # noqa: F401
except ImportError:  # pragma: no cover - only on installs without the SDK
    _mock_strands = MagicMock()
    _mock_strands.tool = lambda f: f  # @tool is a no-op passthrough
    sys.modules["strands"] = _mock_strands

_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.confirmation_protocol import MutationResult  # noqa: E402
from Agents.risk_registry import DEFAULT_RISK_REGISTRY, RiskLevel  # noqa: E402
from Agents.tools._tenant_context import set_current_tenant  # noqa: E402
from Agents.tools.order_mutation_tools import (  # noqa: E402
    ORDER_MUTATION_RISK_CLASSIFICATIONS,
    assign_driver_to_order,
    cancel_order,
    configure_order_mutation_tools,
    update_order_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_order():
    """Create a mock FuelOrder model."""
    order = MagicMock()
    order.order_id = "ord_abc123"
    order.tenant_id = "tenant-1"
    order.status = "placed"
    order.model_dump.return_value = {
        "order_id": "ord_abc123",
        "tenant_id": "tenant-1",
        "status": "placed",
        "assigned_driver_id": None,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "last_event_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    return order


@pytest.fixture
def mock_driver():
    """Create a mock Driver model."""
    driver = MagicMock()
    driver.driver_id = "drv_001"
    driver.tenant_id = "tenant-1"
    driver.status = "active"
    return driver


@pytest.fixture
def mock_order_service():
    """Create a mock OrderService."""
    svc = MagicMock()
    svc.apply_status_transition = AsyncMock(
        return_value={
            "order_id": "ord_abc123",
            "tenant_id": "tenant-1",
            "status": "confirmed",
        }
    )
    return svc


@pytest.fixture
def mock_order_repo(mock_order):
    """Create a mock FuelOrderRepository."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=mock_order)
    repo.upsert_with_last_event_timestamp = AsyncMock()
    return repo


@pytest.fixture
def mock_driver_repo(mock_driver):
    """Create a mock DriverRepository."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=mock_driver)
    repo.increment_counters = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def mock_confirmation_protocol():
    """Create a mock ConfirmationProtocol that auto-executes."""
    cp = MagicMock()
    cp.process_mutation = AsyncMock(
        return_value=MutationResult(
            executed=True,
            risk_level="medium",
            result="Success",
            confirmation_method="immediate",
        )
    )
    return cp


@pytest.fixture
def mock_autonomy_config_service():
    """Create a mock AutonomyConfigService."""
    svc = MagicMock()
    svc.get_level = AsyncMock(return_value="auto-medium")
    return svc


@pytest.fixture
def configured_tools(
    mock_order_service,
    mock_order_repo,
    mock_driver_repo,
    mock_confirmation_protocol,
    mock_autonomy_config_service,
):
    """Configure the mutation tools with mock services."""
    configure_order_mutation_tools(
        order_service=mock_order_service,
        order_repo=mock_order_repo,
        driver_repo=mock_driver_repo,
        confirmation_protocol=mock_confirmation_protocol,
        autonomy_config_service=mock_autonomy_config_service,
    )
    yield
    # Reset module-level state
    configure_order_mutation_tools(
        order_service=None,
        order_repo=None,
        driver_repo=None,
        confirmation_protocol=None,
        autonomy_config_service=None,
    )


# ---------------------------------------------------------------------------
# Risk classification tests
# ---------------------------------------------------------------------------


class TestRiskClassifications:
    """Verify risk classifications are registered correctly."""

    def test_update_order_status_is_medium_risk(self):
        assert ORDER_MUTATION_RISK_CLASSIFICATIONS["update_order_status"] == RiskLevel.MEDIUM

    def test_assign_driver_to_order_is_medium_risk(self):
        assert ORDER_MUTATION_RISK_CLASSIFICATIONS["assign_driver_to_order"] == RiskLevel.MEDIUM

    def test_cancel_order_is_high_risk(self):
        assert ORDER_MUTATION_RISK_CLASSIFICATIONS["cancel_order"] == RiskLevel.HIGH

    def test_registered_in_default_risk_registry(self):
        """All order mutation tools are registered in the global DEFAULT_RISK_REGISTRY."""
        assert DEFAULT_RISK_REGISTRY["update_order_status"] == RiskLevel.MEDIUM
        assert DEFAULT_RISK_REGISTRY["assign_driver_to_order"] == RiskLevel.MEDIUM
        assert DEFAULT_RISK_REGISTRY["cancel_order"] == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Suggest-only autonomy level tests
# ---------------------------------------------------------------------------


class TestSuggestOnlyMode:
    """In suggest-only tenants, every mutation tool returns a suggestion string."""

    @pytest.mark.asyncio
    async def test_update_order_status_returns_suggestion(
        self, configured_tools, mock_autonomy_config_service
    ):
        mock_autonomy_config_service.get_level = AsyncMock(return_value="suggest-only")

        with set_current_tenant("tenant-1"):
            result = await update_order_status(
                order_id="ord_abc123",
                new_status="confirmed",
                reason="test",
            )

        data = json.loads(result)
        assert data["action"] == "suggestion"
        assert data["autonomy_level"] == "suggest-only"
        assert "ord_abc123" in data["suggestion"]
        assert "confirmed" in data["suggestion"]
        assert "suggest-only" in data["suggestion"]

    @pytest.mark.asyncio
    async def test_assign_driver_to_order_returns_suggestion(
        self, configured_tools, mock_autonomy_config_service
    ):
        mock_autonomy_config_service.get_level = AsyncMock(return_value="suggest-only")

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123",
                driver_id="drv_001",
                reason="closest driver",
            )

        data = json.loads(result)
        assert data["action"] == "suggestion"
        assert data["autonomy_level"] == "suggest-only"
        assert "drv_001" in data["suggestion"]
        assert "ord_abc123" in data["suggestion"]

    @pytest.mark.asyncio
    async def test_cancel_order_returns_suggestion(
        self, configured_tools, mock_autonomy_config_service
    ):
        mock_autonomy_config_service.get_level = AsyncMock(return_value="suggest-only")

        with set_current_tenant("tenant-1"):
            result = await cancel_order(
                order_id="ord_abc123",
                reason="customer request",
            )

        data = json.loads(result)
        assert data["action"] == "suggestion"
        assert data["autonomy_level"] == "suggest-only"
        assert "ord_abc123" in data["suggestion"]
        assert "customer request" in data["suggestion"]

    @pytest.mark.asyncio
    async def test_suggest_only_does_not_call_confirmation_protocol(
        self,
        configured_tools,
        mock_autonomy_config_service,
        mock_confirmation_protocol,
    ):
        """ConfirmationProtocol is NOT called in suggest-only mode."""
        mock_autonomy_config_service.get_level = AsyncMock(return_value="suggest-only")

        with set_current_tenant("tenant-1"):
            await update_order_status(
                order_id="ord_abc123", new_status="confirmed"
            )

        mock_confirmation_protocol.process_mutation.assert_not_called()


# ---------------------------------------------------------------------------
# update_order_status tests
# ---------------------------------------------------------------------------


class TestUpdateOrderStatus:
    """Tests for the update_order_status mutation tool."""

    @pytest.mark.asyncio
    async def test_executes_via_order_service(
        self, configured_tools, mock_order_service, mock_order_repo
    ):
        with set_current_tenant("tenant-1"):
            result = await update_order_status(
                order_id="ord_abc123",
                new_status="confirmed",
                reason="verified",
                notes="all good",
            )

        data = json.loads(result)
        assert data["action"] == "executed"
        assert data["order_id"] == "ord_abc123"
        assert data["new_status"] == "confirmed"
        assert data["risk_level"] == "medium"

        # Verify OrderService was called
        mock_order_service.apply_status_transition.assert_called_once()
        call_kwargs = mock_order_service.apply_status_transition.call_args[1]
        assert call_kwargs["new_status"] == "confirmed"
        assert call_kwargs["reason"] == "verified"
        assert call_kwargs["notes"] == "all good"

    @pytest.mark.asyncio
    async def test_routes_through_confirmation_protocol(
        self, configured_tools, mock_confirmation_protocol
    ):
        with set_current_tenant("tenant-1"):
            await update_order_status(
                order_id="ord_abc123", new_status="confirmed"
            )

        mock_confirmation_protocol.process_mutation.assert_called_once()
        request = mock_confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tool_name == "update_order_status"
        assert request.tenant_id == "tenant-1"
        assert request.parameters["order_id"] == "ord_abc123"
        assert request.parameters["new_status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_order_not_found_returns_error(
        self, configured_tools, mock_order_repo
    ):
        mock_order_repo.get = AsyncMock(return_value=None)

        with set_current_tenant("tenant-1"):
            result = await update_order_status(
                order_id="ord_missing", new_status="confirmed"
            )

        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_queued_for_approval_when_not_auto_executed(
        self, configured_tools, mock_confirmation_protocol
    ):
        mock_confirmation_protocol.process_mutation = AsyncMock(
            return_value=MutationResult(
                executed=False,
                approval_id="appr_123",
                risk_level="medium",
                confirmation_method="approval_queue",
            )
        )

        with set_current_tenant("tenant-1"):
            result = await update_order_status(
                order_id="ord_abc123", new_status="confirmed"
            )

        data = json.loads(result)
        assert data["action"] == "queued_for_approval"
        assert data["approval_id"] == "appr_123"
        assert data["confirmation_method"] == "approval_queue"


# ---------------------------------------------------------------------------
# assign_driver_to_order tests
# ---------------------------------------------------------------------------


class TestAssignDriverToOrder:
    """Tests for the assign_driver_to_order mutation tool."""

    @pytest.mark.asyncio
    async def test_executes_assignment(
        self, configured_tools, mock_order_repo, mock_driver_repo
    ):
        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123",
                driver_id="drv_001",
                reason="closest",
            )

        data = json.loads(result)
        assert data["action"] == "executed"
        assert data["order_id"] == "ord_abc123"
        assert data["driver_id"] == "drv_001"
        assert data["risk_level"] == "medium"

    @pytest.mark.asyncio
    async def test_rejects_off_duty_driver(
        self, configured_tools, mock_driver_repo
    ):
        mock_driver = MagicMock()
        mock_driver.status = "off_duty"
        mock_driver_repo.get = AsyncMock(return_value=mock_driver)

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

        data = json.loads(result)
        assert "error" in data
        assert data["error_code"] == "driver_unavailable"

    @pytest.mark.asyncio
    async def test_rejects_inactive_driver(
        self, configured_tools, mock_driver_repo
    ):
        mock_driver = MagicMock()
        mock_driver.status = "inactive"
        mock_driver_repo.get = AsyncMock(return_value=mock_driver)

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

        data = json.loads(result)
        assert "error" in data
        assert data["error_code"] == "driver_unavailable"

    @pytest.mark.asyncio
    async def test_order_not_found_returns_error(
        self, configured_tools, mock_order_repo
    ):
        mock_order_repo.get = AsyncMock(return_value=None)

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_missing", driver_id="drv_001"
            )

        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_driver_not_found_returns_error(
        self, configured_tools, mock_driver_repo
    ):
        mock_driver_repo.get = AsyncMock(return_value=None)

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_missing"
            )

        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_increments_driver_counter(
        self, configured_tools, mock_driver_repo
    ):
        with set_current_tenant("tenant-1"):
            await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

        mock_driver_repo.increment_counters.assert_called_once_with(
            tenant_id="tenant-1",
            driver_id="drv_001",
            delta_active=1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_counter_failure_does_not_block(
        self, configured_tools, mock_driver_repo
    ):
        """Driver counter increment failure does NOT block the assignment."""
        mock_driver_repo.increment_counters = AsyncMock(
            side_effect=Exception("ES timeout")
        )

        with set_current_tenant("tenant-1"):
            result = await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

        data = json.loads(result)
        assert data["action"] == "executed"

    @pytest.mark.asyncio
    async def test_routes_through_confirmation_protocol(
        self, configured_tools, mock_confirmation_protocol
    ):
        with set_current_tenant("tenant-1"):
            await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

        mock_confirmation_protocol.process_mutation.assert_called_once()
        request = mock_confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tool_name == "assign_driver_to_order"
        assert request.tenant_id == "tenant-1"


# ---------------------------------------------------------------------------
# cancel_order tests
# ---------------------------------------------------------------------------


class TestCancelOrder:
    """Tests for the cancel_order mutation tool."""

    @pytest.mark.asyncio
    async def test_executes_cancellation(
        self, configured_tools, mock_order_service, mock_confirmation_protocol
    ):
        mock_confirmation_protocol.process_mutation = AsyncMock(
            return_value=MutationResult(
                executed=True,
                risk_level="high",
                result="Success",
                confirmation_method="immediate",
            )
        )
        mock_order_service.apply_status_transition = AsyncMock(
            return_value={
                "order_id": "ord_abc123",
                "tenant_id": "tenant-1",
                "status": "cancelled",
            }
        )

        with set_current_tenant("tenant-1"):
            result = await cancel_order(
                order_id="ord_abc123",
                reason="customer request",
                notes="called to cancel",
            )

        data = json.loads(result)
        assert data["action"] == "executed"
        assert data["new_status"] == "cancelled"
        assert data["reason"] == "customer request"
        assert data["risk_level"] == "high"

    @pytest.mark.asyncio
    async def test_routes_through_confirmation_protocol_high_risk(
        self, configured_tools, mock_confirmation_protocol
    ):
        mock_confirmation_protocol.process_mutation = AsyncMock(
            return_value=MutationResult(
                executed=True,
                risk_level="high",
                result="Success",
                confirmation_method="immediate",
            )
        )
        mock_order_service = MagicMock()
        mock_order_service.apply_status_transition = AsyncMock(
            return_value={"order_id": "ord_abc123", "status": "cancelled"}
        )

        with set_current_tenant("tenant-1"):
            result = await cancel_order(
                order_id="ord_abc123", reason="test"
            )

        mock_confirmation_protocol.process_mutation.assert_called_once()
        request = mock_confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tool_name == "cancel_order"

    @pytest.mark.asyncio
    async def test_order_not_found_returns_error(
        self, configured_tools, mock_order_repo
    ):
        mock_order_repo.get = AsyncMock(return_value=None)

        with set_current_tenant("tenant-1"):
            result = await cancel_order(
                order_id="ord_missing", reason="test"
            )

        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_queued_for_approval(
        self, configured_tools, mock_confirmation_protocol
    ):
        """HIGH risk cancel_order is typically queued for approval."""
        mock_confirmation_protocol.process_mutation = AsyncMock(
            return_value=MutationResult(
                executed=False,
                approval_id="appr_456",
                risk_level="high",
                confirmation_method="approval_queue",
            )
        )

        with set_current_tenant("tenant-1"):
            result = await cancel_order(
                order_id="ord_abc123", reason="customer request"
            )

        data = json.loads(result)
        assert data["action"] == "queued_for_approval"
        assert data["approval_id"] == "appr_456"
        assert data["risk_level"] == "high"


# ---------------------------------------------------------------------------
# Tenant scoping tests
# ---------------------------------------------------------------------------


class TestTenantScoping:
    """Verify tools enforce tenant scoping via ContextVar."""

    @pytest.mark.asyncio
    async def test_update_order_status_raises_without_tenant(self, configured_tools):
        with pytest.raises(RuntimeError, match="tenant scope"):
            await update_order_status(
                order_id="ord_abc123", new_status="confirmed"
            )

    @pytest.mark.asyncio
    async def test_assign_driver_raises_without_tenant(self, configured_tools):
        with pytest.raises(RuntimeError, match="tenant scope"):
            await assign_driver_to_order(
                order_id="ord_abc123", driver_id="drv_001"
            )

    @pytest.mark.asyncio
    async def test_cancel_order_raises_without_tenant(self, configured_tools):
        with pytest.raises(RuntimeError, match="tenant scope"):
            await cancel_order(order_id="ord_abc123", reason="test")


# ---------------------------------------------------------------------------
# Unconfigured tools tests
# ---------------------------------------------------------------------------


class TestUnconfiguredTools:
    """Verify tools raise RuntimeError when not configured."""

    @pytest.mark.asyncio
    async def test_update_order_status_unconfigured(self):
        # Reset to unconfigured state
        configure_order_mutation_tools(
            order_service=None,
            order_repo=None,
            driver_repo=None,
            confirmation_protocol=None,
            autonomy_config_service=None,
        )
        with set_current_tenant("tenant-1"):
            result = await update_order_status(
                order_id="ord_abc123", new_status="confirmed"
            )
        data = json.loads(result)
        assert "error" in data
        assert "not configured" in data["error"]
