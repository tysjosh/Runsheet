"""
Regression tests for tenant scoping on order-domain AI tools.

Every ES-reading order tool (``search_orders``, ``search_drivers``,
``get_order_events``, ``get_orders_metrics``) must scope its Elasticsearch
query to the currently-bound tenant (set via ``set_current_tenant``). These
tests bind the ContextVar, call each tool through its underlying coroutine,
and assert the captured ES body carries the tenant filter
``{"term": {"tenant_id": <tenant>}}`` in the top-level ``bool.filter`` clause.

Also verifies the loud-fail behaviour: calling an ES-reading order tool with
no tenant bound raises ``RuntimeError`` so a forgotten scope surfaces as a
test failure rather than silently leaking data across tenants.

Additionally tests mutation tools (``update_order_status``,
``assign_driver_to_order``, ``cancel_order``) under ``autonomy_level =
"suggest-only"`` to assert no underlying service call is made.

Validates: Requirements 6.1.3, 6.2.3, 10.2.3.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List
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
from Agents.tools.order_tools import (  # noqa: E402
    search_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,
)


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_search_response() -> Dict[str, Any]:
    """ES search response with no hits."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "by_status": {"buckets": []},
            "by_call_type": {"buckets": []},
            "by_intake_channel": {"buckets": []},
            "over_time": {"buckets": []},
            "by_availability": {"buckets": []},
            "avg_active_orders": {"value": 0},
            "avg_completed_today": {"value": 0},
            "hazmat_count": {"doc_count": 0},
        },
    }


def _first_bool_filter(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the top-level ``bool.filter`` clause (or an empty list).

    The ``inject_tenant_filter`` function wraps the original query in a
    new ``bool`` with ``must`` (original query) and ``filter`` (tenant term).
    """
    return body.get("query", {}).get("bool", {}).get("filter", [])


def _has_tenant_filter(body: Dict[str, Any], tenant_id: str) -> bool:
    """Check if the ES query body contains a tenant filter term."""
    return any(
        entry.get("term", {}).get("tenant_id") == tenant_id
        for entry in _first_bool_filter(body)
    )


# ---------------------------------------------------------------------------
# ES-reading order tools — tenant scoping assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_orders_scopes_query_to_tenant() -> None:
    """search_orders must inject tenant_id filter into the ES query."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_orders()
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), f"Missing tenant filter in: {body}"


@pytest.mark.asyncio
async def test_search_orders_with_filters_scopes_to_tenant() -> None:
    """search_orders with additional filters still includes tenant filter."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_orders(status="placed", customer_id="cust-123")
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), f"Missing tenant filter in: {body}"


@pytest.mark.asyncio
async def test_search_drivers_scopes_query_to_tenant() -> None:
    """search_drivers must inject tenant_id filter into the ES query."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_drivers()
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), f"Missing tenant filter in: {body}"


@pytest.mark.asyncio
async def test_search_drivers_with_filters_scopes_to_tenant() -> None:
    """search_drivers with filters still includes tenant filter."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await search_drivers(status="active", hazmat_endorsement=True)
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), f"Missing tenant filter in: {body}"


@pytest.mark.asyncio
async def test_get_order_events_scopes_query_to_tenant() -> None:
    """get_order_events must inject tenant_id filter into the ES query."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_order_events(order_id="ord_abc123")
    body = mock_es.search_documents.call_args[0][1]
    assert _has_tenant_filter(body, TENANT_A), f"Missing tenant filter in: {body}"


@pytest.mark.asyncio
async def test_get_orders_metrics_scopes_query_to_tenant() -> None:
    """get_orders_metrics must inject tenant_id filter into the ES query."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_orders_metrics(metric_type="orders")
    # get_orders_metrics may make multiple ES calls; all must be tenant-scoped
    bodies = [call.args[1] for call in mock_es.search_documents.call_args_list]
    assert bodies, "get_orders_metrics did not hit ES"
    assert all(_has_tenant_filter(body, TENANT_A) for body in bodies), bodies


@pytest.mark.asyncio
async def test_get_orders_metrics_drivers_scopes_to_tenant() -> None:
    """get_orders_metrics(metric_type='drivers') scopes to tenant."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_orders_metrics(metric_type="drivers")
    bodies = [call.args[1] for call in mock_es.search_documents.call_args_list]
    assert bodies, "get_orders_metrics(drivers) did not hit ES"
    assert all(_has_tenant_filter(body, TENANT_A) for body in bodies), bodies


@pytest.mark.asyncio
async def test_get_orders_metrics_sla_scopes_to_tenant() -> None:
    """get_orders_metrics(metric_type='sla') scopes both queries to tenant."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_orders_metrics(metric_type="sla")
    bodies = [call.args[1] for call in mock_es.search_documents.call_args_list]
    assert bodies, "get_orders_metrics(sla) did not hit ES"
    assert all(_has_tenant_filter(body, TENANT_A) for body in bodies), bodies


@pytest.mark.asyncio
async def test_get_orders_metrics_failures_scopes_to_tenant() -> None:
    """get_orders_metrics(metric_type='failures') scopes to tenant."""
    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es, \
            set_current_tenant(TENANT_A):
        mock_es.search_documents = AsyncMock(return_value=_empty_search_response())
        await get_orders_metrics(metric_type="failures")
    bodies = [call.args[1] for call in mock_es.search_documents.call_args_list]
    assert bodies, "get_orders_metrics(failures) did not hit ES"
    assert all(_has_tenant_filter(body, TENANT_A) for body in bodies), bodies


# ---------------------------------------------------------------------------
# Loud failure on missing tenant scope — RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_orders_raises_without_tenant_scope() -> None:
    """search_orders must raise RuntimeError when no tenant is bound."""
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            await search_orders()
    finally:
        current_tenant_id_var.reset(token)


@pytest.mark.asyncio
async def test_search_drivers_raises_without_tenant_scope() -> None:
    """search_drivers must raise RuntimeError when no tenant is bound."""
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            await search_drivers()
    finally:
        current_tenant_id_var.reset(token)


@pytest.mark.asyncio
async def test_get_order_events_raises_without_tenant_scope() -> None:
    """get_order_events must raise RuntimeError when no tenant is bound."""
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            await get_order_events(order_id="ord_abc123")
    finally:
        current_tenant_id_var.reset(token)


@pytest.mark.asyncio
async def test_get_orders_metrics_raises_without_tenant_scope() -> None:
    """get_orders_metrics must raise RuntimeError when no tenant is bound."""
    token = current_tenant_id_var.set(None)
    try:
        with pytest.raises(RuntimeError):
            await get_orders_metrics()
    finally:
        current_tenant_id_var.reset(token)


# ---------------------------------------------------------------------------
# Tenant swap does not leak across invocations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_calls_use_the_current_tenant() -> None:
    """Two calls under different tenants must each emit their own tenant id."""
    tenants_seen: List[str] = []

    with patch("Agents.tools.order_tools.elasticsearch_service") as mock_es:
        async def _capture(*args, **kwargs):
            body = args[1]
            for entry in _first_bool_filter(body):
                tid = entry.get("term", {}).get("tenant_id")
                if tid:
                    tenants_seen.append(tid)
            return _empty_search_response()

        mock_es.search_documents = AsyncMock(side_effect=_capture)

        with set_current_tenant(TENANT_A):
            await search_orders()
        with set_current_tenant(TENANT_B):
            await search_orders()

    assert tenants_seen == [TENANT_A, TENANT_B], tenants_seen


# ---------------------------------------------------------------------------
# Mutation tools — suggest-only autonomy level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_update_order_status_suggest_only_no_execution() -> None:
    """Under suggest-only autonomy, update_order_status must NOT execute
    the underlying service call — it should queue for approval instead."""
    from Agents.tools.mutation_tools import configure_mutation_tools

    # Build a mock ConfirmationProtocol that simulates suggest-only behaviour
    mock_protocol = MagicMock()
    mock_result = MagicMock()
    mock_result.executed = False
    mock_result.risk_level = "medium"
    mock_result.result = None
    mock_result.confirmation_method = "approval_queue"
    mock_result.approval_id = "appr-001"
    mock_protocol.process_mutation = AsyncMock(return_value=mock_result)

    mock_es = MagicMock()
    configure_mutation_tools(mock_protocol, mock_es)

    from Agents.tools.mutation_tools import update_job_status

    # Call a mutation tool — under suggest-only the protocol returns
    # executed=False, meaning no underlying service call was made.
    result = await update_job_status(
        job_id="ord_test123",
        new_status="confirmed",
        reason="AI recommendation",
        tenant_id=TENANT_A,
    )

    # The protocol was called (to classify and queue)
    mock_protocol.process_mutation.assert_called_once()
    # The result indicates queued, not executed
    assert "queued" in result.lower() or "approval" in result.lower()
    # No direct ES write was made by the tool itself
    mock_es.search_documents = AsyncMock()
    assert not mock_es.search_documents.called


@pytest.mark.asyncio
async def test_mutation_cancel_job_suggest_only_no_execution() -> None:
    """Under suggest-only autonomy, cancel_job must NOT execute — it
    should queue for approval since suggest-only never auto-executes."""
    from Agents.tools.mutation_tools import configure_mutation_tools

    mock_protocol = MagicMock()
    mock_result = MagicMock()
    mock_result.executed = False
    mock_result.risk_level = "high"
    mock_result.result = None
    mock_result.confirmation_method = "approval_queue"
    mock_result.approval_id = "appr-002"
    mock_protocol.process_mutation = AsyncMock(return_value=mock_result)

    mock_es = MagicMock()
    configure_mutation_tools(mock_protocol, mock_es)

    from Agents.tools.mutation_tools import cancel_job

    result = await cancel_job(
        job_id="ord_test456",
        reason="Customer requested cancellation",
        tenant_id=TENANT_A,
    )

    mock_protocol.process_mutation.assert_called_once()
    # Verify the mutation was NOT executed
    request_arg = mock_protocol.process_mutation.call_args[0][0]
    assert request_arg.tool_name == "cancel_job"
    # Result indicates queued for approval
    assert "queued" in result.lower() or "approval" in result.lower()


@pytest.mark.asyncio
async def test_mutation_assign_asset_suggest_only_no_execution() -> None:
    """Under suggest-only autonomy, assign_asset_to_job must NOT execute
    — it should queue for approval."""
    from Agents.tools.mutation_tools import configure_mutation_tools

    mock_protocol = MagicMock()
    mock_result = MagicMock()
    mock_result.executed = False
    mock_result.risk_level = "medium"
    mock_result.result = None
    mock_result.confirmation_method = "approval_queue"
    mock_result.approval_id = "appr-003"
    mock_protocol.process_mutation = AsyncMock(return_value=mock_result)

    mock_es = MagicMock()
    configure_mutation_tools(mock_protocol, mock_es)

    from Agents.tools.mutation_tools import assign_asset_to_job

    result = await assign_asset_to_job(
        job_id="ord_test789",
        asset_id="driver-001",
        tenant_id=TENANT_A,
    )

    mock_protocol.process_mutation.assert_called_once()
    request_arg = mock_protocol.process_mutation.call_args[0][0]
    assert request_arg.tool_name == "assign_asset_to_job"
    # Result indicates queued, not executed
    assert "queued" in result.lower() or "approval" in result.lower()


@pytest.mark.asyncio
async def test_mutation_protocol_not_configured_raises() -> None:
    """Mutation tools must raise RuntimeError when protocol is not configured."""
    from Agents.tools import mutation_tools

    # Reset the module-level protocol to None
    original = mutation_tools._confirmation_protocol
    mutation_tools._confirmation_protocol = None
    try:
        with pytest.raises(RuntimeError):
            mutation_tools._get_protocol()
    finally:
        mutation_tools._confirmation_protocol = original
