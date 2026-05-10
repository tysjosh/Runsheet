"""DyedDieselIntakeHook — validates dyed-diesel orders during intake.

Conforms to the IntakeHook protocol (design §4.4). When a customer
orders a dyed-diesel product (OFF_ROAD_DIESEL, DYED_DIESEL, DYED_ULSD,
OFF_ROAD_ULSD), this hook calls ``DyedDieselEnforcer.validate_order()``
to verify the customer has a valid, non-expired IRS 637M exemption
certificate on file.

If validation fails, the hook raises an exception that rejects the order
with the error code from the ValidationResult (``dyed.no_valid_exemption``).

The hook short-circuits to a no-op when:
- The product_code is not a dyed-diesel code.
- The DyedDieselEnforcer is not available (graceful degradation).

Validates: Requirement 6.1
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class DyedDieselOrderRejected(Exception):
    """Raised when a dyed-diesel order fails validation.

    Attributes:
        error_code: Machine-readable error code from the enforcer.
        message: Human-readable explanation.
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class DyedDieselIntakeHook:
    """Intake hook that validates dyed-diesel orders against IRS 637M certificates.

    Conforms to the IntakeHook protocol. Calls
    ``DyedDieselEnforcer.validate_order()`` for orders with dyed-diesel
    product codes. If validation fails, raises ``DyedDieselOrderRejected``
    which causes the intake pipeline to reject the order.

    Graceful degradation: if the enforcer is unavailable or raises an
    unexpected exception, the hook logs a warning and allows the order
    through (fail-open for non-critical path errors, fail-closed for
    explicit validation failures).

    Args:
        dyed_diesel_enforcer: The DyedDieselEnforcer service instance.

    Validates: Requirement 6.1
    """

    def __init__(self, dyed_diesel_enforcer: Any) -> None:
        self._enforcer = dyed_diesel_enforcer

    async def before_accept(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Validate dyed-diesel orders before acceptance.

        If the order's product_code is a dyed-diesel code, calls
        ``validate_order()`` on the enforcer. If validation fails,
        raises ``DyedDieselOrderRejected`` to reject the order.

        Non-dyed-diesel orders pass through unchanged.

        Args:
            order_draft: Mutable dict representing the order before persist.

        Returns:
            The (unmodified) order draft on success.

        Raises:
            DyedDieselOrderRejected: When the customer lacks a valid
                IRS 637M certificate for dyed-diesel purchases.
        """
        from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer

        product_code = order_draft.get("product_code")
        if not product_code:
            return order_draft

        # Quick check — skip non-dyed products without hitting ES
        if not DyedDieselEnforcer.is_dyed_diesel(product_code):
            return order_draft

        tenant_id = order_draft.get("tenant_id", "")
        customer_id = order_draft.get("customer_id", "")

        if not tenant_id or not customer_id:
            logger.warning(
                "DyedDieselIntakeHook: missing tenant_id or customer_id "
                "on dyed-diesel order — skipping validation"
            )
            return order_draft

        try:
            result = await self._enforcer.validate_order(
                tenant_id=tenant_id,
                customer_id=customer_id,
                product_code=product_code,
            )
        except Exception as exc:
            # Unexpected error from the enforcer — fail-open with warning.
            # The order proceeds; the enforcer may be temporarily unavailable.
            logger.warning(
                "DyedDieselIntakeHook: enforcer raised unexpectedly for "
                "customer=%s, product=%s (tenant=%s): %s — allowing order",
                customer_id,
                product_code,
                tenant_id,
                exc,
            )
            return order_draft

        if not result.valid:
            # Explicit validation failure — reject the order
            logger.info(
                "DyedDieselIntakeHook: rejecting order for customer=%s, "
                "product=%s (tenant=%s): %s",
                customer_id,
                product_code,
                tenant_id,
                result.error_code,
            )
            raise DyedDieselOrderRejected(
                error_code=result.error_code or "dyed.no_valid_exemption",
                message=result.message or (
                    f"Customer '{customer_id}' does not have a valid IRS 637M "
                    f"exemption certificate for dyed diesel purchases"
                ),
            )

        return order_draft

    async def after_accept(self, order: Dict[str, Any]) -> None:
        """No-op after acceptance — dyed-diesel validation is pre-accept only.

        Args:
            order: The persisted order document.
        """
        pass
