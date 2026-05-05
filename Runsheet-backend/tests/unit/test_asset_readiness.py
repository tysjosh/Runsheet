"""
Unit tests for AssetReadinessChecker.

Tests cover:
- ReadinessStatus enum values
- PartAvailability and ReadinessResult dataclasses
- check_readiness classification logic (READY, WARNING, CRITICAL, BLOCKED)
- Fail-open behavior on ES errors
- Tenant blocking policy enforcement
- Empty inventory returns READY

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from inventory.asset_readiness import (
    AssetReadinessChecker,
    PartAvailability,
    ReadinessResult,
    ReadinessStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_es_hit(item_id, name, category, status, quantity, min_threshold, location):
    """Helper to create an ES hit document."""
    return {
        "_source": {
            "item_id": item_id,
            "name": name,
            "category": category,
            "status": status,
            "quantity": quantity,
            "min_threshold": min_threshold,
            "location": location,
        }
    }


def _make_es_response(hits):
    """Wrap hits in an ES search response structure."""
    return {
        "hits": {
            "total": {"value": len(hits)},
            "hits": hits,
        }
    }


@pytest.fixture
def mock_es_service():
    """Create a mock ES service."""
    es = AsyncMock()
    return es


@pytest.fixture
def mock_tenant_config():
    """Create a mock tenant config service."""
    config = AsyncMock()
    config.get_block_on_critical_shortage = AsyncMock(return_value=False)
    return config


# ---------------------------------------------------------------------------
# ReadinessStatus enum tests
# ---------------------------------------------------------------------------


class TestReadinessStatus:
    """Test ReadinessStatus enum values."""

    def test_ready_value(self):
        assert ReadinessStatus.READY == "ready"

    def test_warning_value(self):
        assert ReadinessStatus.WARNING == "warning"

    def test_critical_value(self):
        assert ReadinessStatus.CRITICAL == "critical"

    def test_blocked_value(self):
        assert ReadinessStatus.BLOCKED == "blocked"

    def test_is_string_enum(self):
        assert isinstance(ReadinessStatus.READY, str)


# ---------------------------------------------------------------------------
# Classification logic tests
# ---------------------------------------------------------------------------


class TestCheckReadinessClassification:
    """Test readiness classification logic."""

    @pytest.mark.asyncio
    async def test_all_in_stock_returns_ready(self, mock_es_service, mock_tenant_config):
        """When all critical parts are in_stock, status should be READY."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "in_stock", 10, 3, "Depot A"),
            _make_es_hit("INV_002", "Brake Pads", "brake_parts", "in_stock", 8, 2, "Depot A"),
            _make_es_hit("INV_003", "Oil Filter", "engine_parts", "in_stock", 15, 5, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.READY
        assert result.blocked is False
        assert result.block_reason is None
        assert len(result.parts_checked) == 3
        assert len(result.missing_parts) == 0
        assert len(result.low_parts) == 0

    @pytest.mark.asyncio
    async def test_some_low_stock_returns_warning(self, mock_es_service, mock_tenant_config):
        """When some parts are low_stock but none out_of_stock, status should be WARNING."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "in_stock", 10, 3, "Depot A"),
            _make_es_hit("INV_002", "Brake Pads", "brake_parts", "low_stock", 2, 5, "Depot A"),
            _make_es_hit("INV_003", "Oil Filter", "engine_parts", "in_stock", 15, 5, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.WARNING
        assert result.blocked is False
        assert len(result.low_parts) == 1
        assert result.low_parts[0].item_id == "INV_002"
        assert len(result.missing_parts) == 0

    @pytest.mark.asyncio
    async def test_some_out_of_stock_returns_critical(self, mock_es_service, mock_tenant_config):
        """When some parts are out_of_stock, status should be CRITICAL."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
            _make_es_hit("INV_002", "Brake Pads", "brake_parts", "in_stock", 8, 2, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.CRITICAL
        assert result.blocked is False
        assert len(result.missing_parts) == 1
        assert result.missing_parts[0].item_id == "INV_001"

    @pytest.mark.asyncio
    async def test_out_of_stock_takes_precedence_over_low_stock(
        self, mock_es_service, mock_tenant_config
    ):
        """When both low_stock and out_of_stock exist, status should be CRITICAL."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
            _make_es_hit("INV_002", "Brake Pads", "brake_parts", "low_stock", 1, 5, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.CRITICAL
        assert len(result.missing_parts) == 1
        assert len(result.low_parts) == 1

    @pytest.mark.asyncio
    async def test_empty_inventory_returns_ready(self, mock_es_service, mock_tenant_config):
        """When no critical parts exist for the asset type, status should be READY."""
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response([]))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.READY
        assert len(result.parts_checked) == 0
        assert len(result.missing_parts) == 0
        assert len(result.low_parts) == 0


# ---------------------------------------------------------------------------
# Tenant blocking policy tests
# ---------------------------------------------------------------------------


class TestBlockOnCriticalShortage:
    """Test tenant blocking policy enforcement."""

    @pytest.mark.asyncio
    async def test_blocked_when_policy_enabled_and_parts_out_of_stock(
        self, mock_es_service, mock_tenant_config
    ):
        """When tenant has blocking enabled and parts are out_of_stock, status is BLOCKED."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))
        mock_tenant_config.get_block_on_critical_shortage = AsyncMock(return_value=True)

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.BLOCKED
        assert result.blocked is True
        assert result.block_reason is not None
        assert "Tire Set" in result.block_reason

    @pytest.mark.asyncio
    async def test_not_blocked_when_policy_disabled(self, mock_es_service, mock_tenant_config):
        """When tenant has blocking disabled, status is CRITICAL (not BLOCKED)."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))
        mock_tenant_config.get_block_on_critical_shortage = AsyncMock(return_value=False)

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.CRITICAL
        assert result.blocked is False
        assert result.block_reason is None

    @pytest.mark.asyncio
    async def test_not_blocked_when_no_tenant_config_service(self, mock_es_service):
        """When no tenant config service is provided, blocking is never applied."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))

        checker = AssetReadinessChecker(mock_es_service, tenant_config_service=None)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.CRITICAL
        assert result.blocked is False

    @pytest.mark.asyncio
    async def test_not_blocked_when_policy_check_fails(self, mock_es_service, mock_tenant_config):
        """When tenant config check raises, defaults to not blocked (fail-open)."""
        hits = [
            _make_es_hit("INV_001", "Tire Set", "tires", "out_of_stock", 0, 3, "Depot A"),
        ]
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response(hits))
        mock_tenant_config.get_block_on_critical_shortage = AsyncMock(
            side_effect=Exception("Redis down")
        )

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.CRITICAL
        assert result.blocked is False


# ---------------------------------------------------------------------------
# Fail-open behavior tests
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Test fail-open behavior on ES errors."""

    @pytest.mark.asyncio
    async def test_es_timeout_returns_ready(self, mock_es_service, mock_tenant_config):
        """When ES query times out, should return READY with empty parts."""
        mock_es_service.search_documents = AsyncMock(
            side_effect=Exception("Connection timeout")
        )

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.READY
        assert len(result.parts_checked) == 0
        assert len(result.missing_parts) == 0
        assert len(result.low_parts) == 0
        assert result.blocked is False

    @pytest.mark.asyncio
    async def test_es_unavailable_returns_ready(self, mock_es_service, mock_tenant_config):
        """When ES is unavailable, should return READY with empty parts."""
        mock_es_service.search_documents = AsyncMock(
            side_effect=ConnectionError("ES cluster unavailable")
        )

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        result = await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        assert result.status == ReadinessStatus.READY
        assert len(result.parts_checked) == 0


# ---------------------------------------------------------------------------
# ES query construction tests
# ---------------------------------------------------------------------------


class TestQueryConstruction:
    """Test that the ES query is constructed correctly."""

    @pytest.mark.asyncio
    async def test_query_uses_correct_categories(self, mock_es_service, mock_tenant_config):
        """Query should filter by CRITICAL_CATEGORIES."""
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response([]))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        await checker.check_readiness("TRUCK_01", "vehicle", "tenant-1")

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]  # second positional arg is the query
        bool_must = query["query"]["bool"]["must"]

        # Find the terms clause for category
        category_clause = next(
            (c for c in bool_must if "terms" in c and "category" in c["terms"]),
            None,
        )
        assert category_clause is not None
        assert set(category_clause["terms"]["category"]) == {
            "tires",
            "brake_parts",
            "engine_parts",
        }

    @pytest.mark.asyncio
    async def test_query_filters_by_asset_type(self, mock_es_service, mock_tenant_config):
        """Query should filter by compatible_assets matching asset_type."""
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response([]))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        await checker.check_readiness("TRUCK_01", "heavy_truck", "tenant-1")

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]
        bool_must = query["query"]["bool"]["must"]

        # Find the term clause for compatible_assets
        asset_clause = next(
            (c for c in bool_must if "term" in c and "compatible_assets" in c["term"]),
            None,
        )
        assert asset_clause is not None
        assert asset_clause["term"]["compatible_assets"] == "heavy_truck"

    @pytest.mark.asyncio
    async def test_query_scoped_to_tenant(self, mock_es_service, mock_tenant_config):
        """Query should be scoped to the given tenant_id."""
        mock_es_service.search_documents = AsyncMock(return_value=_make_es_response([]))

        checker = AssetReadinessChecker(mock_es_service, mock_tenant_config)
        await checker.check_readiness("TRUCK_01", "vehicle", "tenant-xyz")

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]
        bool_must = query["query"]["bool"]["must"]

        # Find the term clause for tenant_id
        tenant_clause = next(
            (c for c in bool_must if "term" in c and "tenant_id" in c["term"]),
            None,
        )
        assert tenant_clause is not None
        assert tenant_clause["term"]["tenant_id"] == "tenant-xyz"
