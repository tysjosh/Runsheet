"""
Unit tests for AI fuel tools.

Validates:
- Requirement 7.1: search_fuel_stations tool returns structured results
- Requirement 7.2: get_fuel_summary tool returns structured results
- Requirement 7.3: get_fuel_consumption_history tool returns structured results
- Requirement 7.4: generate_fuel_report tool returns structured results
- Requirement 7.5: Tenant scoping enforcement (tenant_id in ES queries)
- Requirement 7.6: Read-only mode (no mutations)
"""

import sys
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock elasticsearch_service before any transitive import can trigger it.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools.fuel_tools import (  # noqa: E402
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_station(station_id="STN-001", fuel_type="AGO", status="normal", **overrides):
    """Build a minimal fuel station ES document."""
    doc = {
        "station_id": station_id,
        "name": f"Station {station_id}",
        "fuel_type": fuel_type,
        "capacity_liters": 50000,
        "current_stock_liters": 35000,
        "daily_consumption_rate": 1200.0,
        "days_until_empty": 29.2,
        "alert_threshold_pct": 20.0,
        "status": status,
        "location_name": "Industrial Area",
        "tenant_id": "tenant-1",
        "last_updated": "2025-01-15T10:00:00Z",
    }
    doc.update(overrides)
    return doc


def _make_consumption_event(event_id="EVT-001", station_id="STN-001", **overrides):
    """Build a minimal consumption event ES document."""
    doc = {
        "event_id": event_id,
        "station_id": station_id,
        "event_type": "consumption",
        "fuel_type": "AGO",
        "quantity_liters": 150.0,
        "asset_id": "TRUCK-001",
        "operator_id": "OP-001",
        "tenant_id": "tenant-1",
        "event_timestamp": "2025-01-15T08:30:00Z",
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


def _es_agg_response(total_stations=5, total_capacity=250000, total_stock=175000,
                     total_daily=6000, avg_days=29.2, status_buckets=None):
    """Build an ES aggregation response for fuel summary."""
    if status_buckets is None:
        status_buckets = [
            {"key": "normal", "doc_count": 3},
            {"key": "low", "doc_count": 1},
            {"key": "critical", "doc_count": 1},
        ]
    return {
        "hits": {
            "total": {"value": total_stations},
            "hits": [],
        },
        "aggregations": {
            "total_capacity": {"value": total_capacity},
            "total_stock": {"value": total_stock},
            "total_daily_consumption": {"value": total_daily},
            "avg_days_until_empty": {"value": avg_days},
            "by_status": {"buckets": status_buckets},
        },
    }


def _es_report_summary_response():
    """Build an ES aggregation response for generate_fuel_report summary query."""
    return {
        "hits": {"total": {"value": 3}, "hits": []},
        "aggregations": {
            "total_capacity": {"value": 150000},
            "total_stock": {"value": 90000},
            "total_daily_consumption": {"value": 3000},
            "avg_days_until_empty": {"value": 30.0},
            "by_status": {
                "buckets": [
                    {"key": "normal", "doc_count": 2},
                    {"key": "low", "doc_count": 1},
                ]
            },
            "by_fuel_type": {
                "buckets": [
                    {"key": "AGO", "stock": {"value": 60000}, "capacity": {"value": 100000}},
                    {"key": "PMS", "stock": {"value": 30000}, "capacity": {"value": 50000}},
                ]
            },
        },
    }


def _es_report_consumption_response():
    """Build an ES aggregation response for generate_fuel_report consumption query."""
    return {
        "hits": {"total": {"value": 0}, "hits": []},
        "aggregations": {
            "total_consumed": {"value": 21000},
            "by_fuel_type": {
                "buckets": [
                    {"key": "AGO", "consumed": {"value": 15000}},
                    {"key": "PMS", "consumed": {"value": 6000}},
                ]
            },
            "daily_trend": {
                "buckets": [
                    {"key_as_string": "2025-01-14T00:00:00.000Z", "consumed": {"value": 3000}},
                    {"key_as_string": "2025-01-15T00:00:00.000Z", "consumed": {"value": 3500}},
                ]
            },
        },
    }


# ===========================================================================
# search_fuel_stations tests (Requirement 7.1)
# ===========================================================================

class TestSearchFuelStations:
    """Test search_fuel_stations returns structured results and enforces scoping."""

    @pytest.mark.asyncio
    async def test_returns_formatted_text_with_station_data(self):
        """Tool returns formatted text containing station details when results exist."""
        station = _make_station("STN-001", "AGO", "normal")
        mock_response = _es_search_response([station])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await search_fuel_stations(query="Industrial", tenant_id="tenant-1")

            assert isinstance(result, str)
            assert "STN-001" in result
            assert "AGO" in result
            assert "Industrial Area" in result
            assert "normal" in result

    @pytest.mark.asyncio
    async def test_empty_results_returns_no_stations_message(self):
        """Tool returns a descriptive message when no stations match."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await search_fuel_stations(query="nonexistent", tenant_id="tenant-1")

            assert "No fuel stations found" in result

    @pytest.mark.asyncio
    async def test_tenant_scoping_in_query(self):
        """Tenant ID is included as a filter in the ES query."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await search_fuel_stations(query="test", tenant_id="my-tenant")

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            query_str = json.dumps(es_query)
            assert '"tenant_id"' in query_str
            assert '"my-tenant"' in query_str

    @pytest.mark.asyncio
    async def test_read_only_no_mutations(self):
        """search_fuel_stations does not call any write operations on ES."""
        station = _make_station()
        mock_response = _es_search_response([station])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)
            mock_es.index_document = AsyncMock()
            mock_es.update_document = AsyncMock()
            mock_es.delete_document = AsyncMock()

            await search_fuel_stations(query="test", tenant_id="tenant-1")

            mock_es.index_document.assert_not_called()
            mock_es.update_document.assert_not_called()
            mock_es.delete_document.assert_not_called()


    @pytest.mark.asyncio
    async def test_fuel_type_and_status_filters_in_query(self):
        """Optional fuel_type and status filters are included in the ES query."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await search_fuel_stations(
                query="test", fuel_type="PMS", status="low", tenant_id="tenant-1"
            )

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            query_str = json.dumps(es_query)
            assert '"PMS"' in query_str
            assert '"low"' in query_str


# ===========================================================================
# get_fuel_summary tests (Requirement 7.2)
# ===========================================================================

class TestGetFuelSummary:
    """Test get_fuel_summary returns structured network-wide summary."""

    @pytest.mark.asyncio
    async def test_returns_formatted_summary_text(self):
        """Tool returns formatted text with network summary metrics."""
        mock_response = _es_agg_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await get_fuel_summary(tenant_id="tenant-1")

            assert isinstance(result, str)
            assert "Fuel Network Summary" in result
            assert "Total Stations: 5" in result
            assert "250,000" in result  # total capacity
            assert "Normal" in result
            assert "Low" in result
            assert "Critical" in result

    @pytest.mark.asyncio
    async def test_tenant_scoping_in_query(self):
        """Tenant ID is included in the ES query."""
        mock_response = _es_agg_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await get_fuel_summary(tenant_id="my-tenant")

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            query_str = json.dumps(es_query)
            assert '"tenant_id"' in query_str
            assert '"my-tenant"' in query_str

    @pytest.mark.asyncio
    async def test_read_only_no_mutations(self):
        """get_fuel_summary does not call any write operations on ES."""
        mock_response = _es_agg_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)
            mock_es.index_document = AsyncMock()
            mock_es.update_document = AsyncMock()
            mock_es.delete_document = AsyncMock()

            await get_fuel_summary(tenant_id="tenant-1")

            mock_es.index_document.assert_not_called()
            mock_es.update_document.assert_not_called()
            mock_es.delete_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_empty_aggregations(self):
        """Tool handles empty aggregation results gracefully."""
        mock_response = _es_agg_response(
            total_stations=0, total_capacity=0, total_stock=0,
            total_daily=0, avg_days=0, status_buckets=[]
        )

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await get_fuel_summary(tenant_id="tenant-1")

            assert isinstance(result, str)
            assert "Total Stations: 0" in result


# ===========================================================================
# get_fuel_consumption_history tests (Requirement 7.3)
# ===========================================================================

class TestGetFuelConsumptionHistory:
    """Test get_fuel_consumption_history returns structured consumption data."""

    @pytest.mark.asyncio
    async def test_returns_formatted_consumption_events(self):
        """Tool returns formatted text with consumption event details."""
        event = _make_consumption_event()
        mock_response = _es_search_response([event])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await get_fuel_consumption_history(
                station_id="STN-001", days=7, tenant_id="tenant-1"
            )

            assert isinstance(result, str)
            assert "Fuel Consumption History" in result
            assert "150.0 L" in result
            assert "TRUCK-001" in result
            assert "STN-001" in result

    @pytest.mark.asyncio
    async def test_empty_results_returns_no_events_message(self):
        """Tool returns a descriptive message when no events found."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            result = await get_fuel_consumption_history(
                station_id="STN-999", days=7, tenant_id="tenant-1"
            )

            assert "No consumption events found" in result

    @pytest.mark.asyncio
    async def test_tenant_scoping_in_query(self):
        """Tenant ID is included in the ES query filter."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await get_fuel_consumption_history(tenant_id="my-tenant")

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            query_str = json.dumps(es_query)
            assert '"tenant_id"' in query_str
            assert '"my-tenant"' in query_str

    @pytest.mark.asyncio
    async def test_read_only_no_mutations(self):
        """get_fuel_consumption_history does not call any write operations on ES."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)
            mock_es.index_document = AsyncMock()
            mock_es.update_document = AsyncMock()
            mock_es.delete_document = AsyncMock()

            await get_fuel_consumption_history(tenant_id="tenant-1")

            mock_es.index_document.assert_not_called()
            mock_es.update_document.assert_not_called()
            mock_es.delete_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_station_and_asset_filters_in_query(self):
        """Optional station_id and asset_id filters are included in the ES query."""
        mock_response = _es_search_response([])

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(return_value=mock_response)

            await get_fuel_consumption_history(
                station_id="STN-001", asset_id="TRUCK-005", days=14, tenant_id="tenant-1"
            )

            call_args = mock_es.search_documents.call_args
            es_query = call_args[0][1]
            query_str = json.dumps(es_query)
            assert '"STN-001"' in query_str
            assert '"TRUCK-005"' in query_str


# ===========================================================================
# generate_fuel_report tests (Requirement 7.4)
# ===========================================================================

class TestGenerateFuelReport:
    """Test generate_fuel_report returns a markdown report."""

    @pytest.mark.asyncio
    async def test_returns_markdown_report(self):
        """Tool returns a markdown-formatted report with key sections."""
        summary_resp = _es_report_summary_response()
        alerts_resp = _es_search_response([
            _make_station("STN-002", "AGO", "low", current_stock_liters=8000, days_until_empty=6.7)
        ])
        consumption_resp = _es_report_consumption_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(
                side_effect=[summary_resp, alerts_resp, consumption_resp]
            )

            result = await generate_fuel_report(days=7, tenant_id="tenant-1")

            assert isinstance(result, str)
            assert "Fuel Operations Report" in result
            assert "Network Overview" in result
            assert "Station Status" in result
            assert "Consumption Trends" in result
            assert "Active Alerts" in result
            assert "Recommendations" in result

    @pytest.mark.asyncio
    async def test_tenant_scoping_in_all_queries(self):
        """Tenant ID is included in all ES queries made by the report tool."""
        summary_resp = _es_report_summary_response()
        alerts_resp = _es_search_response([])
        consumption_resp = _es_report_consumption_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(
                side_effect=[summary_resp, alerts_resp, consumption_resp]
            )

            await generate_fuel_report(days=7, tenant_id="my-tenant")

            # All three ES calls should include tenant_id
            assert mock_es.search_documents.call_count == 3
            for call in mock_es.search_documents.call_args_list:
                es_query = call[0][1]
                query_str = json.dumps(es_query)
                assert '"tenant_id"' in query_str
                assert '"my-tenant"' in query_str

    @pytest.mark.asyncio
    async def test_read_only_no_mutations(self):
        """generate_fuel_report does not call any write operations on ES."""
        summary_resp = _es_report_summary_response()
        alerts_resp = _es_search_response([])
        consumption_resp = _es_report_consumption_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(
                side_effect=[summary_resp, alerts_resp, consumption_resp]
            )
            mock_es.index_document = AsyncMock()
            mock_es.update_document = AsyncMock()
            mock_es.delete_document = AsyncMock()

            await generate_fuel_report(days=7, tenant_id="tenant-1")

            mock_es.index_document.assert_not_called()
            mock_es.update_document.assert_not_called()
            mock_es.delete_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_includes_stock_by_fuel_type(self):
        """Report includes stock breakdown by fuel type."""
        summary_resp = _es_report_summary_response()
        alerts_resp = _es_search_response([])
        consumption_resp = _es_report_consumption_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(
                side_effect=[summary_resp, alerts_resp, consumption_resp]
            )

            result = await generate_fuel_report(days=7, tenant_id="tenant-1")

            assert "Stock by Fuel Type" in result
            assert "AGO" in result
            assert "PMS" in result

    @pytest.mark.asyncio
    async def test_report_handles_no_alerts(self):
        """Report handles case where no stations have alerts."""
        summary_resp = _es_report_summary_response()
        # Override status buckets to all normal
        summary_resp["aggregations"]["by_status"]["buckets"] = [
            {"key": "normal", "doc_count": 3}
        ]
        alerts_resp = _es_search_response([])
        consumption_resp = _es_report_consumption_response()

        with patch("Agents.tools.fuel_tools.elasticsearch_service") as mock_es, \
             patch("Agents.tools.fuel_tools.get_telemetry_service", return_value=None):
            mock_es.search_documents = AsyncMock(
                side_effect=[summary_resp, alerts_resp, consumption_resp]
            )

            result = await generate_fuel_report(days=7, tenant_id="tenant-1")

            assert "No active alerts" in result
