"""Unit tests for DyedDieselIntakeHook — order intake pipeline wiring.

Tests cover:
- Non-dyed product passes through without calling the enforcer.
- Dyed product with valid certificate passes through.
- Dyed product without valid certificate raises DyedDieselOrderRejected.
- Missing product_code passes through (no validation needed).
- Enforcer exception results in graceful degradation (order allowed).
- after_accept is a no-op.

Validates: Requirement 6.1
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from compliance.hooks.dyed_diesel_intake_hook import (
    DyedDieselIntakeHook,
    DyedDieselOrderRejected,
)
from compliance.services.dyed_diesel_enforcer import ValidationResult


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_fuel_co"
_CUSTOMER_ID = "cust_farm_001"


def _make_order_draft(
    *,
    product_code: str = "DIESEL_2",
    tenant_id: str = _TENANT_ID,
    customer_id: str = _CUSTOMER_ID,
) -> Dict[str, Any]:
    """Build a minimal order draft dict."""
    return {
        "order_id": "ord_test_001",
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "product_code": product_code,
        "gallons_requested": 500.0,
        "status": "placed",
    }


def _make_enforcer_mock(*, valid: bool = True, error_code: str = None, message: str = None) -> AsyncMock:
    """Create a mocked DyedDieselEnforcer with a preset validate_order result."""
    enforcer = AsyncMock()
    enforcer.validate_order = AsyncMock(
        return_value=ValidationResult(
            valid=valid,
            error_code=error_code,
            message=message,
        )
    )
    return enforcer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDyedDieselIntakeHook:
    """Tests for DyedDieselIntakeHook.before_accept."""

    @pytest.mark.asyncio
    async def test_non_dyed_product_passes_without_enforcer_call(self):
        """Non-dyed products (e.g. DIESEL_2) should pass through without
        calling the enforcer at all."""
        enforcer = _make_enforcer_mock()
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft(product_code="DIESEL_2")
        result = await hook.before_accept(draft)

        assert result is draft
        enforcer.validate_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_dyed_product_valid_certificate_passes(self):
        """Dyed-diesel order with a valid 637M certificate should pass."""
        enforcer = _make_enforcer_mock(valid=True)
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft(product_code="OFF_ROAD_DIESEL")
        result = await hook.before_accept(draft)

        assert result is draft
        enforcer.validate_order.assert_called_once_with(
            tenant_id=_TENANT_ID,
            customer_id=_CUSTOMER_ID,
            product_code="OFF_ROAD_DIESEL",
        )

    @pytest.mark.asyncio
    async def test_dyed_product_no_certificate_rejects(self):
        """Dyed-diesel order without a valid certificate should be rejected."""
        enforcer = _make_enforcer_mock(
            valid=False,
            error_code="dyed.no_valid_exemption",
            message="No valid IRS 637M certificate",
        )
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft(product_code="DYED_DIESEL")

        with pytest.raises(DyedDieselOrderRejected) as exc_info:
            await hook.before_accept(draft)

        assert exc_info.value.error_code == "dyed.no_valid_exemption"
        assert "637M" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_missing_product_code_passes(self):
        """Orders without a product_code should pass through (legacy orders)."""
        enforcer = _make_enforcer_mock()
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft()
        draft["product_code"] = None
        result = await hook.before_accept(draft)

        assert result is draft
        enforcer.validate_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_enforcer_exception_allows_order_gracefully(self):
        """If the enforcer raises unexpectedly, the order should still pass
        (graceful degradation / fail-open)."""
        enforcer = AsyncMock()
        enforcer.validate_order = AsyncMock(
            side_effect=RuntimeError("ES connection timeout")
        )
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft(product_code="DYED_ULSD")
        result = await hook.before_accept(draft)

        # Order passes through despite the error
        assert result is draft

    @pytest.mark.asyncio
    async def test_after_accept_is_noop(self):
        """after_accept should be a no-op and not raise."""
        enforcer = _make_enforcer_mock()
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        order = _make_order_draft(product_code="OFF_ROAD_DIESEL")
        # Should not raise
        await hook.after_accept(order)

    @pytest.mark.asyncio
    async def test_all_dyed_product_codes_trigger_validation(self):
        """All recognized dyed-diesel product codes should trigger validation."""
        from compliance.services.dyed_diesel_enforcer import DYED_DIESEL_PRODUCT_CODES

        enforcer = _make_enforcer_mock(valid=True)
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        for code in DYED_DIESEL_PRODUCT_CODES:
            enforcer.validate_order.reset_mock()
            draft = _make_order_draft(product_code=code)
            result = await hook.before_accept(draft)
            assert result is draft
            enforcer.validate_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_tenant_id_skips_validation(self):
        """If tenant_id is missing, skip validation gracefully."""
        enforcer = _make_enforcer_mock()
        hook = DyedDieselIntakeHook(dyed_diesel_enforcer=enforcer)

        draft = _make_order_draft(product_code="OFF_ROAD_DIESEL")
        draft["tenant_id"] = ""
        result = await hook.before_accept(draft)

        assert result is draft
        enforcer.validate_order.assert_not_called()
