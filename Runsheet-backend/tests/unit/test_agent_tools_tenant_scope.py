"""
Regression tests for tenant scoping on AI agent tools.

Every ES-reading ``@tool`` must scope its Elasticsearch query to the
currently-bound tenant (set via ``set_current_tenant``). These tests bind
the ContextVar, call each tool through its underlying coroutine, and
assert the captured ES body carries the tenant filter in the top-level
``bool.filter`` clause. For tools that go through ``semantic_search``
(``search_orders`` / ``search_support_tickets``) we assert the mock was
called with ``tenant_id="tenant-a"`` as the first positional arg.

Also verifies the loud-fail behaviour: calling an ES-reading tool with no
tenant bound raises ``RuntimeError`` so a forgotten scope surfaces as a
test failure rather than silently leaking data across tenants.

Validates: Requirements 9.2, 9.4.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock the ES service BEFORE any tool imports so the module-level instance
# never touches a real cluster.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools._tenant_context import (  # noqa: E402
    current_tenant_id_var,
    get_current_tenant,
    set_current_tenant,
)
from Agents.tools.search_tools import (  # noqa: E402
    search_fleet_data,
    search_inventory,
    search_orders,
    search_support_tickets,
)
from Agents.tools.summary_tools import (  # noqa: E402
    get_fleet_summary,
    get_inventory_summary,
    get_analytics_overview,
)
from Agents.tools.lookup_tools import (  # noqa: E402
    find_truck_by_id,
    get_all_locations,
)


TENANT_A = "tenant-a"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_search_response() -> Dict[str, Any]:
    """ES search response with no hits — triggers the "not found" path in
    tools that branch on results but keeps the call reaching ES."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "by_type": {"buckets": []},
            "by_subtype": {"buckets": []},
            "time_series": {"buckets": []},
            "routes": {"buckets": []},
            "causes": {"buckets": []},
            "regions": {"buckets": []},
        },
    }


def _metrics_search_response() -> Dict[str, Any]:
    """ES response that satisfies ``get_current_metrics`` (needs one hit)."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "metrics": {
                            "delivery_performance_pct": 0.0,
                            "average_delay_minutes": 0,
                            "fleet_utilization_pct": 0,
                            "customer_satisfaction": 0.0,
                        }
                    }
                }
            ],
            "total": {"value": 1},
        },
        "aggregations": {"routes": {"buckets": []}, "causes": {"buckets": []}},
    }


def _first_bool_filter(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the top-level ``bool.filter`` clause (or an empty list)."""
    return body.get("query", {}).get("bool", {}).get("filter", [])


def _has_tenant_filter(body: Dict[str, Any], tenant_id: str) -> bool:
    return any(
        entry.get("term", {}).get("tenant_id") == tenant_id
        for entry in _first_bool_filter(body)
    )


# ---------------------------------------------------------------------------
# Tools that read ES directly via ``search_documents``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fleet_data_scopes_query_to_tenant() -> None:
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_fleet_data(query="delayed")
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


@pytest.mark.asyncio
async def test_get_fleet_summary_scopes_both_queries() -> None:
    with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_fleet_summary()
    # Both the list fetch and the aggregation go through search_documents.
    bodies = [call.args[1] for call in mock_es.search_documents.call_args_list]
    assert bodies, "get_fleet_summary did not hit ES"
    assert all(_has_tenant_filter(body, TENANT_A) for body in bodies), bodies


@pytest.mark.asyncio
async def test_get_inventory_summary_scopes_query() -> None:
    with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_inventory_summary()
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


@pytest.mark.asyncio
async def test_get_analytics_overview_passes_tenant_to_es_service() -> None:
    """``get_analytics_overview`` delegates to three analytics helpers on
    the ES service; each must receive the tenant id as first positional arg."""
    with patch("Agents.tools.summary_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.get_current_metrics = AsyncMock(return_value={})
        mock_es.get_route_performance_data = AsyncMock(return_value=[])
        mock_es.get_delay_causes_data = AsyncMock(return_value=[])
        await get_analytics_overview()

    assert mock_es.get_current_metrics.call_args.args == (TENANT_A,)
    assert mock_es.get_route_performance_data.call_args.args == (TENANT_A,)
    assert mock_es.get_delay_causes_data.call_args.args == (TENANT_A,)


@pytest.mark.asyncio
async def test_find_truck_by_id_scopes_query() -> None:
    with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await find_truck_by_id(truck_identifier="GI-58A")
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


@pytest.mark.asyncio
async def test_get_all_locations_scopes_query() -> None:
    with patch("Agents.tools.lookup_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_all_locations()
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


# ---------------------------------------------------------------------------
# Tools that go through ``semantic_search``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_orders_calls_semantic_search_with_tenant() -> None:
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.semantic_search = AsyncMock(return_value=[])
        await search_orders(query="network gear")

    # First positional arg must be the tenant id.
    args = mock_es.semantic_search.call_args.args
    assert args, mock_es.semantic_search.call_args
    assert args[0] == TENANT_A, f"semantic_search first arg should be tenant_id: {args}"


@pytest.mark.asyncio
async def test_search_support_tickets_calls_semantic_search_with_tenant() -> None:
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.semantic_search = AsyncMock(return_value=[])
        await search_support_tickets(query="late delivery")

    args = mock_es.semantic_search.call_args.args
    assert args, mock_es.semantic_search.call_args
    assert args[0] == TENANT_A


@pytest.mark.asyncio
async def test_search_inventory_calls_semantic_search_with_tenant() -> None:
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.semantic_search = AsyncMock(return_value=[])
        await search_inventory(query="diesel")

    args = mock_es.semantic_search.call_args.args
    assert args, mock_es.semantic_search.call_args
    assert args[0] == TENANT_A


# ---------------------------------------------------------------------------
# Fallback path on search_support_tickets + search_inventory is tenant-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_support_tickets_fallback_stays_tenant_scoped() -> None:
    """When semantic_search raises the fallback must still route through a
    tenant-filtered ``search_documents`` call — not the unbounded
    ``get_all_documents`` path the old code used."""
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.semantic_search = AsyncMock(side_effect=Exception("index missing"))
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_support_tickets(query="late delivery")

    assert mock_es.search_documents.called, "fallback did not hit search_documents"
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


@pytest.mark.asyncio
async def test_search_inventory_fallback_stays_tenant_scoped() -> None:
    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.semantic_search = AsyncMock(side_effect=Exception("index missing"))
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_inventory(query="diesel")

    assert mock_es.search_documents.called, "fallback did not hit search_documents"
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), body


# ---------------------------------------------------------------------------
# Loud failure on missing tenant scope
# ---------------------------------------------------------------------------


def test_get_current_tenant_raises_without_scope() -> None:
    """``get_current_tenant`` must raise when nothing is bound so forgotten
    scopes surface loudly instead of silently leaking data."""
    # Ensure the ContextVar is unset in this test scope.
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            get_current_tenant()
    finally:
        current_tenant_id_var.reset(token)


@pytest.mark.asyncio
async def test_search_fleet_data_raises_without_tenant_scope() -> None:
    """ES-reading tools must refuse to run without a bound tenant."""
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            await search_fleet_data(query="delayed")
    finally:
        current_tenant_id_var.reset(token)


# ---------------------------------------------------------------------------
# Tenant swap does not leak across invocations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_calls_use_the_current_tenant() -> None:
    """Two calls under different tenants must each emit their own tenant id."""
    tenants_seen: List[str] = []

    with patch("Agents.tools.search_tools.elasticsearch_service") as mock_es:
        async def _capture(*args, **kwargs):
            body = args[1]
            for entry in _first_bool_filter(body):
                tid = entry.get("term", {}).get("tenant_id")
                if tid:
                    tenants_seen.append(tid)
            return _empty_search_response()

        mock_es.search_documents = AsyncMock(side_effect=_capture)

        with set_current_tenant("tenant-a"):
            await search_fleet_data(query="a")
        with set_current_tenant("tenant-b"):
            await search_fleet_data(query="b")

    assert tenants_seen == ["tenant-a", "tenant-b"], tenants_seen
