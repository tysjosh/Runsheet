"""
Unit tests for extended AI tools (multi-asset support).

Validates:
- Requirement 5.1: search_fleet_data accepts optional asset_type parameter
- Requirement 5.2: get_fleet_summary returns counts broken down by asset type
- Requirement 5.3: find_truck_by_id finds any asset by ID regardless of type
- Requirement 5.4: AI agent supports multi-asset queries
"""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock elasticsearch_service before any transitive import can trigger it.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools.search_tools import search_fleet_data  # noqa: E402
from Agents.tools.summary_tools import get_fleet_summary  # noqa: E402
from Agents.tools.lookup_tools import find_truck_by_id  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_location(name="Dubai Warehouse"):
    return {
        "id": "LOC-001",
        "name": name,
        "type": "warehouse",
        "coordinates": {"lat": 25.0, "lng": 55.0},
        "address": name,
    }


def _make_asset_doc(asset_id, asset_type="vehicle", asset_subtype="truck", **overrides):
    """Build a minimal ES document for a given asset type."""
    doc = {
        "truck_id": asset_id,
        "asset_id": asset_id,
        "asset_type": asset_type,
        "asset_subtype": asset_subtype,
        "asset_name": overrides.pop("asset_name", f"Asset {asset_id}"),
        "status": overrides.pop("status", "on_time"),
        "current_location": _make_location(),
        "destination": {"name": "Port Rashid"},
        "route": {},
        "last_update": "2025-01-01T12:00:00",
        "estimated_arrival": "2025-01-01T14:00:00",
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


def _es_agg_response(by_type_buckets, by_subtype_buckets):
    """Build an ES aggregation response with type/subtype buckets."""
    return {
        "hits": {"total": {"value": 0}, "hits": []},
        "aggregations": {
            "by_type": {"buckets": by_type_buckets},
            "by_subtype": {"buckets": by_subtype_buckets},
        },
    }


# ===========================================================================
# search_fleet_data tests (Requirement 5.1)
# ===========================================================================

class TestSearchFleetDataAssetType:
    """Test search_fleet_data with asset_type filter."""

    @pytest.mark.asyncio
    async def test_asset_type_filter_adds_term_query(self):
        """When asset_type is provided, ES query includes a term filter for asset_type."""
        truck = _make_asset_doc("T-001", "vehicle", "truck", plate_number="GI-58A")
        mock_response = _es_search_response([truck])

        with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es:
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await search_fleet_data(query="delayed", asset_type="vehicle")

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]  # second positional arg is the query body
            # Should have a bool query with a filter containing asset_type term
            assert "bool" in es_query["query"]
            filters = es_query["query"]["bool"]["filter"]
            asset_type_filter = filters[0]
            assert asset_type_filter == {"term": {"asset_type": "vehicle"}}

    @pytest.mark.asyncio
    async def test_no_asset_type_omits_filter(self):
        """When asset_type is None, no filter is applied — query is a plain multi_match."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es:
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await search_fleet_data(query="delayed", asset_type=None)

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            # Should be a plain multi_match, no bool wrapper
            assert "multi_match" in es_query["query"]
            assert "bool" not in es_query["query"]

    @pytest.mark.asyncio
    async def test_response_includes_asset_type_labels(self):
        """Response text includes asset_type and asset_subtype labels for each result."""
        vessel = _make_asset_doc(
            "V-001", "vessel", "boat",
            asset_name="Sea Falcon", vessel_name="Sea Falcon",
        )
        mock_response = _es_search_response([vessel])

        with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es:
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await search_fleet_data(query="Sea Falcon", asset_type="vessel")

            assert "vessel/boat" in result
            assert "Sea Falcon" in result


# ===========================================================================
# get_fleet_summary tests (Requirement 5.2)
# ===========================================================================

class TestGetFleetSummaryTypeBreakdowns:
    """Test get_fleet_summary includes type breakdowns."""

    @pytest.mark.asyncio
    async def test_response_includes_assets_by_type(self):
        """Response includes 'Assets by Type' section when aggregation returns buckets."""
        trucks = [
            _make_asset_doc("T-001", "vehicle", "truck", status="on_time"),
        ]
        agg_response = _es_agg_response(
            by_type_buckets=[
                {"key": "vehicle", "doc_count": 5},
                {"key": "vessel", "doc_count": 3},
            ],
            by_subtype_buckets=[],
        )

        with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=trucks)
            mock_es.search_documents = AsyncMock(return_value=agg_response)

            result = await get_fleet_summary()

            assert "Assets by Type" in result
            assert "vehicle: 5" in result
            assert "vessel: 3" in result

    @pytest.mark.asyncio
    async def test_response_includes_assets_by_subtype(self):
        """Response includes 'Assets by Subtype' section."""
        trucks = [
            _make_asset_doc("T-001", "vehicle", "truck", status="on_time"),
        ]
        agg_response = _es_agg_response(
            by_type_buckets=[],
            by_subtype_buckets=[
                {"key": "truck", "doc_count": 4},
                {"key": "boat", "doc_count": 2},
                {"key": "crane", "doc_count": 1},
            ],
        )

        with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=trucks)
            mock_es.search_documents = AsyncMock(return_value=agg_response)

            result = await get_fleet_summary()

            assert "Assets by Subtype" in result
            assert "truck: 4" in result
            assert "boat: 2" in result
            assert "crane: 1" in result

    @pytest.mark.asyncio
    async def test_gracefully_handles_aggregation_failure(self):
        """When aggregation query fails, summary still returns basic truck info."""
        trucks = [
            _make_asset_doc("T-001", "vehicle", "truck", status="on_time"),
            _make_asset_doc("T-002", "vehicle", "truck", status="delayed"),
        ]

        with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=trucks)
            mock_es.search_documents = AsyncMock(side_effect=Exception("ES agg error"))

            result = await get_fleet_summary()

            # Should still contain basic fleet summary despite agg failure
            assert "Fleet Summary" in result
            assert "Total Trucks: 2" in result
            # Should NOT contain type breakdown sections
            assert "Assets by Type" not in result


# ===========================================================================
# find_truck_by_id tests (Requirement 5.3)
# ===========================================================================

class TestFindTruckByIdMultiAsset:
    """Test find_truck_by_id finds non-truck assets."""

    @pytest.mark.asyncio
    async def test_find_vessel_by_vessel_name(self):
        """Can find a vessel by vessel_name."""
        vessel = _make_asset_doc(
            "V-001", "vessel", "boat",
            asset_name="Sea Falcon", vessel_name="Sea Falcon",
            imo_number="IMO1234567", port_of_registry="Dubai",
            draft_meters=5.2, vessel_capacity_tonnes=1200.0,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[vessel])

            result = await find_truck_by_id(truck_identifier="Sea Falcon")

            assert "Sea Falcon" in result
            assert "vessel" in result
            assert "boat" in result

    @pytest.mark.asyncio
    async def test_find_equipment_by_model(self):
        """Can find equipment by equipment_model."""
        crane = _make_asset_doc(
            "E-001", "equipment", "crane",
            asset_name="Crane 7", equipment_model="Crane 7",
            lifting_capacity_tonnes=50.0, operational_radius_meters=30.0,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[crane])

            result = await find_truck_by_id(truck_identifier="Crane 7")

            assert "Crane 7" in result
            assert "equipment" in result
            assert "crane" in result

    @pytest.mark.asyncio
    async def test_find_container_by_container_number(self):
        """Can find a container by container_number."""
        container = _make_asset_doc(
            "C-001", "container", "cargo_container",
            asset_name="CONT-123", container_number="CONT-123",
            container_size="40ft", seal_number="SEAL-456",
            contents_description="Electronics", weight_tonnes=18.5,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[container])

            result = await find_truck_by_id(truck_identifier="CONT-123")

            assert "CONT-123" in result
            assert "container" in result
            assert "cargo_container" in result

    @pytest.mark.asyncio
    async def test_response_includes_asset_type_and_subtype(self):
        """Response includes asset_type and asset_subtype for any found asset."""
        vessel = _make_asset_doc(
            "V-002", "vessel", "barge",
            asset_name="River Barge 3", vessel_name="River Barge 3",
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[vessel])

            result = await find_truck_by_id(truck_identifier="River Barge 3")

            assert "vessel" in result
            assert "barge" in result
            # The response format includes "Type: vessel / barge"
            assert "vessel / barge" in result

    @pytest.mark.asyncio
    async def test_vessel_shows_vessel_specific_details(self):
        """Vessel response shows vessel-specific fields (IMO, port, draft, capacity)."""
        vessel = _make_asset_doc(
            "V-001", "vessel", "boat",
            asset_name="Sea Falcon", vessel_name="Sea Falcon",
            imo_number="IMO1234567", port_of_registry="Dubai",
            draft_meters=5.2, vessel_capacity_tonnes=1200.0,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[vessel])

            result = await find_truck_by_id(truck_identifier="Sea Falcon")

            assert "IMO1234567" in result
            assert "Dubai" in result
            assert "5.2" in result
            assert "1200.0" in result

    @pytest.mark.asyncio
    async def test_equipment_shows_equipment_specific_details(self):
        """Equipment response shows equipment-specific fields (model, capacity, radius)."""
        crane = _make_asset_doc(
            "E-001", "equipment", "crane",
            asset_name="Crane 7", equipment_model="Crane 7",
            lifting_capacity_tonnes=50.0, operational_radius_meters=30.0,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[crane])

            result = await find_truck_by_id(truck_identifier="Crane 7")

            assert "Crane 7" in result
            assert "50.0" in result
            assert "30.0" in result

    @pytest.mark.asyncio
    async def test_container_shows_container_specific_details(self):
        """Container response shows container-specific fields (number, size, seal, contents, weight)."""
        container = _make_asset_doc(
            "C-001", "container", "cargo_container",
            asset_name="CONT-123", container_number="CONT-123",
            container_size="40ft", seal_number="SEAL-456",
            contents_description="Electronics", weight_tonnes=18.5,
        )

        with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es:
            mock_es.get_all_documents = AsyncMock(return_value=[container])

            result = await find_truck_by_id(truck_identifier="CONT-123")

            assert "CONT-123" in result
            assert "40ft" in result
            assert "SEAL-456" in result
            assert "Electronics" in result
            assert "18.5" in result
