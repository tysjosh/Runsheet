"""
Unit tests for FuelService consumption analytics methods.

Validates: Requirements 5.1-5.5
- 5.1: Consumption aggregated by time bucket (hourly, daily, weekly)
- 5.2: Fuel efficiency per asset (liters per km)
- 5.3: Filtering by station_id, fuel_type, asset_id, date range
- 5.4: Network-wide fuel summary
- 5.5: Enforce daily bucket for time ranges > 90 days
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fuel.models import MetricsBucket, EfficiencyMetric, FuelNetworkSummary
from fuel.services.fuel_service import FuelService
from fuel.services.fuel_es_mappings import FUEL_STATIONS_INDEX, FUEL_EVENTS_INDEX


TENANT = "tenant-001"


@pytest.fixture
def settings_mock():
    with patch("fuel.services.fuel_service.get_settings") as mock:
        s = MagicMock()
        s.fuel_consumption_rolling_window_days = 7
        s.fuel_critical_days_threshold = 3
        mock.return_value = s
        yield s


def _mock_es_agg(agg_response):
    """Create a mock ES service returning the given aggregation response."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value=agg_response)
    return es


# ---------------------------------------------------------------------------
# Tests: get_consumption_metrics (Req 5.1, 5.3, 5.5)
# ---------------------------------------------------------------------------


class TestGetConsumptionMetrics:

    @pytest.mark.asyncio
    async def test_returns_daily_buckets(self, settings_mock):
        """Req 5.1: Returns consumption aggregated by daily bucket."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 5}, "hits": []},
            "aggregations": {
                "consumption_over_time": {
                    "buckets": [
                        {
                            "key_as_string": "2025-01-01T00:00:00.000Z",
                            "doc_count": 3,
                            "total_liters": {"value": 450.0},
                        },
                        {
                            "key_as_string": "2025-01-02T00:00:00.000Z",
                            "doc_count": 2,
                            "total_liters": {"value": 300.0},
                        },
                    ]
                }
            },
        })
        svc = FuelService(es)

        result = await svc.get_consumption_metrics(TENANT, bucket="daily")

        assert len(result) == 2
        assert isinstance(result[0], MetricsBucket)
        assert result[0].total_liters == 450.0
        assert result[0].event_count == 3
        assert result[1].total_liters == 300.0

    @pytest.mark.asyncio
    async def test_uses_correct_calendar_interval(self, settings_mock):
        """Req 5.1: Hourly/daily/weekly map to correct ES intervals."""
        for bucket, expected_interval in [("hourly", "1h"), ("daily", "1d"), ("weekly", "1w")]:
            es = _mock_es_agg({
                "hits": {"total": {"value": 0}, "hits": []},
                "aggregations": {"consumption_over_time": {"buckets": []}},
            })
            svc = FuelService(es)
            await svc.get_consumption_metrics(TENANT, bucket=bucket)

            query_body = es.search_documents.call_args[0][1]
            interval = query_body["aggs"]["consumption_over_time"]["date_histogram"]["calendar_interval"]
            assert interval == expected_interval, f"bucket={bucket} should use {expected_interval}"

    @pytest.mark.asyncio
    async def test_enforces_daily_for_range_over_90_days(self, settings_mock):
        """Req 5.5: Enforces daily bucket when date range > 90 days."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {"consumption_over_time": {"buckets": []}},
        })
        svc = FuelService(es)

        await svc.get_consumption_metrics(
            TENANT,
            bucket="hourly",
            start_date="2024-01-01T00:00:00+00:00",
            end_date="2024-06-01T00:00:00+00:00",  # > 90 days
        )

        query_body = es.search_documents.call_args[0][1]
        interval = query_body["aggs"]["consumption_over_time"]["date_histogram"]["calendar_interval"]
        assert interval == "1d"

    @pytest.mark.asyncio
    async def test_applies_station_and_fuel_type_filters(self, settings_mock):
        """Req 5.3: Filters by station_id and fuel_type."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {"consumption_over_time": {"buckets": []}},
        })
        svc = FuelService(es)

        await svc.get_consumption_metrics(
            TENANT, station_id="STATION-001", fuel_type="AGO"
        )

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        filter_terms = [f for f in filters if "term" in f]
        assert {"term": {"station_id": "STATION-001"}} in filter_terms
        assert {"term": {"fuel_type": "AGO"}} in filter_terms

    @pytest.mark.asyncio
    async def test_applies_date_range_filter(self, settings_mock):
        """Req 5.3: Filters by date range."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {"consumption_over_time": {"buckets": []}},
        })
        svc = FuelService(es)

        await svc.get_consumption_metrics(
            TENANT,
            start_date="2025-01-01T00:00:00+00:00",
            end_date="2025-01-31T00:00:00+00:00",
        )

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        range_filters = [f for f in filters if "range" in f]
        assert len(range_filters) == 1
        assert "event_timestamp" in range_filters[0]["range"]

    @pytest.mark.asyncio
    async def test_empty_aggregation_returns_empty_list(self, settings_mock):
        """No data returns empty list."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {"consumption_over_time": {"buckets": []}},
        })
        svc = FuelService(es)

        result = await svc.get_consumption_metrics(TENANT)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: get_efficiency_metrics (Req 5.2, 5.3)
# ---------------------------------------------------------------------------


class TestGetEfficiencyMetrics:

    @pytest.mark.asyncio
    async def test_calculates_liters_per_km(self, settings_mock):
        """Req 5.2: Calculates liters per km from odometer data."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 10}, "hits": []},
            "aggregations": {
                "by_asset": {
                    "buckets": [
                        {
                            "key": "TRUCK-001",
                            "doc_count": 5,
                            "total_liters": {"value": 500.0},
                            "min_odometer": {"value": 10000.0},
                            "max_odometer": {"value": 11000.0},
                        }
                    ]
                }
            },
        })
        svc = FuelService(es)

        result = await svc.get_efficiency_metrics(TENANT)

        assert len(result) == 1
        assert isinstance(result[0], EfficiencyMetric)
        assert result[0].asset_id == "TRUCK-001"
        assert result[0].total_liters == 500.0
        assert result[0].total_distance_km == 1000.0
        assert result[0].liters_per_km == 0.5
        assert result[0].event_count == 5

    @pytest.mark.asyncio
    async def test_no_odometer_data_returns_none(self, settings_mock):
        """Req 5.2: When no odometer data, distance and efficiency are None."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 3}, "hits": []},
            "aggregations": {
                "by_asset": {
                    "buckets": [
                        {
                            "key": "TRUCK-002",
                            "doc_count": 3,
                            "total_liters": {"value": 300.0},
                            "min_odometer": {"value": None},
                            "max_odometer": {"value": None},
                        }
                    ]
                }
            },
        })
        svc = FuelService(es)

        result = await svc.get_efficiency_metrics(TENANT)

        assert len(result) == 1
        assert result[0].total_distance_km is None
        assert result[0].liters_per_km is None

    @pytest.mark.asyncio
    async def test_zero_distance_no_division_error(self, settings_mock):
        """Edge case: same odometer readings → distance 0, no liters_per_km."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 2}, "hits": []},
            "aggregations": {
                "by_asset": {
                    "buckets": [
                        {
                            "key": "TRUCK-003",
                            "doc_count": 2,
                            "total_liters": {"value": 100.0},
                            "min_odometer": {"value": 5000.0},
                            "max_odometer": {"value": 5000.0},
                        }
                    ]
                }
            },
        })
        svc = FuelService(es)

        result = await svc.get_efficiency_metrics(TENANT)

        assert result[0].total_distance_km == 0.0
        assert result[0].liters_per_km is None

    @pytest.mark.asyncio
    async def test_filters_by_asset_id(self, settings_mock):
        """Req 5.3: Filters by asset_id."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {"by_asset": {"buckets": []}},
        })
        svc = FuelService(es)

        await svc.get_efficiency_metrics(TENANT, asset_id="TRUCK-001")

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        assert {"term": {"asset_id": "TRUCK-001"}} in filters


# ---------------------------------------------------------------------------
# Tests: get_network_summary (Req 5.4)
# ---------------------------------------------------------------------------


class TestGetNetworkSummary:

    @pytest.mark.asyncio
    async def test_returns_network_summary(self, settings_mock):
        """Req 5.4: Returns aggregated network-wide summary."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 10}, "hits": []},
            "aggregations": {
                "total_capacity": {"value": 500000.0},
                "total_stock": {"value": 350000.0},
                "total_daily_consumption": {"value": 5000.0},
                "avg_days_until_empty": {"value": 70.0},
                "by_status": {
                    "buckets": [
                        {"key": "normal", "doc_count": 6},
                        {"key": "low", "doc_count": 2},
                        {"key": "critical", "doc_count": 1},
                        {"key": "empty", "doc_count": 1},
                    ]
                },
            },
        })
        svc = FuelService(es)

        result = await svc.get_network_summary(TENANT)

        assert isinstance(result, FuelNetworkSummary)
        assert result.total_stations == 10
        assert result.total_capacity_liters == 500000.0
        assert result.total_current_stock_liters == 350000.0
        assert result.total_daily_consumption == 5000.0
        assert result.average_days_until_empty == 70.0
        assert result.stations_normal == 6
        assert result.stations_low == 2
        assert result.stations_critical == 1
        assert result.stations_empty == 1
        assert result.active_alerts == 4  # low + critical + empty

    @pytest.mark.asyncio
    async def test_empty_network(self, settings_mock):
        """Req 5.4: Empty network returns zeros."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {
                "total_capacity": {"value": 0.0},
                "total_stock": {"value": 0.0},
                "total_daily_consumption": {"value": 0.0},
                "avg_days_until_empty": {"value": None},
                "by_status": {"buckets": []},
            },
        })
        svc = FuelService(es)

        result = await svc.get_network_summary(TENANT)

        assert result.total_stations == 0
        assert result.total_capacity_liters == 0.0
        assert result.average_days_until_empty == 0.0
        assert result.active_alerts == 0

    @pytest.mark.asyncio
    async def test_queries_fuel_stations_index(self, settings_mock):
        """Req 5.4: Queries the fuel_stations index with tenant filter."""
        es = _mock_es_agg({
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {
                "total_capacity": {"value": 0.0},
                "total_stock": {"value": 0.0},
                "total_daily_consumption": {"value": 0.0},
                "avg_days_until_empty": {"value": None},
                "by_status": {"buckets": []},
            },
        })
        svc = FuelService(es)

        await svc.get_network_summary(TENANT)

        call_args = es.search_documents.call_args
        assert call_args[0][0] == FUEL_STATIONS_INDEX
        query_body = call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        assert {"term": {"tenant_id": TENANT}} in filters
