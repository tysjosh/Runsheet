"""
Regression tests for tenant scoping on the inline endpoints (upload + location).

Covers:
* ``POST /api/upload/csv`` — every document handed to
  ``data_seeder.upsert_batch_data`` must carry the authenticated tenant id.
* ``POST /api/locations/webhook`` — location updates for a truck that
  belongs to another tenant are rejected, and accepted updates carry the
  authenticated tenant id when written to ``trucks`` and ``locations``.
* ``POST /api/locations/batch`` — ditto, per-item.
* ``convert_csv_row_to_document`` — now skips rows that can't be geocoded
  instead of falling back to a hard-coded Nairobi station.

These tests override ``get_tenant_context`` so the handlers see a
deterministic authenticated tenant without going through JWT verification.

Validates: Requirements 9.2, 9.4.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(tenant_id: str) -> FastAPI:
    """Minimal FastAPI app mounting only the inline router we care about,
    with ``get_tenant_context`` overridden to the requested tenant."""
    import inline_endpoints
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    async def _override_tenant() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-a",
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()
    app.include_router(inline_endpoints.router)
    app.dependency_overrides[get_tenant_context] = _override_tenant
    return app


# ===========================================================================
# Upload CSV — tenant stamping on every document
# ===========================================================================


def test_upload_csv_stamps_tenant_on_every_document() -> None:
    """Every doc handed to ``upsert_batch_data`` must carry the authenticated tenant id."""
    app = _build_app(tenant_id=TENANT_A)

    csv_bytes = (
        "order_id,customer,status,value,items\n"
        "ORD-1,Alice,pending,100,widgets\n"
        "ORD-2,Bob,pending,200,gadgets\n"
    ).encode("utf-8")

    captured: Dict[str, Any] = {}

    async def _fake_upsert(*args, **kwargs):
        captured["call_kwargs"] = kwargs
        captured["call_args"] = args
        return {"status": "success", "recordCount": len(kwargs.get("documents") or [])}

    with patch("services.data_seeder.data_seeder.upsert_batch_data", new=_fake_upsert):
        with TestClient(app) as client:
            resp = client.post(
                "/api/upload/csv",
                files={"file": ("orders.csv", csv_bytes, "text/csv")},
                data={
                    "data_type": "orders",
                    "batch_id": "batch-1",
                    "operational_time": "09:00",
                },
            )

    assert resp.status_code == 200, resp.text
    kwargs = captured.get("call_kwargs") or {}
    # The tenant id was passed through to the seeder.
    assert kwargs.get("tenant_id") == TENANT_A
    # And every generated document is stamped with it.
    documents = kwargs.get("documents") or []
    assert documents, "no documents were uploaded"
    assert all(doc.get("tenant_id") == TENANT_A for doc in documents), documents


def test_upload_csv_under_different_tenant_stamps_that_tenant() -> None:
    """Tenant id is scoped per-request; tenant-B's upload carries tenant-B's id."""
    app = _build_app(tenant_id=TENANT_B)

    csv_bytes = (
        "order_id,customer,status,value,items\n"
        "ORD-1,Alice,pending,100,widgets\n"
    ).encode("utf-8")

    captured: Dict[str, Any] = {}

    async def _fake_upsert(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"status": "success", "recordCount": 1}

    with patch("services.data_seeder.data_seeder.upsert_batch_data", new=_fake_upsert):
        with TestClient(app) as client:
            resp = client.post(
                "/api/upload/csv",
                files={"file": ("orders.csv", csv_bytes, "text/csv")},
                data={
                    "data_type": "orders",
                    "batch_id": "batch-1",
                    "operational_time": "09:00",
                },
            )

    assert resp.status_code == 200, resp.text
    kwargs = captured.get("kwargs") or {}
    assert kwargs.get("tenant_id") == TENANT_B
    assert all(doc.get("tenant_id") == TENANT_B for doc in kwargs.get("documents") or [])


# ===========================================================================
# convert_csv_row_to_document — Nairobi fallback dropped
# ===========================================================================


def test_convert_csv_row_skips_when_location_unknown() -> None:
    """A fleet row with no geocoding and no known location must be skipped
    (returns None) instead of falling back to a hard-coded Nairobi station."""
    from inline_endpoints import convert_csv_row_to_document

    row = {
        "truck_id": "T-404",
        "driver_name": "Ghost",
        "current_location": "Unknown Place 9000",
        "destination": "Nowhere",
    }
    result = convert_csv_row_to_document(row, "trucks", tenant_id=TENANT_A)
    assert result is None


def test_convert_csv_row_stamps_tenant_id_when_provided() -> None:
    """When geocoding is present, the returned doc carries the tenant id."""
    from inline_endpoints import convert_csv_row_to_document

    row = {
        "order_id": "ORD-1",
        "customer": "Alice",
        "status": "pending",
        "value": "100",
        "items": "widgets",
    }
    result = convert_csv_row_to_document(row, "orders", tenant_id=TENANT_A)
    assert result is not None
    assert result["tenant_id"] == TENANT_A


def test_convert_csv_row_without_tenant_is_unstamped() -> None:
    """Back-compat: legacy callers that pass no tenant_id see the doc with no tenant_id."""
    from inline_endpoints import convert_csv_row_to_document

    row = {
        "order_id": "ORD-1",
        "customer": "Alice",
        "status": "pending",
        "value": "100",
        "items": "widgets",
    }
    result = convert_csv_row_to_document(row, "orders")
    assert result is not None
    assert "tenant_id" not in result


# ===========================================================================
# Location webhooks — reject cross-tenant truck ids, stamp tenant on writes
# ===========================================================================


class _FakeDataIngestionService:
    """Test double for the container's ``data_ingestion_service``."""

    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.batch_calls: List[List[Any]] = []
        self._last_update: Optional[Any] = None
        self._result_success = True

    async def process_location_update(self, update):
        self.calls.append(update)
        self._last_update = update
        from ingestion.service import LocationUpdateResult

        return LocationUpdateResult(
            success=True,
            truck_id=update.asset_id or update.truck_id,
            message="ok",
        )

    async def process_batch_updates(self, updates):
        self.batch_calls.append(list(updates))
        from ingestion.service import BatchUpdateResult, LocationUpdateResult

        results = [
            LocationUpdateResult(
                success=True,
                truck_id=u.asset_id or u.truck_id,
                message="ok",
            )
            for u in updates
        ]
        return BatchUpdateResult(
            total=len(results),
            successful=len(results),
            failed=0,
            results=results,
        )


def _install_fake_container(app: FastAPI, service: _FakeDataIngestionService) -> None:
    container = MagicMock()
    container.data_ingestion_service = service
    app.state.container = container


def test_location_webhook_stamps_authenticated_tenant() -> None:
    """Regardless of what the caller claims in the body, the ingestion
    service sees the JWT-derived tenant_id on the update."""
    app = _build_app(tenant_id=TENANT_A)
    fake = _FakeDataIngestionService()
    _install_fake_container(app, fake)

    with TestClient(app) as client:
        # Caller attempts to spoof a different tenant in the body.
        resp = client.post(
            "/api/locations/webhook",
            json={
                "truck_id": "T-001",
                "latitude": 25.0,
                "longitude": 55.0,
                "timestamp": "2025-01-01T00:00:00Z",
                "tenant_id": TENANT_B,  # must be ignored
            },
        )

    assert resp.status_code == 200, resp.text
    assert fake.calls, "ingestion service was not called"
    update = fake.calls[0]
    assert update.tenant_id == TENANT_A, "handler did not override spoofed tenant_id"


def test_location_batch_stamps_authenticated_tenant_on_every_update() -> None:
    app = _build_app(tenant_id=TENANT_A)
    fake = _FakeDataIngestionService()
    _install_fake_container(app, fake)

    with TestClient(app) as client:
        resp = client.post(
            "/api/locations/batch",
            json={
                "updates": [
                    {
                        "truck_id": "T-001",
                        "latitude": 25.0,
                        "longitude": 55.0,
                        "timestamp": "2025-01-01T00:00:00Z",
                        "tenant_id": TENANT_B,  # spoof attempt 1
                    },
                    {
                        "truck_id": "T-002",
                        "latitude": 26.0,
                        "longitude": 56.0,
                        "timestamp": "2025-01-01T00:00:00Z",
                    },
                ]
            },
        )

    assert resp.status_code == 200, resp.text
    assert fake.batch_calls, "ingestion service was not called"
    updates = fake.batch_calls[0]
    assert all(u.tenant_id == TENANT_A for u in updates), (
        f"tenant_id should have been stamped to {TENANT_A} on every update: "
        f"{[u.tenant_id for u in updates]}"
    )


# ===========================================================================
# Ingestion service — cross-tenant truck id is rejected
# ===========================================================================


@pytest.mark.asyncio
async def test_ingestion_rejects_cross_tenant_truck_id() -> None:
    """When ``tenant_id`` is set on the update, ``validate_asset_exists``
    only matches if the asset carries the same tenant id — so a caller for
    tenant A cannot write against an asset that belongs to tenant B."""
    from ingestion.service import DataIngestionService, LocationUpdate
    from errors.exceptions import AppException

    # ES double that only returns hits for tenant-A. The webhook handler
    # stamps tenant_id=tenant-B, so validate_asset_exists must come back
    # empty and the process must fail with a 404-style AppException.
    es = MagicMock()

    async def _search(index, query, size=1):
        # Respect the tenant_id filter the service issues.
        filters = query.get("query", {}).get("bool", {}).get("filter", [])
        has_tenant_a = any(
            entry.get("term", {}).get("tenant_id") == TENANT_A for entry in filters
        )
        if has_tenant_a:
            return {"hits": {"hits": [{"_source": {"truck_id": "T-owned-by-A"}}], "total": {"value": 1}}}
        return {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = _search
    es.index_document = AsyncMock(return_value={"result": "created"})

    service = DataIngestionService(es_service=es, connection_manager=None)

    # Caller authenticated as tenant-B tries to update a truck that only
    # exists under tenant-A. The update must be rejected.
    update = LocationUpdate(
        truck_id="T-owned-by-A",
        latitude=25.0,
        longitude=55.0,
        timestamp="2025-01-01T00:00:00Z",
        tenant_id=TENANT_B,
    )

    with pytest.raises(AppException):
        await service.process_location_update(update)

    # The trucks and locations writes must never have fired.
    assert not es.index_document.called, "cross-tenant update reached the write path"


@pytest.mark.asyncio
async def test_ingestion_stamps_tenant_on_history_writes() -> None:
    """Successful updates carry ``tenant_id`` on both the truck and history writes."""
    from ingestion.service import DataIngestionService, LocationUpdate

    es = MagicMock()

    async def _search(index, query, size=1):
        # Pretend the asset exists and belongs to tenant A.
        return {"hits": {"hits": [{"_source": {"truck_id": "T-owned-by-A"}}], "total": {"value": 1}}}

    es.search_documents = _search
    index_calls: List[Tuple[str, str, Dict[str, Any]]] = []

    async def _index(index: str, doc_id: str, document: Dict[str, Any]):
        index_calls.append((index, doc_id, dict(document)))

    es.index_document = _index

    service = DataIngestionService(es_service=es, connection_manager=None)

    update = LocationUpdate(
        truck_id="T-owned-by-A",
        latitude=25.0,
        longitude=55.0,
        timestamp="2025-01-01T00:00:00Z",
        tenant_id=TENANT_A,
    )

    result = await service.process_location_update(update)
    assert result.success is True

    # Both the trucks (current location) write and the locations history
    # write carry tenant_id.
    assert index_calls, "no writes happened"
    for index, _doc_id, document in index_calls:
        assert document.get("tenant_id") == TENANT_A, (
            f"{index} write missing tenant_id: {document}"
        )
