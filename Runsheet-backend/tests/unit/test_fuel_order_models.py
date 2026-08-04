"""
Unit tests for ``fuel.order_models`` — FuelOrder validator branches.

Covers every validator branch on the :class:`FuelOrder` model:

* Product-code canonicalization (AGO → DIESEL_2, PMS → GASOLINE_REG,
  ATK → KEROSENE, LPG → PROPANE).
* ``missing_product_code`` when ``intake_channel != "legacy"`` and
  ``product_code`` is null.
* Legacy-channel orders accept null ``product_code`` and null ``gallons``.
* ``will_call`` / ``keep_full`` / ``auto_fill`` accept null window.
* ``one_off`` requires window.
* Window coherence check (end > start).
* ``missing_hold_reason`` when ``status=on_hold`` has no reason.
* ``fill_to_full + gallons_requested=null`` acceptance for non-legacy.

Validates: Requirements 10.2.1.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from fuel.order_models import (
    CallType,
    Driver,
    FuelOrder,
    FuelOrderEvent,
    IntakeChannelType,
    IntakeMetadata,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(hours=4)


def _base_order(**overrides) -> dict:
    """Return a minimal valid FuelOrder dict for a non-legacy channel."""
    payload = {
        "order_id": "ord_abc123",
        "tenant_id": "tenant-1",
        "customer_id": "cust-1",
        "customer_name": "Acme Fuels",
        "ship_to_address": "123 Main St",
        "ship_to_lat": 40.0,
        "ship_to_lon": -74.0,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": _NOW.isoformat(),
        "delivery_window_end": _LATER.isoformat(),
        "intake_channel": "dispatcher",
        "intake_channel_id": "ch-disp-1",
        "source_schema_version": "1.0",
        "trace_id": "trace-001",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
        "last_event_timestamp": _NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def _legacy_order(**overrides) -> dict:
    """Return a minimal valid FuelOrder dict for the legacy channel."""
    payload = _base_order(
        intake_channel="legacy",
        intake_channel_id="dinee-legacy",
        product_code=None,
        gallons_requested=None,
        call_type="will_call",
        delivery_window_start=None,
        delivery_window_end=None,
    )
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Product-code canonicalization
# ---------------------------------------------------------------------------


class TestProductCodeCanonicalization:
    """Verify legacy aliases normalize to US product codes."""

    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("AGO", "DIESEL_2"),
            ("PMS", "GASOLINE_REG"),
            ("ATK", "KEROSENE"),
            ("LPG", "PROPANE"),
        ],
    )
    def test_legacy_alias_canonicalizes(self, alias: str, expected: str):
        order = FuelOrder(**_base_order(product_code=alias))
        assert order.product_code == expected

    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("ago", "DIESEL_2"),
            ("pms", "GASOLINE_REG"),
            ("atk", "KEROSENE"),
            ("lpg", "PROPANE"),
        ],
    )
    def test_case_insensitive_canonicalization(self, alias: str, expected: str):
        order = FuelOrder(**_base_order(product_code=alias))
        assert order.product_code == expected

    def test_already_canonical_code_passes_through(self):
        order = FuelOrder(**_base_order(product_code="DIESEL_2"))
        assert order.product_code == "DIESEL_2"

    def test_none_passes_through_for_legacy(self):
        order = FuelOrder(**_legacy_order(product_code=None))
        assert order.product_code is None


# ---------------------------------------------------------------------------
# missing_product_code validator
# ---------------------------------------------------------------------------


class TestMissingProductCode:
    """Non-legacy channels MUST carry a non-null product_code."""

    @pytest.mark.parametrize(
        "channel",
        ["voice", "web_portal", "dispatcher", "csv", "edi", "api_partner"],
    )
    def test_null_product_code_rejected_for_non_legacy(self, channel: str):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                intake_channel=channel,
                product_code=None,
                fill_to_full=True,  # satisfy volume validator
            ))
        errors = exc_info.value.errors()
        assert any("missing_product_code" in str(e) for e in errors)

    def test_null_product_code_accepted_for_legacy(self):
        order = FuelOrder(**_legacy_order(product_code=None))
        assert order.product_code is None


# ---------------------------------------------------------------------------
# Legacy channel accepts null product_code and null gallons
# ---------------------------------------------------------------------------


class TestLegacyChannelNullables:
    """Legacy-channel orders accept null product_code and null gallons."""

    def test_legacy_null_product_code_and_null_gallons(self):
        order = FuelOrder(**_legacy_order(
            product_code=None,
            gallons_requested=None,
            fill_to_full=False,
        ))
        assert order.product_code is None
        assert order.gallons_requested is None

    def test_legacy_with_product_code_still_works(self):
        order = FuelOrder(**_legacy_order(
            product_code="DIESEL_2",
            gallons_requested=100.0,
        ))
        assert order.product_code == "DIESEL_2"
        assert order.gallons_requested == 100.0


# ---------------------------------------------------------------------------
# Volume validator (missing_volume)
# ---------------------------------------------------------------------------


class TestVolumeValidator:
    """Non-legacy channels need gallons_requested > 0 OR fill_to_full."""

    def test_non_legacy_no_gallons_no_fill_to_full_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                gallons_requested=None,
                fill_to_full=False,
            ))
        errors = exc_info.value.errors()
        assert any("missing_volume" in str(e) for e in errors)

    def test_fill_to_full_with_null_gallons_accepted_for_non_legacy(self):
        """fill_to_full + gallons_requested=null is valid for non-legacy."""
        order = FuelOrder(**_base_order(
            gallons_requested=None,
            fill_to_full=True,
        ))
        assert order.fill_to_full is True
        assert order.gallons_requested is None

    def test_gallons_positive_accepted(self):
        order = FuelOrder(**_base_order(gallons_requested=100.0))
        assert order.gallons_requested == 100.0


# ---------------------------------------------------------------------------
# Window validator — call_type acceptance
# ---------------------------------------------------------------------------


class TestWindowValidator:
    """will_call / keep_full / auto_fill accept null window; one_off requires it."""

    @pytest.mark.parametrize("call_type", ["will_call", "keep_full", "auto_fill"])
    def test_null_window_accepted_for_non_one_off(self, call_type: str):
        order = FuelOrder(**_base_order(
            call_type=call_type,
            delivery_window_start=None,
            delivery_window_end=None,
        ))
        assert order.delivery_window_start is None
        assert order.delivery_window_end is None

    def test_one_off_requires_window(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                call_type="one_off",
                delivery_window_start=None,
                delivery_window_end=None,
            ))
        errors = exc_info.value.errors()
        assert any("invalid_delivery_window" in str(e) for e in errors)

    def test_one_off_missing_start_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                call_type="one_off",
                delivery_window_start=None,
                delivery_window_end=_LATER.isoformat(),
            ))
        errors = exc_info.value.errors()
        assert any("invalid_delivery_window" in str(e) for e in errors)

    def test_one_off_missing_end_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                call_type="one_off",
                delivery_window_start=_NOW.isoformat(),
                delivery_window_end=None,
            ))
        errors = exc_info.value.errors()
        assert any("invalid_delivery_window" in str(e) for e in errors)

    def test_one_off_valid_window_accepted(self):
        order = FuelOrder(**_base_order(
            call_type="one_off",
            delivery_window_start=_NOW.isoformat(),
            delivery_window_end=_LATER.isoformat(),
        ))
        assert order.delivery_window_start is not None
        assert order.delivery_window_end is not None


# ---------------------------------------------------------------------------
# Window coherence (end > start)
# ---------------------------------------------------------------------------


class TestWindowCoherence:
    """Window end must be strictly after start whenever both are present."""

    def test_end_before_start_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                delivery_window_start=_LATER.isoformat(),
                delivery_window_end=_NOW.isoformat(),
            ))
        errors = exc_info.value.errors()
        assert any("invalid_delivery_window" in str(e) for e in errors)

    def test_end_equal_to_start_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                delivery_window_start=_NOW.isoformat(),
                delivery_window_end=_NOW.isoformat(),
            ))
        errors = exc_info.value.errors()
        assert any("invalid_delivery_window" in str(e) for e in errors)

    def test_end_after_start_accepted(self):
        order = FuelOrder(**_base_order(
            delivery_window_start=_NOW.isoformat(),
            delivery_window_end=_LATER.isoformat(),
        ))
        assert order.delivery_window_end > order.delivery_window_start


# ---------------------------------------------------------------------------
# Hold reason validator
# ---------------------------------------------------------------------------


class TestHoldReasonValidator:
    """on_hold orders MUST carry a non-empty hold_reason."""

    def test_on_hold_without_reason_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                status="on_hold",
                hold_reason=None,
            ))
        errors = exc_info.value.errors()
        assert any("missing_hold_reason" in str(e) for e in errors)

    def test_on_hold_with_empty_reason_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                status="on_hold",
                hold_reason="",
            ))
        errors = exc_info.value.errors()
        assert any("missing_hold_reason" in str(e) for e in errors)

    def test_on_hold_with_whitespace_only_reason_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            FuelOrder(**_base_order(
                status="on_hold",
                hold_reason="   ",
            ))
        errors = exc_info.value.errors()
        assert any("missing_hold_reason" in str(e) for e in errors)

    def test_on_hold_with_valid_reason_accepted(self):
        order = FuelOrder(**_base_order(
            status="on_hold",
            hold_reason="credit check pending",
        ))
        assert order.hold_reason == "credit check pending"

    def test_non_hold_status_no_reason_accepted(self):
        order = FuelOrder(**_base_order(status="placed", hold_reason=None))
        assert order.status == "placed"
        assert order.hold_reason is None


# ---------------------------------------------------------------------------
# IntakeMetadata extra="forbid"
# ---------------------------------------------------------------------------


class TestIntakeMetadataExtraForbid:
    """IntakeMetadata rejects unknown fields."""

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            IntakeMetadata(sneaky_field="nope")

    def test_valid_fields_accepted(self):
        meta = IntakeMetadata(
            dispatcher_user_id="user-1",
            session_id="sess-abc",
        )
        assert meta.dispatcher_user_id == "user-1"


# ---------------------------------------------------------------------------
# Driver duty-status projection bookkeeping
# ---------------------------------------------------------------------------


def _base_driver(**overrides) -> dict:
    """Return a minimal valid Driver dict."""
    payload = {
        "driver_id": "drv-1",
        "tenant_id": "tenant-1",
        "driver_name": "Ada Driver",
        "status": "active",
        "last_event_timestamp": _NOW,
        "source_schema_version": "1.0.0",
        "trace_id": "trace-1",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    payload.update(overrides)
    return payload


class TestDriverDutyStatusProjectionFields:
    """Driver carries the two nullable duty-status projection fields.

    ``Driver.model_config`` is ``extra="forbid"``, so the duty-status service
    cannot write either field until the model declares it.
    """

    def test_fields_default_to_none(self):
        driver = Driver(**_base_driver())

        assert driver.duty_status_event_id is None
        assert driver.duty_status_updated_at is None

    def test_fields_accepted_when_supplied(self):
        driver = Driver(
            **_base_driver(
                duty_status_event_id="01HZ0000000000000000000000",
                duty_status_updated_at=_LATER,
            )
        )

        assert driver.duty_status_event_id == "01HZ0000000000000000000000"
        assert driver.duty_status_updated_at == _LATER

    def test_status_still_required_and_constrained(self):
        with pytest.raises(ValidationError):
            Driver(**_base_driver(status="on_duty"))
