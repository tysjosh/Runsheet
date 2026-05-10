"""Unit tests for AssetCertificationService.

Tests cover:
- Create: valid creation, validation errors (empty asset_id, empty inspector_name)
- Get: found, not found (404)
- List: pagination, filtering by asset_id and status
- Update: partial updates, validation on update

Validates: Requirement 13.1
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from compliance.services.asset_certification_service import (
    AssetCertificationService,
    AssetEligibility,
    CertAlert,
    CertificationSummary,
    ALERT_THRESHOLD_WARNING_DAYS,
    ALERT_THRESHOLD_URGENT_DAYS,
    ALERT_THRESHOLD_CRITICAL_DAYS,
    DOT_CARGO_TANK_CERT_TYPES,
    RETEST_INTERVAL_DAYS,
)
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_fuel_co"
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_cert_doc(
    *,
    cert_id: str = "cert_test123",
    tenant_id: str = _TENANT_ID,
    asset_id: str = "truck_001",
    certification_type: str = "V_test",
    status: str = "valid",
    certification_date: str = "2024-06-01",
    expiry_date: str = "2027-06-01",
) -> Dict[str, Any]:
    """Build a certification document as returned from ES."""
    return {
        "cert_id": cert_id,
        "tenant_id": tenant_id,
        "asset_id": asset_id,
        "certification_type": certification_type,
        "certification_date": certification_date,
        "expiry_date": expiry_date,
        "inspector_name": "Inspector Jones",
        "certificate_number": "DOT-2024-001",
        "status": status,
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
    }


def _es_search_response(hits: list) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": h,
                    "sort": [h.get("expiry_date", ""), h.get("cert_id", "")],
                }
                for h in hits
            ],
            "total": {"value": len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceCreate:
    """Tests for AssetCertificationService.create."""

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_valid_certification(self, mock_utcnow):
        """A valid create call persists the certification and returns the doc."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_001",
            certification_type="V_test",
            certification_date=date(2024, 6, 1),
            expiry_date=date(2027, 6, 1),
            inspector_name="Inspector Jones",
            certificate_number="DOT-2024-001",
        )

        assert result["tenant_id"] == _TENANT_ID
        assert result["asset_id"] == "truck_001"
        assert result["certification_type"] == "V_test"
        assert result["inspector_name"] == "Inspector Jones"
        assert result["certificate_number"] == "DOT-2024-001"
        assert result["status"] == "valid"
        assert result["cert_id"].startswith("cert_")
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_empty_asset_id_raises(self):
        """Empty asset_id raises a validation error."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        with pytest.raises(ValueError, match="asset_id"):
            await service.create(
                _TENANT_ID,
                asset_id="",
                certification_type="V_test",
                certification_date=date(2024, 6, 1),
                expiry_date=date(2027, 6, 1),
                inspector_name="Inspector Jones",
                certificate_number="DOT-2024-001",
            )

    @pytest.mark.asyncio
    async def test_create_empty_inspector_name_raises(self):
        """Empty inspector_name raises a validation error."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        with pytest.raises(ValueError, match="inspector_name"):
            await service.create(
                _TENANT_ID,
                asset_id="truck_001",
                certification_type="K_test",
                certification_date=date(2024, 6, 1),
                expiry_date=date(2027, 6, 1),
                inspector_name="   ",
                certificate_number="DOT-2024-002",
            )

    @pytest.mark.asyncio
    async def test_create_invalid_certification_type_raises(self):
        """Invalid certification_type raises a validation error."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        with pytest.raises(ValueError):
            await service.create(
                _TENANT_ID,
                asset_id="truck_001",
                certification_type="invalid_type",
                certification_date=date(2024, 6, 1),
                expiry_date=date(2027, 6, 1),
                inspector_name="Inspector Jones",
                certificate_number="DOT-2024-003",
            )


# ---------------------------------------------------------------------------
# Tests: Get
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceGet:
    """Tests for AssetCertificationService.get."""

    @pytest.mark.asyncio
    async def test_get_existing_certification(self):
        """Retrieving an existing certification returns the document."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.get(_TENANT_ID, "cert_test123")

        assert result["cert_id"] == "cert_test123"
        assert result["asset_id"] == "truck_001"
        assert result["certification_type"] == "V_test"

    @pytest.mark.asyncio
    async def test_get_nonexistent_certification_raises_404(self):
        """Retrieving a non-existent certification raises resource_not_found."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_TENANT_ID, "cert_nonexistent")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: List
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceList:
    """Tests for AssetCertificationService.list."""

    @pytest.mark.asyncio
    async def test_list_returns_items(self):
        """List returns items from ES response."""
        es = _make_es_service()
        cert1 = _make_cert_doc(cert_id="cert_001")
        cert2 = _make_cert_doc(cert_id="cert_002", certification_type="K_test")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert1, cert2])
        )
        service = AssetCertificationService(es)

        result = await service.list(_TENANT_ID)

        assert len(result["items"]) == 2
        assert result["items"][0]["cert_id"] == "cert_001"
        assert result["items"][1]["cert_id"] == "cert_002"

    @pytest.mark.asyncio
    async def test_list_with_asset_id_filter(self):
        """List with asset_id filter includes the term in the query."""
        es = _make_es_service()
        cert_doc = _make_cert_doc(asset_id="trailer_005")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.list(_TENANT_ID, asset_id="trailer_005")

        assert len(result["items"]) == 1
        # Verify the query included the asset_id filter
        # inject_tenant_filter wraps the original query inside must[0]
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        # The original query is nested: query.bool.must[0].bool.must
        inner_query = query_body["query"]["bool"]["must"][0]
        must_clauses = inner_query["bool"]["must"]
        asset_filter = [c for c in must_clauses if "term" in c and "asset_id" in c["term"]]
        assert len(asset_filter) == 1

    @pytest.mark.asyncio
    async def test_list_with_status_filter(self):
        """List with status filter includes the term in the query."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )
        service = AssetCertificationService(es)

        result = await service.list(_TENANT_ID, status="expired")

        assert result["items"] == []
        # Verify the query included the status filter
        # inject_tenant_filter wraps the original query inside must[0]
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        inner_query = query_body["query"]["bool"]["must"][0]
        must_clauses = inner_query["bool"]["must"]
        status_filter = [c for c in must_clauses if "term" in c and "status" in c["term"]]
        assert len(status_filter) == 1

    @pytest.mark.asyncio
    async def test_list_clamps_limit(self):
        """List clamps limit to max 200."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )
        service = AssetCertificationService(es)

        result = await service.list(_TENANT_ID, limit=500)

        assert result["limit"] == 200

    @pytest.mark.asyncio
    async def test_list_pagination_returns_next_cursor(self):
        """When results fill the page, next_cursor is returned."""
        es = _make_es_service()
        # Simulate a full page (limit=2) to trigger next_cursor
        cert1 = _make_cert_doc(cert_id="cert_001")
        cert2 = _make_cert_doc(cert_id="cert_002")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert1, cert2])
        )
        service = AssetCertificationService(es)

        result = await service.list(_TENANT_ID, limit=2)

        assert result["next_cursor"] == "cert_002"


# ---------------------------------------------------------------------------
# Tests: Update
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceUpdate:
    """Tests for AssetCertificationService.update."""

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_update_expiry_date(self, mock_utcnow):
        """Updating expiry_date persists the change."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.update(
            _TENANT_ID,
            "cert_test123",
            expiry_date=date(2028, 6, 1),
        )

        assert result["expiry_date"] == "2028-06-01"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_update_status(self, mock_utcnow):
        """Updating status persists the change."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.update(
            _TENANT_ID,
            "cert_test123",
            status="expired",
        )

        assert result["status"] == "expired"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_invalid_status_raises(self):
        """Updating with an invalid status raises validation_error."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        with pytest.raises(AppException):
            await service.update(
                _TENANT_ID,
                "cert_test123",
                status="invalid_status",
            )

    @pytest.mark.asyncio
    async def test_update_empty_inspector_name_raises(self):
        """Updating with empty inspector_name raises validation_error."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        with pytest.raises(AppException):
            await service.update(
                _TENANT_ID,
                "cert_test123",
                inspector_name="   ",
            )

    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self):
        """Updating with no fields returns the existing document unchanged."""
        es = _make_es_service()
        cert_doc = _make_cert_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.update(_TENANT_ID, "cert_test123")

        assert result == cert_doc
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_nonexistent_cert_raises_404(self):
        """Updating a non-existent certification raises resource_not_found."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(
                _TENANT_ID,
                "cert_nonexistent",
                status="expired",
            )

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------


class TestAssetCertificationConstants:
    """Tests for module-level constants."""

    def test_alert_thresholds(self):
        """Alert thresholds match spec requirements."""
        assert ALERT_THRESHOLD_WARNING_DAYS == 60
        assert ALERT_THRESHOLD_URGENT_DAYS == 30
        assert ALERT_THRESHOLD_CRITICAL_DAYS == 7

    def test_dot_cargo_tank_cert_types(self):
        """DOT cargo tank cert types include all V/K/I/P/UT types."""
        expected = {"V_test", "K_test", "I_test", "P_test", "UT_test"}
        assert DOT_CARGO_TANK_CERT_TYPES == expected


# ---------------------------------------------------------------------------
# Tests: check_expiry_alerts (Task 8.3)
# Validates: Requirements 13.2, 13.3, 13.4
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceExpiryAlerts:
    """Tests for AssetCertificationService.check_expiry_alerts.

    Validates: Requirements 13.2, 13.3, 13.4
    """

    @pytest.mark.asyncio
    async def test_no_certifications_returns_empty_alerts(self):
        """When no certifications exist, no alerts are generated."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_warning_alert_at_45_days(self):
        """Cert expiring in 45 days generates a warning-level alert (Req 13.2)."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=45)

        cert_doc = _make_cert_doc(
            cert_id="cert_warn",
            asset_id="truck_001",
            certification_type="V_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].cert_id == "cert_warn"
        assert alerts[0].severity == "warning"
        assert alerts[0].days_until_expiry == 45

    @pytest.mark.asyncio
    async def test_urgent_alert_at_20_days(self):
        """Cert expiring in 20 days generates an urgent-level alert (Req 13.3)."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=20)

        cert_doc = _make_cert_doc(
            cert_id="cert_urgent",
            asset_id="truck_002",
            certification_type="K_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].cert_id == "cert_urgent"
        assert alerts[0].severity == "urgent"
        assert alerts[0].days_until_expiry == 20

    @pytest.mark.asyncio
    async def test_critical_alert_at_5_days(self):
        """Cert expiring in 5 days generates a critical-level alert (Req 13.4)."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=5)

        cert_doc = _make_cert_doc(
            cert_id="cert_critical",
            asset_id="truck_003",
            certification_type="I_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].cert_id == "cert_critical"
        assert alerts[0].severity == "critical"
        assert alerts[0].days_until_expiry == 5
        # Verify status was transitioned to expired
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][2]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_critical_alert_for_already_expired(self):
        """Cert already past expiry generates a critical alert and transitions to expired (Req 13.4)."""
        from datetime import timedelta

        today = date.today()
        expiry = today - timedelta(days=7)

        cert_doc = _make_cert_doc(
            cert_id="cert_past",
            asset_id="truck_004",
            certification_type="P_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].cert_id == "cert_past"
        assert alerts[0].severity == "critical"
        assert alerts[0].days_until_expiry == -7
        # Verify status was transitioned to expired
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_alert_beyond_60_days(self):
        """Cert expiring in more than 60 days generates no alert."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=61)

        cert_doc = _make_cert_doc(
            cert_id="cert_safe",
            asset_id="truck_005",
            certification_type="UT_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert alerts == []
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_certs_different_severities(self):
        """Multiple certs at different thresholds generate correct severity levels."""
        from datetime import timedelta

        today = date.today()

        cert_warning = _make_cert_doc(
            cert_id="cert_w",
            asset_id="truck_001",
            certification_type="V_test",
            status="valid",
            expiry_date=(today + timedelta(days=49)).isoformat(),
        )
        cert_urgent = _make_cert_doc(
            cert_id="cert_u",
            asset_id="truck_002",
            certification_type="K_test",
            status="valid",
            expiry_date=(today + timedelta(days=19)).isoformat(),
        )
        cert_critical = _make_cert_doc(
            cert_id="cert_c",
            asset_id="truck_003",
            certification_type="I_test",
            status="valid",
            expiry_date=(today + timedelta(days=4)).isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_warning, cert_urgent, cert_critical])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 3
        severities = {a.cert_id: a.severity for a in alerts}
        assert severities["cert_w"] == "warning"
        assert severities["cert_u"] == "urgent"
        assert severities["cert_c"] == "critical"

    @pytest.mark.asyncio
    async def test_boundary_31_days_is_warning(self):
        """Cert expiring in exactly 31 days is warning (not urgent)."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=31)

        cert_doc = _make_cert_doc(
            cert_id="cert_31",
            asset_id="truck_006",
            certification_type="V_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert alerts[0].days_until_expiry == 31

    @pytest.mark.asyncio
    async def test_boundary_8_days_is_urgent(self):
        """Cert expiring in exactly 8 days is urgent (not critical)."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=8)

        cert_doc = _make_cert_doc(
            cert_id="cert_8",
            asset_id="truck_007",
            certification_type="K_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        assert alerts[0].days_until_expiry == 8

    @pytest.mark.asyncio
    async def test_expiring_soon_status_included(self):
        """Certs with status 'expiring_soon' are also checked for alerts."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=14)

        cert_doc = _make_cert_doc(
            cert_id="cert_expiring",
            asset_id="truck_008",
            certification_type="P_test",
            status="expiring_soon",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        assert alerts[0].days_until_expiry == 14

    @pytest.mark.asyncio
    async def test_alert_contains_correct_fields(self):
        """Alert contains all expected fields with correct values."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=19)

        cert_doc = _make_cert_doc(
            cert_id="cert_fields",
            asset_id="trailer_001",
            certification_type="UT_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.cert_id == "cert_fields"
        assert alert.asset_id == "trailer_001"
        assert alert.tenant_id == _TENANT_ID
        assert alert.certification_type == "UT_test"
        assert alert.expiry_date == expiry
        assert alert.days_until_expiry == 19
        assert alert.severity == "urgent"
        assert alert.generated_at is not None

    @pytest.mark.asyncio
    async def test_boundary_exactly_60_days_is_warning(self):
        """Cert expiring in exactly 60 days is warning."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=60)

        cert_doc = _make_cert_doc(
            cert_id="cert_60",
            asset_id="truck_009",
            certification_type="V_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert alerts[0].days_until_expiry == 60
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_boundary_exactly_30_days_is_urgent(self):
        """Cert expiring in exactly 30 days is urgent and transitions to expiring_soon."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=30)

        cert_doc = _make_cert_doc(
            cert_id="cert_30",
            asset_id="truck_010",
            certification_type="K_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        assert alerts[0].days_until_expiry == 30
        # Task 8.4: valid → expiring_soon transition at ≤30 days
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][2]["status"] == "expiring_soon"

    @pytest.mark.asyncio
    async def test_boundary_exactly_7_days_is_critical(self):
        """Cert expiring in exactly 7 days is critical and transitions to expired."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=7)

        cert_doc = _make_cert_doc(
            cert_id="cert_7",
            asset_id="truck_011",
            certification_type="I_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert alerts[0].days_until_expiry == 7
        es.update_document.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Status transitions on expiry (Task 8.4)
# Validates: Requirement 13.4
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceStatusTransitions:
    """Tests for status transitions: valid → expiring_soon → expired.

    Validates: Requirement 13.4
    """

    @pytest.mark.asyncio
    async def test_valid_to_expiring_soon_at_25_days(self):
        """Cert at 25 days transitions from 'valid' to 'expiring_soon'."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=25)

        cert_doc = _make_cert_doc(
            cert_id="cert_25d",
            asset_id="truck_t1",
            certification_type="V_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        # Verify transition to expiring_soon was called
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][2]["status"] == "expiring_soon"

    @pytest.mark.asyncio
    async def test_valid_to_expired_at_5_days(self):
        """Cert at 5 days transitions from 'valid' to 'expired'."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=5)

        cert_doc = _make_cert_doc(
            cert_id="cert_5d_valid",
            asset_id="truck_t2",
            certification_type="K_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        # Verify transition to expired was called
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][2]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_expiring_soon_to_expired_at_5_days(self):
        """Cert at 5 days transitions from 'expiring_soon' to 'expired'."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=5)

        cert_doc = _make_cert_doc(
            cert_id="cert_5d_expiring",
            asset_id="truck_t3",
            certification_type="I_test",
            status="expiring_soon",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        # Verify transition to expired was called
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][2]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_already_expiring_soon_at_25_days_no_redundant_update(self):
        """Cert already 'expiring_soon' at 25 days does NOT trigger a redundant update."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=25)

        cert_doc = _make_cert_doc(
            cert_id="cert_already_es",
            asset_id="truck_t4",
            certification_type="P_test",
            status="expiring_soon",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        # Alert is still generated (for notification purposes)
        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        # But NO update_document call — already in target state
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_expired_no_update(self):
        """Cert already 'expired' does NOT appear in alerts (filtered by query).

        The _get_all_non_expired_certifications query only fetches
        status in ('valid', 'expiring_soon'), so expired certs are
        never processed.
        """
        es = _make_es_service()
        # Simulate that the query returns no results (expired certs are excluded)
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert alerts == []
        es.update_document.assert_not_called()



# ---------------------------------------------------------------------------
# Tests: is_dispatch_eligible (Task 8.5)
# Validates: Requirement 13.5
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceDispatchEligibility:
    """Tests for AssetCertificationService.is_dispatch_eligible.

    Validates: Requirement 13.5
    WHEN an asset has an expired DOT cargo tank certification (V/K/I/P/UT),
    THE Route_Planning_Agent SHALL exclude that asset from all fuel delivery routes.
    """

    @pytest.mark.asyncio
    async def test_all_valid_certs_eligible(self):
        """Asset with all valid DOT certs is eligible for dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_v",
                asset_id="truck_001",
                certification_type="V_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
            _make_cert_doc(
                cert_id="cert_k",
                asset_id="truck_001",
                certification_type="K_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
            _make_cert_doc(
                cert_id="cert_i",
                asset_id="truck_001",
                certification_type="I_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
            _make_cert_doc(
                cert_id="cert_p",
                asset_id="truck_001",
                certification_type="P_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
            _make_cert_doc(
                cert_id="cert_ut",
                asset_id="truck_001",
                certification_type="UT_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_001")

        assert result.eligible is True
        assert result.reasons == []
        assert result.asset_id == "truck_001"

    @pytest.mark.asyncio
    async def test_expired_v_test_ineligible(self):
        """Asset with expired V_test cert is NOT eligible for dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_v_expired",
                asset_id="truck_002",
                certification_type="V_test",
                status="expired",
                expiry_date="2025-01-01",
            ),
            _make_cert_doc(
                cert_id="cert_k_valid",
                asset_id="truck_002",
                certification_type="K_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_002")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "V_test" in result.reasons[0]
        assert "cert_v_expired" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_expired_meter_seal_still_eligible(self):
        """Asset with expired meter_seal but valid DOT certs is still eligible.

        Non-DOT certs (meter_seal, fire_extinguisher) do NOT block dispatch.
        """
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_meter",
                asset_id="truck_003",
                certification_type="meter_seal",
                status="expired",
                expiry_date="2025-01-01",
            ),
            _make_cert_doc(
                cert_id="cert_v_valid",
                asset_id="truck_003",
                certification_type="V_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_003")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_no_certifications_eligible(self):
        """Asset with no certifications at all is eligible (no blocking certs)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_004")

        assert result.eligible is True
        assert result.reasons == []
        assert result.asset_id == "truck_004"

    @pytest.mark.asyncio
    async def test_multiple_expired_dot_certs_all_reasons_listed(self):
        """Asset with multiple expired DOT certs lists all reasons."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_v_exp",
                asset_id="truck_005",
                certification_type="V_test",
                status="expired",
                expiry_date="2025-01-01",
            ),
            _make_cert_doc(
                cert_id="cert_k_exp",
                asset_id="truck_005",
                certification_type="K_test",
                status="expired",
                expiry_date="2025-02-01",
            ),
            _make_cert_doc(
                cert_id="cert_p_exp",
                asset_id="truck_005",
                certification_type="P_test",
                status="expired",
                expiry_date="2025-03-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_005")

        assert result.eligible is False
        assert len(result.reasons) == 3
        # Verify each expired cert type is mentioned
        reason_text = " ".join(result.reasons)
        assert "V_test" in reason_text
        assert "K_test" in reason_text
        assert "P_test" in reason_text

    @pytest.mark.asyncio
    async def test_past_expiry_date_but_status_not_updated_ineligible(self):
        """Asset with DOT cert whose expiry_date has passed (even if status not yet updated) is ineligible.

        This covers the case where the daily cron hasn't run yet to
        transition the status, but the date itself has passed.
        """
        from datetime import timedelta

        today = date.today()
        past_expiry = (today - timedelta(days=10)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_stale",
                asset_id="truck_006",
                certification_type="I_test",
                status="valid",  # status not yet transitioned
                expiry_date=past_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_006")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "I_test" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_fire_extinguisher_expired_still_eligible(self):
        """Asset with expired fire_extinguisher but valid DOT certs is still eligible.

        fire_extinguisher is a non-DOT cert and does NOT block dispatch.
        """
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_fire",
                asset_id="truck_007",
                certification_type="fire_extinguisher",
                status="expired",
                expiry_date="2025-01-01",
            ),
            _make_cert_doc(
                cert_id="cert_ut_valid",
                asset_id="truck_007",
                certification_type="UT_test",
                status="valid",
                expiry_date="2027-06-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_007")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_expiring_soon_status_still_eligible(self):
        """Asset with DOT cert in 'expiring_soon' status (not yet expired) is still eligible."""
        from datetime import timedelta

        today = date.today()
        future_expiry = (today + timedelta(days=20)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_soon",
                asset_id="truck_008",
                certification_type="V_test",
                status="expiring_soon",
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_008")

        assert result.eligible is True
        assert result.reasons == []


# ---------------------------------------------------------------------------
# Tests: 3-year retest requirement (Task 8.6)
# Validates: Requirement 13.6
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceRetestRequirement:
    """Tests for 3-year retest requirement in is_dispatch_eligible.

    Validates: Requirement 13.6
    WHEN a 3-year retest date is reached for a cargo tank, THE
    Asset_Certification_Service SHALL require a new certification record
    before the asset can be dispatched.
    """

    @pytest.mark.asyncio
    async def test_cert_within_3_years_eligible(self):
        """Cert dated 2 years ago is within the 3-year window → eligible."""
        from datetime import timedelta

        today = date.today()
        two_years_ago = (today - timedelta(days=730)).isoformat()
        future_expiry = (today + timedelta(days=365)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_recent",
                asset_id="truck_retest_01",
                certification_type="V_test",
                status="valid",
                certification_date=two_years_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_01")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_cert_older_than_3_years_ineligible(self):
        """Cert dated 4 years ago exceeds the 3-year retest window → ineligible."""
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        future_expiry = (today + timedelta(days=100)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_old",
                asset_id="truck_retest_02",
                certification_type="K_test",
                status="valid",
                certification_date=four_years_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_02")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "K_test" in result.reasons[0]
        assert "3-year retest" in result.reasons[0]
        assert "last certified" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_old_cert_but_newer_cert_same_type_eligible(self):
        """Cert dated 4 years ago but a newer cert of same type exists → eligible."""
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        one_year_ago = (today - timedelta(days=365)).isoformat()
        future_expiry = (today + timedelta(days=730)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_old_v",
                asset_id="truck_retest_03",
                certification_type="V_test",
                status="valid",
                certification_date=four_years_ago,
                expiry_date=future_expiry,
            ),
            _make_cert_doc(
                cert_id="cert_new_v",
                asset_id="truck_retest_03",
                certification_type="V_test",
                status="valid",
                certification_date=one_year_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_03")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_multiple_cert_types_one_overdue_ineligible(self):
        """Multiple cert types, one with overdue retest → ineligible with specific reason."""
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        one_year_ago = (today - timedelta(days=365)).isoformat()
        future_expiry = (today + timedelta(days=730)).isoformat()

        es = _make_es_service()
        certs = [
            # V_test: recent, within 3 years → OK
            _make_cert_doc(
                cert_id="cert_v_ok",
                asset_id="truck_retest_04",
                certification_type="V_test",
                status="valid",
                certification_date=one_year_ago,
                expiry_date=future_expiry,
            ),
            # I_test: 4 years old, no newer cert → overdue
            _make_cert_doc(
                cert_id="cert_i_old",
                asset_id="truck_retest_04",
                certification_type="I_test",
                status="valid",
                certification_date=four_years_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_04")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "I_test" in result.reasons[0]
        assert "3-year retest" in result.reasons[0]
        # V_test should NOT appear in reasons
        assert "V_test" not in result.reasons[0]

    @pytest.mark.asyncio
    async def test_retest_constant_is_1095_days(self):
        """RETEST_INTERVAL_DAYS constant is 1095 (3 years)."""
        assert RETEST_INTERVAL_DAYS == 1095

    @pytest.mark.asyncio
    async def test_non_dot_cert_old_does_not_block(self):
        """Non-DOT cert (meter_seal) older than 3 years does NOT block dispatch."""
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        future_expiry = (today + timedelta(days=100)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_meter_old",
                asset_id="truck_retest_05",
                certification_type="meter_seal",
                status="valid",
                certification_date=four_years_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_05")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_boundary_exactly_1095_days_eligible(self):
        """Cert dated exactly 1095 days ago is at the boundary → still eligible (not overdue)."""
        from datetime import timedelta

        today = date.today()
        exactly_3_years = (today - timedelta(days=RETEST_INTERVAL_DAYS)).isoformat()
        future_expiry = (today + timedelta(days=100)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_boundary",
                asset_id="truck_retest_06",
                certification_type="P_test",
                status="valid",
                certification_date=exactly_3_years,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_06")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_boundary_1096_days_ineligible(self):
        """Cert dated 1096 days ago exceeds the 3-year window → ineligible."""
        from datetime import timedelta

        today = date.today()
        just_over_3_years = (today - timedelta(days=RETEST_INTERVAL_DAYS + 1)).isoformat()
        future_expiry = (today + timedelta(days=100)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_over_boundary",
                asset_id="truck_retest_07",
                certification_type="UT_test",
                status="valid",
                certification_date=just_over_3_years,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_retest_07")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "UT_test" in result.reasons[0]
        assert "3-year retest" in result.reasons[0]



# ---------------------------------------------------------------------------
# Tests: get_fleet_dashboard (Task 8.7)
# Validates: Requirement 13.7
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceFleetDashboard:
    """Tests for AssetCertificationService.get_fleet_dashboard.

    Validates: Requirement 13.7
    THE Asset_Certification_Service SHALL provide a fleet certification
    dashboard showing all assets with their certification statuses,
    upcoming expirations, and overdue inspections sorted by urgency.
    """

    @pytest.mark.asyncio
    async def test_empty_tenant_returns_empty_list(self):
        """Tenant with no certifications returns an empty dashboard."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_certs_sorted_by_urgency(self):
        """Multiple certs are sorted by urgency: expired first, then closest to expiry."""
        from datetime import timedelta

        today = date.today()

        # Cert 1: expires in 90 days (far future)
        cert_far = _make_cert_doc(
            cert_id="cert_far",
            asset_id="truck_001",
            certification_type="V_test",
            status="valid",
            certification_date="2024-06-01",
            expiry_date=(today + timedelta(days=90)).isoformat(),
        )
        # Cert 2: expired 10 days ago (most urgent)
        cert_expired = _make_cert_doc(
            cert_id="cert_expired",
            asset_id="truck_002",
            certification_type="K_test",
            status="expired",
            certification_date="2023-01-01",
            expiry_date=(today - timedelta(days=10)).isoformat(),
        )
        # Cert 3: expires in 15 days (urgent)
        cert_soon = _make_cert_doc(
            cert_id="cert_soon",
            asset_id="truck_003",
            certification_type="I_test",
            status="expiring_soon",
            certification_date="2024-01-01",
            expiry_date=(today + timedelta(days=15)).isoformat(),
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_far, cert_expired, cert_soon])
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert len(result) == 3
        # Sorted by days_until_expiry ascending: expired (-10), soon (15), far (90)
        assert result[0].cert_id == "cert_expired"
        assert result[0].days_until_expiry == -10
        assert result[1].cert_id == "cert_soon"
        assert result[1].days_until_expiry == 15
        assert result[2].cert_id == "cert_far"
        assert result[2].days_until_expiry == 90

    @pytest.mark.asyncio
    async def test_mix_of_statuses_all_included(self):
        """All statuses (valid, expiring_soon, expired) are included in the dashboard."""
        from datetime import timedelta

        today = date.today()

        cert_valid = _make_cert_doc(
            cert_id="cert_valid",
            asset_id="truck_001",
            certification_type="V_test",
            status="valid",
            certification_date="2024-06-01",
            expiry_date=(today + timedelta(days=200)).isoformat(),
        )
        cert_expiring = _make_cert_doc(
            cert_id="cert_expiring",
            asset_id="truck_002",
            certification_type="K_test",
            status="expiring_soon",
            certification_date="2024-01-01",
            expiry_date=(today + timedelta(days=25)).isoformat(),
        )
        cert_expired = _make_cert_doc(
            cert_id="cert_expired",
            asset_id="truck_003",
            certification_type="P_test",
            status="expired",
            certification_date="2023-01-01",
            expiry_date=(today - timedelta(days=5)).isoformat(),
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_valid, cert_expiring, cert_expired])
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert len(result) == 3
        statuses = {s.cert_id: s.status for s in result}
        assert statuses["cert_valid"] == "valid"
        assert statuses["cert_expiring"] == "expiring_soon"
        assert statuses["cert_expired"] == "expired"

    @pytest.mark.asyncio
    async def test_days_until_expiry_computed_correctly(self):
        """days_until_expiry is computed as (expiry_date - today).days."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=42)

        cert_doc = _make_cert_doc(
            cert_id="cert_42",
            asset_id="truck_001",
            certification_type="UT_test",
            status="valid",
            certification_date="2024-06-01",
            expiry_date=expiry.isoformat(),
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert len(result) == 1
        assert result[0].days_until_expiry == 42
        assert result[0].expiry_date == expiry
        assert result[0].cert_id == "cert_42"
        assert result[0].asset_id == "truck_001"
        assert result[0].certification_type == "UT_test"
        assert result[0].inspector_name == "Inspector Jones"
        assert result[0].certificate_number == "DOT-2024-001"

    @pytest.mark.asyncio
    async def test_negative_days_for_expired_certs(self):
        """Expired certs have negative days_until_expiry."""
        from datetime import timedelta

        today = date.today()
        past_expiry = today - timedelta(days=30)

        cert_doc = _make_cert_doc(
            cert_id="cert_past",
            asset_id="truck_001",
            certification_type="V_test",
            status="expired",
            certification_date="2022-01-01",
            expiry_date=past_expiry.isoformat(),
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert len(result) == 1
        assert result[0].days_until_expiry == -30

    @pytest.mark.asyncio
    async def test_dashboard_returns_certification_summary_instances(self):
        """Dashboard returns CertificationSummary model instances."""
        from datetime import timedelta

        today = date.today()

        cert_doc = _make_cert_doc(
            cert_id="cert_model",
            asset_id="trailer_001",
            certification_type="meter_seal",
            status="valid",
            certification_date="2025-01-15",
            expiry_date=(today + timedelta(days=100)).isoformat(),
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        result = await service.get_fleet_dashboard(_TENANT_ID)

        assert len(result) == 1
        assert isinstance(result[0], CertificationSummary)
        assert result[0].certification_date == date(2025, 1, 15)


# ---------------------------------------------------------------------------
# Tests: Clear dispatch restrictions (Task 8.8)
# Validates: Requirement 13.8
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceClearDispatchRestrictions:
    """Tests for dispatch restriction clearing when new valid cert is recorded.

    Validates: Requirement 13.8
    """

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_valid_cert_clears_expired_same_type(self, mock_utcnow):
        """Creating a new valid cert clears dispatch restrictions from expired certs of same type.

        When a new valid certification is recorded for an asset, any previously
        expired certifications of the same type should be marked as superseded,
        effectively clearing dispatch restrictions.
        """
        # Existing expired cert of same type for same asset
        expired_cert = _make_cert_doc(
            cert_id="cert_old_expired",
            asset_id="truck_001",
            certification_type="V_test",
            status="expired",
            expiry_date="2025-01-01",
        )

        es = _make_es_service()

        # First call: index_document for the new cert (no search needed)
        # Second call: search_documents for clear_dispatch_restrictions
        es.search_documents = AsyncMock(
            return_value=_es_search_response([expired_cert])
        )

        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_001",
            certification_type="V_test",
            certification_date=date(2026, 6, 1),
            expiry_date=date(2029, 6, 1),
            inspector_name="Inspector Smith",
            certificate_number="DOT-2026-001",
            status="valid",
        )

        # Verify the new cert was created
        assert result["status"] == "valid"
        assert result["certification_type"] == "V_test"
        es.index_document.assert_called_once()

        # Verify the expired cert was updated to superseded
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        assert update_args[0][1] == "cert_old_expired"
        assert update_args[0][2]["status"] == "superseded"

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_valid_cert_does_not_affect_different_type(self, mock_utcnow):
        """Creating a new valid cert of one type doesn't clear restrictions for other types.

        Only expired certifications of the SAME type should be cleared.
        """
        es = _make_es_service()

        # No expired certs of the same type found
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )

        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_001",
            certification_type="K_test",
            certification_date=date(2026, 6, 1),
            expiry_date=date(2029, 6, 1),
            inspector_name="Inspector Smith",
            certificate_number="DOT-2026-002",
            status="valid",
        )

        # Verify the new cert was created
        assert result["status"] == "valid"
        assert result["certification_type"] == "K_test"
        es.index_document.assert_called_once()

        # Verify no expired certs were updated (none found for this type)
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_non_valid_status_does_not_clear_restrictions(self, mock_utcnow):
        """Creating a cert with non-valid status does NOT trigger clearing logic.

        Only certs with status "valid" should clear dispatch restrictions.
        """
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_001",
            certification_type="V_test",
            certification_date=date(2026, 6, 1),
            expiry_date=date(2029, 6, 1),
            inspector_name="Inspector Smith",
            certificate_number="DOT-2026-003",
            status="expiring_soon",
        )

        # Verify the cert was created
        assert result["status"] == "expiring_soon"
        es.index_document.assert_called_once()

        # search_documents should NOT be called for clearing (only valid triggers it)
        es.search_documents.assert_not_called()
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_clear_dispatch_restrictions_directly(self, mock_utcnow):
        """Calling clear_dispatch_restrictions directly marks expired certs as superseded."""
        expired_cert_1 = _make_cert_doc(
            cert_id="cert_exp_1",
            asset_id="truck_002",
            certification_type="I_test",
            status="expired",
            expiry_date="2025-03-01",
        )
        expired_cert_2 = _make_cert_doc(
            cert_id="cert_exp_2",
            asset_id="truck_002",
            certification_type="I_test",
            status="expired",
            expiry_date="2024-06-01",
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([expired_cert_1, expired_cert_2])
        )
        service = AssetCertificationService(es)

        cleared = await service.clear_dispatch_restrictions(
            _TENANT_ID, "truck_002", "I_test"
        )

        assert cleared == 2
        assert es.update_document.call_count == 2

        # Verify both were marked superseded
        for call in es.update_document.call_args_list:
            assert call[0][2]["status"] == "superseded"

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_clear_dispatch_restrictions_no_expired_certs(self, mock_utcnow):
        """When no expired certs exist, clear_dispatch_restrictions returns 0."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )
        service = AssetCertificationService(es)

        cleared = await service.clear_dispatch_restrictions(
            _TENANT_ID, "truck_003", "P_test"
        )

        assert cleared == 0
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_superseded_cert_does_not_block_dispatch(self, mock_utcnow):
        """A superseded cert should NOT block dispatch eligibility.

        After clearing restrictions, the old expired cert (now superseded)
        should not cause is_dispatch_eligible to return ineligible.
        """
        # One superseded cert (old expired, now cleared) and one valid cert
        superseded_cert = _make_cert_doc(
            cert_id="cert_superseded",
            asset_id="truck_001",
            certification_type="V_test",
            status="superseded",
            expiry_date="2025-01-01",
        )
        valid_cert = _make_cert_doc(
            cert_id="cert_new_valid",
            asset_id="truck_001",
            certification_type="V_test",
            status="valid",
            certification_date="2026-06-01",
            expiry_date="2029-06-01",
        )

        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([superseded_cert, valid_cert])
        )
        service = AssetCertificationService(es)

        eligibility = await service.is_dispatch_eligible(_TENANT_ID, "truck_001")

        assert eligibility.eligible is True
        assert eligibility.reasons == []


# ---------------------------------------------------------------------------
# Tests: Each cert type can be created (Task 8.12)
# Validates: Requirement 13.1
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceCreateEachType:
    """Tests that each certification type can be created successfully.

    Validates: Requirement 13.1
    Covers: V_test, K_test, I_test, P_test, UT_test, meter_seal, fire_extinguisher
    """

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_i_test_certification(self, mock_utcnow):
        """I_test certification can be created successfully."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_010",
            certification_type="I_test",
            certification_date=date(2024, 3, 15),
            expiry_date=date(2027, 3, 15),
            inspector_name="Inspector Adams",
            certificate_number="DOT-2024-I-001",
        )

        assert result["certification_type"] == "I_test"
        assert result["asset_id"] == "truck_010"
        assert result["status"] == "valid"
        assert result["cert_id"].startswith("cert_")
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_p_test_certification(self, mock_utcnow):
        """P_test certification can be created successfully."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_011",
            certification_type="P_test",
            certification_date=date(2024, 4, 20),
            expiry_date=date(2027, 4, 20),
            inspector_name="Inspector Baker",
            certificate_number="DOT-2024-P-001",
        )

        assert result["certification_type"] == "P_test"
        assert result["asset_id"] == "truck_011"
        assert result["status"] == "valid"
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_ut_test_certification(self, mock_utcnow):
        """UT_test certification can be created successfully."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_012",
            certification_type="UT_test",
            certification_date=date(2024, 5, 10),
            expiry_date=date(2027, 5, 10),
            inspector_name="Inspector Clark",
            certificate_number="DOT-2024-UT-001",
        )

        assert result["certification_type"] == "UT_test"
        assert result["asset_id"] == "truck_012"
        assert result["status"] == "valid"
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_meter_seal_certification(self, mock_utcnow):
        """meter_seal certification can be created successfully."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_013",
            certification_type="meter_seal",
            certification_date=date(2024, 7, 1),
            expiry_date=date(2025, 7, 1),
            inspector_name="Inspector Davis",
            certificate_number="MS-2024-001",
        )

        assert result["certification_type"] == "meter_seal"
        assert result["asset_id"] == "truck_013"
        assert result["status"] == "valid"
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "compliance.services.asset_certification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_fire_extinguisher_certification(self, mock_utcnow):
        """fire_extinguisher certification can be created successfully."""
        es = _make_es_service()
        service = AssetCertificationService(es)

        result = await service.create(
            _TENANT_ID,
            asset_id="truck_014",
            certification_type="fire_extinguisher",
            certification_date=date(2024, 8, 15),
            expiry_date=date(2025, 8, 15),
            inspector_name="Inspector Evans",
            certificate_number="FE-2024-001",
        )

        assert result["certification_type"] == "fire_extinguisher"
        assert result["asset_id"] == "truck_014"
        assert result["status"] == "valid"
        es.index_document.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Each DOT cert type individually blocks dispatch (Task 8.12)
# Validates: Requirement 13.5
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceEachDOTTypeBlocksDispatch:
    """Tests that each individual DOT cert type blocks dispatch when expired.

    Validates: Requirement 13.5
    """

    @pytest.mark.asyncio
    async def test_expired_k_test_blocks_dispatch(self):
        """Expired K_test individually blocks dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_k_exp",
                asset_id="truck_dot_01",
                certification_type="K_test",
                status="expired",
                expiry_date="2025-01-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_dot_01")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "K_test" in result.reasons[0]
        assert "expired" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_expired_i_test_blocks_dispatch(self):
        """Expired I_test individually blocks dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_i_exp",
                asset_id="truck_dot_02",
                certification_type="I_test",
                status="expired",
                expiry_date="2025-02-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_dot_02")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "I_test" in result.reasons[0]
        assert "expired" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_expired_p_test_blocks_dispatch(self):
        """Expired P_test individually blocks dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_p_exp",
                asset_id="truck_dot_03",
                certification_type="P_test",
                status="expired",
                expiry_date="2025-03-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_dot_03")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "P_test" in result.reasons[0]
        assert "expired" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_expired_ut_test_blocks_dispatch(self):
        """Expired UT_test individually blocks dispatch."""
        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_ut_exp",
                asset_id="truck_dot_04",
                certification_type="UT_test",
                status="expired",
                expiry_date="2025-04-01",
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_dot_04")

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "UT_test" in result.reasons[0]
        assert "expired" in result.reasons[0]


# ---------------------------------------------------------------------------
# Tests: Each DOT cert type generates alerts at correct thresholds (Task 8.12)
# Validates: Requirements 13.2, 13.3, 13.4
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceEachTypeAlerts:
    """Tests that each cert type generates alerts at correct thresholds.

    Validates: Requirements 13.2, 13.3, 13.4
    """

    @pytest.mark.asyncio
    async def test_p_test_warning_alert(self):
        """P_test cert generates warning alert at 50 days."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=50)

        cert_doc = _make_cert_doc(
            cert_id="cert_p_warn",
            asset_id="truck_alert_01",
            certification_type="P_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].certification_type == "P_test"
        assert alerts[0].severity == "warning"

    @pytest.mark.asyncio
    async def test_ut_test_urgent_alert(self):
        """UT_test cert generates urgent alert at 25 days."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=25)

        cert_doc = _make_cert_doc(
            cert_id="cert_ut_urgent",
            asset_id="truck_alert_02",
            certification_type="UT_test",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].certification_type == "UT_test"
        assert alerts[0].severity == "urgent"

    @pytest.mark.asyncio
    async def test_meter_seal_critical_alert(self):
        """meter_seal cert generates critical alert at 3 days."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=3)

        cert_doc = _make_cert_doc(
            cert_id="cert_ms_critical",
            asset_id="truck_alert_03",
            certification_type="meter_seal",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].certification_type == "meter_seal"
        assert alerts[0].severity == "critical"

    @pytest.mark.asyncio
    async def test_fire_extinguisher_warning_alert(self):
        """fire_extinguisher cert generates warning alert at 40 days."""
        from datetime import timedelta

        today = date.today()
        expiry = today + timedelta(days=40)

        cert_doc = _make_cert_doc(
            cert_id="cert_fe_warn",
            asset_id="truck_alert_04",
            certification_type="fire_extinguisher",
            status="valid",
            expiry_date=expiry.isoformat(),
        )
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([cert_doc])
        )
        service = AssetCertificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].certification_type == "fire_extinguisher"
        assert alerts[0].severity == "warning"


# ---------------------------------------------------------------------------
# Tests: 3-year retest — newer cert of same type clears restriction (Task 8.12)
# Validates: Requirement 13.6, 13.8
# ---------------------------------------------------------------------------


class TestAssetCertificationServiceRetestClearance:
    """Tests that a newer cert of the same type clears the 3-year retest restriction.

    Validates: Requirements 13.6, 13.8
    """

    @pytest.mark.asyncio
    async def test_newer_cert_same_type_clears_retest_restriction(self):
        """A newer valid cert of the same type clears the 3-year retest block.

        Even if an old cert is >3 years old, a newer cert within 3 years
        means the most recent certification_date is used for the check.
        """
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        six_months_ago = (today - timedelta(days=180)).isoformat()
        future_expiry = (today + timedelta(days=730)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_old_p",
                asset_id="truck_clear_01",
                certification_type="P_test",
                status="valid",
                certification_date=four_years_ago,
                expiry_date=future_expiry,
            ),
            _make_cert_doc(
                cert_id="cert_new_p",
                asset_id="truck_clear_01",
                certification_type="P_test",
                status="valid",
                certification_date=six_months_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_clear_01")

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_superseded_old_cert_does_not_trigger_retest(self):
        """A superseded cert (cleared by new valid cert) does not trigger retest check.

        Superseded certs are skipped in the 3-year retest evaluation.
        """
        from datetime import timedelta

        today = date.today()
        four_years_ago = (today - timedelta(days=1460)).isoformat()
        six_months_ago = (today - timedelta(days=180)).isoformat()
        future_expiry = (today + timedelta(days=730)).isoformat()

        es = _make_es_service()
        certs = [
            _make_cert_doc(
                cert_id="cert_superseded_ut",
                asset_id="truck_clear_02",
                certification_type="UT_test",
                status="superseded",
                certification_date=four_years_ago,
                expiry_date="2025-01-01",
            ),
            _make_cert_doc(
                cert_id="cert_new_ut",
                asset_id="truck_clear_02",
                certification_type="UT_test",
                status="valid",
                certification_date=six_months_ago,
                expiry_date=future_expiry,
            ),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(certs)
        )
        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(_TENANT_ID, "truck_clear_02")

        assert result.eligible is True
        assert result.reasons == []
