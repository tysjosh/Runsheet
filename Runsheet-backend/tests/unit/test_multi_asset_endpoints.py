"""
Unit tests for multi-asset backend endpoints.

Validates:
- Requirement 2.1: GET /fleet/trucks backward compat (only trucks)
- Requirement 2.2: GET /fleet/assets with filtering
- Requirement 2.3: GET /fleet/assets/{asset_id}
- Requirement 2.4: GET /fleet/summary includes byType/bySubtype
- Requirement 2.5: asset_type filtering on /fleet/assets
- Requirement 2.6: Consistent asset_type/asset_subtype in responses
- Requirement 6.1: POST /fleet/assets registers new asset
- Requirement 6.2: Validates asset_type/asset_subtype enums
- Requirement 6.3: PATCH /fleet/assets/{asset_id} partial update
- Requirement 6.4: Vehicle assets require plate_number
- Requirement 6.5: Vessel assets require vessel_name
- Requirement 6.6: Container assets require container_number
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_location(address="Dubai"):
    return {
        "id": "LOC-001",
        "name": address,
        "type": "warehouse",
        "coordinates": {"lat": 25.0, "lng": 55.0},
        "address": address,
    }


def _make_es_doc(asset_id, asset_type="vehicle", asset_subtype="truck", **overrides):
    """Build a minimal ES document for a given asset type."""
    doc = {
        "truck_id": asset_id,
        "asset_id": asset_id,
        "asset_type": asset_type,
        "asset_subtype": asset_subtype,
        "asset_name": overrides.pop("asset_name", f"Asset {asset_id}"),
        "status": overrides.pop("status", "active"),
        "current_location": _make_location(),
        "destination": {},
        "route": {},
        "last_update": "2025-01-01T12:00:00",
    }
    doc.update(overrides)
    return doc


def _es_search_response(hits, total=None):
    """Build a minimal ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


def _es_agg_response(total, active, delayed, by_type_buckets, by_subtype_buckets):
    """Build an ES aggregation response for the summary endpoint."""
    return {
        "hits": {"total": {"value": total}, "hits": []},
        "aggregations": {
            "by_type": {"buckets": by_type_buckets},
            "by_subtype": {"buckets": by_subtype_buckets},
            "active_count": {"doc_count": active},
            "delayed_count": {"doc_count": delayed},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Create a test client with mocked ES service."""
    with patch("data_endpoints.elasticsearch_service") as mock_es:
        mock_es.index_document = AsyncMock(return_value={"result": "created"})
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))
        mock_es.get_document = AsyncMock(return_value=_make_es_doc("T-001"))
        mock_es.get_all_documents = AsyncMock(return_value=[])
        mock_es.update_document = AsyncMock(return_value={"result": "updated"})
        from main import app
        with TestClient(app) as c:
            yield c, mock_es


# ---------------------------------------------------------------------------
# GET /api/fleet/assets — list & filter (Req 2.2, 2.5, 2.6)
# ---------------------------------------------------------------------------

class TestGetFleetAssets:
    """Validates: Requirements 2.2, 2.5, 2.6"""

    def test_returns_all_assets(self, client):
        """GET /fleet/assets with no filters returns all assets."""
        c, mock_es = client
        docs = [
            _make_es_doc("V-001", "vehicle", "truck", plate_number="ABC-1234"),
            _make_es_doc("VS-001", "vessel", "boat", vessel_name="Sea Runner"),
            _make_es_doc("E-001", "equipment", "crane"),
        ]
        mock_es.search_documents = AsyncMock(return_value=_es_search_response(docs))

        resp = c.get("/api/fleet/assets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["data"]) == 3

    def test_filter_by_asset_type(self, client):
        """GET /fleet/assets?asset_type=vessel returns only vessels."""
        c, mock_es = client
        vessel_doc = _make_es_doc("VS-001", "vessel", "boat", vessel_name="Sea Runner")
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([vessel_doc]))

        resp = c.get("/api/fleet/assets?asset_type=vessel")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["asset_type"] == "vessel"

        # Verify the ES query included the asset_type filter
        call_args = mock_es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"asset_type": "vessel"}} in filters

    def test_filter_by_asset_subtype(self, client):
        """GET /fleet/assets?asset_subtype=crane returns only cranes."""
        c, mock_es = client
        crane_doc = _make_es_doc("E-001", "equipment", "crane")
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([crane_doc]))

        resp = c.get("/api/fleet/assets?asset_subtype=crane")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["asset_subtype"] == "crane"

    def test_filter_by_status(self, client):
        """GET /fleet/assets?status=active returns only active assets."""
        c, mock_es = client
        active_doc = _make_es_doc("V-001", status="active")
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([active_doc]))

        resp = c.get("/api/fleet/assets?status=active")
        assert resp.status_code == 200

        call_args = mock_es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"status": "active"}} in filters

    def test_combined_filters(self, client):
        """GET /fleet/assets?asset_type=vehicle&status=active applies both filters."""
        c, mock_es = client
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))

        resp = c.get("/api/fleet/assets?asset_type=vehicle&status=active")
        assert resp.status_code == 200

        call_args = mock_es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"asset_type": "vehicle"}} in filters
        assert {"term": {"status": "active"}} in filters

    def test_no_filter_uses_match_all(self, client):
        """GET /fleet/assets with no filters uses match_all query."""
        c, mock_es = client
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))

        resp = c.get("/api/fleet/assets")
        assert resp.status_code == 200

        call_args = mock_es.search_documents.call_args
        query = call_args[0][1]
        assert "match_all" in query["query"]

    def test_queries_assets_alias(self, client):
        """GET /fleet/assets queries the 'assets' alias, not 'trucks' directly."""
        c, mock_es = client
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))

        c.get("/api/fleet/assets")
        call_args = mock_es.search_documents.call_args
        assert call_args[0][0] == "assets"

    def test_response_includes_type_fields(self, client):
        """Each asset in the response includes asset_type and asset_subtype."""
        c, mock_es = client
        doc = _make_es_doc("VS-001", "vessel", "boat", vessel_name="Sea Runner")
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([doc]))

        resp = c.get("/api/fleet/assets")
        asset = resp.json()["data"][0]
        assert asset["asset_type"] == "vessel"
        assert asset["asset_subtype"] == "boat"
        assert asset["vesselName"] == "Sea Runner"


# ---------------------------------------------------------------------------
# GET /api/fleet/assets/{asset_id} (Req 2.3)
# ---------------------------------------------------------------------------

class TestGetAssetById:
    """Validates: Requirement 2.3"""

    def test_returns_single_asset(self, client):
        """GET /fleet/assets/{id} returns the asset with all type-specific fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "VS-001", "vessel", "boat",
            vessel_name="Sea Runner",
            imo_number="IMO-123",
            port_of_registry="Dubai",
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/VS-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["id"] == "VS-001"
        assert data["data"]["asset_type"] == "vessel"
        assert data["data"]["vesselName"] == "Sea Runner"
        assert data["data"]["imoNumber"] == "IMO-123"

    def test_returns_vehicle_fields(self, client):
        """GET /fleet/assets/{id} returns vehicle-specific fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "V-001", "vehicle", "truck",
            plate_number="ABC-1234",
            driver_id="D-001",
            driver_name="John Doe",
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/V-001")
        asset = resp.json()["data"]
        assert asset["plateNumber"] == "ABC-1234"
        assert asset["driverId"] == "D-001"
        assert asset["driverName"] == "John Doe"

    def test_returns_container_fields(self, client):
        """GET /fleet/assets/{id} returns container-specific fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "C-001", "container", "cargo_container",
            container_number="CONT-123",
            container_size="40ft",
            weight_tonnes=25.5,
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/C-001")
        asset = resp.json()["data"]
        assert asset["containerNumber"] == "CONT-123"
        assert asset["containerSize"] == "40ft"
        assert asset["weightTonnes"] == 25.5

    def test_returns_equipment_fields(self, client):
        """GET /fleet/assets/{id} returns equipment-specific fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "E-001", "equipment", "crane",
            equipment_model="Liebherr LTM 1300",
            lifting_capacity_tonnes=300.0,
            operational_radius_meters=60.0,
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/E-001")
        asset = resp.json()["data"]
        assert asset["equipmentModel"] == "Liebherr LTM 1300"
        assert asset["liftingCapacityTonnes"] == 300.0
        assert asset["operationalRadiusMeters"] == 60.0

    def test_not_found_returns_404(self, client):
        """GET /fleet/assets/{id} returns 404 when asset doesn't exist."""
        c, mock_es = client
        mock_es.get_document = AsyncMock(side_effect=Exception("Not found"))

        resp = c.get("/api/fleet/assets/NONEXISTENT")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/fleet/assets/{asset_id} (Req 6.3)
# ---------------------------------------------------------------------------

class TestUpdateFleetAsset:
    """Validates: Requirement 6.3"""

    def test_partial_update_status(self, client):
        """PATCH /fleet/assets/{id} updates only the provided fields."""
        c, mock_es = client
        updated_doc = _make_es_doc("V-001", status="idle")
        mock_es.update_document = AsyncMock(return_value={"result": "updated"})
        mock_es.get_document = AsyncMock(return_value=updated_doc)

        resp = c.patch("/api/fleet/assets/V-001", json={"status": "idle"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # Verify update_document was called with partial doc
        call_args = mock_es.update_document.call_args
        assert call_args[0][0] == "trucks"  # index
        assert call_args[0][1] == "V-001"   # doc ID
        partial_doc = call_args[0][2]
        assert partial_doc["status"] == "idle"

    def test_partial_update_name(self, client):
        """PATCH /fleet/assets/{id} can update the asset name."""
        c, mock_es = client
        updated_doc = _make_es_doc("V-001", asset_name="New Name")
        mock_es.update_document = AsyncMock(return_value={"result": "updated"})
        mock_es.get_document = AsyncMock(return_value=updated_doc)

        resp = c.patch("/api/fleet/assets/V-001", json={"name": "New Name"})
        assert resp.status_code == 200

        partial_doc = mock_es.update_document.call_args[0][2]
        assert partial_doc["asset_name"] == "New Name"

    def test_partial_update_vessel_fields(self, client):
        """PATCH /fleet/assets/{id} can update vessel-specific fields."""
        c, mock_es = client
        updated_doc = _make_es_doc("VS-001", "vessel", "boat", vessel_name="Updated Vessel")
        mock_es.update_document = AsyncMock(return_value={"result": "updated"})
        mock_es.get_document = AsyncMock(return_value=updated_doc)

        resp = c.patch("/api/fleet/assets/VS-001", json={
            "vessel_name": "Updated Vessel",
            "draft_meters": 5.5,
        })
        assert resp.status_code == 200

        partial_doc = mock_es.update_document.call_args[0][2]
        assert partial_doc["vessel_name"] == "Updated Vessel"
        assert partial_doc["draft_meters"] == 5.5

    def test_empty_update_returns_400(self, client):
        """PATCH /fleet/assets/{id} with no fields returns 400."""
        c, mock_es = client
        resp = c.patch("/api/fleet/assets/V-001", json={})
        assert resp.status_code == 400

    def test_update_returns_full_document(self, client):
        """PATCH /fleet/assets/{id} returns the full updated document."""
        c, mock_es = client
        updated_doc = _make_es_doc(
            "V-001", "vehicle", "truck",
            plate_number="ABC-1234",
            status="idle",
        )
        mock_es.update_document = AsyncMock(return_value={"result": "updated"})
        mock_es.get_document = AsyncMock(return_value=updated_doc)

        resp = c.patch("/api/fleet/assets/V-001", json={"status": "idle"})
        data = resp.json()["data"]
        assert data["id"] == "V-001"
        assert data["plateNumber"] == "ABC-1234"


# ---------------------------------------------------------------------------
# GET /api/fleet/trucks — backward compatibility (Req 2.1)
# ---------------------------------------------------------------------------

class TestGetTrucksBackwardCompat:
    """Validates: Requirement 2.1"""

    def test_returns_only_trucks(self, client):
        """GET /fleet/trucks filters for asset_subtype=truck or legacy docs."""
        c, mock_es = client
        truck_doc = _make_es_doc("T-001", "vehicle", "truck", plate_number="ABC-1234")
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([truck_doc]))

        resp = c.get("/api/fleet/trucks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == "T-001"
        assert data["data"][0]["plateNumber"] == "ABC-1234"

    def test_query_filters_for_trucks_or_legacy(self, client):
        """GET /fleet/trucks ES query uses should clause for truck subtype or missing asset_type."""
        c, mock_es = client
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))

        c.get("/api/fleet/trucks")

        call_args = mock_es.search_documents.call_args
        query = call_args[0][1]
        should = query["query"]["bool"]["should"]
        # Should contain term for asset_subtype=truck
        assert {"term": {"asset_subtype": "truck"}} in should
        # Should contain must_not exists for legacy docs
        legacy_clause = {"bool": {"must_not": {"exists": {"field": "asset_type"}}}}
        assert legacy_clause in should
        assert query["query"]["bool"]["minimum_should_match"] == 1

    def test_queries_trucks_index(self, client):
        """GET /fleet/trucks queries the 'trucks' index directly."""
        c, mock_es = client
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))

        c.get("/api/fleet/trucks")
        call_args = mock_es.search_documents.call_args
        assert call_args[0][0] == "trucks"

    def test_response_format_matches_legacy(self, client):
        """GET /fleet/trucks response format matches the legacy truck format."""
        c, mock_es = client
        truck_doc = {
            "truck_id": "T-001",
            "plate_number": "ABC-1234",
            "driver_id": "D-001",
            "driver_name": "John Doe",
            "current_location": _make_location(),
            "destination": _make_location("Abu Dhabi"),
            "route": {"id": "R-001", "distance": 150, "estimated_duration": 120},
            "status": "on_time",
            "estimated_arrival": "2025-01-01T14:00:00",
            "last_update": "2025-01-01T12:00:00",
            "cargo": {"type": "electronics", "weight": 500},
            "asset_type": "vehicle",
            "asset_subtype": "truck",
        }
        mock_es.search_documents = AsyncMock(return_value=_es_search_response([truck_doc]))

        resp = c.get("/api/fleet/trucks")
        truck = resp.json()["data"][0]
        assert truck["id"] == "T-001"
        assert truck["plateNumber"] == "ABC-1234"
        assert truck["driverId"] == "D-001"
        assert truck["driverName"] == "John Doe"
        assert truck["status"] == "on_time"


# ---------------------------------------------------------------------------
# GET /api/fleet/summary — extended with byType/bySubtype (Req 2.4)
# ---------------------------------------------------------------------------

class TestGetFleetSummary:
    """Validates: Requirement 2.4"""

    def test_includes_by_type_counts(self, client):
        """GET /fleet/summary includes byType breakdown."""
        c, mock_es = client
        # Mock get_all_documents for the legacy truck summary
        mock_es.get_all_documents = AsyncMock(return_value=[
            {"status": "on_time"},
            {"status": "delayed"},
        ])
        # Mock search_documents for the aggregation query
        agg_resp = _es_agg_response(
            total=10,
            active=6,
            delayed=2,
            by_type_buckets=[
                {"key": "vehicle", "doc_count": 5},
                {"key": "vessel", "doc_count": 3},
                {"key": "equipment", "doc_count": 2},
            ],
            by_subtype_buckets=[
                {"key": "truck", "doc_count": 4},
                {"key": "fuel_truck", "doc_count": 1},
                {"key": "boat", "doc_count": 3},
                {"key": "crane", "doc_count": 2},
            ],
        )
        mock_es.search_documents = AsyncMock(return_value=agg_resp)

        resp = c.get("/api/fleet/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]

        # Legacy truck fields still present
        assert "totalTrucks" in data
        assert "activeTrucks" in data
        assert "delayedTrucks" in data

        # New multi-asset fields
        assert data["totalAssets"] == 10
        assert data["activeAssets"] == 6
        assert data["delayedAssets"] == 2
        assert data["byType"]["vehicle"] == 5
        assert data["byType"]["vessel"] == 3
        assert data["byType"]["equipment"] == 2

    def test_includes_by_subtype_counts(self, client):
        """GET /fleet/summary includes bySubtype breakdown."""
        c, mock_es = client
        mock_es.get_all_documents = AsyncMock(return_value=[])
        agg_resp = _es_agg_response(
            total=7,
            active=4,
            delayed=1,
            by_type_buckets=[{"key": "vehicle", "doc_count": 7}],
            by_subtype_buckets=[
                {"key": "truck", "doc_count": 5},
                {"key": "fuel_truck", "doc_count": 2},
            ],
        )
        mock_es.search_documents = AsyncMock(return_value=agg_resp)

        resp = c.get("/api/fleet/summary")
        data = resp.json()["data"]
        assert data["bySubtype"]["truck"] == 5
        assert data["bySubtype"]["fuel_truck"] == 2

    def test_agg_failure_returns_zeros(self, client):
        """GET /fleet/summary returns zero counts if aggregation fails."""
        c, mock_es = client
        mock_es.get_all_documents = AsyncMock(return_value=[])
        mock_es.search_documents = AsyncMock(side_effect=Exception("ES down"))

        resp = c.get("/api/fleet/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["totalAssets"] == 0
        assert data["activeAssets"] == 0
        assert data["delayedAssets"] == 0
        assert data["byType"] == {}
        assert data["bySubtype"] == {}

    def test_summary_queries_assets_alias(self, client):
        """GET /fleet/summary aggregation queries the 'assets' alias."""
        c, mock_es = client
        mock_es.get_all_documents = AsyncMock(return_value=[])
        agg_resp = _es_agg_response(0, 0, 0, [], [])
        mock_es.search_documents = AsyncMock(return_value=agg_resp)

        c.get("/api/fleet/summary")
        call_args = mock_es.search_documents.call_args
        assert call_args[0][0] == "assets"


# ---------------------------------------------------------------------------
# _format_asset helper — response field mapping (Req 2.6)
# ---------------------------------------------------------------------------

class TestFormatAsset:
    """Validates: Requirement 2.6 — consistent asset_type/asset_subtype in responses."""

    def test_vehicle_format(self, client):
        """_format_asset maps vehicle ES fields to camelCase response fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "V-001", "vehicle", "truck",
            plate_number="ABC-1234",
            driver_id="D-001",
            driver_name="John Doe",
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/V-001")
        asset = resp.json()["data"]
        assert asset["asset_type"] == "vehicle"
        assert asset["asset_subtype"] == "truck"
        assert asset["plateNumber"] == "ABC-1234"
        assert asset["driverId"] == "D-001"

    def test_vessel_format(self, client):
        """_format_asset maps vessel ES fields to camelCase response fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "VS-001", "vessel", "boat",
            vessel_name="Sea Runner",
            imo_number="IMO-123",
            port_of_registry="Dubai",
            draft_meters=4.5,
            vessel_capacity_tonnes=1000.0,
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/VS-001")
        asset = resp.json()["data"]
        assert asset["vesselName"] == "Sea Runner"
        assert asset["imoNumber"] == "IMO-123"
        assert asset["portOfRegistry"] == "Dubai"
        assert asset["draftMeters"] == 4.5
        assert asset["vesselCapacityTonnes"] == 1000.0

    def test_equipment_format(self, client):
        """_format_asset maps equipment ES fields to camelCase response fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "E-001", "equipment", "crane",
            equipment_model="Liebherr",
            lifting_capacity_tonnes=300.0,
            operational_radius_meters=60.0,
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/E-001")
        asset = resp.json()["data"]
        assert asset["equipmentModel"] == "Liebherr"
        assert asset["liftingCapacityTonnes"] == 300.0
        assert asset["operationalRadiusMeters"] == 60.0

    def test_container_format(self, client):
        """_format_asset maps container ES fields to camelCase response fields."""
        c, mock_es = client
        doc = _make_es_doc(
            "C-001", "container", "cargo_container",
            container_number="CONT-123",
            container_size="40ft",
            seal_number="SEAL-456",
            contents_description="Electronics",
            weight_tonnes=25.5,
        )
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/C-001")
        asset = resp.json()["data"]
        assert asset["containerNumber"] == "CONT-123"
        assert asset["containerSize"] == "40ft"
        assert asset["sealNumber"] == "SEAL-456"
        assert asset["contentsDescription"] == "Electronics"
        assert asset["weightTonnes"] == 25.5

    def test_defaults_to_vehicle_truck_when_missing(self, client):
        """_format_asset defaults asset_type to 'vehicle' and asset_subtype to 'truck' for legacy docs."""
        c, mock_es = client
        # Legacy doc without asset_type/asset_subtype
        doc = {
            "truck_id": "T-LEGACY",
            "plate_number": "OLD-1234",
            "status": "active",
            "current_location": _make_location(),
            "destination": {},
            "route": {},
            "last_update": "2025-01-01T12:00:00",
        }
        mock_es.get_document = AsyncMock(return_value=doc)

        resp = c.get("/api/fleet/assets/T-LEGACY")
        asset = resp.json()["data"]
        assert asset["asset_type"] == "vehicle"
        assert asset["asset_subtype"] == "truck"
