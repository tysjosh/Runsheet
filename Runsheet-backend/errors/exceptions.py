"""
Exception classes for the Runsheet backend.

This module provides the AppException class and convenience factory
functions for creating application-specific exceptions with proper
error codes and HTTP status codes.

Validates: Requirement 2.2 - Define a catalog of error codes covering
validation errors, authentication errors, external service failures,
and internal errors.
"""

from typing import Any, Optional, Union

from errors.codes import ErrorCode, get_default_status_code


class AppException(Exception):
    """
    Base exception class for all application-specific errors.
    
    This exception provides structured error information including:
    - error_code: A standardized error code from the ErrorCode enum
    - message: A human-readable error message
    - status_code: The HTTP status code to return
    - details: Optional additional context (e.g., field-level errors)
    
    Example:
        raise AppException(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Invalid latitude value",
            status_code=400,
            details={"field": "latitude", "reason": "Must be between -90 and 90"}
        )
    """
    
    def __init__(
        self,
        error_code: Union[ErrorCode, str],
        message: str,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
        *,
        headers: Optional[dict[str, str]] = None,
    ):
        """
        Initialize an AppException.
        
        Args:
            error_code: The error code from the ErrorCode enum, or a plain
                string for a module-local code (e.g. the lowercase fuel-ops
                codes). A string code has no default status, so it requires
                an explicit ``status_code``.
            message: A human-readable error message
            status_code: The HTTP status code (defaults to the error code's default)
            details: Optional dictionary with additional error context
            headers: Optional response headers (e.g. ``Retry-After`` on a 429)

        Raises:
            ValueError: A string ``error_code`` without a ``status_code``.
        """
        if not isinstance(error_code, ErrorCode) and status_code is None:
            raise ValueError(
                f"AppException with string error_code {error_code!r} "
                "needs an explicit status_code"
            )
        self.error_code = error_code
        self.message = message
        self.status_code = status_code or get_default_status_code(error_code)
        self.details = details
        self.headers = headers
        super().__init__(message)
    
    def to_dict(self) -> dict[str, Any]:
        """
        Convert the exception to a dictionary for JSON serialization.
        
        Returns:
            Dictionary containing error_code, message, and details
        """
        result = {
            "error_code": error_code_value(self.error_code),
            "message": self.message,
        }
        if self.details is not None:
            result["details"] = self.details
        return result
    
    def __repr__(self) -> str:
        return (
            f"AppException(error_code={error_code_value(self.error_code)!r}, "
            f"message={self.message!r}, status_code={self.status_code}, "
            f"details={self.details!r})"
        )


def error_code_value(code: Union[ErrorCode, str]) -> str:
    """Return the wire string for an ``ErrorCode`` member or a plain string code."""
    return str(getattr(code, "value", code))


# Convenience factory functions for common error types

def validation_error(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a validation error exception."""
    return AppException(
        error_code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=details
    )


def invalid_request(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid request exception."""
    return AppException(
        error_code=ErrorCode.INVALID_REQUEST,
        message=message,
        details=details
    )


def resource_not_found(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a resource not found exception."""
    return AppException(
        error_code=ErrorCode.RESOURCE_NOT_FOUND,
        message=message,
        details=details
    )


def already_exists(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a 409 for a create whose id is already taken.

    Ids are global across tenants, so the message must stay generic: it never
    names the owning tenant or echoes the stored document.
    """
    return AppException(
        error_code=ErrorCode.RESOURCE_ALREADY_EXISTS,
        message=message,
        details=details
    )


def legacy_ng_delivery_disabled(
    surface: Optional[str] = None,
) -> AppException:
    """Create the 404 raised when the legacy NG last-mile surface is off.

    The ``legacy_ng_delivery`` flag defaults OFF (product-owner audit
    2026-05-08 recommendation #1), which hides the pre-pivot Nigerian
    rider/shipment surface from US fuel tenants.

    Args:
        surface: Optional name of the blocked surface, echoed in ``details``
            so operators can tell which gated route was hit.
    """
    return AppException(
        error_code=ErrorCode.LEGACY_NG_DELIVERY_DISABLED,
        message=(
            "The legacy Nigerian last-mile delivery surface is disabled. "
            "Set LEGACY_NG_DELIVERY_ENABLED=true to re-enable it."
        ),
        details={"surface": surface} if surface else None,
    )


def unauthorized(
    message: str = "Authentication required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an unauthorized exception."""
    return AppException(
        error_code=ErrorCode.UNAUTHORIZED,
        message=message,
        details=details
    )


def forbidden(
    message: str = "Insufficient permissions",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a forbidden exception."""
    return AppException(
        error_code=ErrorCode.FORBIDDEN,
        message=message,
        details=details
    )


def rate_limited(
    message: str = "Too many requests",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a rate limited exception."""
    return AppException(
        error_code=ErrorCode.RATE_LIMITED,
        message=message,
        details=details
    )


def too_many_attempts(retry_after_seconds: int) -> AppException:
    """Create the 429 for a throttled sign-in or password-reset (staging finding F5).

    Carries ``Retry-After`` and ``details.retry_after_seconds`` so clients can
    show how long to wait.
    """
    seconds = max(1, int(retry_after_seconds))
    return AppException(
        error_code=ErrorCode.RATE_LIMITED,
        message=f"Too many attempts, try again in {seconds} seconds",
        details={"retry_after_seconds": seconds},
        headers={"Retry-After": str(seconds)},
    )


def elasticsearch_unavailable(
    message: str = "Database connection failed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an Elasticsearch unavailable exception."""
    return AppException(
        error_code=ErrorCode.ELASTICSEARCH_UNAVAILABLE,
        message=message,
        details=details
    )


def ai_service_unavailable(
    message: str = "AI service unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an AI service unavailable exception."""
    return AppException(
        error_code=ErrorCode.AI_SERVICE_UNAVAILABLE,
        message=message,
        details=details
    )


def ai_rate_limited(
    message: str = "AI service is rate limited",
    retry_after_seconds: Optional[int] = None,
) -> AppException:
    """Create the 429 for an LLM provider rate limit or exhausted quota (F3).

    Carries ``Retry-After`` and ``details.retry_after_seconds`` when the
    provider said how long to wait.
    """
    headers = None
    details = None
    if retry_after_seconds is not None:
        seconds = max(1, int(retry_after_seconds))
        headers = {"Retry-After": str(seconds)}
        details = {"retry_after_seconds": seconds}
    return AppException(
        error_code=ErrorCode.AI_RATE_LIMITED,
        message=message,
        details=details,
        headers=headers,
    )


def session_store_unavailable(
    message: str = "Session store unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a session store unavailable exception."""
    return AppException(
        error_code=ErrorCode.SESSION_STORE_UNAVAILABLE,
        message=message,
        details=details
    )


def internal_error(
    message: str = "An unexpected error occurred",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an internal error exception."""
    return AppException(
        error_code=ErrorCode.INTERNAL_ERROR,
        message=message,
        details=details
    )


def circuit_open(
    message: str = "Service temporarily unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a circuit open exception."""
    return AppException(
        error_code=ErrorCode.CIRCUIT_OPEN,
        message=message,
        details=details
    )

def webhook_signature_invalid(
    message: str = "Webhook signature verification failed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a webhook signature invalid exception."""
    return AppException(
        error_code=ErrorCode.WEBHOOK_SIGNATURE_INVALID,
        message=message,
        details=details
    )


def webhook_schema_unknown(
    message: str = "Unknown webhook schema version",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a webhook schema unknown exception."""
    return AppException(
        error_code=ErrorCode.WEBHOOK_SCHEMA_UNKNOWN,
        message=message,
        details=details
    )


def tenant_not_found(
    message: str = "Tenant not found",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a tenant not found exception."""
    return AppException(
        error_code=ErrorCode.TENANT_NOT_FOUND,
        message=message,
        details=details
    )


def tenant_disabled(
    message: str = "Ops intelligence is not enabled for this tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a tenant disabled exception."""
    return AppException(
        error_code=ErrorCode.TENANT_DISABLED,
        message=message,
        details=details
    )


def poison_queue_max_retries(
    message: str = "Event exceeded maximum retry count",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a poison queue max retries exceeded exception."""
    return AppException(
        error_code=ErrorCode.POISON_QUEUE_MAX_RETRIES,
        message=message,
        details=details
    )


def drift_threshold_exceeded(
    message: str = "Source-replica drift exceeds threshold",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a drift threshold exceeded exception."""
    return AppException(
        error_code=ErrorCode.DRIFT_THRESHOLD_EXCEEDED,
        message=message,
        details=details
    )


def backfill_in_progress(
    message: str = "A backfill job is already running for this tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a backfill in progress exception."""
    return AppException(
        error_code=ErrorCode.BACKFILL_IN_PROGRESS,
        message=message,
        details=details
    )


# --- Order Intake Pipeline factory functions ---


def conflict(
    message: str,
    error_code: str = "INVALID_STATUS_TRANSITION",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a conflict exception (409) using a string error code lookup.

    This is a convenience helper used by the order state machine and
    other pipeline components that raise 409 conflicts with specific
    error codes.
    """
    code = ErrorCode(error_code.upper()) if isinstance(error_code, str) else error_code
    return AppException(
        error_code=code,
        message=message,
        details=details
    )


def invalid_customer_tank_ref(
    message: str = "Referenced customer tank does not exist or belongs to another tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid customer tank reference exception."""
    return AppException(
        error_code=ErrorCode.INVALID_CUSTOMER_TANK_REF,
        message=message,
        details=details
    )


def missing_volume(
    message: str = "Order must specify gallons_requested > 0 or fill_to_full = true",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing volume exception."""
    return AppException(
        error_code=ErrorCode.MISSING_VOLUME,
        message=message,
        details=details
    )


def invalid_delivery_window(
    message: str = "Delivery window is invalid — end must be after start",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid delivery window exception."""
    return AppException(
        error_code=ErrorCode.INVALID_DELIVERY_WINDOW,
        message=message,
        details=details
    )


def missing_delivery_window(
    message: str = "Order lacks a delivery window required for the target status",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing delivery window exception."""
    return AppException(
        error_code=ErrorCode.MISSING_DELIVERY_WINDOW,
        message=message,
        details=details
    )


def missing_product_code(
    message: str = "Non-legacy intake channel order must carry a product code",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing product code exception."""
    return AppException(
        error_code=ErrorCode.MISSING_PRODUCT_CODE,
        message=message,
        details=details
    )


def missing_client_event_id(
    message: str = "Dispatcher intake request must include a client_event_id",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing client event ID exception."""
    return AppException(
        error_code=ErrorCode.MISSING_CLIENT_EVENT_ID,
        message=message,
        details=details
    )


def missing_hold_reason(
    message: str = "Order placed on hold must include a hold_reason",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing hold reason exception."""
    return AppException(
        error_code=ErrorCode.MISSING_HOLD_REASON,
        message=message,
        details=details
    )


def invalid_status_transition(
    message: str = "Requested status transition is not allowed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid status transition exception."""
    return AppException(
        error_code=ErrorCode.INVALID_STATUS_TRANSITION,
        message=message,
        details=details
    )


def channel_disabled(
    message: str = "Intake channel is disabled and cannot accept orders",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a channel disabled exception."""
    return AppException(
        error_code=ErrorCode.CHANNEL_DISABLED,
        message=message,
        details=details
    )


def order_intake_disabled(
    message: str = "Order intake isn't enabled for this account",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create the 409 for an order refused because intake is switched off.

    Raised when the tenant's ``order_intake_pipeline`` flag is ``disabled``,
    so a dispatcher create never reports success for an order that was not
    stored.
    """
    return AppException(
        error_code=ErrorCode.ORDER_INTAKE_DISABLED,
        message=message,
        details=details
    )


def insufficient_role(
    message: str = "Caller lacks the required role for this operation",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an insufficient role exception."""
    return AppException(
        error_code=ErrorCode.INSUFFICIENT_ROLE,
        message=message,
        details=details
    )


def driver_unavailable(
    message: str = "Driver is off_duty or inactive and cannot be assigned",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver unavailable exception."""
    return AppException(
        error_code=ErrorCode.DRIVER_UNAVAILABLE,
        message=message,
        details=details
    )


def legacy_route_sunset(
    message: str = "This legacy route has been sunset — use the new order surface",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a legacy route sunset exception."""
    return AppException(
        error_code=ErrorCode.LEGACY_ROUTE_SUNSET,
        message=message,
        details=details
    )


def security_tenant_id_mismatch(
    message: str = "Payload tenant_id does not match the channel's tenant_id",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a security tenant ID mismatch exception."""
    return AppException(
        error_code=ErrorCode.SECURITY_TENANT_ID_MISMATCH,
        message=message,
        details=details
    )


# --- Dinee Voice Integration factory functions ---
#
# These envelopes back Surface A (voice order submission) and Surface B
# (read/driver endpoints). Per Requirements 9.3 and 10.6, rejection
# envelopes MUST NOT carry tenant data or credential values: the factories
# below take fixed, non-sensitive default messages and only ever attach
# caller-provided `details` that the call sites keep free of tenant
# identifiers, API keys, HMAC secrets, or signatures.


def voice_replay_window_exceeded(
    message: str = "Request timestamp is missing, invalid, or outside the allowed replay window",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice replay-window exceeded exception (HTTP 401)."""
    return AppException(
        error_code=ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED,
        message=message,
        details=details
    )


def missing_idempotency_key(
    message: str = "The X-Idempotency-Key header is required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing idempotency key exception (HTTP 400)."""
    return AppException(
        error_code=ErrorCode.MISSING_IDEMPOTENCY_KEY,
        message=message,
        details=details
    )


def idempotency_conflict(
    message: str = "The idempotency key was reused with a different request body",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an idempotency conflict exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.IDEMPOTENCY_CONFLICT,
        message=message,
        details=details
    )


def unsupported_schema_version(
    message: str = "The X-Schema-Version header is missing or unsupported",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an unsupported schema version exception (HTTP 422)."""
    return AppException(
        error_code=ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        message=message,
        details=details
    )


def voice_payload_invalid(
    missing_fields: Optional[list[str]] = None,
    message: str = "The voice submission payload is missing required fields",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice payload validation exception (HTTP 422).

    The response body identifies the absent required fields via
    ``details.missing_fields`` (Requirement 7.3). When ``details`` is not
    supplied, it is built from ``missing_fields``; the field names are the
    only context carried and never include tenant data or credentials.
    """
    if details is None:
        details = {"missing_fields": list(missing_fields or [])}
    return AppException(
        error_code=ErrorCode.VOICE_PAYLOAD_INVALID,
        message=message,
        details=details
    )


def order_payload_invalid(
    invalid_fields: Optional[list[str]] = None,
    message: str = "The order payload failed value validation",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an order payload value-validation exception (HTTP 422).

    Raised by :class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`
    when ``FuelOrder.model_validate`` rejects an adapter's output — the
    cross-field invariants the per-channel request models cannot express.

    ``details.invalid_fields`` names the offending field paths and/or rule
    codes. Field names and rule codes only: never the submitted values, so no
    customer address, phone, or credential can leak into an error response.
    """
    if details is None:
        details = {"invalid_fields": list(invalid_fields or [])}
    return AppException(
        error_code=ErrorCode.ORDER_PAYLOAD_INVALID,
        message=message,
        details=details
    )


def voice_unauthorized(
    message: str = "Authentication required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice unauthorized exception (HTTP 401)."""
    return AppException(
        error_code=ErrorCode.VOICE_UNAUTHORIZED,
        message=message,
        details=details
    )


def voice_tenant_mismatch(
    message: str = "The tenant header does not match the authenticated credential",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice tenant mismatch exception (HTTP 403)."""
    return AppException(
        error_code=ErrorCode.VOICE_TENANT_MISMATCH,
        message=message,
        details=details
    )


# --- Fuel-ops / compliance entity-lookup factory functions ---
#
# These wrap error codes whose *wire value* is lower-case because the
# endpoints below shipped with a raw ``HTTPException`` carrying
# ``detail.error_code``. The generic ``resource_not_found`` /
# ``internal_error`` factories would have renamed the code clients match
# on, so each site keeps its published code through a dedicated factory.


def depot_not_found(
    message: str = "Depot not found",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a depot-not-found exception (HTTP 404)."""
    return AppException(
        error_code=ErrorCode.DEPOT_NOT_FOUND,
        message=message,
        details=details
    )


def driver_not_found(
    message: str = "Referenced driver does not exist in this tenant.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver-not-found exception (HTTP 400).

    Note the 400: this is a *reference validation* failure on a submitted
    payload field, not a missing addressed resource.
    """
    return AppException(
        error_code=ErrorCode.DRIVER_NOT_FOUND,
        message=message,
        details=details
    )


def terminal_not_found(
    message: str = "Terminal not found",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a terminal-not-found exception (HTTP 404)."""
    return AppException(
        error_code=ErrorCode.TERMINAL_NOT_FOUND,
        message=message,
        details=details
    )


def supplier_contract_not_found(
    message: str = "Supplier contract not found",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a supplier-contract-not-found exception (HTTP 404)."""
    return AppException(
        error_code=ErrorCode.SUPPLIER_CONTRACT_NOT_FOUND,
        message=message,
        details=details
    )


def kfactor_variance_history_failed(
    message: str = "Failed to load K-factor variance history.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a K-factor variance-history failure exception (HTTP 500)."""
    return AppException(
        error_code=ErrorCode.KFACTOR_VARIANCE_HISTORY_FAILED,
        message=message,
        details=details
    )


# --- Driver Mobile App factory functions ---
#
# Every driver-surface rejection is an ``AppException`` (Requirement 15.10) —
# no driver module raises a raw ``HTTPException``. Requirement 15.14 forbids
# echoing the caller's held roles or the assigned driver's identity in an
# authorization rejection, so the default messages below are fixed and
# non-identifying and only caller-supplied ``details`` are attached.


def session_expired(
    message: str = "The session has expired. Sign in again to continue.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a session expired exception (HTTP 401).

    Raised for an expired access token regardless of whether the accompanying
    refresh token is still valid (Requirement 1.8).
    """
    return AppException(
        error_code=ErrorCode.SESSION_EXPIRED,
        message=message,
        details=details
    )


def driver_identity_missing(
    message: str = "This operation requires a driver identity on the session.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver identity missing exception (HTTP 403).

    Raised when a request reaches a ``/api/driver`` surface with a
    ``TenantContext`` whose ``driver_id`` is absent (Requirement 1.6).
    """
    return AppException(
        error_code=ErrorCode.DRIVER_IDENTITY_MISSING,
        message=message,
        details=details
    )


def driver_record_not_provisioned(
    message: str = "Driver app access is not provisioned for this account.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver record not provisioned exception (HTTP 403).

    Raised at sign-in when a ``driver``-role user has no ``drivers_current``
    record (Requirement 1.15).
    """
    return AppException(
        error_code=ErrorCode.DRIVER_RECORD_NOT_PROVISIONED,
        message=message,
        details=details
    )


def app_access_already_linked(
    message: str = "Driver app access is already linked to another account.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an app access already linked exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.APP_ACCESS_ALREADY_LINKED,
        message=message,
        details=details
    )


def invalid_pin_format(
    message: str = "The PIN does not meet the required format.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid PIN format exception (HTTP 422)."""
    return AppException(
        error_code=ErrorCode.INVALID_PIN_FORMAT,
        message=message,
        details=details
    )


def weak_pin(
    message: str = "The PIN is too easily guessed. Choose a different PIN.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a weak PIN exception (HTTP 422)."""
    return AppException(
        error_code=ErrorCode.WEAK_PIN,
        message=message,
        details=details
    )


def pin_verification_failed(
    message: str = "PIN verification failed.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a PIN verification failed exception (HTTP 403)."""
    return AppException(
        error_code=ErrorCode.PIN_VERIFICATION_FAILED,
        message=message,
        details=details
    )


def pin_attempts_exceeded(
    message: str = "Too many failed PIN attempts. Try again later.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a PIN attempts exceeded exception (HTTP 429)."""
    return AppException(
        error_code=ErrorCode.PIN_ATTEMPTS_EXCEEDED,
        message=message,
        details=details
    )


def otp_required(
    message: str = "A proof-of-delivery code is required for this delivery.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an OTP required exception (HTTP 422).

    Replaces the former HTTP 200 ``OTP_REQUIRED`` body so the rejection
    carries a real status code (Requirements 5.9, 15.10).
    """
    return AppException(
        error_code=ErrorCode.OTP_REQUIRED,
        message=message,
        details=details
    )


def otp_not_provisioned(
    message: str = "No proof-of-delivery code is provisioned for this delivery.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an OTP not provisioned exception (HTTP 409).

    Fails closed: an OTP is required by policy but none exists to verify
    against. Replaces the former HTTP 200 body.
    """
    return AppException(
        error_code=ErrorCode.OTP_NOT_PROVISIONED,
        message=message,
        details=details
    )


def otp_verification_failed(
    message: str = "Proof-of-delivery code verification failed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an OTP verification failed exception (HTTP 403).

    Replaces the former HTTP 200 ``OTP_INVALID`` body (Requirement 5.9).
    """
    return AppException(
        error_code=ErrorCode.OTP_VERIFICATION_FAILED,
        message=message,
        details=details
    )


def otp_window_expired(
    message: str = "The proof-of-delivery code is outside its validity window.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an OTP window expired exception (HTTP 409, Requirement 5.29)."""
    return AppException(
        error_code=ErrorCode.OTP_WINDOW_EXPIRED,
        message=message,
        details=details
    )


def pod_gallons_confirmation_required(
    message: str = "Confirm the delivered gallon count to complete this proof of delivery.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a POD gallons confirmation required exception (HTTP 409).

    Raised when meter-ticket OCR times out, errors, or flags
    ``requires_manual_review``. Call sites attach the OCR diagnostic string in
    ``details`` so the app can prompt for a manual entry (Requirement 5.11).
    """
    return AppException(
        error_code=ErrorCode.POD_GALLONS_CONFIRMATION_REQUIRED,
        message=message,
        details=details
    )


def delivered_gallons_required(
    message: str = "A delivered gallon count is required unless the delivery was refused.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a delivered gallons required exception (HTTP 422, Requirement 5.12)."""
    return AppException(
        error_code=ErrorCode.DELIVERED_GALLONS_REQUIRED,
        message=message,
        details=details
    )


def pod_order_reference_required(
    message: str = "A proof of delivery must reference an order.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a POD order reference required exception (HTTP 422, Requirement 5.22)."""
    return AppException(
        error_code=ErrorCode.POD_ORDER_REFERENCE_REQUIRED,
        message=message,
        details=details
    )


def stop_already_completed(
    message: str = "This stop is already completed.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a stop already completed exception (HTTP 409, Requirement 6.6)."""
    return AppException(
        error_code=ErrorCode.STOP_ALREADY_COMPLETED,
        message=message,
        details=details
    )


def ambiguous_volume_unit(
    message: str = (
        "Supply either actual_quantities (litres) or actual_quantities_gallons, not both."
    ),
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an ambiguous volume unit exception (HTTP 422, Requirement 6.16)."""
    return AppException(
        error_code=ErrorCode.AMBIGUOUS_VOLUME_UNIT,
        message=message,
        details=details
    )


def volume_quantities_required(
    message: str = (
        "Supply actual_quantities (litres) or actual_quantities_gallons for this check-in."
    ),
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a volume quantities required exception (HTTP 422, Requirement 6.17)."""
    return AppException(
        error_code=ErrorCode.VOLUME_QUANTITIES_REQUIRED,
        message=message,
        details=details
    )


def sender_identity_mismatch(
    message: str = "The message sender does not match the authenticated identity.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a sender identity mismatch exception (HTTP 403, Requirement 7.6)."""
    return AppException(
        error_code=ErrorCode.SENDER_IDENTITY_MISMATCH,
        message=message,
        details=details
    )


def asset_out_of_service(
    message: str = "The assigned asset is out of service.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an asset out of service exception (HTTP 409, Requirement 8.6)."""
    return AppException(
        error_code=ErrorCode.ASSET_OUT_OF_SERVICE,
        message=message,
        details=details
    )


def pretrip_inspection_required(
    message: str = "A pre-trip inspection is required before this trip can start.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a pre-trip inspection required exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.PRETRIP_INSPECTION_REQUIRED,
        message=message,
        details=details
    )


def active_delivery_in_progress(
    message: str = "A delivery is in progress. Complete it before changing duty status.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an active delivery in progress exception (HTTP 409, Requirement 13.6)."""
    return AppException(
        error_code=ErrorCode.ACTIVE_DELIVERY_IN_PROGRESS,
        message=message,
        details=details
    )


def duty_status_projection_pending(
    message: str = "The duty status change was recorded and is still being applied.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a duty status projection pending exception (HTTP 202).

    Unusual by design: a 2xx carrying an ``error_code``. The transition
    succeeded — the event is durable — and only the ``drivers_current``
    projection lags, so the offline queue must dequeue rather than retry and
    append a duplicate event (Requirement 13.18).
    """
    return AppException(
        error_code=ErrorCode.DUTY_STATUS_PROJECTION_PENDING,
        message=message,
        details=details
    )


def hos_limit_reached(
    message: str = "Hours-of-service limit reached.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an hours-of-service limit reached exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.HOS_LIMIT_REACHED,
        message=message,
        details=details
    )


def hos_figures_unavailable(
    message: str = "Hours-of-service figures are unavailable.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an hours-of-service figures unavailable exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.HOS_FIGURES_UNAVAILABLE,
        message=message,
        details=details
    )


def driver_not_dispatch_eligible(
    message: str = "The driver is not eligible for dispatch.",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver not dispatch eligible exception (HTTP 409, Requirement 17.31)."""
    return AppException(
        error_code=ErrorCode.DRIVER_NOT_DISPATCH_ELIGIBLE,
        message=message,
        details=details
    )


# ── Customer portal (OI-06, design §11) ─────────────────────────────────


def _portal_error(
    code: ErrorCode, message: str, details: Optional[dict[str, Any]]
) -> AppException:
    return AppException(error_code=code, message=message, details=details)


def portal_disabled(
    message: str = "The customer portal is not available.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Portal flag (or the commerce backbone) is off (HTTP 404)."""
    return _portal_error(ErrorCode.PORTAL_DISABLED, message, details)


def portal_route_forbidden(
    message: str = "This account can't use this part of Runsheet",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """A customer session called a non-portal route (HTTP 403)."""
    return _portal_error(ErrorCode.PORTAL_ROUTE_FORBIDDEN, message, details)


def portal_identity_invalid(
    message: str = "This account can't use this part of Runsheet",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """A malformed customer identity (extra role or no customer_id) (HTTP 403)."""
    return _portal_error(ErrorCode.PORTAL_IDENTITY_INVALID, message, details)


def portal_access_suspended(
    message: str = "Portal access for this account is suspended.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Grant revoked or customer archived (HTTP 403)."""
    return _portal_error(ErrorCode.PORTAL_ACCESS_SUSPENDED, message, details)


def portal_unavailable(
    message: str = "The customer portal is temporarily unavailable.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Principal-check store failure (HTTP 503)."""
    return _portal_error(ErrorCode.PORTAL_UNAVAILABLE, message, details)


def portal_email_in_use(
    message: str = "This email can't be invited to the portal.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Invite email belongs to an ineligible identity (HTTP 409)."""
    return _portal_error(ErrorCode.PORTAL_EMAIL_IN_USE, message, details)


def portal_user_limit_reached(
    message: str = "This customer already has the maximum number of portal users.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Active portal-user cap reached (HTTP 409)."""
    return _portal_error(ErrorCode.PORTAL_USER_LIMIT_REACHED, message, details)


def portal_customer_archived(
    message: str = "Portal users can't be invited for an archived customer.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Invite for an archived customer (HTTP 409)."""
    return _portal_error(ErrorCode.PORTAL_CUSTOMER_ARCHIVED, message, details)


def order_not_cancellable(
    message: str = "This request can no longer be cancelled.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Portal cancel on an order not awaiting confirmation (HTTP 409)."""
    return _portal_error(ErrorCode.ORDER_NOT_CANCELLABLE, message, details)


def order_request_rejected(
    message: str = "This delivery request couldn't be accepted.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Portal order rejected by intake validation or a hook (HTTP 422)."""
    return _portal_error(ErrorCode.ORDER_REQUEST_REJECTED, message, details)


def invoice_not_payable(
    message: str = "This invoice can't be paid.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Payment create on a non-payable invoice (HTTP 409)."""
    return _portal_error(ErrorCode.INVOICE_NOT_PAYABLE, message, details)


def payment_amount_invalid(
    message: str = "The payment amount is not valid for this invoice.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Payment amount out of range (HTTP 422)."""
    return _portal_error(ErrorCode.PAYMENT_AMOUNT_INVALID, message, details)


def payment_in_progress(
    message: str = "A payment for this invoice is already in progress.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Another attempt for the invoice is in flight (HTTP 409)."""
    return _portal_error(ErrorCode.PAYMENT_IN_PROGRESS, message, details)


def portal_payments_unavailable(
    message: str = "Online payments are not available.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """No enabled payment provider for portal payments (HTTP 409)."""
    return _portal_error(ErrorCode.PORTAL_PAYMENTS_UNAVAILABLE, message, details)


def payment_provider_error(
    message: str = "The payment provider couldn't process this request.",
    details: Optional[dict[str, Any]] = None,
) -> AppException:
    """Payment provider call failed (HTTP 502)."""
    return _portal_error(ErrorCode.PAYMENT_PROVIDER_ERROR, message, details)
