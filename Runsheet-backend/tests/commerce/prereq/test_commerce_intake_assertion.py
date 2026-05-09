"""
Tests for the commerce→intake prerequisite assertion in bootstrap/core.py.

Validates that `commerce.backbone_enabled` can only be set true for a tenant
when `intake.pipeline_enabled` is already true. The assertion runs at startup
in non-production environments and fails loudly on misconfiguration.

Validates: Commerce Backbone Requirement — Relationship to order-intake-pipeline.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bootstrap.container import ServiceContainer
from bootstrap.core import (
    assert_commerce_requires_intake,
    CommerceIntakeMisconfigurationError,
    COMMERCE_BACKBONE_FLAG_KEY,
    ORDER_INTAKE_PIPELINE_FLAG_KEY,
    _ACTIVE_OVERLAY_STATES,
)


@pytest.fixture
def container():
    """Create a ServiceContainer with mocked settings and feature flag service."""
    c = ServiceContainer()

    # Mock settings as development environment
    settings = MagicMock()
    settings.environment = MagicMock()
    settings.environment.value = "development"
    # Make the equality check work for Environment.PRODUCTION
    from config.settings import Environment
    settings.environment = Environment.DEVELOPMENT
    c.settings = settings

    return c


@pytest.fixture
def mock_feature_flag_service():
    """Create a mock FeatureFlagService with a connected Redis client."""
    service = MagicMock()
    service.client = AsyncMock()
    service.get_overlay_state = AsyncMock()
    return service


class TestCommerceIntakeAssertion:
    """Tests for assert_commerce_requires_intake."""

    @pytest.mark.asyncio
    async def test_skips_in_production(self, container):
        """Assertion is skipped entirely in production environments."""
        from config.settings import Environment

        container.settings = MagicMock()
        container.settings.environment = Environment.PRODUCTION

        # Should not raise even without feature flag service
        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_skips_when_feature_flag_service_unavailable(self, container):
        """Assertion is skipped when ops_feature_flags is not registered."""
        # container has no ops_feature_flags registered
        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_skips_when_redis_not_connected(self, container, mock_feature_flag_service):
        """Assertion is skipped when Redis client is None."""
        mock_feature_flag_service.client = None
        container.ops_feature_flags = mock_feature_flag_service

        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_passes_when_no_commerce_tenants(self, container, mock_feature_flag_service):
        """Assertion passes when no tenants have commerce backbone enabled."""
        container.ops_feature_flags = mock_feature_flag_service

        # Redis scan returns no keys
        mock_feature_flag_service.client.scan = AsyncMock(return_value=("0", []))

        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_passes_when_commerce_and_intake_both_active(
        self, container, mock_feature_flag_service
    ):
        """Assertion passes when both commerce and intake are active for a tenant."""
        container.ops_feature_flags = mock_feature_flag_service

        # Redis scan returns one tenant with commerce backbone key
        prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
        mock_feature_flag_service.client.scan = AsyncMock(
            return_value=("0", [f"{prefix}tenant-abc"])
        )

        # Both flags are active
        async def mock_get_overlay_state(flag_key, tenant_id):
            if flag_key == COMMERCE_BACKBONE_FLAG_KEY:
                return "active_gated"
            if flag_key == ORDER_INTAKE_PIPELINE_FLAG_KEY:
                return "active_auto"
            return "disabled"

        mock_feature_flag_service.get_overlay_state = AsyncMock(
            side_effect=mock_get_overlay_state
        )

        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_raises_when_commerce_active_but_intake_disabled(
        self, container, mock_feature_flag_service
    ):
        """Assertion raises when commerce is active but intake is disabled."""
        container.ops_feature_flags = mock_feature_flag_service

        prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
        mock_feature_flag_service.client.scan = AsyncMock(
            return_value=("0", [f"{prefix}tenant-xyz"])
        )

        async def mock_get_overlay_state(flag_key, tenant_id):
            if flag_key == COMMERCE_BACKBONE_FLAG_KEY:
                return "active_gated"
            if flag_key == ORDER_INTAKE_PIPELINE_FLAG_KEY:
                return "disabled"
            return "disabled"

        mock_feature_flag_service.get_overlay_state = AsyncMock(
            side_effect=mock_get_overlay_state
        )

        with pytest.raises(CommerceIntakeMisconfigurationError) as exc_info:
            await assert_commerce_requires_intake(container)

        error_msg = str(exc_info.value)
        assert "tenant-xyz" in error_msg
        assert "commerce.backbone_enabled is active" in error_msg
        assert "intake.pipeline_enabled is NOT active" in error_msg

    @pytest.mark.asyncio
    async def test_raises_when_commerce_active_but_intake_shadow(
        self, container, mock_feature_flag_service
    ):
        """Assertion raises when commerce is active but intake is only in shadow mode."""
        container.ops_feature_flags = mock_feature_flag_service

        prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
        mock_feature_flag_service.client.scan = AsyncMock(
            return_value=("0", [f"{prefix}tenant-shadow"])
        )

        async def mock_get_overlay_state(flag_key, tenant_id):
            if flag_key == COMMERCE_BACKBONE_FLAG_KEY:
                return "active_auto"
            if flag_key == ORDER_INTAKE_PIPELINE_FLAG_KEY:
                return "shadow"  # shadow is NOT active
            return "disabled"

        mock_feature_flag_service.get_overlay_state = AsyncMock(
            side_effect=mock_get_overlay_state
        )

        with pytest.raises(CommerceIntakeMisconfigurationError):
            await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_skips_tenants_with_commerce_not_active(
        self, container, mock_feature_flag_service
    ):
        """Tenants with commerce in shadow/disabled state are not checked."""
        container.ops_feature_flags = mock_feature_flag_service

        prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
        mock_feature_flag_service.client.scan = AsyncMock(
            return_value=("0", [f"{prefix}tenant-shadow-commerce"])
        )

        async def mock_get_overlay_state(flag_key, tenant_id):
            if flag_key == COMMERCE_BACKBONE_FLAG_KEY:
                return "shadow"  # Not active — should be skipped
            if flag_key == ORDER_INTAKE_PIPELINE_FLAG_KEY:
                return "disabled"
            return "disabled"

        mock_feature_flag_service.get_overlay_state = AsyncMock(
            side_effect=mock_get_overlay_state
        )

        # Should NOT raise because commerce is only in shadow mode
        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_reports_multiple_misconfigured_tenants(
        self, container, mock_feature_flag_service
    ):
        """Error message includes all misconfigured tenants."""
        container.ops_feature_flags = mock_feature_flag_service

        prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
        mock_feature_flag_service.client.scan = AsyncMock(
            return_value=(
                "0",
                [f"{prefix}tenant-a", f"{prefix}tenant-b"],
            )
        )

        async def mock_get_overlay_state(flag_key, tenant_id):
            if flag_key == COMMERCE_BACKBONE_FLAG_KEY:
                return "active_gated"
            if flag_key == ORDER_INTAKE_PIPELINE_FLAG_KEY:
                return "disabled"
            return "disabled"

        mock_feature_flag_service.get_overlay_state = AsyncMock(
            side_effect=mock_get_overlay_state
        )

        with pytest.raises(CommerceIntakeMisconfigurationError) as exc_info:
            await assert_commerce_requires_intake(container)

        error_msg = str(exc_info.value)
        assert "tenant-a" in error_msg
        assert "tenant-b" in error_msg

    @pytest.mark.asyncio
    async def test_handles_redis_scan_error_gracefully(
        self, container, mock_feature_flag_service
    ):
        """Assertion does not raise on Redis errors — logs warning instead."""
        container.ops_feature_flags = mock_feature_flag_service

        mock_feature_flag_service.client.scan = AsyncMock(
            side_effect=ConnectionError("Redis unavailable")
        )

        # Should not raise — graceful degradation
        await assert_commerce_requires_intake(container)

    @pytest.mark.asyncio
    async def test_active_overlay_states_are_correct(self):
        """Verify the active overlay states match the platform convention."""
        assert _ACTIVE_OVERLAY_STATES == frozenset({"active_gated", "active_auto"})

    @pytest.mark.asyncio
    async def test_flag_keys_are_correct(self):
        """Verify the flag key constants match the expected values."""
        assert COMMERCE_BACKBONE_FLAG_KEY == "commerce_backbone"
        assert ORDER_INTAKE_PIPELINE_FLAG_KEY == "order_intake_pipeline"
