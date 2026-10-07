"""``DYED_DIESEL_CHECK_UNAVAILABLE`` is the 503 a blocked dyed-diesel plan carries (OI-02).

Pins the wire value, the explicit status-map entry (not the 500 fallback) and
the typed exception's details, so a rename or a missing map entry fails here.
"""
from compliance.services.dyed_diesel_enforcer import DyedDieselCheckUnavailable
from errors.codes import ERROR_CODE_STATUS_MAP, ErrorCode, get_default_status_code
from errors.exceptions import AppException


def test_code_value_matches_member():
    assert ErrorCode.DYED_DIESEL_CHECK_UNAVAILABLE.value == "DYED_DIESEL_CHECK_UNAVAILABLE"


def test_status_map_entry_is_503():
    assert ERROR_CODE_STATUS_MAP[ErrorCode.DYED_DIESEL_CHECK_UNAVAILABLE] == 503
    assert get_default_status_code(ErrorCode.DYED_DIESEL_CHECK_UNAVAILABLE) == 503


def test_exception_binds_code_status_message_and_details():
    exc = DyedDieselCheckUnavailable(
        reason="enforcer_error",
        tenant_id="t1",
        plan_id="p1",
        truck_id="truck-1",
        compartment_id="c1",
        cause="RuntimeError",
    )
    assert isinstance(exc, AppException)
    assert exc.error_code is ErrorCode.DYED_DIESEL_CHECK_UNAVAILABLE
    assert exc.status_code == 503
    assert "blocked" in exc.message
    assert exc.details == {
        "reason": "enforcer_error",
        "tenant_id": "t1",
        "plan_id": "p1",
        "truck_id": "truck-1",
        "compartment_id": "c1",
        "cause": "RuntimeError",
    }
    assert exc.reason == "enforcer_error"


def test_not_wired_defaults():
    exc = DyedDieselCheckUnavailable(
        reason="enforcer_not_wired", tenant_id="t1", plan_id="p1", truck_id="truck-1"
    )
    assert exc.details["compartment_id"] is None
    assert exc.details["cause"] is None
