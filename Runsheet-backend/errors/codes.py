"""
Error code catalog for the Runsheet backend.

This module defines all error codes used throughout the application,
covering validation errors, authentication errors, external service
failures, and internal errors.

Validates: Requirement 2.2 - Define a catalog of error codes covering
validation errors, authentication errors, external service failures,
and internal errors.
"""

from enum import Enum


class ErrorCode(str, Enum):
    """
    Enumeration of all error codes used in the application.
    
    Each error code maps to a specific HTTP status code and error category:
    - Validation errors (4xx): Client request issues
    - Authentication errors (4xx): Auth/authz failures
    - External service errors (5xx): Dependency failures
    - Internal errors (5xx): Server-side issues
    """
    
    # Validation errors (4xx)
    VALIDATION_ERROR = "VALIDATION_ERROR"
    """Request payload validation failed (HTTP 400)"""
    
    INVALID_REQUEST = "INVALID_REQUEST"
    """Malformed request structure (HTTP 400)"""
    
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    """Requested resource does not exist (HTTP 404)"""
    
    # Authentication errors (4xx)
    UNAUTHORIZED = "UNAUTHORIZED"
    """Authentication required (HTTP 401)"""
    
    FORBIDDEN = "FORBIDDEN"
    """Insufficient permissions (HTTP 403)"""
    
    RATE_LIMITED = "RATE_LIMITED"
    """Too many requests (HTTP 429)"""
    
    # External service errors (5xx)
    ELASTICSEARCH_UNAVAILABLE = "ELASTICSEARCH_UNAVAILABLE"
    """Database connection failed (HTTP 503)"""
    
    AI_SERVICE_UNAVAILABLE = "AI_SERVICE_UNAVAILABLE"
    """Gemini API unavailable (HTTP 503)"""
    
    SESSION_STORE_UNAVAILABLE = "SESSION_STORE_UNAVAILABLE"
    """Redis/DynamoDB unavailable (HTTP 503)"""
    
    # Ops Intelligence errors (4xx)
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    """Webhook HMAC-SHA256 signature verification failed (HTTP 401)"""
    
    WEBHOOK_SCHEMA_UNKNOWN = "WEBHOOK_SCHEMA_UNKNOWN"
    """Webhook payload contains unrecognized schema version (HTTP 200, routed to poison queue)"""
    
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    """Tenant does not exist (HTTP 404)"""
    
    TENANT_DISABLED = "TENANT_DISABLED"
    """Tenant ops intelligence is disabled via feature flag (HTTP 404)"""

    LEGACY_NG_DELIVERY_DISABLED = "LEGACY_NG_DELIVERY_DISABLED"
    """Legacy Nigerian last-mile surface is off via ``legacy_ng_delivery`` (HTTP 404)"""
    
    # Ops Intelligence errors (5xx / operational)
    POISON_QUEUE_MAX_RETRIES = "POISON_QUEUE_MAX_RETRIES"
    """Poison queue event exceeded maximum retry count (HTTP 422)"""
    
    DRIFT_THRESHOLD_EXCEEDED = "DRIFT_THRESHOLD_EXCEEDED"
    """Source-replica drift exceeds configured threshold (HTTP 409)"""
    
    BACKFILL_IN_PROGRESS = "BACKFILL_IN_PROGRESS"
    """A backfill job is already running for this tenant (HTTP 409)"""
    
    # Order Intake Pipeline errors (4xx / conflict)
    INVALID_CUSTOMER_TANK_REF = "INVALID_CUSTOMER_TANK_REF"
    """Referenced customer tank does not exist or belongs to another tenant (HTTP 400)"""

    MISSING_VOLUME = "MISSING_VOLUME"
    """Order has no gallons_requested and fill_to_full is false (HTTP 400)"""

    INVALID_DELIVERY_WINDOW = "INVALID_DELIVERY_WINDOW"
    """Delivery window is invalid — end is before or equal to start (HTTP 400)"""

    MISSING_DELIVERY_WINDOW = "MISSING_DELIVERY_WINDOW"
    """Order lacks a delivery window required for the target status (HTTP 409)"""

    MISSING_PRODUCT_CODE = "MISSING_PRODUCT_CODE"
    """Non-legacy intake channel order is missing a product code (HTTP 400)"""

    MISSING_CLIENT_EVENT_ID = "MISSING_CLIENT_EVENT_ID"
    """Dispatcher intake request is missing the required client_event_id (HTTP 400)"""

    MISSING_HOLD_REASON = "MISSING_HOLD_REASON"
    """Order placed on hold without a hold_reason (HTTP 400)"""

    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    """Requested status transition is not allowed by the order state machine (HTTP 409)"""

    CHANNEL_DISABLED = "CHANNEL_DISABLED"
    """Intake channel is disabled and cannot accept orders (HTTP 403)"""

    INSUFFICIENT_ROLE = "INSUFFICIENT_ROLE"
    """Caller lacks the required role for this operation (HTTP 403)"""

    DRIVER_UNAVAILABLE = "DRIVER_UNAVAILABLE"
    """Driver is off_duty or inactive and cannot be assigned (HTTP 409)"""

    LEGACY_ROUTE_SUNSET = "LEGACY_ROUTE_SUNSET"
    """Legacy route has been sunset and is no longer available (HTTP 410)"""

    SECURITY_TENANT_ID_MISMATCH = "SECURITY_TENANT_ID_MISMATCH"
    """Payload tenant_id does not match the channel's tenant_id (HTTP 403)"""

    # Commerce Backbone errors (4xx)
    COMMERCE_PRICING_NO_RULE = "COMMERCE_PRICING_NO_RULE"
    """PricingEngine found no matching rule for the given product/account/moment (HTTP 422)"""

    COMMERCE_PRICING_UNKNOWN_PRODUCT = "COMMERCE_PRICING_UNKNOWN_PRODUCT"
    """Product code failed canonicalization (HTTP 400)"""

    COMMERCE_PRICING_AMBIGUOUS_RESOLVED = "COMMERCE_PRICING_AMBIGUOUS_RESOLVED"
    """Two pricing rules tied at the same precedence — deterministic tiebreak applied (HTTP 200, warning)"""

    COMMERCE_CREDIT_HOLD = "COMMERCE_CREDIT_HOLD"
    """Credit check blocks the order because account is at/over limit (HTTP 402)"""

    COMMERCE_CREDIT_OVERRIDE_EXPIRED = "COMMERCE_CREDIT_OVERRIDE_EXPIRED"
    """Credit override has expired and is no longer valid (HTTP 409)"""

    COMMERCE_INVOICE_INVALID_STATE = "COMMERCE_INVOICE_INVALID_STATE"
    """Requested invoice state transition is not allowed (HTTP 409)"""

    COMMERCE_INVOICE_ALREADY_VOIDED = "COMMERCE_INVOICE_ALREADY_VOIDED"
    """Attempted to void an invoice that is already voided (HTTP 409)"""

    COMMERCE_PAYMENT_DUPLICATE = "COMMERCE_PAYMENT_DUPLICATE"
    """Duplicate payment detected via IdempotencyService (HTTP 409)"""

    COMMERCE_PAYMENT_AMOUNT_EXCEEDS_INVOICE = "COMMERCE_PAYMENT_AMOUNT_EXCEEDS_INVOICE"
    """Payment amount exceeds the invoice remaining balance (HTTP 422)"""

    # Dinee Voice Integration errors (4xx)
    VOICE_REPLAY_WINDOW_EXCEEDED = "VOICE_REPLAY_WINDOW_EXCEEDED"
    """Voice submission X-Timestamp is stale/missing/invalid — outside the replay window (HTTP 401)"""

    MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
    """Voice submission omitted the required X-Idempotency-Key header (HTTP 400)"""

    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    """Same idempotency key reused with a different request body for the same tenant (HTTP 409)"""

    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    """Voice submission X-Schema-Version is missing or unsupported by the voice channel (HTTP 422)"""

    VOICE_PAYLOAD_INVALID = "VOICE_PAYLOAD_INVALID"
    """VoiceIntakePayload is missing required fields — carries details.missing_fields (HTTP 422)"""

    VOICE_UNAUTHORIZED = "VOICE_UNAUTHORIZED"
    """Voice request failed Bearer/API-key authentication (HTTP 401)"""

    VOICE_TENANT_MISMATCH = "VOICE_TENANT_MISMATCH"
    """X-Runsheet-Tenant does not match the tenant bound to the presented credential (HTTP 403)"""

    # Fuel-ops / compliance entity-lookup errors (4xx, 5xx)
    #
    # NOTE: unlike the rest of this catalog these values are lower-case.
    # They are the already-shipped wire strings for endpoints that used to
    # raise a raw ``HTTPException`` with a ``detail.error_code`` payload.
    # Migrating those handlers to the structured ``ErrorResponse`` envelope
    # must not change the code clients match on, so the value is frozen as
    # the original string while the member name follows the enum convention.
    DEPOT_NOT_FOUND = "depot_not_found"
    """Depot does not exist for the tenant (HTTP 404)"""

    DRIVER_NOT_FOUND = "driver_not_found"
    """Referenced driver does not resolve in the tenant (HTTP 400)"""

    TERMINAL_NOT_FOUND = "terminal_not_found"
    """Terminal does not exist for the tenant (HTTP 404)"""

    SUPPLIER_CONTRACT_NOT_FOUND = "supplier_contract_not_found"
    """Supplier contract does not exist for the tenant (HTTP 404)"""

    KFACTOR_VARIANCE_HISTORY_FAILED = "kfactor.variance_history_failed"
    """Per-tank K-factor variance history could not be loaded (HTTP 500)"""

    # Driver Mobile App errors (4xx / 202)
    #
    # Values are UPPER_SNAKE, matching the rest of the catalog rather than the
    # frozen lower-case fuel-ops lookup block above.
    SESSION_EXPIRED = "SESSION_EXPIRED"
    """Mobile session access token has expired, regardless of refresh-token validity (HTTP 401)"""

    DRIVER_IDENTITY_MISSING = "DRIVER_IDENTITY_MISSING"
    """Caller reached a driver surface with no driver_id on the TenantContext (HTTP 403)"""

    DRIVER_RECORD_NOT_PROVISIONED = "DRIVER_RECORD_NOT_PROVISIONED"
    """Driver-role user has no drivers_current record and cannot sign in (HTTP 403)"""

    APP_ACCESS_ALREADY_LINKED = "APP_ACCESS_ALREADY_LINKED"
    """Driver app access is already linked to another auth user (HTTP 409)"""

    INVALID_PIN_FORMAT = "INVALID_PIN_FORMAT"
    """Submitted PIN does not match the required format (HTTP 422)"""

    WEAK_PIN = "WEAK_PIN"
    """Submitted PIN fails the weak-PIN policy (HTTP 422)"""

    PIN_VERIFICATION_FAILED = "PIN_VERIFICATION_FAILED"
    """Submitted PIN did not verify (HTTP 403)"""

    PIN_ATTEMPTS_EXCEEDED = "PIN_ATTEMPTS_EXCEEDED"
    """Too many failed PIN attempts — lockout in force (HTTP 429)"""

    OTP_REQUIRED = "OTP_REQUIRED"
    """Tenant policy requires an OTP on POD submission and none was supplied (HTTP 422)"""

    OTP_NOT_PROVISIONED = "OTP_NOT_PROVISIONED"
    """OTP is required but none was provisioned for the delivery — fail closed (HTTP 409)"""

    OTP_VERIFICATION_FAILED = "OTP_VERIFICATION_FAILED"
    """Supplied POD OTP did not match the provisioned value (HTTP 403)"""

    OTP_WINDOW_EXPIRED = "OTP_WINDOW_EXPIRED"
    """Supplied POD OTP is outside its validity window (HTTP 409)"""

    POD_GALLONS_CONFIRMATION_REQUIRED = "POD_GALLONS_CONFIRMATION_REQUIRED"
    """Meter-ticket OCR failed or needs review — driver must confirm the gallon count (HTTP 409)"""

    DELIVERED_GALLONS_REQUIRED = "DELIVERED_GALLONS_REQUIRED"
    """POD records no refusal and carries no delivered_gallons value (HTTP 422)"""

    POD_ORDER_REFERENCE_REQUIRED = "POD_ORDER_REFERENCE_REQUIRED"
    """POD submission resolved to an absent or blank order reference (HTTP 422)"""

    STOP_ALREADY_COMPLETED = "STOP_ALREADY_COMPLETED"
    """Stop check-in targets a stop that is already completed (HTTP 409)"""

    AMBIGUOUS_VOLUME_UNIT = "AMBIGUOUS_VOLUME_UNIT"
    """Check-in supplied both actual_quantities (litres) and actual_quantities_gallons (HTTP 422)"""

    VOLUME_QUANTITIES_REQUIRED = "VOLUME_QUANTITIES_REQUIRED"
    """Check-in supplied neither actual_quantities nor actual_quantities_gallons (HTTP 422)"""

    SENDER_IDENTITY_MISMATCH = "SENDER_IDENTITY_MISMATCH"
    """Message body sender_id differs from the identity derived from TenantContext (HTTP 403)"""

    ASSET_OUT_OF_SERVICE = "ASSET_OUT_OF_SERVICE"
    """Assigned asset is out_of_service and cannot move to in_transit (HTTP 409)"""

    PRETRIP_INSPECTION_REQUIRED = "PRETRIP_INSPECTION_REQUIRED"
    """A pre-trip inspection is required and has not been recorded (HTTP 409)"""

    ACTIVE_DELIVERY_IN_PROGRESS = "ACTIVE_DELIVERY_IN_PROGRESS"
    """Duty-status transition blocked by an assigned order still in_transit (HTTP 409)"""

    DUTY_STATUS_PROJECTION_PENDING = "DUTY_STATUS_PROJECTION_PENDING"
    """Duty-status event is durable but the drivers_current projection write lags (HTTP 202)"""

    HOS_LIMIT_REACHED = "HOS_LIMIT_REACHED"
    """Hours-of-service limit reached — the transition is gated (HTTP 409)"""

    HOS_FIGURES_UNAVAILABLE = "HOS_FIGURES_UNAVAILABLE"
    """Hours-of-service figures could not be obtained to evaluate the gate (HTTP 409)"""

    DRIVER_NOT_DISPATCH_ELIGIBLE = "DRIVER_NOT_DISPATCH_ELIGIBLE"
    """Driver fails Dispatch_Eligibility for the requested operation (HTTP 409)"""

    # Internal errors (5xx)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Unexpected server error (HTTP 500)"""
    
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    """Circuit breaker is open (HTTP 503)"""


# Mapping of error codes to their default HTTP status codes
ERROR_CODE_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.ELASTICSEARCH_UNAVAILABLE: 503,
    ErrorCode.AI_SERVICE_UNAVAILABLE: 503,
    ErrorCode.SESSION_STORE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.CIRCUIT_OPEN: 503,
    # Ops Intelligence error codes
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: 401,
    ErrorCode.WEBHOOK_SCHEMA_UNKNOWN: 200,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_DISABLED: 404,
    ErrorCode.LEGACY_NG_DELIVERY_DISABLED: 404,
    ErrorCode.POISON_QUEUE_MAX_RETRIES: 422,
    ErrorCode.DRIFT_THRESHOLD_EXCEEDED: 409,
    ErrorCode.BACKFILL_IN_PROGRESS: 409,
    # Order Intake Pipeline error codes
    # Commerce Backbone error codes
    ErrorCode.COMMERCE_PRICING_NO_RULE: 422,
    ErrorCode.COMMERCE_PRICING_UNKNOWN_PRODUCT: 400,
    ErrorCode.COMMERCE_PRICING_AMBIGUOUS_RESOLVED: 200,
    ErrorCode.COMMERCE_CREDIT_HOLD: 402,
    ErrorCode.COMMERCE_CREDIT_OVERRIDE_EXPIRED: 409,
    ErrorCode.COMMERCE_INVOICE_INVALID_STATE: 409,
    ErrorCode.COMMERCE_INVOICE_ALREADY_VOIDED: 409,
    ErrorCode.COMMERCE_PAYMENT_DUPLICATE: 409,
    ErrorCode.COMMERCE_PAYMENT_AMOUNT_EXCEEDS_INVOICE: 422,
    # Order Intake Pipeline error codes
    ErrorCode.INVALID_CUSTOMER_TANK_REF: 400,
    ErrorCode.MISSING_VOLUME: 400,
    ErrorCode.INVALID_DELIVERY_WINDOW: 400,
    ErrorCode.MISSING_DELIVERY_WINDOW: 409,
    ErrorCode.MISSING_PRODUCT_CODE: 400,
    ErrorCode.MISSING_CLIENT_EVENT_ID: 400,
    ErrorCode.MISSING_HOLD_REASON: 400,
    ErrorCode.INVALID_STATUS_TRANSITION: 409,
    ErrorCode.CHANNEL_DISABLED: 403,
    ErrorCode.INSUFFICIENT_ROLE: 403,
    ErrorCode.DRIVER_UNAVAILABLE: 409,
    ErrorCode.LEGACY_ROUTE_SUNSET: 410,
    ErrorCode.SECURITY_TENANT_ID_MISMATCH: 403,
    # Dinee Voice Integration error codes
    ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED: 401,
    ErrorCode.MISSING_IDEMPOTENCY_KEY: 400,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: 422,
    ErrorCode.VOICE_PAYLOAD_INVALID: 422,
    ErrorCode.VOICE_UNAUTHORIZED: 401,
    ErrorCode.VOICE_TENANT_MISMATCH: 403,
    # Fuel-ops / compliance entity-lookup error codes
    ErrorCode.DEPOT_NOT_FOUND: 404,
    ErrorCode.DRIVER_NOT_FOUND: 400,
    ErrorCode.TERMINAL_NOT_FOUND: 404,
    ErrorCode.SUPPLIER_CONTRACT_NOT_FOUND: 404,
    ErrorCode.KFACTOR_VARIANCE_HISTORY_FAILED: 500,
    # Driver Mobile App error codes
    ErrorCode.SESSION_EXPIRED: 401,
    ErrorCode.DRIVER_IDENTITY_MISSING: 403,
    ErrorCode.DRIVER_RECORD_NOT_PROVISIONED: 403,
    ErrorCode.APP_ACCESS_ALREADY_LINKED: 409,
    ErrorCode.INVALID_PIN_FORMAT: 422,
    ErrorCode.WEAK_PIN: 422,
    ErrorCode.PIN_VERIFICATION_FAILED: 403,
    ErrorCode.PIN_ATTEMPTS_EXCEEDED: 429,
    ErrorCode.OTP_REQUIRED: 422,
    ErrorCode.OTP_NOT_PROVISIONED: 409,
    ErrorCode.OTP_VERIFICATION_FAILED: 403,
    ErrorCode.OTP_WINDOW_EXPIRED: 409,
    ErrorCode.POD_GALLONS_CONFIRMATION_REQUIRED: 409,
    ErrorCode.DELIVERED_GALLONS_REQUIRED: 422,
    ErrorCode.POD_ORDER_REFERENCE_REQUIRED: 422,
    ErrorCode.STOP_ALREADY_COMPLETED: 409,
    ErrorCode.AMBIGUOUS_VOLUME_UNIT: 422,
    ErrorCode.VOLUME_QUANTITIES_REQUIRED: 422,
    ErrorCode.SENDER_IDENTITY_MISMATCH: 403,
    ErrorCode.ASSET_OUT_OF_SERVICE: 409,
    ErrorCode.PRETRIP_INSPECTION_REQUIRED: 409,
    ErrorCode.ACTIVE_DELIVERY_IN_PROGRESS: 409,
    ErrorCode.DUTY_STATUS_PROJECTION_PENDING: 202,
    ErrorCode.HOS_LIMIT_REACHED: 409,
    ErrorCode.HOS_FIGURES_UNAVAILABLE: 409,
    ErrorCode.DRIVER_NOT_DISPATCH_ELIGIBLE: 409,
}


def get_default_status_code(error_code: ErrorCode) -> int:
    """
    Get the default HTTP status code for an error code.
    
    Args:
        error_code: The error code to look up
        
    Returns:
        The default HTTP status code for the error code
    """
    return ERROR_CODE_STATUS_MAP.get(error_code, 500)
