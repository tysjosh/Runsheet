"""Unit tests for :class:`compliance.models.driver.Driver`.

Covers Task 6.1 of the Fuel Compliance Backbone spec, which validates
Requirement 5.1 (Driver entity with all qualification expiry fields).

The tests assert:
- Happy-path construction with all required fields.
- Auto-generated ``driver_id`` with correct prefix.
- Default status is "active".
- Validators: full_name non-empty, cdl_number non-empty, cdl_state is
  2 uppercase letters.
- Optional fields default to None.
- Extra fields are forbidden (schema hygiene).
- Invalid cdl_class and status literals are rejected.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from compliance.models.driver import Driver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "full_name": "John Smith",
        "cdl_number": "D1234567",
        "cdl_state": "TX",
        "cdl_class": "A",
        "cdl_expiry_date": date(2027, 6, 15),
        "medical_card_expiry_date": date(2026, 12, 31),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Valid Driver construction with required and optional fields."""

    def test_minimal_required_fields(self):
        driver = Driver(**_base_payload())

        assert driver.tenant_id == "tenant-1"
        assert driver.full_name == "John Smith"
        assert driver.cdl_number == "D1234567"
        assert driver.cdl_state == "TX"
        assert driver.cdl_class == "A"
        assert driver.cdl_expiry_date == date(2027, 6, 15)
        assert driver.medical_card_expiry_date == date(2026, 12, 31)
        assert driver.status == "active"

    def test_driver_id_auto_generated_with_prefix(self):
        driver = Driver(**_base_payload())

        assert driver.driver_id.startswith("driver_")
        assert len(driver.driver_id) > len("driver_")

    def test_unique_driver_ids_generated(self):
        d1 = Driver(**_base_payload())
        d2 = Driver(**_base_payload())

        assert d1.driver_id != d2.driver_id

    def test_all_optional_fields_populated(self):
        driver = Driver(
            **_base_payload(
                hazmat_endorsement_expiry_date=date(2027, 3, 1),
                tanker_endorsement_expiry_date=date(2027, 4, 1),
                last_drug_test_date=date(2026, 1, 15),
                last_mvr_date=date(2026, 2, 20),
                status="suspended",
                suspension_reason="CDL expired",
                external_refs={"geotab_driver_id": "GT-123"},
            )
        )

        assert driver.hazmat_endorsement_expiry_date == date(2027, 3, 1)
        assert driver.tanker_endorsement_expiry_date == date(2027, 4, 1)
        assert driver.last_drug_test_date == date(2026, 1, 15)
        assert driver.last_mvr_date == date(2026, 2, 20)
        assert driver.status == "suspended"
        assert driver.suspension_reason == "CDL expired"
        assert driver.external_refs == {"geotab_driver_id": "GT-123"}

    def test_optional_fields_default_to_none(self):
        driver = Driver(**_base_payload())

        assert driver.hazmat_endorsement_expiry_date is None
        assert driver.tanker_endorsement_expiry_date is None
        assert driver.last_drug_test_date is None
        assert driver.last_mvr_date is None
        assert driver.suspension_reason is None
        assert driver.external_refs is None

    def test_timestamps_are_utc_aware(self):
        driver = Driver(**_base_payload())

        assert driver.created_at.tzinfo is not None
        assert driver.updated_at.tzinfo is not None
        assert driver.created_at.tzinfo == timezone.utc

    def test_all_cdl_classes_accepted(self):
        for cdl_class in ("A", "B", "C"):
            driver = Driver(**_base_payload(cdl_class=cdl_class))
            assert driver.cdl_class == cdl_class

    def test_all_status_values_accepted(self):
        for status in ("active", "suspended", "expired"):
            driver = Driver(**_base_payload(status=status))
            assert driver.status == status


# ---------------------------------------------------------------------------
# full_name validation
# ---------------------------------------------------------------------------


class TestFullNameValidation:
    """full_name must be non-empty."""

    def test_empty_full_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(full_name=""))

        assert "full_name" in str(exc_info.value)

    def test_whitespace_only_full_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(full_name="   "))

        assert "full_name" in str(exc_info.value)

    def test_full_name_is_stripped(self):
        driver = Driver(**_base_payload(full_name="  Jane Doe  "))
        assert driver.full_name == "Jane Doe"


# ---------------------------------------------------------------------------
# cdl_number validation
# ---------------------------------------------------------------------------


class TestCdlNumberValidation:
    """cdl_number must be non-empty."""

    def test_empty_cdl_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_number=""))

        assert "cdl_number" in str(exc_info.value)

    def test_whitespace_only_cdl_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_number="   "))

        assert "cdl_number" in str(exc_info.value)

    def test_cdl_number_is_stripped(self):
        driver = Driver(**_base_payload(cdl_number="  ABC123  "))
        assert driver.cdl_number == "ABC123"


# ---------------------------------------------------------------------------
# cdl_state validation
# ---------------------------------------------------------------------------


class TestCdlStateValidation:
    """cdl_state must be exactly 2 uppercase letters."""

    def test_lowercase_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state="tx"))

        assert "cdl_state" in str(exc_info.value)

    def test_mixed_case_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state="Tx"))

        assert "cdl_state" in str(exc_info.value)

    def test_single_letter_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state="T"))

        assert "cdl_state" in str(exc_info.value)

    def test_three_letter_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state="TEX"))

        assert "cdl_state" in str(exc_info.value)

    def test_numeric_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state="12"))

        assert "cdl_state" in str(exc_info.value)

    def test_empty_state_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Driver(**_base_payload(cdl_state=""))

        assert "cdl_state" in str(exc_info.value)

    def test_cdl_state_is_stripped(self):
        driver = Driver(**_base_payload(cdl_state=" CA "))
        assert driver.cdl_state == "CA"


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            Driver(**_base_payload(unexpected_field="value"))

    def test_invalid_cdl_class_rejected(self):
        with pytest.raises(ValidationError):
            Driver(**_base_payload(cdl_class="D"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            Driver(**_base_payload(status="inactive"))


# ---------------------------------------------------------------------------
# suspension_reason normalization
# ---------------------------------------------------------------------------


class TestSuspensionReasonNormalization:
    """suspension_reason is stripped; all-whitespace collapses to None."""

    def test_suspension_reason_is_stripped(self):
        driver = Driver(**_base_payload(suspension_reason="  CDL expired  "))
        assert driver.suspension_reason == "CDL expired"

    def test_whitespace_only_suspension_reason_becomes_none(self):
        driver = Driver(**_base_payload(suspension_reason="   "))
        assert driver.suspension_reason is None
