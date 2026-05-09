"""
Commerce error codes for the Runsheet backend.

This module defines domain-specific error codes used by the Commerce
Backbone services. Each code follows the dotted-namespace convention:
`commerce.<domain>.<condition>`.

These codes are used in API error responses, structured logging, and
metrics dimensions throughout the commerce layer.
"""

from enum import Enum


class CommerceErrorCode(str, Enum):
    """
    Enumeration of commerce-specific error codes.

    Codes follow the pattern `commerce.<domain>.<condition>` and map to
    specific business-rule violations within the commerce backbone.
    """

    # Pricing errors
    PRICING_NO_RULE = "commerce.pricing.no_rule"
    """PricingEngine found no matching rule for the given product/account/moment (Req 3.2, 4.2)"""

    PRICING_UNKNOWN_PRODUCT = "commerce.pricing.unknown_product"
    """Product code failed canonicalization via fuel_product_catalog.canonicalize"""

    PRICING_AMBIGUOUS_RESOLVED = "commerce.pricing.ambiguous_resolved"
    """Two rules tied at the same precedence tier — deterministic tiebreak applied (Req 3.5)"""

    # Credit errors
    CREDIT_HOLD = "commerce.credit.hold"
    """Credit check blocks the order because account is at/over limit (Req 4.3)"""

    CREDIT_OVERRIDE_EXPIRED = "commerce.credit.override_expired"
    """A credit override has expired and is no longer valid"""

    # Invoice errors
    INVOICE_INVALID_STATE = "commerce.invoice.invalid_state"
    """Requested invoice state transition is not allowed by the lifecycle"""

    INVOICE_ALREADY_VOIDED = "commerce.invoice.already_voided"
    """Attempted to void an invoice that is already in void state"""

    # Payment errors
    PAYMENT_DUPLICATE = "commerce.payment.duplicate"
    """Duplicate payment detected via IdempotencyService (Req 6.5)"""

    PAYMENT_AMOUNT_EXCEEDS_INVOICE = "commerce.payment.amount_exceeds_invoice"
    """Payment amount exceeds the invoice remaining balance"""
