"""
Fuel station search treats status words as filters, not free text (N-new-3).

Staging finding N-new-3: ``search_fuel_stations(query="critical fuel stations",
status="critical")`` ANDed a ``multi_match`` of the whole phrase with the
status filter. No station's name contains "critical fuel stations", so a
critical station was never found. This mirrors F5 (``_split_fleet_query``).
"""
from unittest.mock import AsyncMock, patch

import pytest

from Agents.tools._tenant_context import set_current_tenant
from Agents.tools.fuel_tools import search_fuel_stations
from persistence.document_matcher import matches

TENANT = "t"

CRITICAL_STATION = {
    "tenant_id": TENANT,
    "station_id": "QA-V2-STATION-01",
    "name": "QA-V2 Station 01",
    "status": "critical",
}


def _empty_response():
    return {"hits": {"hits": [], "total": {"value": 0}}}


async def _station_query(**kwargs):
    with patch("Agents.tools.fuel_tools.elasticsearch_service") as es, \
            set_current_tenant(TENANT):
        es.search_documents = AsyncMock(return_value=_empty_response())
        result = await search_fuel_stations(**kwargs)
    return es, result


@pytest.mark.asyncio
async def test_critical_fuel_stations_matches_a_critical_station():
    es, _ = await _station_query(query="critical fuel stations", status="critical")
    query = es.search_documents.call_args[0][1]["query"]
    assert matches(CRITICAL_STATION, query)
    assert not matches({**CRITICAL_STATION, "status": "normal"}, query)
    assert not matches({**CRITICAL_STATION, "tenant_id": "other"}, query)


@pytest.mark.asyncio
async def test_low_stock_stations_filters_on_low_with_match_all():
    es, _ = await _station_query(query="low stock stations")
    body = es.search_documents.call_args[0][1]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"status": "low"}} in filters
    assert {"term": {"tenant_id": TENANT}} in filters
    low = {**CRITICAL_STATION, "status": "low"}
    assert matches(low, body["query"])
    assert not matches(CRITICAL_STATION, body["query"])


@pytest.mark.asyncio
async def test_place_name_still_uses_multi_match():
    es, _ = await _station_query(query="Industrial Area")
    body = es.search_documents.call_args[0][1]
    must = body["query"]["bool"]["must"]
    assert must[0]["multi_match"]["query"] == "Industrial Area"
    assert not any("status" in str(f) for f in body["query"]["bool"]["filter"])


@pytest.mark.asyncio
async def test_unknown_status_returns_valid_list_without_query():
    es, result = await _station_query(query="stations", status="bogus")
    es.search_documents.assert_not_called()
    assert "bogus" in result
    for value in ("normal", "low", "critical", "empty"):
        assert value in result


@pytest.mark.asyncio
async def test_ago_stations_matches_an_ago_station():
    es, _ = await _station_query(query="AGO stations", fuel_type="AGO")
    query = es.search_documents.call_args[0][1]["query"]
    ago = {**CRITICAL_STATION, "fuel_type": "AGO"}
    assert matches(ago, query)
    assert not matches({**ago, "fuel_type": "PMS"}, query)
