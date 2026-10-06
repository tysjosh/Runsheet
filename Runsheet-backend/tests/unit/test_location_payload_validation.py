"""
Regression tests for B7: invalid location payloads are a 422, not a 500.

``POST /api/locations/webhook`` and ``POST /api/locations/batch`` used to
build their pydantic models straight from ``await request.json()``, so a
missing field, a non-JSON body or an empty ``updates`` list escaped as an
unhandled exception (500). They now return a 422 ``VALIDATION_ERROR``
envelope and never reach the ingestion service.
"""
from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT_A = "tenant-a"


class _RecordingIngestion:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def process_location_update(self, update):  # pragma: no cover - must not run
        self.calls.append(update)
        raise AssertionError("ingestion must not be called for an invalid payload")

    async def process_batch_updates(self, updates):  # pragma: no cover - must not run
        self.calls.append(updates)
        raise AssertionError("ingestion must not be called for an invalid payload")


def _build_app() -> tuple[FastAPI, _RecordingIngestion]:
    import inline_endpoints
    from errors.handlers import register_exception_handlers
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    async def _override_tenant() -> TenantContext:
        return TenantContext(
            tenant_id=TENANT_A,
            user_id="user-a",
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(inline_endpoints.router)
    app.dependency_overrides[get_tenant_context] = _override_tenant
    fake = _RecordingIngestion()
    container = MagicMock()
    container.data_ingestion_service = fake
    app.state.container = container
    return app, fake


def _assert_422(resp, fake: _RecordingIngestion) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"truck_id": "T-001", "timestamp": "2025-01-01T00:00:00Z"},  # no lat/lon
        {"truck_id": "T-001", "latitude": 95.0, "longitude": 55.0,
         "timestamp": "2025-01-01T00:00:00Z"},  # latitude out of range
        {},
    ],
)
def test_webhook_invalid_payload_is_422(payload) -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/locations/webhook", json=payload)
    _assert_422(resp, fake)
    # Field errors are reported without echoing the submitted input.
    errors = resp.json()["details"]["errors"]
    assert errors and all("input" not in e for e in errors)


def test_webhook_non_json_body_is_422() -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/locations/webhook",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
    _assert_422(resp, fake)


def test_webhook_json_array_body_is_422() -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/locations/webhook", json=[1, 2])
    _assert_422(resp, fake)


def test_batch_empty_updates_is_422() -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/locations/batch", json={"updates": []})
    _assert_422(resp, fake)


def test_batch_invalid_item_is_422() -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/locations/batch",
            json={"updates": [{"truck_id": "T-001", "timestamp": "2025-01-01T00:00:00Z"}]},
        )
    _assert_422(resp, fake)


def test_batch_non_json_body_is_422() -> None:
    app, fake = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/locations/batch",
            content=b"{broken",
            headers={"Content-Type": "application/json"},
        )
    _assert_422(resp, fake)
