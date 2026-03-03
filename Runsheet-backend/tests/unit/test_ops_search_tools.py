"""
Unit tests for ops search tools (AI agent tools).

Validates:
- Requirement 17.1: search_shipments with status, rider, time range, free-text filters
- Requirement 17.2: search_riders with status, availability, utilization filters
- Requirement 17.3: get_shipment_events for specific shipment event timeline
- Requirement 17.4: get_ops_metrics for aggregated metrics
- Requirement 17.5: Tenant scoping via TenantGuard
- Requirement 17.6: Structured format for AI agent interpretation
- Requirements 19.1-19.2: Read-only access
- Requirement 19.5: Audit logging of tool invocations
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock elasticsearch_service before any transitive import can trigger it.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools.ops_search_tools import (  # noqa: E402
    configure_ops_search_tools,
    search_shipments,
    search_riders,
    get_shipment_events,
    get_ops_metrics,
)
import Agents.tools.ops_search_tools as mod  # noqa: E402


def _make_es_response(hits: list[dict], total: int = None) -> dict:
    """Build a minimal ES search response."""
    if total is None:
        total = len(hits)
    return {
        "hits": {
            "total": {"value": total, "relation": "eq"},
            "hits": [{"_source": h} for h in hits],
        },
        "aggregations": {},
    }


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module-level service reference between tests."""
    import Agents.tools.ops_feature_guard as ff_mod
    original = mod._ops_es_service
    original_ff = ff_mod._feature_flag_service
    # Reset feature flag guard so tests don't inherit state from other test modules
    ff_mod._feature_flag_service = None
    yield
    mod._ops_es_service = original
    ff_mod._feature_flag_service = original_ff


@pytest.fixture()
def mock_ops_es():
    """Create a mock OpsElasticsearchService."""
    svc = MagicMock()
    svc.SHIPMENTS_CURRENT = "shipments_current"
    svc.SHIPMENT_EVENTS = "shipment_events"
    svc.RIDERS_CURRENT = "riders_current"
    svc.client = MagicMock()
    svc.client.search = AsyncMock(return_value=_make_es_response([]))
    return svc


# ---------------------------------------------------------------------------
# configure_ops_search_tools
# ---------------------------------------------------------------------------


def test_configure_sets_module_service(mock_ops_es):
    configure_ops_search_tools(mock_ops_es)
    assert mod._ops_es_service is mock_ops_es


# ---------------------------------------------------------------------------
# search_shipments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_shipments_basic(mock_ops_es):
    """Basic search returns structured JSON with shipments key."""
    configure_ops_search_tools(mock_ops_es)
    shipment = {"shipment_id": "S-001", "status": "in_transit", "tenant_id": "t1"}
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([shipment], 1))

    result = await search_shipments(tenant_id="t1")
    parsed = json.loads(result)

    assert parsed["tool"] == "search_shipments"
    assert parsed["total"] == 1
    assert len(parsed["shipments"]) == 1
    assert parsed["shipments"][0]["shipment_id"] == "S-001"


@pytest.mark.asyncio
async def test_search_shipments_tenant_scoping(mock_ops_es):
    """Verify tenant_id filter is injected into the ES query."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await search_shipments(tenant_id="t1")

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    # The query should contain a tenant_id filter
    query_str = json.dumps(body)
    assert '"tenant_id"' in query_str
    assert '"t1"' in query_str


@pytest.mark.asyncio
async def test_search_shipments_with_filters(mock_ops_es):
    """Filters (status, rider_id, date range) are included in the query."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await search_shipments(
        tenant_id="t1",
        status="failed",
        rider_id="R-001",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    query_str = json.dumps(body)
    assert "failed" in query_str
    assert "R-001" in query_str
    assert "2025-01-01" in query_str


@pytest.mark.asyncio
async def test_search_shipments_free_text(mock_ops_es):
    """Free-text query uses multi_match on origin/destination."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await search_shipments(tenant_id="t1", query="downtown warehouse")

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    query_str = json.dumps(body)
    assert "multi_match" in query_str
    assert "downtown warehouse" in query_str


@pytest.mark.asyncio
async def test_search_shipments_disabled_tenant(mock_ops_es):
    """Disabled tenant gets structured disabled response."""
    configure_ops_search_tools(mock_ops_es)

    with patch("Agents.tools.ops_search_tools.check_ops_feature_flag", new_callable=AsyncMock) as mock_ff:
        mock_ff.return_value = json.dumps({"status": "disabled", "message": "not enabled"})
        result = await search_shipments(tenant_id="t-disabled")

    parsed = json.loads(result)
    assert parsed["status"] == "disabled"
    # ES should not have been called
    mock_ops_es.client.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_shipments_pii_masked(mock_ops_es):
    """PII fields are masked in AI tool output."""
    configure_ops_search_tools(mock_ops_es)
    shipment = {
        "shipment_id": "S-002",
        "status": "delivered",
        "tenant_id": "t1",
        "customer_name": "Jane Doe",
    }
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([shipment]))

    result = await search_shipments(tenant_id="t1")
    parsed = json.loads(result)

    # customer_name should be masked
    assert parsed["shipments"][0]["customer_name"] == "***"


# ---------------------------------------------------------------------------
# search_riders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_riders_basic(mock_ops_es):
    """Basic rider search returns structured JSON."""
    configure_ops_search_tools(mock_ops_es)
    rider = {"rider_id": "R-001", "status": "active", "tenant_id": "t1"}
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([rider], 1))

    result = await search_riders(tenant_id="t1")
    parsed = json.loads(result)

    assert parsed["tool"] == "search_riders"
    assert parsed["total"] == 1
    assert parsed["riders"][0]["rider_id"] == "R-001"


@pytest.mark.asyncio
async def test_search_riders_with_filters(mock_ops_es):
    """Status, availability, and utilization filters are applied."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await search_riders(
        tenant_id="t1",
        status="active",
        availability="available",
        min_utilization=2,
        max_utilization=10,
    )

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    query_str = json.dumps(body)
    assert "active" in query_str
    assert "available" in query_str
    assert "active_shipment_count" in query_str


@pytest.mark.asyncio
async def test_search_riders_tenant_scoping(mock_ops_es):
    """Tenant filter is injected into rider queries."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await search_riders(tenant_id="t2")

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    query_str = json.dumps(body)
    assert '"t2"' in query_str


# ---------------------------------------------------------------------------
# get_shipment_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_shipment_events_basic(mock_ops_es):
    """Returns event timeline for a specific shipment."""
    configure_ops_search_tools(mock_ops_es)
    event = {
        "event_id": "E-001",
        "shipment_id": "S-001",
        "event_type": "shipment_created",
        "event_timestamp": "2025-01-01T10:00:00Z",
        "tenant_id": "t1",
    }
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([event], 1))

    result = await get_shipment_events(shipment_id="S-001", tenant_id="t1")
    parsed = json.loads(result)

    assert parsed["tool"] == "get_shipment_events"
    assert parsed["shipment_id"] == "S-001"
    assert parsed["total"] == 1
    assert parsed["events"][0]["event_type"] == "shipment_created"


@pytest.mark.asyncio
async def test_get_shipment_events_sorted_asc(mock_ops_es):
    """Events are sorted by event_timestamp ascending."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await get_shipment_events(shipment_id="S-001", tenant_id="t1")

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    assert body["sort"] == [{"event_timestamp": {"order": "asc"}}]


@pytest.mark.asyncio
async def test_get_shipment_events_tenant_scoping(mock_ops_es):
    """Tenant filter is injected into event queries."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(return_value=_make_es_response([]))

    await get_shipment_events(shipment_id="S-001", tenant_id="t3")

    call_args = mock_ops_es.client.search.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    query_str = json.dumps(body)
    assert '"t3"' in query_str


# ---------------------------------------------------------------------------
# get_ops_metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ops_metrics_shipments(mock_ops_es):
    """Shipment metrics returns structured aggregation data."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 50}, "hits": []},
            "aggregations": {
                "by_status": {
                    "buckets": [
                        {"key": "delivered", "doc_count": 30},
                        {"key": "in_transit", "doc_count": 20},
                    ]
                },
                "over_time": {"buckets": []},
            },
        }
    )

    result = await get_ops_metrics(tenant_id="t1", metric_type="shipments")
    parsed = json.loads(result)

    assert parsed["tool"] == "get_ops_metrics"
    assert parsed["metric_type"] == "shipments"
    assert parsed["summary"]["delivered"] == 30
    assert parsed["summary"]["in_transit"] == 20


@pytest.mark.asyncio
async def test_get_ops_metrics_unknown_type(mock_ops_es):
    """Unknown metric_type returns an error message."""
    configure_ops_search_tools(mock_ops_es)

    result = await get_ops_metrics(tenant_id="t1", metric_type="unknown")
    parsed = json.loads(result)

    assert "error" in parsed
    assert "unknown" in parsed["error"].lower()


@pytest.mark.asyncio
async def test_get_ops_metrics_disabled_tenant(mock_ops_es):
    """Disabled tenant gets structured disabled response from metrics."""
    configure_ops_search_tools(mock_ops_es)

    with patch("Agents.tools.ops_search_tools.check_ops_feature_flag", new_callable=AsyncMock) as mock_ff:
        mock_ff.return_value = json.dumps({"status": "disabled", "message": "not enabled"})
        result = await get_ops_metrics(tenant_id="t-disabled")

    parsed = json.loads(result)
    assert parsed["status"] == "disabled"


@pytest.mark.asyncio
async def test_get_ops_metrics_error_handling(mock_ops_es):
    """ES errors are caught and returned as structured error response."""
    configure_ops_search_tools(mock_ops_es)
    mock_ops_es.client.search = AsyncMock(side_effect=RuntimeError("ES connection lost"))

    result = await get_ops_metrics(tenant_id="t1", metric_type="shipments")
    parsed = json.loads(result)

    assert "error" in parsed
    assert "ES connection lost" in parsed["error"]
