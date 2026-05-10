"""Unit tests for :class:`compliance.models.asset_certification.AssetCertification`.

Covers Task 8.1 of the Fuel Compliance Backbone spec, which validates
Requirement 13.1 (Asset certification registry with all cert types).

The tests assert:
- Happy-path construction with all required fields.
- Auto-generated ``cert_id`` with correct prefix.
- Default status is "valid".
- All certification types are accepted.
- Validators: inspector_name non-empty, certificate_number non-empty,
  asset_id non-empty, tenant_id non-empty.
- Extra fields are forbidden (schema hygiene).
- Invalid certification_type and status literals are rejected.
- Timestamps are UTC-aware.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest
from pydantic import ValidationError

from compliance.models.asset_certification import AssetCertification


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "asset_id": "truck-001",
        "certification_type": "V_test",
        "certification_date": date(2025, 1, 15),
        "expiry_date": date(2026, 1, 15),
        "inspector_name": "John Inspector",
        "certificate_number": "CERT-2025-001",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Valid AssetCertification construction with required fields."""

    def test_minimal_required_fields(self):
        cert = AssetCertification(**_base_payload())

        assert cert.tenant_id == "tenant-1"
        assert cert.asset_id == "truck-001"
        assert cert.certification_type == "V_test"
        assert cert.certification_date == date(2025, 1, 15)
        assert cert.expiry_date == date(2026, 1, 15)
        assert cert.inspector_name == "John Inspector"
        assert cert.certificate_number == "CERT-2025-001"
        assert cert.status == "valid"

    def test_cert_id_auto_generated_with_prefix(self):
        cert = AssetCertification(**_base_payload())

        assert cert.cert_id.startswith("cert_")
        assert len(cert.cert_id) > len("cert_")

    def test_unique_cert_ids_generated(self):
        c1 = AssetCertification(**_base_payload())
        c2 = AssetCertification(**_base_payload())

        assert c1.cert_id != c2.cert_id

    def test_timestamps_are_utc_aware(self):
        cert = AssetCertification(**_base_payload())

        assert cert.created_at.tzinfo is not None
        assert cert.updated_at.tzinfo is not None
        assert cert.created_at.tzinfo == timezone.utc

    def test_all_certification_types_accepted(self):
        valid_types = [
            "V_test",
            "K_test",
            "I_test",
            "P_test",
            "UT_test",
            "meter_seal",
            "fire_extinguisher",
        ]
        for cert_type in valid_types:
            cert = AssetCertification(**_base_payload(certification_type=cert_type))
            assert cert.certification_type == cert_type

    def test_all_status_values_accepted(self):
        for status in ("valid", "expiring_soon", "expired"):
            cert = AssetCertification(**_base_payload(status=status))
            assert cert.status == status

    def test_explicit_status_override(self):
        cert = AssetCertification(**_base_payload(status="expired"))
        assert cert.status == "expired"


# ---------------------------------------------------------------------------
# inspector_name validation
# ---------------------------------------------------------------------------


class TestInspectorNameValidation:
    """inspector_name must be non-empty."""

    def test_empty_inspector_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(inspector_name=""))

        assert "inspector_name" in str(exc_info.value)

    def test_whitespace_only_inspector_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(inspector_name="   "))

        assert "inspector_name" in str(exc_info.value)

    def test_inspector_name_is_stripped(self):
        cert = AssetCertification(**_base_payload(inspector_name="  Jane Doe  "))
        assert cert.inspector_name == "Jane Doe"


# ---------------------------------------------------------------------------
# certificate_number validation
# ---------------------------------------------------------------------------


class TestCertificateNumberValidation:
    """certificate_number must be non-empty."""

    def test_empty_certificate_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(certificate_number=""))

        assert "certificate_number" in str(exc_info.value)

    def test_whitespace_only_certificate_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(certificate_number="   "))

        assert "certificate_number" in str(exc_info.value)

    def test_certificate_number_is_stripped(self):
        cert = AssetCertification(**_base_payload(certificate_number="  ABC-123  "))
        assert cert.certificate_number == "ABC-123"


# ---------------------------------------------------------------------------
# asset_id validation
# ---------------------------------------------------------------------------


class TestAssetIdValidation:
    """asset_id must be non-empty."""

    def test_empty_asset_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(asset_id=""))

        assert "asset_id" in str(exc_info.value)

    def test_whitespace_only_asset_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(asset_id="   "))

        assert "asset_id" in str(exc_info.value)

    def test_asset_id_is_stripped(self):
        cert = AssetCertification(**_base_payload(asset_id="  truck-002  "))
        assert cert.asset_id == "truck-002"


# ---------------------------------------------------------------------------
# tenant_id validation
# ---------------------------------------------------------------------------


class TestTenantIdValidation:
    """tenant_id must be non-empty."""

    def test_empty_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(tenant_id=""))

        assert "tenant_id" in str(exc_info.value)

    def test_whitespace_only_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            AssetCertification(**_base_payload(tenant_id="   "))

        assert "tenant_id" in str(exc_info.value)

    def test_tenant_id_is_stripped(self):
        cert = AssetCertification(**_base_payload(tenant_id="  tenant-2  "))
        assert cert.tenant_id == "tenant-2"


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            AssetCertification(**_base_payload(unexpected_field="value"))

    def test_invalid_certification_type_rejected(self):
        with pytest.raises(ValidationError):
            AssetCertification(**_base_payload(certification_type="X_test"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            AssetCertification(**_base_payload(status="inactive"))
