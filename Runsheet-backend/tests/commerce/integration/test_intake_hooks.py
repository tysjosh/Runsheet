"""Integration tests for PricingHook and CreditCheckHook on the intake pipeline.

Spins up a mock intake pipeline, registers the hooks, and asserts:
1. Pricing attaches to an accepted order (unit_price_cents, subtotal_cents,
   tax_cents, total_cents populated).
2. no_rule_matched rejects the order (PricingError propagates).
3. A credit-hold lands the order with status=on_hold and
   hold_reason=credit_limit_exceeded.

Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6, 8.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.hooks.intake_hooks import CreditCheckHook, PricingHook
from commerce.models.price_book import PricingResult, PricingScopeType
from commerce.services.credit_service import CreditDecision
from commerce.services.pricing_engine import PricingError


# ---------------------------------------------------------------------------
# Mock Intake Pipeline
# ---------------------------------------------------------------------------


class MockOrderIntakePipeline:
    """A lightweight mock of the OrderIntakePipeline for integration testing.

    Simulates the hook execution flow from the real pipeline:
    1. Accepts an order draft
    2. Runs registered hooks' before_accept in sequence
    3. Persists the order (mock)
    4. Runs registered hooks' after_accept in sequence

    This mirrors the real pipeline's _ingest_common logic at steps (i2)
    and (m3) without requiring ES, Redis, or other infrastructure.
    """

    def __init__(self) -> None:
        self._hooks: List[Any] = []
        self._persisted_orders: List[Dict[str, Any]] = []

    def register_hook(self, hook: Any) -> None:
        """Register an IntakeHook conforming to the protocol."""
        self._hooks.append(hook)

    async def ingest(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Run the intake pipeline on an order draft.

        Steps:
        1. Run each hook's before_accept in registration order.
           If any hook raises, the order is rejected (exception propagates).
        2. Persist the order (append to internal list).
        3. Run each hook's after_accept in registration order.
           Failures in after_accept are logged but do not block.

        Returns:
            The final order document after all hooks and persistence.

        Raises:
            Any exception raised by a before_accept hook (order rejected).
        """
        # Run before_accept hooks in sequence
        doc = dict(order_draft)
        for hook in self._hooks:
            doc = await hook.before_accept(doc)

        # Persist the order (mock)
        self._persisted_orders.append(doc)

        # Run after_accept hooks in sequence (side-effects only)
        for hook in self._hooks:
            try:
                await hook.after_accept(doc)
            except Exception:
                pass  # after_accept failures are non-blocking

        return doc

    @property
    def persisted_orders(self) -> List[Dict[str, Any]]:
        """Access the list of persisted orders."""
        return self._persisted_orders


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_integration_test"
_ACCOUNT_ID = "acct_integ_001"
_PRODUCT_CODE = "ULSD"


def _make_order_draft(
    *,
    tenant_id: str = _TENANT_ID,
    account_id: str = _ACCOUNT_ID,
    product_code: str = _PRODUCT_CODE,
    gallons_requested: float = 500.0,
    ship_to_state: str = "TX",
) -> Dict[str, Any]:
    """Build a minimal order draft for testing."""
    return {
        "order_id": "ord_test_001",
        "tenant_id": tenant_id,
        "account_id": account_id,
        "product_code": product_code,
        "gallons_requested": gallons_requested,
        "ship_to_state": ship_to_state,
        "status": "pending",
    }


def _make_pricing_engine(
    *,
    unit_price_cents: int = 35000,
    rule_id: str = "rule_default_ulsd",
    scope_type: PricingScopeType = PricingScopeType.DEFAULT,
    raise_no_rule: bool = False,
) -> AsyncMock:
    """Create a mocked PricingEngine.

    When raise_no_rule=True, resolve() raises PricingError.no_rule_matched.
    Otherwise, it returns a PricingResult with the given values.
    """
    engine = AsyncMock()

    if raise_no_rule:
        engine.resolve = AsyncMock(
            side_effect=PricingError.no_rule_matched(
                _TENANT_ID, _PRODUCT_CODE, _ACCOUNT_ID
            )
        )
    else:
        engine.resolve = AsyncMock(
            return_value=PricingResult(
                unit_price_cents=unit_price_cents,
                rule_id=rule_id,
                scope_type=scope_type,
                matched_from_cache=False,
            )
        )

    # Provide a mock ES client for the _resolve_account helper
    engine._es = AsyncMock()
    engine._es.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "account_id": _ACCOUNT_ID,
                            "tenant_id": _TENANT_ID,
                            "customer_id": "cust_test_001",
                            "display_name": "Test Account",
                            "tier": "default",
                            "credit_limit_cents": 1000000,
                            "net_terms_days": 30,
                        }
                    }
                ]
            }
        }
    )

    return engine


def _make_credit_service(
    *, hold_required: bool = False, reason: str = "within_credit_limit"
) -> AsyncMock:
    """Create a mocked CreditService.

    When hold_required=True, check() returns a CreditDecision with
    hold_required=True and reason=credit_limit_exceeded.
    """
    service = AsyncMock()
    service.check = AsyncMock(
        return_value=CreditDecision(
            approved=not hold_required,
            reason=reason,
            hold_required=hold_required,
            override_active=False,
        )
    )
    return service


def _mock_settings(
    *,
    pricing_enabled: bool = True,
    credit_enabled: bool = True,
) -> MagicMock:
    """Create a mock settings object with commerce feature flags."""
    settings = MagicMock()
    settings.commerce_backbone_enabled = True
    settings.commerce_pricing_engine_enabled = pricing_enabled
    settings.commerce_credit_holds_enabled = credit_enabled
    settings.commerce_customers_enabled = True
    settings.commerce_invoicing_enabled = True
    settings.commerce_dunning_enabled = False
    return settings


# ---------------------------------------------------------------------------
# Test: Pricing attaches to an accepted order
# ---------------------------------------------------------------------------


class TestPricingAttaches:
    """When PricingEngine resolves successfully, the order draft gets
    unit_price_cents, subtotal_cents, tax_cents, total_cents attached.

    Validates: Requirements 4.1, 4.5
    """

    @pytest.mark.asyncio
    async def test_pricing_fields_attached_on_successful_resolve(self):
        """Pricing hook populates all four pricing fields on the order."""
        pricing_engine = _make_pricing_engine(unit_price_cents=35000)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pricing_hook = PricingHook(pricing_engine)
        credit_hook = CreditCheckHook(credit_service)

        pipeline.register_hook(pricing_hook)
        pipeline.register_hook(credit_hook)

        draft = _make_order_draft(gallons_requested=500.0, ship_to_state="TX")

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            # Mock the tax rates to return a known rate for TX (625 bps = 6.25%)
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {"TX": 625}},
            ):
                result = await pipeline.ingest(draft)

        # Verify pricing fields are attached
        assert result["unit_price_cents"] == 35000

        # subtotal = unit_price_cents * gallons = 35000 * 500 = 17500000
        expected_subtotal = int(35000 * 500.0)
        assert result["subtotal_cents"] == expected_subtotal

        # tax = (subtotal * 625) // 10000
        expected_tax = (expected_subtotal * 625) // 10000
        assert result["tax_cents"] == expected_tax

        # total = subtotal + tax
        expected_total = expected_subtotal + expected_tax
        assert result["total_cents"] == expected_total

    @pytest.mark.asyncio
    async def test_pricing_fields_are_integer_cents(self):
        """All pricing fields are integer values (Constraint C1)."""
        pricing_engine = _make_pricing_engine(unit_price_cents=12345)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft(gallons_requested=123.456)

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 500, "states": {}},
            ):
                result = await pipeline.ingest(draft)

        # All pricing fields must be integers
        assert isinstance(result["unit_price_cents"], int)
        assert isinstance(result["subtotal_cents"], int)
        assert isinstance(result["tax_cents"], int)
        assert isinstance(result["total_cents"], int)

    @pytest.mark.asyncio
    async def test_order_persisted_after_pricing(self):
        """The order is persisted in the pipeline after pricing attaches."""
        pricing_engine = _make_pricing_engine(unit_price_cents=20000)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                await pipeline.ingest(draft)

        assert len(pipeline.persisted_orders) == 1
        persisted = pipeline.persisted_orders[0]
        assert persisted["unit_price_cents"] == 20000
        assert "total_cents" in persisted


# ---------------------------------------------------------------------------
# Test: no_rule_matched rejects the order
# ---------------------------------------------------------------------------


class TestNoRuleMatchedRejects:
    """When PricingEngine raises PricingError.no_rule_matched, the order
    is rejected (exception propagates, order is NOT persisted).

    Validates: Requirements 4.2
    """

    @pytest.mark.asyncio
    async def test_no_rule_matched_raises_pricing_error(self):
        """PricingError.no_rule_matched propagates and rejects the order."""
        pricing_engine = _make_pricing_engine(raise_no_rule=True)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with pytest.raises(PricingError) as exc_info:
                await pipeline.ingest(draft)

        # Verify the error code matches the no_rule error
        assert "no_rule" in exc_info.value.code.lower() or "no_rule" in exc_info.value.code

    @pytest.mark.asyncio
    async def test_no_rule_matched_order_not_persisted(self):
        """When pricing rejects, the order is NOT persisted."""
        pricing_engine = _make_pricing_engine(raise_no_rule=True)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with pytest.raises(PricingError):
                await pipeline.ingest(draft)

        # Order must NOT be persisted
        assert len(pipeline.persisted_orders) == 0

    @pytest.mark.asyncio
    async def test_no_rule_matched_error_contains_details(self):
        """The PricingError includes tenant, product, and account details."""
        pricing_engine = _make_pricing_engine(raise_no_rule=True)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with pytest.raises(PricingError) as exc_info:
                await pipeline.ingest(draft)

        assert exc_info.value.details["tenant_id"] == _TENANT_ID
        assert exc_info.value.details["product_code"] == _PRODUCT_CODE
        assert exc_info.value.details["account_id"] == _ACCOUNT_ID


# ---------------------------------------------------------------------------
# Test: Credit hold lands the order with status=on_hold
# ---------------------------------------------------------------------------


class TestCreditHoldOnHold:
    """When CreditService.check returns hold_required=True, the order gets
    status=on_hold and hold_reason=credit_limit_exceeded.

    Validates: Requirements 4.3
    """

    @pytest.mark.asyncio
    async def test_credit_hold_stamps_on_hold_status(self):
        """Order gets status=on_hold when credit check requires hold."""
        pricing_engine = _make_pricing_engine(unit_price_cents=50000)
        credit_service = _make_credit_service(
            hold_required=True, reason="credit_limit_exceeded"
        )

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                result = await pipeline.ingest(draft)

        assert result["status"] == "on_hold"

    @pytest.mark.asyncio
    async def test_credit_hold_stamps_hold_reason(self):
        """Order gets hold_reason=credit_limit_exceeded on credit hold."""
        pricing_engine = _make_pricing_engine(unit_price_cents=50000)
        credit_service = _make_credit_service(
            hold_required=True, reason="credit_limit_exceeded"
        )

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                result = await pipeline.ingest(draft)

        assert result["hold_reason"] == "credit_limit_exceeded"

    @pytest.mark.asyncio
    async def test_credit_hold_order_still_persisted(self):
        """On-hold orders are still persisted (accepted but held)."""
        pricing_engine = _make_pricing_engine(unit_price_cents=50000)
        credit_service = _make_credit_service(
            hold_required=True, reason="credit_limit_exceeded"
        )

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                await pipeline.ingest(draft)

        # Order IS persisted (unlike the no_rule_matched case)
        assert len(pipeline.persisted_orders) == 1
        persisted = pipeline.persisted_orders[0]
        assert persisted["status"] == "on_hold"
        assert persisted["hold_reason"] == "credit_limit_exceeded"

    @pytest.mark.asyncio
    async def test_credit_hold_pricing_still_attached(self):
        """Even when on hold, pricing fields are still attached."""
        pricing_engine = _make_pricing_engine(unit_price_cents=40000)
        credit_service = _make_credit_service(
            hold_required=True, reason="credit_limit_exceeded"
        )

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft(gallons_requested=100.0)

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                result = await pipeline.ingest(draft)

        # Pricing is attached even though order is on hold
        assert result["unit_price_cents"] == 40000
        assert result["subtotal_cents"] == int(40000 * 100.0)
        assert result["total_cents"] == int(40000 * 100.0)  # 0% tax
        assert result["status"] == "on_hold"


# ---------------------------------------------------------------------------
# Test: Feature flag gating
# ---------------------------------------------------------------------------


class TestFeatureFlagGating:
    """Hooks short-circuit to no-op when their feature flags are off.

    Validates: Requirements 4.6, 8.2
    """

    @pytest.mark.asyncio
    async def test_pricing_disabled_leaves_fields_null(self):
        """When pricing flag is off, pricing fields remain unset."""
        pricing_engine = _make_pricing_engine(unit_price_cents=99999)
        credit_service = _make_credit_service(hold_required=False)

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=False, credit_enabled=True)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            result = await pipeline.ingest(draft)

        # Pricing fields should NOT be set
        assert "unit_price_cents" not in result
        assert "subtotal_cents" not in result
        assert "tax_cents" not in result
        assert "total_cents" not in result

    @pytest.mark.asyncio
    async def test_credit_disabled_no_hold(self):
        """When credit flag is off, no hold is applied even if over limit."""
        pricing_engine = _make_pricing_engine(unit_price_cents=50000)
        credit_service = _make_credit_service(
            hold_required=True, reason="credit_limit_exceeded"
        )

        pipeline = MockOrderIntakePipeline()
        pipeline.register_hook(PricingHook(pricing_engine))
        pipeline.register_hook(CreditCheckHook(credit_service))

        draft = _make_order_draft()

        mock_settings = _mock_settings(pricing_enabled=True, credit_enabled=False)

        with patch("commerce.hooks.intake_hooks.get_settings", return_value=mock_settings):
            with patch(
                "commerce.hooks.intake_hooks._load_tax_rates",
                return_value={"default_rate_bps": 0, "states": {}},
            ):
                result = await pipeline.ingest(draft)

        # Credit check should NOT have run — no hold
        assert result.get("status") != "on_hold"
        assert "hold_reason" not in result
