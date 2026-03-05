"""
Unit tests for POST /api/fleet/assets endpoint.

Validates:
- Requirement 6.1: POST /api/fleet/assets registers a new asset
- Requirement 6.2: Validates asset_type and asset_subtype enums
- Requirement 6.4: Vehicle assets require plate_number
- Requirement 6.5: Vessel assets require vessel_name
- Requirement 6.6: Container assets require container_number
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client with mocked ES service."""
    with patch("data_endpoints.elasticsearch_service") as mock_es:
        mock_es.index_document = AsyncMock(return_value={"result": "created"})
        from main import app
        with TestClient(app) as c:
            yield c, mock_es


class TestCreateFleetAsset:
    """Tests for POST /api/fleet/assets endpoint."""

    def _make_location(self, address="Dubai"):
        """Helper to create a valid Location payload."""
        return {
            "id": "LOC-001",
            "name": address,
            "type": "warehouse",
            "coordinates": {"lat": 25.0, "lng": 55.0},
            "address": address,
        }

    def test_create_vehicle_asset_success(self, client):
        """A valid vehicle asset with plate_number should be created successfully."""
        c, mock_es = client
        payload = {
            "asset_id": "V-001",
            "asset_type": "vehicle",
            "asset_subtype": "truck",
            "name": "Truck Alpha",
            "status": "active",
            "current_location": self._make_location("Dubai"),
            "plate_number": "ABC-1234",
            "driver_id": "D-001",
            "driver_name": "John Doe",
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["id"] == "V-001"
        assert data["data"]["asset_type"] == "vehicle"
        assert data["data"]["asset_subtype"] == "truck"
        assert data["data"]["plateNumber"] == "ABC-1234"
        # Verify ES was called with the right index and doc ID
        mock_es.index_document.assert_called_once()
        call_args = mock_es.index_document.call_args
        assert call_args[0][0] == "trucks"  # index name
        assert call_args[0][1] == "V-001"   # doc ID

    def test_create_vessel_asset_success(self, client):
        """A valid vessel asset with vessel_name should be created successfully."""
        c, mock_es = client
        payload = {
            "asset_id": "VS-001",
            "asset_type": "vessel",
            "asset_subtype": "boat",
            "name": "Sea Runner",
            "status": "active",
            "current_location": self._make_location("Port"),
            "vessel_name": "Sea Runner",
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["asset_type"] == "vessel"
        assert data["data"]["vesselName"] == "Sea Runner"

    def test_create_container_asset_success(self, client):
        """A valid container asset with container_number should be created successfully."""
        c, mock_es = client
        payload = {
            "asset_id": "C-001",
            "asset_type": "container",
            "asset_subtype": "cargo_container",
            "name": "Container X",
            "status": "active",
            "current_location": self._make_location("Yard"),
            "container_number": "CONT-12345",
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["containerNumber"] == "CONT-12345"

    def test_create_equipment_asset_success(self, client):
        """Equipment assets don't require extra fields beyond the base ones."""
        c, mock_es = client
        payload = {
            "asset_id": "E-001",
            "asset_type": "equipment",
            "asset_subtype": "crane",
            "name": "Crane 7",
            "status": "active",
            "current_location": self._make_location("Site A"),
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["asset_type"] == "equipment"
        assert data["data"]["asset_subtype"] == "crane"

    def test_create_vehicle_missing_plate_number_fails(self, client):
        """Vehicle assets without plate_number should be rejected (422)."""
        c, _ = client
        payload = {
            "asset_id": "V-002",
            "asset_type": "vehicle",
            "asset_subtype": "truck",
            "name": "No Plate Truck",
            "current_location": self._make_location("Dubai"),
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 422

    def test_create_vessel_missing_vessel_name_fails(self, client):
        """Vessel assets without vessel_name should be rejected (422)."""
        c, _ = client
        payload = {
            "asset_id": "VS-002",
            "asset_type": "vessel",
            "asset_subtype": "boat",
            "name": "Unnamed Boat",
            "current_location": self._make_location("Port"),
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 422

    def test_create_container_missing_container_number_fails(self, client):
        """Container assets without container_number should be rejected (422)."""
        c, _ = client
        payload = {
            "asset_id": "C-002",
            "asset_type": "container",
            "asset_subtype": "cargo_container",
            "name": "No Number Container",
            "current_location": self._make_location("Yard"),
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 422

    def test_invalid_subtype_for_type_fails(self, client):
        """A subtype that doesn't match the asset_type should be rejected (422)."""
        c, _ = client
        payload = {
            "asset_id": "X-001",
            "asset_type": "vehicle",
            "asset_subtype": "boat",  # boat is a vessel subtype, not vehicle
            "name": "Bad Combo",
            "current_location": self._make_location("Nowhere"),
            "plate_number": "XYZ-999",
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 422

    def test_invalid_asset_type_fails(self, client):
        """An unrecognized asset_type should be rejected (422)."""
        c, _ = client
        payload = {
            "asset_id": "X-002",
            "asset_type": "spaceship",
            "asset_subtype": "truck",
            "name": "Bad Type",
            "current_location": self._make_location("Space"),
        }
        resp = c.post("/api/fleet/assets", json=payload)
        assert resp.status_code == 422
