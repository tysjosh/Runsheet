"""
Unit tests for extended GPS ingestion service with multi-asset support.

Validates:
- Requirement 3.1: LocationUpdate accepts asset_id alongside truck_id
- Requirement 3.2: LocationUpdate accepts optional asset_type field
- Requirement 3.3: DataIngestionService uses asset_id for document lookups
- Requirement 3.4: Legacy truck_id treated as asset_id for backward compat
- Requirement 3.5: WebSocket broadcast includes asset_type and asset_subtype
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from ingestion.service import LocationUpdate, DataIngestionService, sanitize_location_update


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _make_update(**overrides):
    """Build a minimal LocationUpdate dict with sensible defaults."""
    data = {
        "latitude": 25.276987,
        "longitude": 55.296249,
        "timestamp": NOW.isoformat(),
    }
    data.update(overrides)
    return data


def _mock_es_service():
    """Create a mock ES service with common async methods."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [{"_source": {"truck_id": "T-001"}}], "total": {"value": 1}}
    })
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.get_document = AsyncMock(return_value={
        "truck_id": "T-001",
        "asset_type": "vehicle",
        "asset_subtype": "truck",
    })
    return es


def _mock_connection_manager():
    """Create a mock WebSocket connection manager."""
    cm = MagicMock()
    cm.broadcast_location_update = AsyncMock(return_value=3)
    return cm


# ---------------------------------------------------------------------------
# LocationUpdate model — asset_id field (Req 3.1)
# ---------------------------------------------------------------------------

class TestLocationUpdateAssetId:
    """Validates: Requirement 3.1"""

    def test_accepts_asset_id(self):
        """LocationUpdate accepts asset_id as the primary identifier."""
        update = LocationUpdate(**_make_update(asset_id="VESSEL-001"))
        assert update.asset_id == "VESSEL-001"

    def test_asset_id_without_truck_id(self):
        """LocationUpdate works with only asset_id (no truck_id)."""
        update = LocationUpdate(**_make_update(asset_id="CRANE-007"))
        assert update.asset_id == "CRANE-007"
        assert update.truck_id is None

    def test_both_asset_id_and_truck_id(self):
        """LocationUpdate accepts both asset_id and truck_id; asset_id takes precedence."""
        update = LocationUpdate(**_make_update(asset_id="A-001", truck_id="T-001"))
        assert update.asset_id == "A-001"
        assert update.truck_id == "T-001"


# ---------------------------------------------------------------------------
# LocationUpdate model — legacy truck_id backward compat (Req 3.4)
# ---------------------------------------------------------------------------

class TestLocationUpdateLegacyTruckId:
    """Validates: Requirement 3.4"""

    def test_truck_id_copies_to_asset_id(self):
        """When only truck_id is provided, it is copied to asset_id."""
        update = LocationUpdate(**_make_update(truck_id="TRUCK-042"))
        assert update.asset_id == "TRUCK-042"
        assert update.truck_id == "TRUCK-042"

    def test_legacy_format_still_works(self):
        """A payload with only truck_id (legacy format) is fully valid."""
        update = LocationUpdate(**_make_update(truck_id="T-LEGACY"))
        assert update.asset_id == "T-LEGACY"


# ---------------------------------------------------------------------------
# LocationUpdate model — requires either id (Req 3.1)
# ---------------------------------------------------------------------------

class TestLocationUpdateRequiresId:
    """Validates: Requirement 3.1"""

    def test_no_id_raises_error(self):
        """LocationUpdate raises ValueError when neither asset_id nor truck_id is provided."""
        with pytest.raises(Exception):
            LocationUpdate(**_make_update())

    def test_empty_asset_id_raises_error(self):
        """LocationUpdate rejects empty-string asset_id."""
        with pytest.raises(Exception):
            LocationUpdate(**_make_update(asset_id=""))

    def test_empty_truck_id_raises_error(self):
        """LocationUpdate rejects empty-string truck_id."""
        with pytest.raises(Exception):
            LocationUpdate(**_make_update(truck_id=""))


# ---------------------------------------------------------------------------
# LocationUpdate model — optional asset_type (Req 3.2)
# ---------------------------------------------------------------------------

class TestLocationUpdateAssetType:
    """Validates: Requirement 3.2"""

    def test_accepts_asset_type(self):
        """LocationUpdate accepts an optional asset_type field."""
        update = LocationUpdate(**_make_update(asset_id="VS-001", asset_type="vessel"))
        assert update.asset_type == "vessel"

    def test_asset_type_defaults_to_none(self):
        """asset_type defaults to None when not provided."""
        update = LocationUpdate(**_make_update(asset_id="A-001"))
        assert update.asset_type is None

    def test_asset_type_in_sanitized_output(self):
        """sanitize_location_update preserves asset_type in the output dict."""
        update = LocationUpdate(**_make_update(asset_id="E-001", asset_type="equipment"))
        sanitized = sanitize_location_update(update)
        assert sanitized["asset_type"] == "equipment"


# ---------------------------------------------------------------------------
# WebSocket broadcast includes asset_type and asset_subtype (Req 3.5)
# ---------------------------------------------------------------------------

class TestBroadcastIncludesAssetType:
    """Validates: Requirement 3.5"""

    @pytest.mark.asyncio
    async def test_broadcast_passes_asset_type_from_data(self):
        """_broadcast_location_update passes asset_type and asset_subtype to the connection manager."""
        es = _mock_es_service()
        cm = _mock_connection_manager()
        service = DataIngestionService(es_service=es, connection_manager=cm)

        sanitized = {
            "asset_id": "VS-001",
            "latitude": 25.0,
            "longitude": 55.0,
            "timestamp": NOW.isoformat(),
            "asset_type": "vessel",
            "asset_subtype": "boat",
        }

        await service._broadcast_location_update(sanitized)

        cm.broadcast_location_update.assert_called_once()
        call_kwargs = cm.broadcast_location_update.call_args
        # Check keyword args
        assert call_kwargs.kwargs.get("asset_type") == "vessel" or \
               call_kwargs[1].get("asset_type") == "vessel"
        assert call_kwargs.kwargs.get("asset_subtype") == "boat" or \
               call_kwargs[1].get("asset_subtype") == "boat"

    @pytest.mark.asyncio
    async def test_broadcast_looks_up_asset_type_from_es(self):
        """When asset_type is missing from the update, it is looked up from ES."""
        es = _mock_es_service()
        es.get_document = AsyncMock(return_value={
            "asset_type": "equipment",
            "asset_subtype": "crane",
        })
        cm = _mock_connection_manager()
        service = DataIngestionService(es_service=es, connection_manager=cm)

        sanitized = {
            "asset_id": "E-001",
            "latitude": 25.0,
            "longitude": 55.0,
            "timestamp": NOW.isoformat(),
            # No asset_type or asset_subtype in the update
        }

        await service._broadcast_location_update(sanitized)

        # Should have looked up from ES
        es.get_document.assert_called_once_with("assets", "E-001")
        # Should pass the looked-up values to broadcast
        call_kwargs = cm.broadcast_location_update.call_args
        assert call_kwargs.kwargs.get("asset_type") == "equipment" or \
               call_kwargs[1].get("asset_type") == "equipment"
        assert call_kwargs.kwargs.get("asset_subtype") == "crane" or \
               call_kwargs[1].get("asset_subtype") == "crane"

    @pytest.mark.asyncio
    async def test_broadcast_skipped_without_connection_manager(self):
        """_broadcast_location_update is a no-op when no connection manager is set."""
        es = _mock_es_service()
        service = DataIngestionService(es_service=es, connection_manager=None)

        sanitized = {
            "asset_id": "A-001",
            "latitude": 25.0,
            "longitude": 55.0,
        }

        # Should not raise
        await service._broadcast_location_update(sanitized)


# ---------------------------------------------------------------------------
# DataIngestionService.process_location_update uses asset_id (Req 3.3)
# ---------------------------------------------------------------------------

class TestProcessLocationUpdateUsesAssetId:
    """Validates: Requirement 3.3"""

    @pytest.mark.asyncio
    async def test_uses_asset_id_for_es_lookup(self):
        """process_location_update uses asset_id to verify asset existence."""
        es = _mock_es_service()
        service = DataIngestionService(es_service=es, connection_manager=None)

        update = LocationUpdate(**_make_update(asset_id="VESSEL-001"))
        result = await service.process_location_update(update)

        assert result.success is True
        # validate_asset_exists should have searched for the asset_id
        es.search_documents.assert_called()
        search_call = es.search_documents.call_args
        query = search_call[0][1]
        assert query["query"]["term"]["truck_id"] == "VESSEL-001"

    @pytest.mark.asyncio
    async def test_uses_asset_id_for_document_index(self):
        """process_location_update indexes the document using asset_id as doc_id."""
        es = _mock_es_service()
        service = DataIngestionService(es_service=es, connection_manager=None)

        update = LocationUpdate(**_make_update(asset_id="CRANE-007"))
        result = await service.process_location_update(update)

        assert result.success is True
        # Check that index_document was called with asset_id as the doc_id
        index_calls = es.index_document.call_args_list
        # First call is for the trucks index (location update)
        trucks_call = index_calls[0]
        assert trucks_call.kwargs.get("doc_id") == "CRANE-007" or \
               trucks_call[1].get("doc_id") == "CRANE-007" or \
               (len(trucks_call[0]) > 1 and trucks_call[0][1] == "CRANE-007")

    @pytest.mark.asyncio
    async def test_legacy_truck_id_resolves_to_asset_id(self):
        """process_location_update with truck_id uses it as asset_id for lookups."""
        es = _mock_es_service()
        service = DataIngestionService(es_service=es, connection_manager=None)

        update = LocationUpdate(**_make_update(truck_id="T-LEGACY"))
        result = await service.process_location_update(update)

        assert result.success is True
        # The model_validator copies truck_id → asset_id, so ES lookup uses "T-LEGACY"
        search_call = es.search_documents.call_args
        query = search_call[0][1]
        assert query["query"]["term"]["truck_id"] == "T-LEGACY"
