"""Unit tests for :class:`compliance.models.meter.MeterRegistration` and
:class:`compliance.models.meter.MeterAuditEntry`.

Covers Task 10.1 of the Fuel Compliance Backbone spec, which validates
Requirement 8.3 (Meter registry with calibration and authority fields).

The tests assert:
- Happy-path construction with all required fields.
- Auto-generated IDs with correct prefixes (meter_, maudit_).
- Default status is "active" for MeterRegistration.
- Validators: meter_number non-empty, truck_id non-empty, tenant_id non-empty.
- Optional fields default correctly.
- Extra fields are forbidden (schema hygiene).
- Invalid status literals are rejected.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from compliance.models.meter import MeterAuditEntry, MeterRegistration


# ---------------------------------------------------------------------------
# Fixtures — MeterRegistration
# ---------------------------------------------------------------------------


def _meter_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "meter_number": "FM-2024-001",
        "truck_id": "truck_abc123",
        "calibration_certificate_number": "CAL-2024-5678",
        "calibration_date": date(2024, 6, 1),
        "calibration_expiry_date": date(2025, 6, 1),
        "weights_measures_authority": "Texas Dept of Agriculture",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Fixtures — MeterAuditEntry
# ---------------------------------------------------------------------------


def _audit_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "meter_id": "meter_abc123",
        "meter_ticket_id": "mt_ticket_001",
        "delivery_id": "del_001",
        "gross_gallons": 2500.0,
        "timestamp": datetime(2024, 7, 15, 10, 30, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return payload


# ===========================================================================
# MeterRegistration Tests
# ===========================================================================


class TestMeterRegistrationHappyPath:
    """Valid MeterRegistration construction with required and optional fields."""

    def test_minimal_required_fields(self):
        meter = MeterRegistration(**_meter_payload())

        assert meter.tenant_id == "tenant-1"
        assert meter.meter_number == "FM-2024-001"
        assert meter.truck_id == "truck_abc123"
        assert meter.calibration_certificate_number == "CAL-2024-5678"
        assert meter.calibration_date == date(2024, 6, 1)
        assert meter.calibration_expiry_date == date(2025, 6, 1)
        assert meter.weights_measures_authority == "Texas Dept of Agriculture"
        assert meter.status == "active"

    def test_meter_id_auto_generated_with_prefix(self):
        meter = MeterRegistration(**_meter_payload())

        assert meter.meter_id.startswith("meter_")
        assert len(meter.meter_id) > len("meter_")

    def test_unique_meter_ids_generated(self):
        m1 = MeterRegistration(**_meter_payload())
        m2 = MeterRegistration(**_meter_payload())

        assert m1.meter_id != m2.meter_id

    def test_timestamps_are_utc_aware(self):
        meter = MeterRegistration(**_meter_payload())

        assert meter.created_at.tzinfo is not None
        assert meter.updated_at.tzinfo is not None
        assert meter.created_at.tzinfo == timezone.utc

    def test_all_status_values_accepted(self):
        for status in ("active", "expired_calibration"):
            meter = MeterRegistration(**_meter_payload(status=status))
            assert meter.status == status

    def test_explicit_meter_id_accepted(self):
        meter = MeterRegistration(**_meter_payload(meter_id="meter_custom"))
        assert meter.meter_id == "meter_custom"


# ---------------------------------------------------------------------------
# MeterRegistration — meter_number validation
# ---------------------------------------------------------------------------


class TestMeterNumberValidation:
    """meter_number must be non-empty."""

    def test_empty_meter_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(meter_number=""))

        assert "meter_number" in str(exc_info.value)

    def test_whitespace_only_meter_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(meter_number="   "))

        assert "meter_number" in str(exc_info.value)

    def test_meter_number_is_stripped(self):
        meter = MeterRegistration(**_meter_payload(meter_number="  FM-001  "))
        assert meter.meter_number == "FM-001"


# ---------------------------------------------------------------------------
# MeterRegistration — truck_id validation
# ---------------------------------------------------------------------------


class TestTruckIdValidation:
    """truck_id must be non-empty."""

    def test_empty_truck_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(truck_id=""))

        assert "truck_id" in str(exc_info.value)

    def test_whitespace_only_truck_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(truck_id="   "))

        assert "truck_id" in str(exc_info.value)

    def test_truck_id_is_stripped(self):
        meter = MeterRegistration(**_meter_payload(truck_id="  truck_1  "))
        assert meter.truck_id == "truck_1"


# ---------------------------------------------------------------------------
# MeterRegistration — tenant_id validation
# ---------------------------------------------------------------------------


class TestMeterTenantIdValidation:
    """tenant_id must be non-empty."""

    def test_empty_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(tenant_id=""))

        assert "tenant_id" in str(exc_info.value)

    def test_whitespace_only_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterRegistration(**_meter_payload(tenant_id="   "))

        assert "tenant_id" in str(exc_info.value)

    def test_tenant_id_is_stripped(self):
        meter = MeterRegistration(**_meter_payload(tenant_id="  tenant-2  "))
        assert meter.tenant_id == "tenant-2"


# ---------------------------------------------------------------------------
# MeterRegistration — schema hygiene
# ---------------------------------------------------------------------------


class TestMeterSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            MeterRegistration(**_meter_payload(unexpected_field="value"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            MeterRegistration(**_meter_payload(status="inactive"))


# ===========================================================================
# MeterAuditEntry Tests
# ===========================================================================


class TestMeterAuditEntryHappyPath:
    """Valid MeterAuditEntry construction with required and optional fields."""

    def test_minimal_required_fields(self):
        entry = MeterAuditEntry(**_audit_payload())

        assert entry.tenant_id == "tenant-1"
        assert entry.meter_id == "meter_abc123"
        assert entry.meter_ticket_id == "mt_ticket_001"
        assert entry.delivery_id == "del_001"
        assert entry.gross_gallons == 2500.0
        assert entry.timestamp == datetime(2024, 7, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert entry.invoice_id is None

    def test_audit_id_auto_generated_with_prefix(self):
        entry = MeterAuditEntry(**_audit_payload())

        assert entry.audit_id.startswith("maudit_")
        assert len(entry.audit_id) > len("maudit_")

    def test_unique_audit_ids_generated(self):
        e1 = MeterAuditEntry(**_audit_payload())
        e2 = MeterAuditEntry(**_audit_payload())

        assert e1.audit_id != e2.audit_id

    def test_invoice_id_can_be_set(self):
        entry = MeterAuditEntry(**_audit_payload(invoice_id="inv_001"))
        assert entry.invoice_id == "inv_001"

    def test_created_at_is_utc_aware(self):
        entry = MeterAuditEntry(**_audit_payload())

        assert entry.created_at.tzinfo is not None
        assert entry.created_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# MeterAuditEntry — tenant_id validation
# ---------------------------------------------------------------------------


class TestAuditTenantIdValidation:
    """tenant_id must be non-empty on MeterAuditEntry."""

    def test_empty_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(tenant_id=""))

        assert "tenant_id" in str(exc_info.value)

    def test_whitespace_only_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(tenant_id="   "))

        assert "tenant_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MeterAuditEntry — meter_id validation
# ---------------------------------------------------------------------------


class TestAuditMeterIdValidation:
    """meter_id must be non-empty on MeterAuditEntry."""

    def test_empty_meter_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(meter_id=""))

        assert "meter_id" in str(exc_info.value)

    def test_whitespace_only_meter_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(meter_id="   "))

        assert "meter_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MeterAuditEntry — meter_ticket_id validation
# ---------------------------------------------------------------------------


class TestAuditMeterTicketIdValidation:
    """meter_ticket_id must be non-empty on MeterAuditEntry."""

    def test_empty_meter_ticket_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(meter_ticket_id=""))

        assert "meter_ticket_id" in str(exc_info.value)

    def test_whitespace_only_meter_ticket_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(meter_ticket_id="   "))

        assert "meter_ticket_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MeterAuditEntry — delivery_id validation
# ---------------------------------------------------------------------------


class TestAuditDeliveryIdValidation:
    """delivery_id must be non-empty on MeterAuditEntry."""

    def test_empty_delivery_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(delivery_id=""))

        assert "delivery_id" in str(exc_info.value)

    def test_whitespace_only_delivery_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            MeterAuditEntry(**_audit_payload(delivery_id="   "))

        assert "delivery_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MeterAuditEntry — schema hygiene
# ---------------------------------------------------------------------------


class TestAuditSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            MeterAuditEntry(**_audit_payload(unexpected_field="value"))
