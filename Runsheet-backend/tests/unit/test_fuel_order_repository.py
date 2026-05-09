"""
Unit tests for ``fuel.order_repository`` — FuelOrderRepository tenant isolation.

Uses a thin fake ES service mirroring the ``test_terminal_endpoints.py`` style.
Asserts every query emitted contains ``{"term": {"tenant_id": <tenant>}}`` in
the top-level ``bool.filter``.

Validates: Requirements 10.2.1.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from fuel.order_models import FuelOrder, FuelOrderEvent
from fuel.order_repository import (
    FuelOrderRepository,
    OrderCrossTenantAccessError,
)


# ---------------------------------------------------------------------------
# Fake ES service (recording queries for assertion)
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub that records queries for tenant-filter assertions.

    Implements ``index_document``, ``search_documents``, and exposes a
    ``recorded_queries`` list so tests can inspect the exact query shape
    emitted by the repository.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.recorded_queries: List[Dict[str, Any]] = []
        # Provide a mock client for the scripted-upsert path
        self.client = MagicMock()
        self.client.update = MagicMock(return_value={"result": "updated"})

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.recorded_queries.append(dict(query))

        # Extract tenant_id from the bool.filter for filtering
        tenant_id: Optional[str] = None
        bool_clause = query.get("query", {}).get("bool", {})
        filters = bool_clause.get("filter", [])
        for f in filters:
            term = f.get("term") if isinstance(f, dict) else None
            if term and "tenant_id" in term:
                tenant_id = term["tenant_id"]

        # Extract order_id from must clause for single-doc lookup
        must = bool_clause.get("must", [])
        order_id_lookup: Optional[str] = None
        for clause in must:
            if isinstance(clause, dict):
                term = clause.get("term")
                if term and "order_id" in term:
                    order_id_lookup = term["order_id"]

        # Single-doc lookup by order_id
        if order_id_lookup is not None:
            doc = self.docs.get(order_id_lookup)
            if doc is None:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            if tenant_id and doc.get("tenant_id") != tenant_id:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)}],
                    "total": {"value": 1},
                }
            }

        # General search — filter by tenant_id
        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id and doc.get("tenant_id") != tenant_id:
                continue
            matches.append({"_source": dict(doc)})

        matches = matches[:size]
        return {"hits": {"hits": matches, "total": {"value": len(matches)}}}

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _valid_order_dict(
    order_id: str = "ord_test001",
    tenant_id: str = "tenant-1",
    **overrides,
) -> Dict[str, Any]:
    """Return a valid FuelOrder dict for seeding the fake ES."""
    payload = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": "cust-1",
        "customer_name": "Acme Fuels",
        "ship_to_address": "123 Main St",
        "ship_to_lat": 40.0,
        "ship_to_lon": -74.0,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": _NOW.isoformat(),
        "delivery_window_end": (_NOW.replace(hour=16)).isoformat(),
        "intake_channel": "dispatcher",
        "intake_channel_id": "ch-disp-1",
        "source_schema_version": "1.0",
        "trace_id": "trace-001",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
        "last_event_timestamp": _NOW.isoformat(),
        "status": "placed",
    }
    payload.update(overrides)
    return payload


def _valid_event_dict(
    event_id: str = "evt_test001",
    order_id: str = "ord_test001",
    tenant_id: str = "tenant-1",
    **overrides,
) -> Dict[str, Any]:
    """Return a valid FuelOrderEvent dict."""
    payload = {
        "event_id": event_id,
        "order_id": order_id,
        "tenant_id": tenant_id,
        "event_type": "order_placed",
        "event_timestamp": _NOW.isoformat(),
        "ingested_at": _NOW.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": "trace-001",
    }
    payload.update(overrides)
    return payload


def _assert_tenant_filter_present(
    query: Dict[str, Any], expected_tenant: str
) -> None:
    """Assert the query contains {"term": {"tenant_id": <tenant>}} in bool.filter."""
    bool_clause = query.get("query", {}).get("bool", {})
    filters = bool_clause.get("filter", [])
    tenant_terms = [
        f for f in filters
        if isinstance(f, dict) and f.get("term", {}).get("tenant_id") == expected_tenant
    ]
    assert len(tenant_terms) == 1, (
        f"Expected exactly one tenant_id filter for {expected_tenant!r} "
        f"in bool.filter, got {filters}"
    )


# ---------------------------------------------------------------------------
# Tests: Tenant filter injection
# ---------------------------------------------------------------------------


class TestTenantFilterInjection:
    """Every query emitted by the repository MUST contain tenant_id in bool.filter."""

    @pytest.mark.asyncio
    async def test_get_injects_tenant_filter(self):
        es = _FakeESService()
        es.docs["ord_test001"] = _valid_order_dict()
        repo = FuelOrderRepository(es)

        await repo.get("tenant-1", "ord_test001")

        assert len(es.recorded_queries) == 1
        _assert_tenant_filter_present(es.recorded_queries[0], "tenant-1")

    @pytest.mark.asyncio
    async def test_list_for_tenant_injects_tenant_filter(self):
        es = _FakeESService()
        es.docs["ord_test001"] = _valid_order_dict()
        repo = FuelOrderRepository(es)

        await repo.list_for_tenant("tenant-1")

        assert len(es.recorded_queries) == 1
        _assert_tenant_filter_present(es.recorded_queries[0], "tenant-1")

    @pytest.mark.asyncio
    async def test_search_injects_tenant_filter(self):
        es = _FakeESService()
        es.docs["ord_test001"] = _valid_order_dict()
        repo = FuelOrderRepository(es)

        await repo.search("tenant-1", status="placed")

        assert len(es.recorded_queries) == 1
        _assert_tenant_filter_present(es.recorded_queries[0], "tenant-1")

    @pytest.mark.asyncio
    async def test_search_with_multiple_filters_still_has_tenant(self):
        es = _FakeESService()
        es.docs["ord_test001"] = _valid_order_dict()
        repo = FuelOrderRepository(es)

        await repo.search(
            "tenant-1",
            status="placed",
            customer_id="cust-1",
            call_type="one_off",
        )

        assert len(es.recorded_queries) == 1
        _assert_tenant_filter_present(es.recorded_queries[0], "tenant-1")

    @pytest.mark.asyncio
    async def test_get_events_for_order_injects_tenant_filter(self):
        es = _FakeESService()
        es.docs["evt_test001"] = _valid_event_dict()
        repo = FuelOrderRepository(es)

        await repo.get_events_for_order("tenant-1", "ord_test001")

        assert len(es.recorded_queries) == 1
        _assert_tenant_filter_present(es.recorded_queries[0], "tenant-1")


# ---------------------------------------------------------------------------
# Tests: Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Cross-tenant reads degrade to None/empty; writes raise."""

    @pytest.mark.asyncio
    async def test_get_returns_none_for_cross_tenant(self):
        es = _FakeESService()
        es.docs["ord_test001"] = _valid_order_dict(tenant_id="tenant-2")
        repo = FuelOrderRepository(es)

        result = await repo.get("tenant-1", "ord_test001")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_for_tenant_excludes_other_tenants(self):
        es = _FakeESService()
        es.docs["ord_mine"] = _valid_order_dict(
            order_id="ord_mine", tenant_id="tenant-1"
        )
        es.docs["ord_other"] = _valid_order_dict(
            order_id="ord_other", tenant_id="tenant-2"
        )
        repo = FuelOrderRepository(es)

        results = await repo.list_for_tenant("tenant-1")
        order_ids = [o.order_id for o in results]
        assert "ord_mine" in order_ids
        assert "ord_other" not in order_ids

    @pytest.mark.asyncio
    async def test_search_excludes_other_tenants(self):
        es = _FakeESService()
        es.docs["ord_mine"] = _valid_order_dict(
            order_id="ord_mine", tenant_id="tenant-1"
        )
        es.docs["ord_other"] = _valid_order_dict(
            order_id="ord_other", tenant_id="tenant-2"
        )
        repo = FuelOrderRepository(es)

        result = await repo.search("tenant-1")
        order_ids = [o.order_id for o in result["orders"]]
        assert "ord_mine" in order_ids
        assert "ord_other" not in order_ids

    @pytest.mark.asyncio
    async def test_create_raises_on_cross_tenant_write(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        order_data = _valid_order_dict(tenant_id="tenant-2")
        with pytest.raises(OrderCrossTenantAccessError):
            await repo.create("tenant-1", order_data)

    @pytest.mark.asyncio
    async def test_append_event_raises_on_cross_tenant_write(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        event_data = _valid_event_dict(tenant_id="tenant-2")
        with pytest.raises(OrderCrossTenantAccessError):
            await repo.append_event("tenant-1", event_data)


# ---------------------------------------------------------------------------
# Tests: Create and append
# ---------------------------------------------------------------------------


class TestCreateAndAppend:
    """Basic create and append_event operations."""

    @pytest.mark.asyncio
    async def test_create_persists_order(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        order_data = _valid_order_dict()
        result = await repo.create("tenant-1", order_data)

        assert result.order_id == "ord_test001"
        assert result.tenant_id == "tenant-1"
        assert "ord_test001" in es.docs

    @pytest.mark.asyncio
    async def test_append_event_persists_event(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        event_data = _valid_event_dict()
        result = await repo.append_event("tenant-1", event_data)

        assert result.event_id == "evt_test001"
        assert result.tenant_id == "tenant-1"
        assert "evt_test001" in es.docs


# ---------------------------------------------------------------------------
# Tests: Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Repository rejects invalid inputs."""

    @pytest.mark.asyncio
    async def test_get_rejects_empty_tenant_id(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        with pytest.raises(ValueError, match="tenant_id"):
            await repo.get("", "ord_test001")

    @pytest.mark.asyncio
    async def test_get_rejects_empty_order_id(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        with pytest.raises(ValueError, match="order_id"):
            await repo.get("tenant-1", "")

    @pytest.mark.asyncio
    async def test_list_for_tenant_rejects_zero_size(self):
        es = _FakeESService()
        repo = FuelOrderRepository(es)

        with pytest.raises(ValueError, match="size"):
            await repo.list_for_tenant("tenant-1", size=0)

    def test_constructor_rejects_none_es_service(self):
        with pytest.raises(ValueError, match="es_service"):
            FuelOrderRepository(None)
