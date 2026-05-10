"""Unit tests for DriverQualificationService.

Tests cover:
- Create: valid creation, validation errors (empty full_name, invalid cdl_state)
- Get: found, not found (404)
- List: pagination, status filtering
- Update: partial updates, validation on update
- is_dispatch_eligible: active vs suspended driver (skeleton)

Validates: Requirement 5.1
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from compliance.services.driver_qualification_service import (
    DriverQualificationService,
    DriverEligibility,
    DQFDashboard,
    Alert,
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


def _make_driver_doc(
    *,
    driver_id: str = "driver_test123",
    tenant_id: str = _TENANT_ID,
    full_name: str = "John Smith",
    status: str = "active",
) -> Dict[str, Any]:
    """Build a driver document as returned from ES."""
    return {
        "driver_id": driver_id,
        "tenant_id": tenant_id,
        "full_name": full_name,
        "cdl_number": "CDL123456",
        "cdl_state": "TX",
        "cdl_class": "A",
        "cdl_expiry_date": "2027-06-01",
        "medical_card_expiry_date": "2027-03-15",
        "hazmat_endorsement_expiry_date": "2027-01-01",
        "tanker_endorsement_expiry_date": "2027-02-01",
        "last_drug_test_date": "2026-01-15",
        "last_mvr_date": "2026-02-01",
        "status": status,
        "suspension_reason": None,
        "external_refs": None,
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
                    "sort": [h.get("created_at", ""), h.get("driver_id", "")],
                }
                for h in hits
            ],
            "total": {"value": len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceCreate:
    """Tests for DriverQualificationService.create."""

    @pytest.mark.asyncio
    @patch(
        "compliance.services.driver_qualification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_create_valid_driver(self, mock_utcnow):
        """A valid create call persists the driver and returns the doc."""
        es = _make_es_service()
        service = DriverQualificationService(es)

        result = await service.create(
            _TENANT_ID,
            full_name="John Smith",
            cdl_number="CDL123456",
            cdl_state="TX",
            cdl_class="A",
            cdl_expiry_date=date(2027, 6, 1),
            medical_card_expiry_date=date(2027, 3, 15),
            hazmat_endorsement_expiry_date=date(2027, 1, 1),
            tanker_endorsement_expiry_date=date(2027, 2, 1),
            last_drug_test_date=date(2026, 1, 15),
        )

        assert result["tenant_id"] == _TENANT_ID
        assert result["full_name"] == "John Smith"
        assert result["cdl_number"] == "CDL123456"
        assert result["cdl_state"] == "TX"
        assert result["cdl_class"] == "A"
        assert result["status"] == "active"
        assert result["driver_id"].startswith("driver_")
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_empty_full_name_raises(self):
        """Empty full_name raises a validation error."""
        es = _make_es_service()
        service = DriverQualificationService(es)

        with pytest.raises(ValueError, match="full_name"):
            await service.create(
                _TENANT_ID,
                full_name="",
                cdl_number="CDL123456",
                cdl_state="TX",
                cdl_class="A",
                cdl_expiry_date=date(2027, 6, 1),
                medical_card_expiry_date=date(2027, 3, 15),
            )

    @pytest.mark.asyncio
    async def test_create_invalid_cdl_state_raises(self):
        """Invalid cdl_state raises a validation error."""
        es = _make_es_service()
        service = DriverQualificationService(es)

        with pytest.raises(ValueError, match="cdl_state"):
            await service.create(
                _TENANT_ID,
                full_name="John Smith",
                cdl_number="CDL123456",
                cdl_state="Texas",  # Should be 2-letter code
                cdl_class="A",
                cdl_expiry_date=date(2027, 6, 1),
                medical_card_expiry_date=date(2027, 3, 15),
            )


# ---------------------------------------------------------------------------
# Tests: Get
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceGet:
    """Tests for DriverQualificationService.get."""

    @pytest.mark.asyncio
    async def test_get_existing_driver(self):
        """Retrieving an existing driver returns the document."""
        es = _make_es_service()
        driver_doc = _make_driver_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.get(_TENANT_ID, "driver_test123")

        assert result["driver_id"] == "driver_test123"
        assert result["full_name"] == "John Smith"

    @pytest.mark.asyncio
    async def test_get_nonexistent_driver_raises_404(self):
        """Retrieving a non-existent driver raises resource_not_found."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = DriverQualificationService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_TENANT_ID, "driver_nonexistent")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: List
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceList:
    """Tests for DriverQualificationService.list."""

    @pytest.mark.asyncio
    async def test_list_returns_items(self):
        """List returns driver items for the tenant."""
        es = _make_es_service()
        drivers = [
            _make_driver_doc(driver_id="driver_1"),
            _make_driver_doc(driver_id="driver_2"),
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(drivers)
        )
        service = DriverQualificationService(es)

        result = await service.list(_TENANT_ID)

        assert len(result["items"]) == 2
        assert result["items"][0]["driver_id"] == "driver_1"

    @pytest.mark.asyncio
    async def test_list_clamps_limit(self):
        """List clamps limit to max 200."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )
        service = DriverQualificationService(es)

        result = await service.list(_TENANT_ID, limit=500)

        assert result["limit"] == 200


# ---------------------------------------------------------------------------
# Tests: Update
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceUpdate:
    """Tests for DriverQualificationService.update."""

    @pytest.mark.asyncio
    @patch(
        "compliance.services.driver_qualification_service.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_update_partial_fields(self, mock_utcnow):
        """Partial update applies only provided fields."""
        es = _make_es_service()
        driver_doc = _make_driver_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.update(
            _TENANT_ID,
            "driver_test123",
            full_name="Jane Doe",
            cdl_class="B",
        )

        assert result["full_name"] == "Jane Doe"
        assert result["cdl_class"] == "B"
        # Unchanged fields remain
        assert result["cdl_state"] == "TX"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_invalid_status_raises(self):
        """Invalid status value raises validation error."""
        es = _make_es_service()
        driver_doc = _make_driver_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(
                _TENANT_ID,
                "driver_test123",
                status="invalid_status",
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self):
        """Update with no changes returns existing doc without ES call."""
        es = _make_es_service()
        driver_doc = _make_driver_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.update(_TENANT_ID, "driver_test123")

        assert result == driver_doc
        es.update_document.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Dispatch Eligibility (Task 6.3)
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceEligibility:
    """Tests for DriverQualificationService.is_dispatch_eligible.

    Validates: Requirements 5.5, 5.6, 5.7
    """

    @pytest.mark.asyncio
    async def test_active_driver_all_valid_is_eligible(self):
        """An active driver with all valid qualifications is eligible."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_suspended_driver_is_not_eligible(self):
        """A suspended driver is excluded from all route assignments (Req 5.5)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="suspended")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is False
        assert any("suspended" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_expired_driver_is_not_eligible(self):
        """An expired driver is excluded from all route assignments (Req 5.5)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="expired")
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is False
        assert any("expired" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_expired_cdl_makes_driver_ineligible(self):
        """A driver with an expired CDL is ineligible."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["cdl_expiry_date"] = "2020-01-01"  # expired
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is False
        assert any("CDL expired" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_expired_medical_card_makes_driver_ineligible(self):
        """A driver with an expired medical card is ineligible."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["medical_card_expiry_date"] = "2020-01-01"  # expired
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is False
        assert any("Medical card expired" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_hazmat_required_but_driver_has_no_endorsement(self):
        """Driver without HAZMAT endorsement is excluded from HAZMAT routes (Req 5.6)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["hazmat_endorsement_expiry_date"] = None  # no endorsement
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_hazmat": True},
        )

        assert result.eligible is False
        assert any("HAZMAT endorsement" in r and "none" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_hazmat_required_but_endorsement_expired(self):
        """Driver with expired HAZMAT endorsement is excluded from HAZMAT routes (Req 5.6)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["hazmat_endorsement_expiry_date"] = "2020-01-01"  # expired
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_hazmat": True},
        )

        assert result.eligible is False
        assert any("HAZMAT endorsement expired" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_hazmat_not_required_no_endorsement_still_eligible(self):
        """Driver without HAZMAT endorsement is eligible if route doesn't require it."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["hazmat_endorsement_expiry_date"] = None
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_hazmat": False},
        )

        assert result.eligible is True

    @pytest.mark.asyncio
    async def test_tanker_required_but_driver_has_no_endorsement(self):
        """Driver without tanker endorsement is excluded from tanker routes (Req 5.7)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["tanker_endorsement_expiry_date"] = None  # no endorsement
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_tanker": True},
        )

        assert result.eligible is False
        assert any("tanker endorsement" in r and "none" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_tanker_required_but_endorsement_expired(self):
        """Driver with expired tanker endorsement is excluded from tanker routes (Req 5.7)."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["tanker_endorsement_expiry_date"] = "2020-01-01"  # expired
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_tanker": True},
        )

        assert result.eligible is False
        assert any("Tanker endorsement expired" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_tanker_not_required_no_endorsement_still_eligible(self):
        """Driver without tanker endorsement is eligible if route doesn't require it."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["tanker_endorsement_expiry_date"] = None
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"requires_tanker": False},
        )

        assert result.eligible is True

    @pytest.mark.asyncio
    async def test_cdl_class_insufficient_for_route(self):
        """Driver with CDL class B is ineligible for a route requiring class A."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["cdl_class"] = "B"
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={"min_cdl_class": "A"},
        )

        assert result.eligible is False
        assert any("CDL class" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_cdl_class_a_meets_all_requirements(self):
        """Driver with CDL class A meets any CDL class requirement."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        driver_doc["cdl_class"] = "A"
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        for min_class in ("A", "B", "C"):
            result = await service.is_dispatch_eligible(
                _TENANT_ID,
                "driver_test123",
                route_requirements={"min_cdl_class": min_class},
            )
            assert result.eligible is True, f"Class A should meet min {min_class}"

    @pytest.mark.asyncio
    async def test_multiple_failures_accumulate_reasons(self):
        """Multiple eligibility failures are all reported in reasons list."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="suspended")
        driver_doc["cdl_expiry_date"] = "2020-01-01"
        driver_doc["medical_card_expiry_date"] = "2020-01-01"
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = "2020-01-01"
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID,
            "driver_test123",
            route_requirements={
                "requires_hazmat": True,
                "requires_tanker": True,
            },
        )

        assert result.eligible is False
        # Should have at least 5 reasons: status, CDL, medical, hazmat, tanker
        assert len(result.reasons) >= 5

    @pytest.mark.asyncio
    async def test_no_route_requirements_only_checks_base(self):
        """With no route_requirements, only status/CDL/medical are checked."""
        es = _make_es_service()
        driver_doc = _make_driver_doc(status="active")
        # No hazmat or tanker endorsement, but route doesn't require them
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            _TENANT_ID, "driver_test123"
        )

        assert result.eligible is True
        assert result.reasons == []


# ---------------------------------------------------------------------------
# Tests: check_expiry_alerts (Task 6.4)
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceExpiryAlerts:
    """Tests for DriverQualificationService.check_expiry_alerts.

    Validates: Requirements 5.2, 5.3, 5.4
    """

    @pytest.mark.asyncio
    async def test_cdl_expiring_in_45_days_generates_warning(self):
        """CDL expiring in 45 days generates a warning-level alert (Req 5.2)."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=45)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future to avoid extra alerts
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.qualification_type == "cdl"
        assert alert.severity == "warning"
        assert alert.days_until_expiry == 45
        assert alert.driver_id == "driver_test123"
        assert alert.tenant_id == _TENANT_ID
        assert alert.full_name == "John Smith"
        assert alert.expiry_date == expiry

    @pytest.mark.asyncio
    async def test_medical_card_expiring_in_20_days_generates_urgent(self):
        """Medical card expiring in 20 days generates an urgent-level alert (Req 5.3)."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=20)
        driver_doc["medical_card_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.qualification_type == "medical_card"
        assert alert.severity == "urgent"
        assert alert.days_until_expiry == 20

    @pytest.mark.asyncio
    async def test_hazmat_expiring_in_5_days_generates_critical(self):
        """HAZMAT expiring in 5 days generates a critical-level alert (Req 5.4)."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=5)
        driver_doc["hazmat_endorsement_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.qualification_type == "hazmat"
        assert alert.severity == "critical"
        assert alert.days_until_expiry == 5

    @pytest.mark.asyncio
    async def test_no_hazmat_endorsement_no_alert(self):
        """Driver without HAZMAT endorsement (None) generates no alert for that field."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        driver_doc["hazmat_endorsement_expiry_date"] = None
        # Set other dates far in the future
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 0
        # Verify no hazmat alert was generated
        hazmat_alerts = [a for a in alerts if a.qualification_type == "hazmat"]
        assert len(hazmat_alerts) == 0

    @pytest.mark.asyncio
    async def test_multiple_alerts_for_same_driver_different_fields(self):
        """Driver with multiple fields at different thresholds generates multiple alerts."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        # CDL at 45 days → warning
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=45)).isoformat()
        # Medical card at 20 days → urgent
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=20)).isoformat()
        # HAZMAT at 5 days → critical
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=5)).isoformat()
        # Tanker far in the future → no alert
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 3

        # Verify each alert has the correct severity
        alerts_by_type = {a.qualification_type: a for a in alerts}
        assert alerts_by_type["cdl"].severity == "warning"
        assert alerts_by_type["medical_card"].severity == "urgent"
        assert alerts_by_type["hazmat"].severity == "critical"

    @pytest.mark.asyncio
    async def test_no_active_drivers_returns_empty(self):
        """No active drivers returns an empty alert list."""
        es = _make_es_service()
        # Return empty search results (no active drivers)
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_all_dates_beyond_60_days_no_alerts(self):
        """Driver with all dates beyond 60 days generates no alerts."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        far_future = (today + timedelta(days=365)).isoformat()
        driver_doc["cdl_expiry_date"] = far_future
        driver_doc["medical_card_expiry_date"] = far_future
        driver_doc["hazmat_endorsement_expiry_date"] = far_future
        driver_doc["tanker_endorsement_expiry_date"] = far_future

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_boundary_exactly_60_days_generates_warning(self):
        """Expiry exactly at 60 days generates a warning alert."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=60)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert alerts[0].days_until_expiry == 60

    @pytest.mark.asyncio
    async def test_boundary_exactly_30_days_generates_urgent(self):
        """Expiry exactly at 30 days generates an urgent alert."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=30)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "urgent"
        assert alerts[0].days_until_expiry == 30

    @pytest.mark.asyncio
    async def test_boundary_exactly_7_days_generates_critical(self):
        """Expiry exactly at 7 days generates a critical alert."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=7)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert alerts[0].days_until_expiry == 7

    @pytest.mark.asyncio
    async def test_boundary_exactly_61_days_no_alert(self):
        """Expiry at exactly 61 days does NOT generate an alert (just above threshold)."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today + timedelta(days=61)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_already_expired_generates_critical(self):
        """A qualification already expired (negative days) generates a critical alert."""
        es = _make_es_service()
        today = date.today()
        driver_doc = _make_driver_doc(status="active")
        from datetime import timedelta

        expiry = today - timedelta(days=3)  # expired 3 days ago
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        alerts = await service.check_expiry_alerts(_TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert alerts[0].days_until_expiry == -3


# ---------------------------------------------------------------------------
# Tests: auto_suspend_expired_drivers (Task 6.5)
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceAutoSuspend:
    """Tests for DriverQualificationService.auto_suspend_expired_drivers.

    Validates: Requirement 5.4
    """

    @pytest.mark.asyncio
    async def test_cdl_expiring_in_5_days_suspends_driver(self):
        """Driver with CDL expiring in 5 days → status transitions to suspended (Req 5.4)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        expiry = today + timedelta(days=5)
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        # First call: _get_all_active_drivers (list active drivers)
        # Second call: self.get() inside self.update()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["driver_id"] == "driver_test123"
        assert suspensions[0]["qualification_type"] == "cdl"
        assert suspensions[0]["days_until_expiry"] == 5
        assert "CDL" in suspensions[0]["suspension_reason"]
        assert "5 days" in suspensions[0]["suspension_reason"]
        # Verify update was called to persist the status change
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_medical_card_expiring_in_3_days_suspends_driver(self):
        """Driver with medical card expiring in 3 days → status transitions to suspended (Req 5.4)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        expiry = today + timedelta(days=3)
        driver_doc["medical_card_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["driver_id"] == "driver_test123"
        assert suspensions[0]["qualification_type"] == "medical_card"
        assert suspensions[0]["days_until_expiry"] == 3
        assert "MEDICAL CARD" in suspensions[0]["suspension_reason"]
        assert "3 days" in suspensions[0]["suspension_reason"]
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_suspended_driver_not_re_suspended(self):
        """Already suspended driver → no redundant update (Req 5.4)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        # Driver is already suspended — _get_all_active_drivers only returns
        # active drivers, so this driver won't appear in the scan.
        # Simulate: no active drivers returned
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 0
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_qualification_at_8_days_not_suspended(self):
        """Driver with qualification at 8 days → NOT suspended (just above threshold)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        # 8 days is above the 7-day threshold
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=8)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["tanker_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 0
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_suspension_reason_recorded_correctly(self):
        """Suspension reason is recorded correctly with qualification type and days."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        expiry = today + timedelta(days=2)
        driver_doc["tanker_endorsement_expiry_date"] = expiry.isoformat()
        # Set other dates far in the future
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        reason = suspensions[0]["suspension_reason"]
        assert "TANKER" in reason
        assert "2 days" in reason

        # Verify the update_document call includes the suspension_reason
        call_args = es.update_document.call_args
        assert call_args is not None
        partial_doc = call_args[0][2]  # Third positional arg is the partial doc
        assert partial_doc["status"] == "suspended"
        assert "TANKER" in partial_doc["suspension_reason"]

    @pytest.mark.asyncio
    async def test_already_expired_qualification_suspends_driver(self):
        """A qualification already expired (negative days) also triggers suspension."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        expiry = today - timedelta(days=2)  # expired 2 days ago
        driver_doc["cdl_expiry_date"] = expiry.isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["days_until_expiry"] == -2
        assert "expired" in suspensions[0]["suspension_reason"].lower()
        assert "2 days ago" in suspensions[0]["suspension_reason"]

    @pytest.mark.asyncio
    async def test_multiple_drivers_some_suspended_some_not(self):
        """Multiple drivers: only those within 7 days are suspended."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        # Driver 1: CDL expiring in 5 days → should be suspended
        driver1 = _make_driver_doc(driver_id="driver_001", status="active")
        driver1["cdl_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver1["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver1["hazmat_endorsement_expiry_date"] = None
        driver1["tanker_endorsement_expiry_date"] = None

        # Driver 2: All dates far in the future → should NOT be suspended
        driver2 = _make_driver_doc(driver_id="driver_002", status="active")
        driver2["cdl_expiry_date"] = (today + timedelta(days=90)).isoformat()
        driver2["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver2["hazmat_endorsement_expiry_date"] = None
        driver2["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver1, driver2])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["driver_id"] == "driver_001"

    @pytest.mark.asyncio
    async def test_boundary_exactly_7_days_suspends(self):
        """Driver with qualification expiring in exactly 7 days IS suspended (≤7 threshold)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=7)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["days_until_expiry"] == 7

    @pytest.mark.asyncio
    async def test_boundary_exactly_0_days_expiring_today_suspends(self):
        """Driver with qualification expiring today (0 days) IS suspended."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        driver_doc["cdl_expiry_date"] = today.isoformat()  # expires today
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["hazmat_endorsement_expiry_date"] = None
        driver_doc["tanker_endorsement_expiry_date"] = None

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        suspensions = await service.auto_suspend_expired_drivers(_TENANT_ID)

        assert len(suspensions) == 1
        assert suspensions[0]["days_until_expiry"] == 0
        assert "today" in suspensions[0]["suspension_reason"].lower()


# ---------------------------------------------------------------------------
# Tests: check_drug_test_overdue (Task 6.6)
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceDrugTestOverdue:
    """Tests for DriverQualificationService.check_drug_test_overdue.

    Validates: Requirement 5.8
    """

    @pytest.mark.asyncio
    async def test_driver_with_drug_test_400_days_ago_flagged(self):
        """Driver with last_drug_test_date 400 days ago → flagged as overdue."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        test_date = today - timedelta(days=400)
        driver_doc["last_drug_test_date"] = test_date.isoformat()
        # Set other dates far in the future to avoid interference
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.check_drug_test_overdue(_TENANT_ID)

        assert len(result) == 1
        assert result[0]["driver_id"] == "driver_test123"
        assert result[0]["full_name"] == "John Smith"
        assert result[0]["last_drug_test_date"] == test_date.isoformat()
        assert result[0]["days_overdue"] == 35  # 400 - 365 = 35

    @pytest.mark.asyncio
    async def test_driver_with_drug_test_300_days_ago_not_flagged(self):
        """Driver with last_drug_test_date 300 days ago → NOT flagged (within interval)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        test_date = today - timedelta(days=300)
        driver_doc["last_drug_test_date"] = test_date.isoformat()
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.check_drug_test_overdue(_TENANT_ID)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_driver_with_no_drug_test_date_flagged(self):
        """Driver with last_drug_test_date None → flagged as overdue (never tested)."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        driver_doc["last_drug_test_date"] = None
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.check_drug_test_overdue(_TENANT_ID)

        assert len(result) == 1
        assert result[0]["driver_id"] == "driver_test123"
        assert result[0]["full_name"] == "John Smith"
        assert result[0]["last_drug_test_date"] is None
        assert result[0]["days_overdue"] is None

    @pytest.mark.asyncio
    async def test_custom_interval_180_days(self):
        """Custom interval (180 days) works correctly — 200 days ago is overdue."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        driver_doc = _make_driver_doc(status="active")
        test_date = today - timedelta(days=200)
        driver_doc["last_drug_test_date"] = test_date.isoformat()
        driver_doc["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver_doc["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver_doc])
        )
        service = DriverQualificationService(es)

        result = await service.check_drug_test_overdue(_TENANT_ID, interval_days=180)

        assert len(result) == 1
        assert result[0]["days_overdue"] == 20  # 200 - 180 = 20

    @pytest.mark.asyncio
    async def test_multiple_drivers_only_overdue_returned(self):
        """Multiple drivers: only overdue ones are returned."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        # Driver 1: last test 400 days ago → overdue
        driver1 = _make_driver_doc(driver_id="driver_001", full_name="Alice Overdue", status="active")
        driver1["last_drug_test_date"] = (today - timedelta(days=400)).isoformat()
        driver1["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver1["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        # Driver 2: last test 100 days ago → NOT overdue
        driver2 = _make_driver_doc(driver_id="driver_002", full_name="Bob Current", status="active")
        driver2["last_drug_test_date"] = (today - timedelta(days=100)).isoformat()
        driver2["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver2["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        # Driver 3: never tested → overdue
        driver3 = _make_driver_doc(driver_id="driver_003", full_name="Charlie Never", status="active")
        driver3["last_drug_test_date"] = None
        driver3["cdl_expiry_date"] = (today + timedelta(days=365)).isoformat()
        driver3["medical_card_expiry_date"] = (today + timedelta(days=365)).isoformat()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver1, driver2, driver3])
        )
        service = DriverQualificationService(es)

        result = await service.check_drug_test_overdue(_TENANT_ID)

        assert len(result) == 2
        flagged_ids = [r["driver_id"] for r in result]
        assert "driver_001" in flagged_ids
        assert "driver_003" in flagged_ids
        assert "driver_002" not in flagged_ids


# ---------------------------------------------------------------------------
# Tests: DQF Dashboard (Task 6.7)
# ---------------------------------------------------------------------------


class TestDriverQualificationServiceDashboard:
    """Tests for DriverQualificationService.get_dqf_dashboard.

    Validates: Requirement 5.9
    """

    @pytest.mark.asyncio
    async def test_empty_tenant_returns_all_zeros(self):
        """Empty tenant (no drivers) returns dashboard with all zero counts."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert isinstance(result, DQFDashboard)
        assert result.tenant_id == _TENANT_ID
        assert result.total_drivers == 0
        assert result.active_drivers == 0
        assert result.suspended_drivers == 0
        assert result.expired_drivers == 0
        assert result.expiring_within_60_days == 0
        assert result.expiring_within_30_days == 0
        assert result.expiring_within_7_days == 0
        assert result.drug_test_overdue == 0

    @pytest.mark.asyncio
    async def test_mix_of_statuses_correct_counts(self):
        """Mix of active/suspended/expired drivers produces correct status counts."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        far_future = (today + timedelta(days=365)).isoformat()
        recent_test = (today - timedelta(days=30)).isoformat()

        # 2 active, 1 suspended, 1 expired
        driver_active1 = _make_driver_doc(driver_id="d1", status="active")
        driver_active1["cdl_expiry_date"] = far_future
        driver_active1["medical_card_expiry_date"] = far_future
        driver_active1["hazmat_endorsement_expiry_date"] = far_future
        driver_active1["tanker_endorsement_expiry_date"] = far_future
        driver_active1["last_drug_test_date"] = recent_test

        driver_active2 = _make_driver_doc(driver_id="d2", status="active")
        driver_active2["cdl_expiry_date"] = far_future
        driver_active2["medical_card_expiry_date"] = far_future
        driver_active2["hazmat_endorsement_expiry_date"] = far_future
        driver_active2["tanker_endorsement_expiry_date"] = far_future
        driver_active2["last_drug_test_date"] = recent_test

        driver_suspended = _make_driver_doc(driver_id="d3", status="suspended")
        driver_suspended["cdl_expiry_date"] = far_future
        driver_suspended["medical_card_expiry_date"] = far_future
        driver_suspended["last_drug_test_date"] = recent_test

        driver_expired = _make_driver_doc(driver_id="d4", status="expired")
        driver_expired["cdl_expiry_date"] = far_future
        driver_expired["medical_card_expiry_date"] = far_future
        driver_expired["last_drug_test_date"] = recent_test

        all_drivers = [driver_active1, driver_active2, driver_suspended, driver_expired]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(all_drivers)
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert result.total_drivers == 4
        assert result.active_drivers == 2
        assert result.suspended_drivers == 1
        assert result.expired_drivers == 1
        # No expiring qualifications (all far future)
        assert result.expiring_within_60_days == 0
        assert result.expiring_within_30_days == 0
        assert result.expiring_within_7_days == 0
        assert result.drug_test_overdue == 0

    @pytest.mark.asyncio
    async def test_expiry_thresholds_correct_counts(self):
        """Drivers with various expiry thresholds produce correct expiring counts."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        far_future = (today + timedelta(days=365)).isoformat()
        recent_test = (today - timedelta(days=30)).isoformat()

        # Driver 1: CDL expiring in 50 days → counted in 60-day bucket only
        driver1 = _make_driver_doc(driver_id="d1", status="active")
        driver1["cdl_expiry_date"] = (today + timedelta(days=50)).isoformat()
        driver1["medical_card_expiry_date"] = far_future
        driver1["hazmat_endorsement_expiry_date"] = far_future
        driver1["tanker_endorsement_expiry_date"] = far_future
        driver1["last_drug_test_date"] = recent_test

        # Driver 2: medical card expiring in 25 days → counted in 60 and 30 day buckets
        driver2 = _make_driver_doc(driver_id="d2", status="active")
        driver2["cdl_expiry_date"] = far_future
        driver2["medical_card_expiry_date"] = (today + timedelta(days=25)).isoformat()
        driver2["hazmat_endorsement_expiry_date"] = far_future
        driver2["tanker_endorsement_expiry_date"] = far_future
        driver2["last_drug_test_date"] = recent_test

        # Driver 3: hazmat expiring in 5 days → counted in all three buckets
        driver3 = _make_driver_doc(driver_id="d3", status="active")
        driver3["cdl_expiry_date"] = far_future
        driver3["medical_card_expiry_date"] = far_future
        driver3["hazmat_endorsement_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver3["tanker_endorsement_expiry_date"] = far_future
        driver3["last_drug_test_date"] = recent_test

        # Driver 4: all far future → not counted in any bucket
        driver4 = _make_driver_doc(driver_id="d4", status="active")
        driver4["cdl_expiry_date"] = far_future
        driver4["medical_card_expiry_date"] = far_future
        driver4["hazmat_endorsement_expiry_date"] = far_future
        driver4["tanker_endorsement_expiry_date"] = far_future
        driver4["last_drug_test_date"] = recent_test

        all_drivers = [driver1, driver2, driver3, driver4]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(all_drivers)
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert result.total_drivers == 4
        assert result.active_drivers == 4
        # Unique drivers: d1 (50d), d2 (25d), d3 (5d) → 3 within 60 days
        assert result.expiring_within_60_days == 3
        # Unique drivers: d2 (25d), d3 (5d) → 2 within 30 days
        assert result.expiring_within_30_days == 2
        # Unique drivers: d3 (5d) → 1 within 7 days
        assert result.expiring_within_7_days == 1

    @pytest.mark.asyncio
    async def test_driver_counted_once_even_with_multiple_expiring_fields(self):
        """A driver with multiple expiring qualifications is counted once per threshold."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        recent_test = (today - timedelta(days=30)).isoformat()

        # Driver with CDL AND medical card both expiring in 20 days
        driver = _make_driver_doc(driver_id="d1", status="active")
        driver["cdl_expiry_date"] = (today + timedelta(days=20)).isoformat()
        driver["medical_card_expiry_date"] = (today + timedelta(days=20)).isoformat()
        driver["hazmat_endorsement_expiry_date"] = (today + timedelta(days=20)).isoformat()
        driver["tanker_endorsement_expiry_date"] = (today + timedelta(days=20)).isoformat()
        driver["last_drug_test_date"] = recent_test

        es.search_documents = AsyncMock(
            return_value=_es_search_response([driver])
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        # Should be counted as 1 unique driver, not 4
        assert result.expiring_within_60_days == 1
        assert result.expiring_within_30_days == 1
        assert result.expiring_within_7_days == 0

    @pytest.mark.asyncio
    async def test_drug_test_overdue_count_accurate(self):
        """Drug test overdue count matches check_drug_test_overdue logic."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        far_future = (today + timedelta(days=365)).isoformat()

        # Driver 1: last test 400 days ago → overdue
        driver1 = _make_driver_doc(driver_id="d1", status="active")
        driver1["cdl_expiry_date"] = far_future
        driver1["medical_card_expiry_date"] = far_future
        driver1["hazmat_endorsement_expiry_date"] = far_future
        driver1["tanker_endorsement_expiry_date"] = far_future
        driver1["last_drug_test_date"] = (today - timedelta(days=400)).isoformat()

        # Driver 2: never tested → overdue
        driver2 = _make_driver_doc(driver_id="d2", status="active")
        driver2["cdl_expiry_date"] = far_future
        driver2["medical_card_expiry_date"] = far_future
        driver2["hazmat_endorsement_expiry_date"] = far_future
        driver2["tanker_endorsement_expiry_date"] = far_future
        driver2["last_drug_test_date"] = None

        # Driver 3: tested 100 days ago → NOT overdue
        driver3 = _make_driver_doc(driver_id="d3", status="active")
        driver3["cdl_expiry_date"] = far_future
        driver3["medical_card_expiry_date"] = far_future
        driver3["hazmat_endorsement_expiry_date"] = far_future
        driver3["tanker_endorsement_expiry_date"] = far_future
        driver3["last_drug_test_date"] = (today - timedelta(days=100)).isoformat()

        # Driver 4: suspended, last test 400 days ago → NOT counted (not active)
        driver4 = _make_driver_doc(driver_id="d4", status="suspended")
        driver4["cdl_expiry_date"] = far_future
        driver4["medical_card_expiry_date"] = far_future
        driver4["hazmat_endorsement_expiry_date"] = far_future
        driver4["tanker_endorsement_expiry_date"] = far_future
        driver4["last_drug_test_date"] = (today - timedelta(days=400)).isoformat()

        all_drivers = [driver1, driver2, driver3, driver4]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(all_drivers)
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert result.total_drivers == 4
        assert result.active_drivers == 3
        assert result.suspended_drivers == 1
        # Only active drivers with overdue tests: d1 and d2
        assert result.drug_test_overdue == 2

    @pytest.mark.asyncio
    async def test_suspended_expired_drivers_not_counted_in_expiry_thresholds(self):
        """Suspended/expired drivers are not counted in expiry threshold buckets."""
        es = _make_es_service()
        today = date.today()
        from datetime import timedelta

        recent_test = (today - timedelta(days=30)).isoformat()

        # Suspended driver with CDL expiring in 5 days — should NOT be in expiry counts
        driver_suspended = _make_driver_doc(driver_id="d1", status="suspended")
        driver_suspended["cdl_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver_suspended["medical_card_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver_suspended["last_drug_test_date"] = recent_test

        # Expired driver with CDL expiring in 5 days — should NOT be in expiry counts
        driver_expired = _make_driver_doc(driver_id="d2", status="expired")
        driver_expired["cdl_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver_expired["medical_card_expiry_date"] = (today + timedelta(days=5)).isoformat()
        driver_expired["last_drug_test_date"] = recent_test

        all_drivers = [driver_suspended, driver_expired]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(all_drivers)
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert result.total_drivers == 2
        assert result.active_drivers == 0
        assert result.suspended_drivers == 1
        assert result.expired_drivers == 1
        # No active drivers → no expiry threshold counts
        assert result.expiring_within_60_days == 0
        assert result.expiring_within_30_days == 0
        assert result.expiring_within_7_days == 0
        assert result.drug_test_overdue == 0

    @pytest.mark.asyncio
    async def test_dashboard_generated_at_is_set(self):
        """Dashboard generated_at field is populated."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        service = DriverQualificationService(es)

        result = await service.get_dqf_dashboard(_TENANT_ID)

        assert result.generated_at is not None
        assert isinstance(result.generated_at, datetime)
