"""Unit tests for DyedDieselEnforcer.

Tests cover:
- validate_order: valid certificate, missing certificate, expired certificate,
  non-dyed product (bypass), sales team notification on rejection
- validate_load_plan: non-dyed product (bypass), dyed-compatible compartment,
  clear-only compartment rejection, compartment not found, legacy docs
- is_dyed_diesel: product code classification

Validates: Requirement 6.1, 6.2, 6.3, 6.4
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from compliance.services.dyed_diesel_enforcer import (
    DYED_DIESEL_PRODUCT_CODES,
    IRS_637M_EXEMPTION_TYPE,
    DyedDieselEnforcer,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_fuel_co"
_CUSTOMER_ID = "cust_farm_ranch_001"


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_exemption_doc(
    *,
    customer_id: str = _CUSTOMER_ID,
    tenant_id: str = _TENANT_ID,
    exemption_type: str = IRS_637M_EXEMPTION_TYPE,
    certificate_number: str = "637M-2025-001234",
    expiry_date: str = "2027-12-31",
    status: str = "valid",
) -> Dict[str, Any]:
    """Build a tax_exemptions document as returned from ES."""
    return {
        "exemption_id": "exempt_001",
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": "acct_001",
        "exemption_type": exemption_type,
        "certificate_number": certificate_number,
        "letter_suffix": "M",
        "issuing_authority": "IRS",
        "product_codes": ["OFF_ROAD_DIESEL", "DYED_DIESEL"],
        "jurisdiction_fips": "48",
        "issued_date": "2025-01-01",
        "expiry_date": expiry_date,
        "status": status,
        "document_ref": "s3://certs/637m-001.pdf",
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _es_search_response(hits: list) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [
                {"_source": h, "sort": [h.get("created_at", "")]}
                for h in hits
            ],
            "total": {"value": len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: is_dyed_diesel
# ---------------------------------------------------------------------------


class TestIsDyedDiesel:
    """Tests for DyedDieselEnforcer.is_dyed_diesel static method."""

    def test_off_road_diesel_is_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("OFF_ROAD_DIESEL") is True

    def test_dyed_diesel_is_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("DYED_DIESEL") is True

    def test_dyed_ulsd_is_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("DYED_ULSD") is True

    def test_off_road_ulsd_is_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("OFF_ROAD_ULSD") is True

    def test_case_insensitive(self):
        assert DyedDieselEnforcer.is_dyed_diesel("off_road_diesel") is True
        assert DyedDieselEnforcer.is_dyed_diesel("Dyed_Diesel") is True

    def test_clear_diesel_is_not_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("ULSD") is False
        assert DyedDieselEnforcer.is_dyed_diesel("DIESEL") is False
        assert DyedDieselEnforcer.is_dyed_diesel("GASOLINE_87") is False

    def test_empty_string_is_not_dyed(self):
        assert DyedDieselEnforcer.is_dyed_diesel("") is False


# ---------------------------------------------------------------------------
# Tests: validate_order
# ---------------------------------------------------------------------------


class TestValidateOrder:
    """Tests for DyedDieselEnforcer.validate_order."""

    @pytest.mark.asyncio
    async def test_non_dyed_product_passes_without_check(self):
        """Non-dyed products should pass validation without querying ES."""
        es = _make_es_service()
        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "ULSD"
        )

        assert result.valid is True
        assert result.error_code is None
        # ES should NOT have been queried
        es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_certificate_passes(self):
        """Customer with a valid, non-expired 637M cert should pass."""
        es = _make_es_service()
        cert_doc = _make_exemption_doc(expiry_date="2027-12-31")
        es.search_documents.return_value = _es_search_response([cert_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is True
        assert result.error_code is None
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_certificate_rejects(self):
        """Customer without any 637M cert should be rejected."""
        es = _make_es_service()
        # No hits returned — no certificate found
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        assert _CUSTOMER_ID in result.message

    @pytest.mark.asyncio
    async def test_missing_certificate_notifies_sales_team(self):
        """Req 6.2: Rejection should notify the sales team via signal bus."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        signal_bus = AsyncMock()
        signal_bus.publish = AsyncMock(return_value=1)

        enforcer = DyedDieselEnforcer(es, signal_bus=signal_bus)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        # Signal bus should have been called to notify sales team
        signal_bus.publish.assert_called_once()
        signal = signal_bus.publish.call_args[0][0]
        assert signal.source_agent == "dyed_diesel_enforcer"
        assert signal.entity_id == _CUSTOMER_ID
        assert signal.entity_type == "customer"
        assert signal.tenant_id == _TENANT_ID
        assert signal.context["event"] == "dyed_diesel_order_rejected"
        assert signal.context["action_required"] == "sales_team_followup"

    @pytest.mark.asyncio
    async def test_missing_certificate_no_signal_bus_still_rejects(self):
        """Rejection works even without signal_bus (graceful degradation)."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        # No signal_bus provided
        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"

    @pytest.mark.asyncio
    async def test_expired_certificate_rejects(self):
        """Customer whose 637M cert is expired should be rejected.

        The ES query filters by expiry_date >= today, so an expired cert
        won't appear in results — simulated by returning empty hits.
        """
        es = _make_es_service()
        # Expired cert won't match the range filter, so ES returns empty
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "DYED_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"

    @pytest.mark.asyncio
    async def test_dyed_ulsd_variant_triggers_check(self):
        """DYED_ULSD product code should trigger the exemption check."""
        es = _make_es_service()
        cert_doc = _make_exemption_doc()
        es.search_documents.return_value = _es_search_response([cert_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "DYED_ULSD"
        )

        assert result.valid is True
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(self):
        """The ES query should be wrapped with tenant_id filter."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        # Verify the query passed to ES includes tenant_id filtering
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]  # second positional arg is the query

        # The inject_tenant_filter wraps the query in a bool with a
        # filter clause containing {"term": {"tenant_id": ...}}
        query_str = str(query_body)
        assert "tenant_id" in query_str
        assert _TENANT_ID in query_str


# ---------------------------------------------------------------------------
# Tests: validate_load_plan (Req 6.3, 6.4)
# ---------------------------------------------------------------------------


class TestValidateLoadPlan:
    """Tests for DyedDieselEnforcer.validate_load_plan.

    Validates: Requirements 6.3, 6.4
    """

    @pytest.mark.asyncio
    async def test_non_dyed_product_passes_without_check(self):
        """Non-dyed products should pass validation without querying ES."""
        es = _make_es_service()
        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_load_plan(
            _TENANT_ID, "FT-001_C1", "ULSD"
        )

        assert result.valid is True
        assert result.error_code is None
        # ES should NOT have been queried
        es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_dyed_compatible_compartment_passes(self):
        """Compartment with dyed_compatible=True should pass."""
        es = _make_es_service()
        compartment_doc = {
            "compartment_id": "C1",
            "truck_id": "FT-001",
            "capacity_liters": 12000.0,
            "allowed_grades": ["DIESEL_2", "OFF_ROAD_DIESEL"],
            "dyed_compatible": True,
            "tenant_id": _TENANT_ID,
        }
        es.search_documents.return_value = _es_search_response([compartment_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_load_plan(
            _TENANT_ID, "C1", "OFF_ROAD_DIESEL"
        )

        assert result.valid is True
        assert result.error_code is None
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_only_compartment_rejected(self):
        """Compartment with dyed_compatible=False should be rejected.

        Validates: Requirement 6.4
        """
        es = _make_es_service()
        compartment_doc = {
            "compartment_id": "C2",
            "truck_id": "FT-001",
            "capacity_liters": 12000.0,
            "allowed_grades": ["DIESEL_2", "GASOLINE_REG"],
            "dyed_compatible": False,
            "tenant_id": _TENANT_ID,
        }
        es.search_documents.return_value = _es_search_response([compartment_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_load_plan(
            _TENANT_ID, "C2", "DYED_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.compartment_incompatible"
        assert "C2" in result.message
        assert "clear-only" in result.message

    @pytest.mark.asyncio
    async def test_compartment_not_found_rejected(self):
        """Non-existent compartment should be rejected."""
        es = _make_es_service()
        # No hits returned — compartment not found
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_load_plan(
            _TENANT_ID, "NONEXISTENT", "OFF_ROAD_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.compartment_not_found"
        assert "NONEXISTENT" in result.message

    @pytest.mark.asyncio
    async def test_legacy_compartment_without_dyed_flag_passes(self):
        """Legacy compartments without dyed_compatible field default to True."""
        es = _make_es_service()
        # Legacy doc without dyed_compatible field
        compartment_doc = {
            "compartment_id": "C3",
            "truck_id": "FT-002",
            "capacity_liters": 10000.0,
            "allowed_grades": ["DIESEL_2"],
            "tenant_id": _TENANT_ID,
        }
        es.search_documents.return_value = _es_search_response([compartment_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_load_plan(
            _TENANT_ID, "C3", "DYED_ULSD"
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(self):
        """The ES query should be wrapped with tenant_id filter."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        await enforcer.validate_load_plan(
            _TENANT_ID, "C1", "OFF_ROAD_DIESEL"
        )

        # Verify the query passed to ES includes tenant_id filtering
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]  # second positional arg is the query

        query_str = str(query_body)
        assert "tenant_id" in query_str
        assert _TENANT_ID in query_str


# ---------------------------------------------------------------------------
# Tests: Certificate Expiry Blocking (Req 6.6)
# ---------------------------------------------------------------------------


class TestCertificateExpiryBlocking:
    """Tests for Req 6.6: Block future dyed orders when certificate expires.

    Validates: Requirement 6.6
    """

    @pytest.mark.asyncio
    async def test_order_passes_day_before_expiry(self):
        """Customer with cert expiring tomorrow → order passes today.

        Validates: Requirement 6.6 — orders are allowed while cert is valid.
        """
        es = _make_es_service()
        # Certificate expires tomorrow — still valid today
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cert_doc = _make_exemption_doc(expiry_date=tomorrow)
        es.search_documents.return_value = _es_search_response([cert_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_order_blocked_after_cert_expires(self):
        """Same customer after cert expires → order blocked.

        Validates: Requirement 6.6 — once expired, future orders are blocked.
        The ES query filters by expiry_date >= today, so an expired cert
        won't appear in results.
        """
        es = _make_es_service()
        # Certificate expired yesterday — ES range filter won't match it
        # so ES returns empty results
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        assert _CUSTOMER_ID in result.message

    @pytest.mark.asyncio
    async def test_order_unblocked_after_renewed_cert_uploaded(self):
        """Customer uploads new valid cert → order passes again.

        Validates: Requirement 6.6 — block is lifted when renewed
        certificate is uploaded and validated.
        """
        es = _make_es_service()
        # New certificate with future expiry date
        future_expiry = (date.today() + timedelta(days=365)).isoformat()
        renewed_cert = _make_exemption_doc(
            expiry_date=future_expiry,
            certificate_number="637M-2026-RENEWED",
        )
        es.search_documents.return_value = _es_search_response([renewed_cert])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_full_lifecycle_valid_then_expired_then_renewed(self):
        """Full lifecycle: valid → expired → renewed.

        Validates: Requirement 6.6 — demonstrates the complete blocking
        and unblocking cycle.
        """
        es = _make_es_service()
        enforcer = DyedDieselEnforcer(es)

        # Step 1: Certificate is valid — order passes
        valid_cert = _make_exemption_doc(
            expiry_date=(date.today() + timedelta(days=30)).isoformat()
        )
        es.search_documents.return_value = _es_search_response([valid_cert])

        result1 = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "DYED_DIESEL"
        )
        assert result1.valid is True

        # Step 2: Certificate expires — order blocked
        # (expired cert won't match the range filter)
        es.search_documents.return_value = _es_search_response([])

        result2 = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "DYED_DIESEL"
        )
        assert result2.valid is False
        assert result2.error_code == "dyed.no_valid_exemption"

        # Step 3: Customer uploads renewed certificate — order passes again
        renewed_cert = _make_exemption_doc(
            expiry_date=(date.today() + timedelta(days=365)).isoformat(),
            certificate_number="637M-2026-RENEWED",
        )
        es.search_documents.return_value = _es_search_response([renewed_cert])

        result3 = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "DYED_DIESEL"
        )
        assert result3.valid is True

    @pytest.mark.asyncio
    async def test_cert_expiring_today_still_valid(self):
        """Certificate expiring today (expiry_date == today) is still valid.

        The range filter uses gte (>=), so a cert expiring today is still
        valid for today's orders.

        Validates: Requirement 6.6
        """
        es = _make_es_service()
        # Certificate expires today — still valid (gte includes today)
        today = date.today().isoformat()
        cert_doc = _make_exemption_doc(expiry_date=today)
        es.search_documents.return_value = _es_search_response([cert_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_order(
            _TENANT_ID, _CUSTOMER_ID, "OFF_ROAD_DIESEL"
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_multiple_products_all_blocked_when_expired(self):
        """All dyed diesel product codes are blocked when cert expires.

        Validates: Requirement 6.6 — blocking applies to all dyed variants.
        """
        es = _make_es_service()
        # No valid cert — all dyed products should be blocked
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        for product_code in ["OFF_ROAD_DIESEL", "DYED_DIESEL", "DYED_ULSD", "OFF_ROAD_ULSD"]:
            result = await enforcer.validate_order(
                _TENANT_ID, _CUSTOMER_ID, product_code
            )
            assert result.valid is False, f"{product_code} should be blocked"
            assert result.error_code == "dyed.no_valid_exemption"


# ---------------------------------------------------------------------------
# Tests: check_expiring_certificates (Req 6.6 — proactive alerting)
# ---------------------------------------------------------------------------


class TestCheckExpiringCertificates:
    """Tests for DyedDieselEnforcer.check_expiring_certificates.

    Validates: Requirement 6.6
    """

    @pytest.mark.asyncio
    async def test_returns_certs_expiring_within_window(self):
        """Should return certificates expiring within the specified window."""
        es = _make_es_service()
        expiring_cert = _make_exemption_doc(
            expiry_date=(date.today() + timedelta(days=15)).isoformat()
        )
        es.search_documents.return_value = _es_search_response([expiring_cert])

        enforcer = DyedDieselEnforcer(es)

        results = await enforcer.check_expiring_certificates(
            _TENANT_ID, days_ahead=30
        )

        assert len(results) == 1
        assert results[0]["customer_id"] == _CUSTOMER_ID
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_certs_expiring(self):
        """Should return empty list when no certificates are expiring."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        results = await enforcer.check_expiring_certificates(
            _TENANT_ID, days_ahead=30
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_custom_days_ahead_window(self):
        """Should respect the custom days_ahead parameter."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        await enforcer.check_expiring_certificates(
            _TENANT_ID, days_ahead=60
        )

        # Verify the query uses the correct date range
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        query_str = str(query_body)

        future_date = (date.today() + timedelta(days=60)).isoformat()
        assert future_date in query_str

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(self):
        """The ES query should be wrapped with tenant_id filter."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        await enforcer.check_expiring_certificates(_TENANT_ID)

        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        query_str = str(query_body)
        assert "tenant_id" in query_str
        assert _TENANT_ID in query_str


# ---------------------------------------------------------------------------
# Tests: ValidationResult model
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Tests for the ValidationResult Pydantic model."""

    def test_valid_result(self):
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.error_code is None
        assert result.message is None

    def test_invalid_result_with_error(self):
        result = ValidationResult(
            valid=False,
            error_code="dyed.no_valid_exemption",
            message="No certificate found",
        )
        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        assert result.message == "No certificate found"

    def test_extra_fields_rejected(self):
        """Extra fields should be rejected due to extra='forbid'."""
        with pytest.raises(Exception):
            ValidationResult(valid=True, extra_field="bad")


# ---------------------------------------------------------------------------
# Tests: validate_invoice (Req 6.5)
# ---------------------------------------------------------------------------


class TestValidateInvoice:
    """Tests for DyedDieselEnforcer.validate_invoice.

    Validates: Requirement 6.5
    """

    @pytest.mark.asyncio
    async def test_non_dyed_diesel_invoice_passes_without_check(self):
        """Invoice with only non-dyed products should pass without tax check."""
        es = _make_es_service()
        invoice_doc = {
            "invoice_id": "inv_001",
            "tenant_id": _TENANT_ID,
            "customer_id": _CUSTOMER_ID,
            "line_items": [
                {"product_code": "ULSD", "quantity_gallons": 500.0},
                {"product_code": "GASOLINE_87", "quantity_gallons": 300.0},
            ],
            "tax_breakdown": {
                "federal_cents": 1220,
                "state_cents": 900,
                "county_cents": 0,
                "city_cents": 0,
                "ust_cents": 50,
                "spcc_cents": 0,
                "environmental_cents": 0,
                "total_tax_cents": 2170,
                "line_items": [
                    {
                        "tax_component_name": "federal_excise",
                        "jurisdiction_fips": "00",
                        "jurisdiction_level": "federal",
                        "rate_cents_per_gallon": 24,
                        "gallons": 500.0,
                        "amount_cents": 1220,
                    },
                    {
                        "tax_component_name": "state_excise",
                        "jurisdiction_fips": "48",
                        "jurisdiction_level": "state",
                        "rate_cents_per_gallon": 20,
                        "gallons": 500.0,
                        "amount_cents": 900,
                    },
                ],
                "exemptions_applied": [],
            },
        }
        es.search_documents.return_value = _es_search_response([invoice_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_001")

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_dyed_diesel_invoice_no_excise_taxes_passes(self):
        """Dyed diesel invoice with no excise taxes should pass."""
        es = _make_es_service()
        invoice_doc = {
            "invoice_id": "inv_002",
            "tenant_id": _TENANT_ID,
            "customer_id": _CUSTOMER_ID,
            "line_items": [
                {"product_code": "OFF_ROAD_DIESEL", "quantity_gallons": 1000.0},
            ],
            "tax_breakdown": {
                "federal_cents": 0,
                "state_cents": 0,
                "county_cents": 0,
                "city_cents": 0,
                "ust_cents": 50,
                "spcc_cents": 10,
                "environmental_cents": 5,
                "total_tax_cents": 65,
                "line_items": [
                    {
                        "tax_component_name": "ust_fee",
                        "jurisdiction_fips": "48",
                        "jurisdiction_level": "state",
                        "rate_cents_per_gallon": 1,
                        "gallons": 1000.0,
                        "amount_cents": 50,
                    },
                ],
                "exemptions_applied": ["exempt_637m_001"],
            },
        }
        es.search_documents.return_value = _es_search_response([invoice_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_002")

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_dyed_diesel_invoice_with_federal_excise_fails(self):
        """Dyed diesel invoice with federal excise tax should fail."""
        es = _make_es_service()
        invoice_doc = {
            "invoice_id": "inv_003",
            "tenant_id": _TENANT_ID,
            "customer_id": _CUSTOMER_ID,
            "line_items": [
                {"product_code": "DYED_DIESEL", "quantity_gallons": 800.0},
            ],
            "tax_breakdown": {
                "federal_cents": 1952,
                "state_cents": 0,
                "county_cents": 0,
                "city_cents": 0,
                "ust_cents": 0,
                "spcc_cents": 0,
                "environmental_cents": 0,
                "total_tax_cents": 1952,
                "line_items": [
                    {
                        "tax_component_name": "federal_excise",
                        "jurisdiction_fips": "00",
                        "jurisdiction_level": "federal",
                        "rate_cents_per_gallon": 24,
                        "gallons": 800.0,
                        "amount_cents": 1952,
                    },
                ],
                "exemptions_applied": [],
            },
        }
        es.search_documents.return_value = _es_search_response([invoice_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_003")

        assert result.valid is False
        assert result.error_code == "dyed.tax_exemption_not_applied"
        assert "federal_excise" in result.message

    @pytest.mark.asyncio
    async def test_dyed_diesel_invoice_with_state_excise_fails(self):
        """Dyed diesel invoice with state excise tax should fail."""
        es = _make_es_service()
        invoice_doc = {
            "invoice_id": "inv_004",
            "tenant_id": _TENANT_ID,
            "customer_id": _CUSTOMER_ID,
            "line_items": [
                {"product_code": "OFF_ROAD_ULSD", "quantity_gallons": 600.0},
            ],
            "tax_breakdown": {
                "federal_cents": 0,
                "state_cents": 1200,
                "county_cents": 0,
                "city_cents": 0,
                "ust_cents": 0,
                "spcc_cents": 0,
                "environmental_cents": 0,
                "total_tax_cents": 1200,
                "line_items": [
                    {
                        "tax_component_name": "state_excise",
                        "jurisdiction_fips": "48",
                        "jurisdiction_level": "state",
                        "rate_cents_per_gallon": 20,
                        "gallons": 600.0,
                        "amount_cents": 1200,
                    },
                ],
                "exemptions_applied": [],
            },
        }
        es.search_documents.return_value = _es_search_response([invoice_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_004")

        assert result.valid is False
        assert result.error_code == "dyed.tax_exemption_not_applied"
        assert "state_excise" in result.message

    @pytest.mark.asyncio
    async def test_invoice_not_found_fails(self):
        """Invoice not found should fail with appropriate error."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_nonexistent")

        assert result.valid is False
        assert result.error_code == "dyed.invoice_not_found"
        assert "inv_nonexistent" in result.message

    @pytest.mark.asyncio
    async def test_dyed_diesel_invoice_no_tax_breakdown_passes(self):
        """Dyed diesel invoice without tax_breakdown field should pass.

        Legacy invoices may not have a tax_breakdown. If no breakdown
        is present, there are no excise taxes to flag.
        """
        es = _make_es_service()
        invoice_doc = {
            "invoice_id": "inv_005",
            "tenant_id": _TENANT_ID,
            "customer_id": _CUSTOMER_ID,
            "line_items": [
                {"product_code": "DYED_ULSD", "quantity_gallons": 500.0},
            ],
            # No tax_breakdown field
        }
        es.search_documents.return_value = _es_search_response([invoice_doc])

        enforcer = DyedDieselEnforcer(es)

        result = await enforcer.validate_invoice(_TENANT_ID, "inv_005")

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(self):
        """The ES query should be wrapped with tenant_id filter."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        enforcer = DyedDieselEnforcer(es)

        await enforcer.validate_invoice(_TENANT_ID, "inv_any")

        # Verify the query passed to ES includes tenant_id filtering
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]  # second positional arg is the query

        query_str = str(query_body)
        assert "tenant_id" in query_str
        assert _TENANT_ID in query_str

# ---------------------------------------------------------------------------
# Tests: log_dyed_sale (Req 6.7)
# ---------------------------------------------------------------------------


class TestLogDyedSale:
    """Tests for DyedDieselEnforcer.log_dyed_sale.

    Validates: Requirement 6.7
    """

    @pytest.mark.asyncio
    async def test_successful_audit_log_write(self):
        """Should persist all required fields to the dyed_diesel_audit_log index."""
        es = _make_es_service()
        es.index_document = AsyncMock(return_value=None)

        enforcer = DyedDieselEnforcer(es)

        await enforcer.log_dyed_sale(
            tenant_id=_TENANT_ID,
            customer_id=_CUSTOMER_ID,
            certificate_id="637M-2025-001234",
            certificate_expiry="2027-12-31",
            gallons=1500.0,
            invoice_id="inv_dyed_001",
            product_code="OFF_ROAD_DIESEL",
        )

        # Verify index_document was called once
        es.index_document.assert_called_once()

        # Extract the call arguments
        call_args = es.index_document.call_args
        index_name = call_args[0][0]
        doc_id = call_args[0][1]
        doc_body = call_args[0][2]

        # Verify index name
        assert index_name == "dyed_diesel_audit_log"

        # Verify doc_id starts with expected prefix
        assert doc_id.startswith("dyed_audit_")

        # Verify all required fields are present
        assert doc_body["tenant_id"] == _TENANT_ID
        assert doc_body["customer_id"] == _CUSTOMER_ID
        assert doc_body["certificate_id"] == "637M-2025-001234"
        assert doc_body["certificate_expiry"] == "2027-12-31"
        assert doc_body["gallons"] == 1500.0
        assert doc_body["invoice_id"] == "inv_dyed_001"
        assert doc_body["product_code"] == "OFF_ROAD_DIESEL"

        # Verify timestamp fields are present
        assert "timestamp" in doc_body
        assert "created_at" in doc_body
        assert "updated_at" in doc_body

    @pytest.mark.asyncio
    async def test_es_failure_does_not_raise(self):
        """ES write failure should be logged but not raise an exception.

        Validates: Requirement 6.7 — non-blocking audit log.
        """
        es = _make_es_service()
        es.index_document = AsyncMock(
            side_effect=Exception("ES connection timeout")
        )

        enforcer = DyedDieselEnforcer(es)

        # Should NOT raise — graceful degradation
        await enforcer.log_dyed_sale(
            tenant_id=_TENANT_ID,
            customer_id=_CUSTOMER_ID,
            certificate_id="637M-2025-001234",
            certificate_expiry="2027-12-31",
            gallons=800.0,
            invoice_id="inv_dyed_002",
            product_code="DYED_DIESEL",
        )

        # Verify the write was attempted
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_required_fields_persisted(self):
        """All IRS-required fields must be present in the persisted document.

        Required fields per Req 6.7:
        - customer_id
        - certificate_id
        - certificate_expiry
        - gallons
        - invoice_id

        Additional fields for audit trail:
        - tenant_id
        - product_code
        - timestamp
        """
        es = _make_es_service()
        es.index_document = AsyncMock(return_value=None)

        enforcer = DyedDieselEnforcer(es)

        await enforcer.log_dyed_sale(
            tenant_id=_TENANT_ID,
            customer_id="cust_xyz",
            certificate_id="637M-2026-ABCDEF",
            certificate_expiry="2028-06-30",
            gallons=2500.5,
            invoice_id="inv_dyed_003",
            product_code="DYED_ULSD",
        )

        call_args = es.index_document.call_args
        doc_body = call_args[0][2]

        # IRS-required fields
        assert doc_body["customer_id"] == "cust_xyz"
        assert doc_body["certificate_id"] == "637M-2026-ABCDEF"
        assert doc_body["certificate_expiry"] == "2028-06-30"
        assert doc_body["gallons"] == 2500.5
        assert doc_body["invoice_id"] == "inv_dyed_003"

        # Additional audit fields
        assert doc_body["tenant_id"] == _TENANT_ID
        assert doc_body["product_code"] == "DYED_ULSD"
        assert doc_body["audit_id"].startswith("dyed_audit_")
        assert doc_body["timestamp"] is not None
