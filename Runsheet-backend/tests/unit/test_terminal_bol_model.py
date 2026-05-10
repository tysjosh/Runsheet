"""Unit tests for :class:`compliance.models.terminal_bol.TerminalBOL`.

Covers Task 11.1 of the Fuel Compliance Backbone spec, which validates
Requirement 10.1 (Terminal BOL parsing and field extraction).

The tests assert:
- Happy-path construction with all required fields.
- Auto-generated ``bol_id`` with correct prefix.
- Default status is "ingested".
- Validators: load_number non-empty, product_code non-empty,
  tenant_id non-empty, gross_gallons positive, net_gallons positive.
- Optional fields default to None.
- Extra fields are forbidden (schema hygiene).
- Invalid status literals are rejected.
- Timestamps are UTC-aware.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from compliance.models.terminal_bol import TerminalBOL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "load_number": "LOAD-2025-001",
        "product_code": "ULSD",
        "gross_gallons": 8500.0,
        "net_gallons": 8450.2,
        "observed_temperature_f": 72.5,
        "api_gravity": 35.0,
        "supplier_name": "Marathon Petroleum",
        "terminal_name": "Houston Terminal A",
        "driver_id": "driver-101",
        "timestamp": datetime(2025, 5, 10, 14, 30, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Valid TerminalBOL construction with required fields."""

    def test_minimal_required_fields(self):
        bol = TerminalBOL(**_base_payload())

        assert bol.tenant_id == "tenant-1"
        assert bol.load_number == "LOAD-2025-001"
        assert bol.product_code == "ULSD"
        assert bol.gross_gallons == 8500.0
        assert bol.net_gallons == 8450.2
        assert bol.observed_temperature_f == 72.5
        assert bol.api_gravity == 35.0
        assert bol.supplier_name == "Marathon Petroleum"
        assert bol.terminal_name == "Houston Terminal A"
        assert bol.driver_id == "driver-101"
        assert bol.timestamp == datetime(2025, 5, 10, 14, 30, 0, tzinfo=timezone.utc)

    def test_bol_id_auto_generated_with_prefix(self):
        bol = TerminalBOL(**_base_payload())

        assert bol.bol_id.startswith("bol_")
        assert len(bol.bol_id) > len("bol_")

    def test_unique_bol_ids_generated(self):
        b1 = TerminalBOL(**_base_payload())
        b2 = TerminalBOL(**_base_payload())

        assert b1.bol_id != b2.bol_id

    def test_default_status_is_ingested(self):
        bol = TerminalBOL(**_base_payload())
        assert bol.status == "ingested"

    def test_optional_fields_default_to_none(self):
        bol = TerminalBOL(**_base_payload())

        assert bol.raw_document_ref is None
        assert bol.load_plan_id is None
        assert bol.vcf_discrepancy_flag is None

    def test_timestamps_are_utc_aware(self):
        bol = TerminalBOL(**_base_payload())

        assert bol.created_at.tzinfo is not None
        assert bol.updated_at.tzinfo is not None
        assert bol.created_at.tzinfo == timezone.utc

    def test_all_status_values_accepted(self):
        for status in ("ingested", "linked", "verified"):
            bol = TerminalBOL(**_base_payload(status=status))
            assert bol.status == status

    def test_explicit_optional_fields(self):
        bol = TerminalBOL(
            **_base_payload(
                raw_document_ref="s3://bucket/bols/raw/LOAD-2025-001.edi",
                load_plan_id="lp-001",
                vcf_discrepancy_flag=True,
            )
        )

        assert bol.raw_document_ref == "s3://bucket/bols/raw/LOAD-2025-001.edi"
        assert bol.load_plan_id == "lp-001"
        assert bol.vcf_discrepancy_flag is True


# ---------------------------------------------------------------------------
# load_number validation
# ---------------------------------------------------------------------------


class TestLoadNumberValidation:
    """load_number must be non-empty."""

    def test_empty_load_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(load_number=""))

        assert "load_number" in str(exc_info.value)

    def test_whitespace_only_load_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(load_number="   "))

        assert "load_number" in str(exc_info.value)

    def test_load_number_is_stripped(self):
        bol = TerminalBOL(**_base_payload(load_number="  LOAD-123  "))
        assert bol.load_number == "LOAD-123"


# ---------------------------------------------------------------------------
# product_code validation
# ---------------------------------------------------------------------------


class TestProductCodeValidation:
    """product_code must be non-empty."""

    def test_empty_product_code_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(product_code=""))

        assert "product_code" in str(exc_info.value)

    def test_whitespace_only_product_code_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(product_code="   "))

        assert "product_code" in str(exc_info.value)

    def test_product_code_is_stripped(self):
        bol = TerminalBOL(**_base_payload(product_code="  UNL87  "))
        assert bol.product_code == "UNL87"


# ---------------------------------------------------------------------------
# tenant_id validation
# ---------------------------------------------------------------------------


class TestTenantIdValidation:
    """tenant_id must be non-empty."""

    def test_empty_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(tenant_id=""))

        assert "tenant_id" in str(exc_info.value)

    def test_whitespace_only_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(tenant_id="   "))

        assert "tenant_id" in str(exc_info.value)

    def test_tenant_id_is_stripped(self):
        bol = TerminalBOL(**_base_payload(tenant_id="  tenant-2  "))
        assert bol.tenant_id == "tenant-2"


# ---------------------------------------------------------------------------
# gross_gallons validation
# ---------------------------------------------------------------------------


class TestGrossGallonsValidation:
    """gross_gallons must be positive."""

    def test_zero_gross_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(gross_gallons=0))

        assert "gross_gallons" in str(exc_info.value)

    def test_negative_gross_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(gross_gallons=-100.0))

        assert "gross_gallons" in str(exc_info.value)

    def test_small_positive_gross_gallons_accepted(self):
        bol = TerminalBOL(**_base_payload(gross_gallons=0.1))
        assert bol.gross_gallons == 0.1


# ---------------------------------------------------------------------------
# net_gallons validation
# ---------------------------------------------------------------------------


class TestNetGallonsValidation:
    """net_gallons must be positive."""

    def test_zero_net_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(net_gallons=0))

        assert "net_gallons" in str(exc_info.value)

    def test_negative_net_gallons_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TerminalBOL(**_base_payload(net_gallons=-50.0))

        assert "net_gallons" in str(exc_info.value)

    def test_small_positive_net_gallons_accepted(self):
        bol = TerminalBOL(**_base_payload(net_gallons=0.1))
        assert bol.net_gallons == 0.1


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            TerminalBOL(**_base_payload(unexpected_field="value"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            TerminalBOL(**_base_payload(status="cancelled"))
