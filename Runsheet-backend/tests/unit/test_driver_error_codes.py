"""
Unit tests for the driver-mobile-app error codes and factories.

Pins the wire value, default HTTP status, and factory binding of every error
code added for the driver surface, so a rename or a missing
``ERROR_CODE_STATUS_MAP`` entry fails here rather than in a handler.

Validates: Requirements 15.10, 1.6, 1.8, 1.15, 5.9, 5.11, 5.12, 5.22, 5.29,
6.6, 6.16, 6.17, 7.6, 8.6, 13.6, 13.18, 17.31
"""

import pytest

from errors import exceptions
from errors.codes import ERROR_CODE_STATUS_MAP, ErrorCode, get_default_status_code

# (member name, expected HTTP status, factory name)
DRIVER_ERROR_CODES = [
    ("SESSION_EXPIRED", 401, "session_expired"),
    ("DRIVER_IDENTITY_MISSING", 403, "driver_identity_missing"),
    ("DRIVER_RECORD_NOT_PROVISIONED", 403, "driver_record_not_provisioned"),
    ("APP_ACCESS_ALREADY_LINKED", 409, "app_access_already_linked"),
    ("INVALID_PIN_FORMAT", 422, "invalid_pin_format"),
    ("WEAK_PIN", 422, "weak_pin"),
    ("PIN_VERIFICATION_FAILED", 403, "pin_verification_failed"),
    ("PIN_ATTEMPTS_EXCEEDED", 429, "pin_attempts_exceeded"),
    ("OTP_REQUIRED", 422, "otp_required"),
    ("OTP_NOT_PROVISIONED", 409, "otp_not_provisioned"),
    ("OTP_VERIFICATION_FAILED", 403, "otp_verification_failed"),
    ("OTP_WINDOW_EXPIRED", 409, "otp_window_expired"),
    ("POD_GALLONS_CONFIRMATION_REQUIRED", 409, "pod_gallons_confirmation_required"),
    ("DELIVERED_GALLONS_REQUIRED", 422, "delivered_gallons_required"),
    ("POD_ORDER_REFERENCE_REQUIRED", 422, "pod_order_reference_required"),
    ("STOP_ALREADY_COMPLETED", 409, "stop_already_completed"),
    ("AMBIGUOUS_VOLUME_UNIT", 422, "ambiguous_volume_unit"),
    ("VOLUME_QUANTITIES_REQUIRED", 422, "volume_quantities_required"),
    ("SENDER_IDENTITY_MISMATCH", 403, "sender_identity_mismatch"),
    ("ASSET_OUT_OF_SERVICE", 409, "asset_out_of_service"),
    ("PRETRIP_INSPECTION_REQUIRED", 409, "pretrip_inspection_required"),
    ("ACTIVE_DELIVERY_IN_PROGRESS", 409, "active_delivery_in_progress"),
    ("DUTY_STATUS_PROJECTION_PENDING", 202, "duty_status_projection_pending"),
    ("HOS_LIMIT_REACHED", 409, "hos_limit_reached"),
    ("HOS_FIGURES_UNAVAILABLE", 409, "hos_figures_unavailable"),
    ("DRIVER_NOT_DISPATCH_ELIGIBLE", 409, "driver_not_dispatch_eligible"),
]


@pytest.mark.parametrize("member,status,factory_name", DRIVER_ERROR_CODES)
def test_code_value_is_upper_snake_and_matches_member(member, status, factory_name):
    """Wire values are UPPER_SNAKE and identical to the member name."""
    code = ErrorCode[member]
    assert code.value == member


@pytest.mark.parametrize("member,status,factory_name", DRIVER_ERROR_CODES)
def test_status_map_entry_exists_with_expected_status(member, status, factory_name):
    """Every driver code has an explicit status map entry, not the 500 fallback."""
    code = ErrorCode[member]
    assert code in ERROR_CODE_STATUS_MAP
    assert ERROR_CODE_STATUS_MAP[code] == status
    assert get_default_status_code(code) == status


@pytest.mark.parametrize("member,status,factory_name", DRIVER_ERROR_CODES)
def test_factory_builds_app_exception_with_default_status(member, status, factory_name):
    """The factory binds the code, resolves its default status, and has a message."""
    factory = getattr(exceptions, factory_name)
    exc = factory()

    assert exc.error_code is ErrorCode[member]
    assert exc.status_code == status
    assert exc.message
    assert exc.details is None
    assert exc.to_dict()["error_code"] == member


def test_factory_accepts_caller_supplied_details():
    """Call sites attach context through `details` — e.g. the OCR diagnostic."""
    exc = exceptions.pod_gallons_confirmation_required(
        details={"ocr_error": "timeout after 15s"}
    )

    assert exc.status_code == 409
    assert exc.to_dict()["details"] == {"ocr_error": "timeout after 15s"}


def test_duty_status_projection_pending_is_a_2xx_carrying_an_error_code():
    """R13.18: the event is durable, only the projection lags, so status is 2xx."""
    exc = exceptions.duty_status_projection_pending()

    assert 200 <= exc.status_code < 300
    assert exc.to_dict()["error_code"] == "DUTY_STATUS_PROJECTION_PENDING"


def test_conflict_helper_resolves_new_driver_codes_by_string():
    """`conflict()` looks codes up by value, so the new UPPER_SNAKE codes work."""
    exc = exceptions.conflict("stop done", error_code="STOP_ALREADY_COMPLETED")

    assert exc.error_code is ErrorCode.STOP_ALREADY_COMPLETED
    assert exc.status_code == 409
