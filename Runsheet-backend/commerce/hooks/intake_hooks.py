"""PricingHook and CreditCheckHook for the OrderIntakePipeline.

These hooks conform to the IntakeHook protocol defined in design §4.4:

    class IntakeHook(Protocol):
        async def before_accept(self, order_draft: OrderDraft) -> OrderDraft: ...
        async def after_accept(self, order: Order) -> None: ...

Commerce registers two hooks:

- **PricingHook**: Resolves PricingEngine for each line item, populates
  unit_price_cents, subtotal_cents, tax_cents, total_cents. Raises
  PricingError.no_rule_matched → intake rejects the order.

- **CreditCheckHook**: Runs CreditService.check. If hold_required, stamps
  draft.hold_reason = "credit_limit_exceeded"; intake still accepts but
  with status=on_hold.

Both hooks short-circuit to no-op when their respective feature flags are
off. The flags are re-evaluated per-request (not just at startup) so a
flag flip takes effect without a restart.

Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6, 8.1, 8.2
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

import yaml

from config.settings import get_settings
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IntakeHook Protocol (design §4.4)
# ---------------------------------------------------------------------------


class IntakeHook(Protocol):
    """Protocol for hooks that plug into the OrderIntakePipeline.

    Hooks are called in sequence during order intake:
    - before_accept: called before the order is persisted. May mutate
      the draft or raise to reject the order.
    - after_accept: called after the order is persisted. Used for
      side-effects (notifications, event emission).
    """

    async def before_accept(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Process the order draft before acceptance.

        Args:
            order_draft: Mutable dict representing the order before persist.

        Returns:
            The (possibly mutated) order draft.

        Raises:
            Any exception to reject the order.
        """
        ...  # pragma: no cover

    async def after_accept(self, order: Dict[str, Any]) -> None:
        """Process the order after acceptance (side-effects only).

        Args:
            order: The persisted order document.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Tax rate loader (Req 4.5)
# ---------------------------------------------------------------------------

_TAX_RATES_CACHE: Optional[Dict[str, Any]] = None


def _load_tax_rates() -> Dict[str, Any]:
    """Load flat-rate-per-state tax rates from config/commerce/tax_rates.yml.

    Caches the result in module-level state. The file is read once per
    process lifetime — a restart picks up changes.

    Returns:
        Dict with 'default_rate_bps' (int) and 'states' (dict of state -> bps).
    """
    global _TAX_RATES_CACHE
    if _TAX_RATES_CACHE is not None:
        return _TAX_RATES_CACHE

    # Resolve the path relative to the backend root
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "commerce" / "tax_rates.yml"

    if not config_path.exists():
        logger.warning(
            "Tax rates config not found at %s — using 0%% default",
            config_path,
        )
        _TAX_RATES_CACHE = {"default_rate_bps": 0, "states": {}}
        return _TAX_RATES_CACHE

    with open(config_path, "r") as f:
        data = yaml.safe_load(f)

    _TAX_RATES_CACHE = {
        "default_rate_bps": data.get("default_rate_bps", 0),
        "states": data.get("states", {}),
    }
    return _TAX_RATES_CACHE


def _get_tax_rate_bps(state: Optional[str]) -> int:
    """Get the tax rate in basis points for a given US state code.

    Args:
        state: Two-letter US state code (e.g. "TX"). None or unknown
               states fall back to default_rate_bps.

    Returns:
        Tax rate in basis points (e.g. 625 = 6.25%).
    """
    rates = _load_tax_rates()
    if state and state.upper() in rates["states"]:
        return rates["states"][state.upper()]
    return rates["default_rate_bps"]


def _compute_tax_cents(subtotal_cents: int, state: Optional[str]) -> int:
    """Compute tax in integer cents using flat-rate-per-state lookup.

    Formula: tax_cents = (subtotal_cents * rate_bps) // 10000

    All arithmetic is integer-only (Constraint C1).

    Args:
        subtotal_cents: The subtotal in cents.
        state: Two-letter US state code for tax lookup.

    Returns:
        Tax amount in integer cents.
    """
    rate_bps = _get_tax_rate_bps(state)
    # Integer arithmetic only — no floats (Constraint C1)
    return (subtotal_cents * rate_bps) // 10000


# ---------------------------------------------------------------------------
# PricingHook
# ---------------------------------------------------------------------------


class PricingHook:
    """Intake hook that resolves pricing for each line item on an order.

    Conforms to the IntakeHook protocol. When
    ``commerce.pricing_engine_enabled`` is off for the tenant, the hook
    short-circuits to a no-op and the order proceeds with pricing fields
    as null (Req 4.6).

    When enabled, the hook:
    1. Resolves PricingEngine for each line item (currently single-line
       orders based on the FuelOrder model).
    2. Attaches unit_price_cents, subtotal_cents, tax_cents, total_cents.
    3. Raises PricingError.no_rule_matched if no pricing rule applies,
       causing the intake pipeline to reject the order (Req 4.2).

    The feature flag is re-evaluated on every call (not cached at startup)
    so flag flips take effect without a restart.

    Validates: Requirements 4.1, 4.2, 4.5, 4.6, 8.2
    """

    def __init__(self, pricing_engine: Any) -> None:
        """Initialize the PricingHook.

        Args:
            pricing_engine: A PricingEngine instance for resolving prices.
        """
        self._pricing_engine = pricing_engine

    async def before_accept(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve pricing for the order draft's line items.

        Short-circuits to no-op when ``commerce.pricing_engine_enabled``
        is off. The flag is checked per-request.

        Args:
            order_draft: Mutable dict representing the order before persist.

        Returns:
            The order draft with pricing fields populated (or unchanged
            if the flag is off).

        Raises:
            PricingError: When no pricing rule matches (Req 4.2).
        """
        # Re-evaluate feature flag per-request (not cached at startup)
        settings = get_settings()
        if not settings.commerce_pricing_engine_enabled:
            logger.debug(
                "PricingHook: commerce_pricing_engine_enabled is off, "
                "skipping pricing for order tenant=%s",
                order_draft.get("tenant_id"),
            )
            return order_draft

        tenant_id = order_draft.get("tenant_id", "")
        product_code = order_draft.get("product_code")
        quantity_gallons = order_draft.get("gallons_requested") or 0.0
        account_id = order_draft.get("account_id")

        # If no product_code, skip pricing (legacy orders)
        if not product_code:
            logger.debug(
                "PricingHook: no product_code on order draft, skipping pricing"
            )
            return order_draft

        # Build a minimal Account object for the pricing engine
        account = await self._resolve_account(tenant_id, account_id)
        if account is None:
            logger.warning(
                "PricingHook: could not resolve account for tenant=%s "
                "account_id=%s, skipping pricing",
                tenant_id,
                account_id,
            )
            return order_draft

        # Resolve pricing — raises PricingError.no_rule_matched on failure
        from commerce.services.pricing_engine import PricingError

        result = await self._pricing_engine.resolve(
            tenant_id=tenant_id,
            account=account,
            product_code=product_code,
            moment=utcnow(),
            quantity_gallons=quantity_gallons,
        )

        # Compute line totals (integer cents only — Constraint C1)
        unit_price_cents = result.unit_price_cents
        subtotal_cents = int(unit_price_cents * quantity_gallons)

        # Tax calculation using flat-rate-per-state (Req 4.5)
        state = self._extract_state(order_draft)
        tax_cents = _compute_tax_cents(subtotal_cents, state)
        total_cents = subtotal_cents + tax_cents

        # Attach pricing fields to the order draft (Req 4.1)
        order_draft["unit_price_cents"] = unit_price_cents
        order_draft["subtotal_cents"] = subtotal_cents
        order_draft["tax_cents"] = tax_cents
        order_draft["total_cents"] = total_cents

        logger.info(
            "PricingHook: priced order for tenant=%s product=%s "
            "unit=%d subtotal=%d tax=%d total=%d (rule=%s)",
            tenant_id,
            product_code,
            unit_price_cents,
            subtotal_cents,
            tax_cents,
            total_cents,
            result.rule_id,
        )

        return order_draft

    async def after_accept(self, order: Dict[str, Any]) -> None:
        """No-op after acceptance for the pricing hook."""
        pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_account(
        self, tenant_id: str, account_id: Optional[str]
    ) -> Optional[Any]:
        """Resolve an Account object for the pricing engine.

        Attempts to load the account from ES via the AccountService.
        Returns None if account_id is not provided or the account
        cannot be found.
        """
        if not account_id:
            return None

        try:
            from commerce.models.account import Account, AccountTier
            from commerce.services.commerce_es_mappings import ACCOUNTS_CURRENT_INDEX
            from ops.middleware.tenant_guard import inject_tenant_filter

            # Use the pricing engine's ES client to look up the account
            query: Dict[str, Any] = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"account_id": account_id}},
                        ]
                    }
                },
                "size": 1,
            }
            query = inject_tenant_filter(query, tenant_id)

            response = await self._pricing_engine._es.search_documents(
                ACCOUNTS_CURRENT_INDEX, query, size=1
            )

            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                return None

            account_data = hits[0]["_source"]

            # Build an Account model for the pricing engine
            return Account(
                account_id=account_data.get("account_id", account_id),
                tenant_id=tenant_id,
                customer_id=account_data.get("customer_id", ""),
                display_name=account_data.get("display_name", ""),
                tier=account_data.get("tier", "default"),
                credit_limit_cents=account_data.get("credit_limit_cents", 0),
                net_terms_days=account_data.get("net_terms_days", 30),
            )
        except Exception as exc:
            logger.warning(
                "PricingHook: failed to resolve account %s for tenant %s: %s",
                account_id,
                tenant_id,
                exc,
            )
            return None

    @staticmethod
    def _extract_state(order_draft: Dict[str, Any]) -> Optional[str]:
        """Extract the US state code from the order draft for tax lookup.

        Looks for a 'state' field in the order draft, or attempts to
        parse it from the ship_to_address. Returns None if not found.
        """
        # Direct state field (if present on the draft)
        state = order_draft.get("ship_to_state")
        if state:
            return state

        # Try to extract from billing/shipping address
        billing_address = order_draft.get("billing_address")
        if isinstance(billing_address, dict):
            state = billing_address.get("state")
            if state:
                return state

        # Fallback: no state available
        return None


# ---------------------------------------------------------------------------
# CreditCheckHook
# ---------------------------------------------------------------------------


class CreditCheckHook:
    """Intake hook that runs a credit check on the order's account.

    Conforms to the IntakeHook protocol. When
    ``commerce.credit_holds_enabled`` is off for the tenant, the hook
    short-circuits to a no-op and the order proceeds without credit
    evaluation (Req 8.2).

    When enabled, the hook:
    1. Runs CreditService.check for the order's account.
    2. If hold_required, stamps draft.hold_reason = "credit_limit_exceeded"
       and draft.status = "on_hold". The intake pipeline still accepts the
       order but with on_hold status (Req 4.3).
    3. If approved, the order proceeds normally.

    The feature flag is re-evaluated on every call (not cached at startup)
    so flag flips take effect without a restart.

    Validates: Requirements 4.3, 4.4, 8.2
    """

    def __init__(self, credit_service: Any) -> None:
        """Initialize the CreditCheckHook.

        Args:
            credit_service: A CreditService instance for credit evaluation.
        """
        self._credit_service = credit_service

    async def before_accept(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Run credit check on the order draft.

        Short-circuits to no-op when ``commerce.credit_holds_enabled``
        is off. The flag is checked per-request.

        Args:
            order_draft: Mutable dict representing the order before persist.

        Returns:
            The order draft, possibly with hold_reason and status=on_hold
            stamped if the credit check requires a hold.
        """
        # Re-evaluate feature flag per-request (not cached at startup)
        settings = get_settings()
        if not settings.commerce_credit_holds_enabled:
            logger.debug(
                "CreditCheckHook: commerce_credit_holds_enabled is off, "
                "skipping credit check for order tenant=%s",
                order_draft.get("tenant_id"),
            )
            return order_draft

        tenant_id = order_draft.get("tenant_id", "")
        account_id = order_draft.get("account_id")

        # If no account_id, skip credit check (cannot evaluate without an account)
        if not account_id:
            logger.debug(
                "CreditCheckHook: no account_id on order draft, "
                "skipping credit check"
            )
            return order_draft

        # Compute order total for credit evaluation
        order_total_cents = order_draft.get("total_cents") or 0

        try:
            decision = await self._credit_service.check(
                tenant_id=tenant_id,
                account_id=account_id,
                order_total_cents=order_total_cents,
            )
        except Exception as exc:
            # Credit check failures should not block order intake —
            # log and proceed without hold
            logger.error(
                "CreditCheckHook: credit check failed for tenant=%s "
                "account=%s: %s — proceeding without hold",
                tenant_id,
                account_id,
                exc,
            )
            return order_draft

        if decision.hold_required:
            # Stamp hold_reason and status=on_hold (Req 4.3)
            order_draft["hold_reason"] = "credit_limit_exceeded"
            order_draft["status"] = "on_hold"

            logger.info(
                "CreditCheckHook: order placed on hold for tenant=%s "
                "account=%s reason=%s (override_active=%s)",
                tenant_id,
                account_id,
                decision.reason,
                decision.override_active,
            )
        else:
            logger.debug(
                "CreditCheckHook: credit approved for tenant=%s account=%s "
                "reason=%s",
                tenant_id,
                account_id,
                decision.reason,
            )

        return order_draft

    async def after_accept(self, order: Dict[str, Any]) -> None:
        """No-op after acceptance for the credit check hook."""
        pass
