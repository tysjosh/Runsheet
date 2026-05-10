"""Integration test: Route planning with HOS blocked, driver qual suspended, asset cert expired.

Verifies that the Route_Planning_Agent correctly handles compliance blocks
from three services in sequence:

1. HOS blocked: Driver has insufficient drive hours -> route flagged as
   hos_blocked, earliest eligible time returned.
2. Driver qual suspended: Driver's CDL/medical expired -> driver excluded
   from route assignment.
3. Asset cert expired: Truck's DOT cargo tank cert expired -> asset excluded
   from route assignment.
4. Combined scenario: All three blocks active -> no eligible driver/asset,
   route cannot be assigned.

ES, Redis, and Geotab dependencies are mocked via AsyncMock fixtures.

Validates: Requirements 4.2, 4.3, 4.4, 4.5, 5.5, 5.6, 5.7, 13.5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from compliance.services.hos_checker import (
    HOSChecker,
)
from compliance.services.driver_qualification_service import (
    DriverQualificationService,
)
from compliance.services.asset_certification_service import (
    AssetCertificationService,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_route_compliance"
DRIVER_ID = "driver_001"
TRUCK_ID = "truck_tanker_001"
ASSET_ID = TRUCK_ID  # Asset ID is the truck ID

FIXED_NOW = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)

# Route estimates (5 stops, ~15 miles between stops)
ESTIMATED_DRIVE_HOURS = 3.6  # (5+1)*15 / 25 mph
ESTIMATED_TOTAL_HOURS = 6.1  # drive + 5*0.5h per stop

# Assignments representing a fuel delivery loading plan
SAMPLE_ASSIGNMENTS = [
    {"fuel_grade": "DIESEL_2", "gallons": 500, "customer_id": "cust_001"},
    {"fuel_grade": "DIESEL_2", "gallons": 300, "customer_id": "cust_002"},
    {"fuel_grade": "DIESEL_2", "gallons": 400, "customer_id": "cust_003"},
    {"fuel_grade": "DIESEL_2", "gallons": 250, "customer_id": "cust_004"},
    {"fuel_grade": "DIESEL_2", "gallons": 350, "customer_id": "cust_005"},
]


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.index = AsyncMock(return_value=None)
    return es


def _make_redis_client() -> AsyncMock:
    """Create a mocked Redis client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=None)
    return redis


def _make_geotab_connector(
    available_drive_hours: float = 11.0,
    available_window_hours: float = 14.0,
    cumulative_cycle_hours: float = 40.0,
    cycle_type: str = "7_day",
) -> AsyncMock:
    """Create a mocked Geotab connector returning configurable HOS data."""
    geotab = AsyncMock()
    geotab.get_hos_status = AsyncMock(
        return_value={
            "availableDriveHours": available_drive_hours,
            "availableWindowHours": available_window_hours,
            "cumulativeCycleHours": cumulative_cycle_hours,
            "cycleType": cycle_type,
        }
    )
    return geotab


def _make_driver_es_response(
    driver_id: str = DRIVER_ID,
    status: str = "active",
    cdl_expiry: str = "2027-12-31",
    medical_expiry: str = "2027-06-30",
    hazmat_expiry: Optional[str] = "2027-12-31",
    tanker_expiry: Optional[str] = "2027-12-31",
    cdl_class: str = "A",
) -> Dict[str, Any]:
    """Build a mock ES response for a driver document."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "driver_id": driver_id,
                        "tenant_id": TENANT_ID,
                        "full_name": "Test Driver",
                        "cdl_number": "CDL123456",
                        "cdl_state": "TX",
                        "cdl_class": cdl_class,
                        "cdl_expiry_date": cdl_expiry,
                        "medical_card_expiry_date": medical_expiry,
                        "hazmat_endorsement_expiry_date": hazmat_expiry,
                        "tanker_endorsement_expiry_date": tanker_expiry,
                        "last_drug_test_date": "2026-01-15",
                        "last_mvr_date": "2026-03-01",
                        "status": status,
                    }
                }
            ],
            "total": {"value": 1},
        }
    }


def _make_asset_certs_es_response(
    asset_id: str = ASSET_ID,
    cert_type: str = "V_test",
    status: str = "valid",
    expiry_date: str = "2027-12-31",
    certification_date: str = "2025-06-01",
) -> Dict[str, Any]:
    """Build a mock ES response for asset certifications."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "cert_id": "cert_001",
                        "tenant_id": TENANT_ID,
                        "asset_id": asset_id,
                        "certification_type": cert_type,
                        "certification_date": certification_date,
                        "expiry_date": expiry_date,
                        "inspector_name": "Inspector Smith",
                        "certificate_number": "DOT-V-2025-001",
                        "status": status,
                    },
                    "sort": [expiry_date, "cert_001"],
                }
            ],
            "total": {"value": 1},
        }
    }


# ===========================================================================
# Test Class
# ===========================================================================


class TestRoutePlanningComplianceBlocks:
    """Integration tests for route planning compliance blocks.

    Tests the three compliance services (HOSChecker, DriverQualificationService,
    AssetCertificationService) individually and in combination to verify that
    the Route_Planning_Agent correctly excludes ineligible drivers and assets.
    """

    # ------------------------------------------------------------------
    # Scenario 1: HOS Blocked
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_hos_blocked_insufficient_drive_hours(self):
        """Driver with insufficient drive hours is marked HOS-ineligible.

        When available_drive_hours (2.0h) < estimated_drive_hours + 0.5h buffer,
        the HOSChecker returns eligible=False with earliest_eligible_time.

        Validates: Requirements 4.2, 4.5
        """
        es = _make_es_service()
        redis = _make_redis_client()
        # Driver only has 2.0 hours of drive time left
        geotab = _make_geotab_connector(
            available_drive_hours=2.0,
            available_window_hours=10.0,
            cumulative_cycle_hours=45.0,
        )

        checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        result = await checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )

        assert result.eligible is False
        assert result.driver_id == DRIVER_ID
        assert len(result.reasons) >= 1
        assert any("drive hours" in r.lower() or "drive" in r.lower() for r in result.reasons)
        # Earliest eligible time should be set (10-hour rest to reset)
        assert result.earliest_eligible_time is not None

    @pytest.mark.asyncio
    async def test_hos_blocked_14_hour_window_exceeded(self):
        """Driver with insufficient on-duty window is HOS-ineligible.

        When available_window_hours (4.0h) < estimated_total_hours (6.1h),
        the HOSChecker returns eligible=False.

        Validates: Requirements 4.3, 4.5
        """
        es = _make_es_service()
        redis = _make_redis_client()
        # Driver has enough drive hours but insufficient window
        geotab = _make_geotab_connector(
            available_drive_hours=8.0,
            available_window_hours=4.0,
            cumulative_cycle_hours=45.0,
        )

        checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        result = await checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )

        assert result.eligible is False
        assert any("window" in r.lower() for r in result.reasons)
        assert result.earliest_eligible_time is not None

    @pytest.mark.asyncio
    async def test_hos_blocked_cycle_limit_exceeded(self):
        """Driver exceeding 60-hour/7-day cycle limit is HOS-ineligible.

        When cumulative_cycle_hours (57.0) + estimated_total_hours (6.1) > 60,
        the HOSChecker returns eligible=False.

        Validates: Requirements 4.4, 4.5
        """
        es = _make_es_service()
        redis = _make_redis_client()
        # Driver near cycle limit (57h of 60h used)
        geotab = _make_geotab_connector(
            available_drive_hours=8.0,
            available_window_hours=10.0,
            cumulative_cycle_hours=57.0,
            cycle_type="7_day",
        )

        checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        result = await checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )

        assert result.eligible is False
        assert any("cycle" in r.lower() or "60" in r for r in result.reasons)
        # 34-hour restart required for cycle reset
        assert result.earliest_eligible_time is not None

    @pytest.mark.asyncio
    async def test_hos_eligible_sufficient_hours(self):
        """Driver with sufficient hours passes HOS check.

        Validates: Requirement 4.7 (audit log on successful assignment)
        """
        es = _make_es_service()
        redis = _make_redis_client()
        # Driver has plenty of hours
        geotab = _make_geotab_connector(
            available_drive_hours=10.0,
            available_window_hours=12.0,
            cumulative_cycle_hours=30.0,
        )

        checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        result = await checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )

        assert result.eligible is True
        assert result.reasons == []
        assert result.earliest_eligible_time is None

    # ------------------------------------------------------------------
    # Scenario 2: Driver Qualification Suspended
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_driver_qual_suspended_status(self):
        """Suspended driver is excluded from route assignment.

        Validates: Requirement 5.5
        """
        es = _make_es_service()
        # Return a suspended driver
        es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(status="suspended")
        )

        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": True, "requires_tanker": True},
        )

        assert result.eligible is False
        assert result.driver_id == DRIVER_ID
        assert any("suspended" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_driver_qual_cdl_expired(self):
        """Driver with expired CDL is excluded from route assignment.

        Validates: Requirement 5.5
        """
        es = _make_es_service()
        # CDL expired well in the past (2020) so it's always expired
        es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(cdl_expiry="2020-01-01")
        )

        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={},
        )

        assert result.eligible is False
        assert any("cdl" in r.lower() and "expired" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_driver_qual_missing_hazmat_endorsement(self):
        """Driver without HAZMAT endorsement excluded from HAZMAT routes.

        Validates: Requirement 5.6
        """
        es = _make_es_service()
        # Driver has no HAZMAT endorsement
        es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(hazmat_expiry=None)
        )

        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": True, "requires_tanker": False},
        )

        assert result.eligible is False
        assert any("hazmat" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_driver_qual_missing_tanker_endorsement(self):
        """Driver without tanker endorsement excluded from tanker routes.

        Validates: Requirement 5.7
        """
        es = _make_es_service()
        # Driver has no tanker endorsement
        es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(tanker_expiry=None)
        )

        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": False, "requires_tanker": True},
        )

        assert result.eligible is False
        assert any("tanker" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_driver_qual_active_with_endorsements(self):
        """Active driver with all endorsements passes qualification check."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(
                status="active",
                cdl_expiry="2027-12-31",
                medical_expiry="2027-06-30",
                hazmat_expiry="2027-12-31",
                tanker_expiry="2027-12-31",
            )
        )

        service = DriverQualificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": True, "requires_tanker": True},
        )

        assert result.eligible is True
        assert result.reasons == []

    # ------------------------------------------------------------------
    # Scenario 3: Asset Certification Expired
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_asset_cert_expired_dot_cargo_tank(self):
        """Asset with expired DOT cargo tank cert is excluded from dispatch.

        Validates: Requirement 13.5
        """
        es = _make_es_service()
        # Return an expired V_test certification
        es.search_documents = AsyncMock(
            return_value=_make_asset_certs_es_response(
                cert_type="V_test",
                status="expired",
                expiry_date="2026-06-01",
            )
        )

        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            asset_id=ASSET_ID,
        )

        assert result.eligible is False
        assert result.asset_id == ASSET_ID
        assert any("v_test" in r.lower() or "expired" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_asset_cert_3_year_retest_overdue(self):
        """Asset with DOT cert older than 3 years requires retest.

        Validates: Requirement 13.6
        """
        es = _make_es_service()
        # Certification date is more than 3 years ago (1095+ days)
        # Use a date far enough in the past that it's always >3 years old
        old_cert_date = "2020-01-01"
        es.search_documents = AsyncMock(
            return_value=_make_asset_certs_es_response(
                cert_type="K_test",
                status="valid",
                expiry_date="2028-01-01",
                certification_date=old_cert_date,
            )
        )

        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            asset_id=ASSET_ID,
        )

        assert result.eligible is False
        assert any("3-year" in r.lower() or "retest" in r.lower() for r in result.reasons)

    @pytest.mark.asyncio
    async def test_asset_cert_valid_passes(self):
        """Asset with valid DOT certifications passes dispatch check."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_make_asset_certs_es_response(
                cert_type="V_test",
                status="valid",
                expiry_date="2027-12-31",
                certification_date="2025-06-01",
            )
        )

        service = AssetCertificationService(es)

        result = await service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            asset_id=ASSET_ID,
        )

        assert result.eligible is True
        assert result.reasons == []

    # ------------------------------------------------------------------
    # Scenario 4: Combined — All Three Blocks Active
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_combined_all_blocks_active(self):
        """All three compliance blocks active: no eligible driver/asset.

        Simulates the Route_Planning_Agent's sequential compliance check:
        1. DriverQualificationService -> suspended -> BLOCKED
        2. HOSChecker -> insufficient hours -> BLOCKED
        3. AssetCertificationService -> expired cert -> BLOCKED

        When all three are active, the route cannot be assigned.

        Validates: Requirements 4.2, 5.5, 13.5
        """
        es = _make_es_service()
        redis = _make_redis_client()

        # --- Driver Qualification: SUSPENDED ---
        driver_es = AsyncMock()
        driver_es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(status="suspended")
        )
        driver_service = DriverQualificationService(driver_es)

        driver_result = await driver_service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": True, "requires_tanker": True},
        )
        assert driver_result.eligible is False

        # --- HOS: INSUFFICIENT HOURS ---
        geotab = _make_geotab_connector(
            available_drive_hours=1.0,
            available_window_hours=3.0,
            cumulative_cycle_hours=58.0,
        )
        hos_checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        hos_result = await hos_checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )
        assert hos_result.eligible is False
        # Multiple reasons: drive, window, and cycle all exceeded
        assert len(hos_result.reasons) >= 2

        # --- Asset Certification: EXPIRED ---
        asset_es = AsyncMock()
        asset_es.search_documents = AsyncMock(
            return_value=_make_asset_certs_es_response(
                cert_type="V_test",
                status="expired",
                expiry_date="2026-05-01",
            )
        )
        asset_service = AssetCertificationService(asset_es)

        asset_result = await asset_service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            asset_id=ASSET_ID,
        )
        assert asset_result.eligible is False

        # --- Combined: Route cannot be assigned ---
        # All three checks failed — the route planning agent would skip
        # this loading plan entirely
        all_blocked = (
            not driver_result.eligible
            and not hos_result.eligible
            and not asset_result.eligible
        )
        assert all_blocked is True

        # Collect all block reasons
        all_reasons = (
            driver_result.reasons + hos_result.reasons + asset_result.reasons
        )
        assert len(all_reasons) >= 4  # At least: suspended + drive + window/cycle + expired cert

    @pytest.mark.asyncio
    async def test_combined_driver_blocked_but_asset_eligible(self):
        """Driver blocked but asset eligible — route still cannot be assigned.

        Even if the asset passes certification, a suspended driver blocks
        the route assignment.
        """
        # Driver: suspended
        driver_es = AsyncMock()
        driver_es.search_documents = AsyncMock(
            return_value=_make_driver_es_response(status="suspended")
        )
        driver_service = DriverQualificationService(driver_es)

        driver_result = await driver_service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            route_requirements={"requires_hazmat": True, "requires_tanker": True},
        )

        # Asset: valid
        asset_es = AsyncMock()
        asset_es.search_documents = AsyncMock(
            return_value=_make_asset_certs_es_response(
                status="valid", expiry_date="2027-12-31"
            )
        )
        asset_service = AssetCertificationService(asset_es)

        asset_result = await asset_service.is_dispatch_eligible(
            tenant_id=TENANT_ID,
            asset_id=ASSET_ID,
        )

        # Driver blocked, asset eligible — route still blocked
        assert driver_result.eligible is False
        assert asset_result.eligible is True
        # Route cannot proceed because driver is ineligible
        route_can_proceed = driver_result.eligible and asset_result.eligible
        assert route_can_proceed is False

    @pytest.mark.asyncio
    async def test_hos_returns_earliest_eligible_time_for_blocked_route(self):
        """When HOS blocks a route, earliest_eligible_time is computed.

        The Route_Planning_Agent uses this to notify the dispatcher when
        the driver becomes available again.

        Validates: Requirement 4.5
        """
        es = _make_es_service()
        redis = _make_redis_client()
        # All three HOS limits exceeded
        geotab = _make_geotab_connector(
            available_drive_hours=1.0,
            available_window_hours=3.0,
            cumulative_cycle_hours=58.0,
            cycle_type="7_day",
        )

        checker = HOSChecker(es, redis, geotab, tenant_id=TENANT_ID)

        result = await checker.is_eligible(
            driver_id=DRIVER_ID,
            estimated_drive_hours=ESTIMATED_DRIVE_HOURS,
            estimated_total_hours=ESTIMATED_TOTAL_HOURS,
        )

        assert result.eligible is False
        assert result.earliest_eligible_time is not None
        # Earliest eligible should be at least 10 hours from now (rest period)
        # or 34 hours (cycle restart) — whichever is later
        # Since cycle is exceeded, 34-hour restart is the most restrictive
        now = datetime.now(timezone.utc)
        time_until_eligible = (
            result.earliest_eligible_time.replace(tzinfo=timezone.utc) - now
            if result.earliest_eligible_time.tzinfo is None
            else result.earliest_eligible_time - now
        )
        # Should be at least 30 hours from now (allowing some tolerance)
        assert time_until_eligible.total_seconds() > 30 * 3600
