"""
Unit tests for the repaired driver idempotency dependency.

``check_idempotency`` used to read ``request.state.tenant_id`` and fall back to
the literal ``"unknown"``, so its lookup key was ``unknown:{key}`` while
``store_idempotency_response`` wrote ``{tenant_id}:{key}``. The two never
agreed and no replay was ever served. The dependency now takes the tenant from
the tenant guard (``Depends(get_tenant_context)``), which makes the check key
and the store key identical and removes the reliance on the incidental order in
which a handler declares its dependencies.

These tests drive a one-route app through the Test_Auth_Path seam against an
in-memory stand-in for the ``idempotency_keys`` index.

Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.middleware.idempotency import (
    IdempotencyMiddleware,
    IdempotencyResult,
    check_idempotency,
    configure_idempotency_middleware,
    store_idempotency_response,
)
from driver.services.driver_es_mappings import IDEMPOTENCY_KEYS_INDEX
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT_A = "t1"
TENANT_B = "t2"
DRIVER_ID = "drv_1"
KEY = "idem-key-1"


class FakeES:
    """Minimal in-memory stand-in for the two ES calls the store uses."""

    def __init__(self) -> None:
        self.docs: Dict[str, dict] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        assert index == IDEMPOTENCY_KEYS_INDEX
        self.docs[doc_id] = document

    async def get_document(self, index: str, doc_id: str) -> Optional[dict]:
        assert index == IDEMPOTENCY_KEYS_INDEX
        return self.docs.get(doc_id)


@pytest.fixture
def fake_es() -> FakeES:
    return FakeES()


@pytest.fixture
def configured(fake_es: FakeES):
    """Register the module-global middleware and tear it back down."""
    previous = idempotency_module.get_idempotency_middleware()
    middleware = configure_idempotency_middleware(es_service=fake_es)
    try:
        yield middleware
    finally:
        idempotency_module._idempotency_middleware = previous


@pytest.fixture
def client(configured, fake_es: FakeES):
    """A one-route app that replays, or processes and stores, per the design."""
    app = FastAPI()
    register_exception_handlers(app)
    calls: list[str] = []

    @app.post("/api/driver/echo")
    async def echo(
        request: Request,
        tenant: TenantContext = Depends(get_tenant_context),
        idempotency: IdempotencyResult = Depends(check_idempotency),
    ):
        if idempotency.is_replay:
            return idempotency.replay_response()
        calls.append(tenant.tenant_id)
        body = {"call_count": len(calls), "tenant_id": tenant.tenant_id}
        if idempotency.key:
            await store_idempotency_response(
                idempotency.key, tenant.tenant_id, body, 201
            )
        return JSONResponse(content=body, status_code=201)

    install_test_auth(app)
    test_client = TestClient(app)
    test_client.calls = calls  # type: ignore[attr-defined]
    return test_client


def _headers(tenant_id: str, key: Optional[str] = None) -> dict:
    headers = auth_headers(tenant_id, roles=["driver"], driver_id=DRIVER_ID)
    if key:
        headers["X-Idempotency-Key"] = key
    return headers


class TestCheckKeyMatchesStoreKey:
    """The check key is the verified tenant's key (Req 11.1, 11.2)."""

    def test_repeated_key_replays_stored_body_and_status(self, client, fake_es):
        first = client.post("/api/driver/echo", headers=_headers(TENANT_A, KEY))
        assert first.status_code == 201
        assert first.headers.get("X-Idempotent-Replayed") is None

        second = client.post("/api/driver/echo", headers=_headers(TENANT_A, KEY))
        assert second.status_code == 201
        assert second.json() == first.json()
        assert second.headers["X-Idempotent-Replayed"] == "true"
        # The handler body ran exactly once.
        assert client.calls == [TENANT_A]

    def test_stored_document_id_is_tenant_scoped(self, client, fake_es):
        client.post("/api/driver/echo", headers=_headers(TENANT_A, KEY))
        assert list(fake_es.docs) == [f"{TENANT_A}:{KEY}"]

    def test_distinct_keys_are_not_deduplicated(self, client):
        client.post("/api/driver/echo", headers=_headers(TENANT_A, KEY))
        second = client.post("/api/driver/echo", headers=_headers(TENANT_A, "other"))
        assert second.headers.get("X-Idempotent-Replayed") is None
        assert client.calls == [TENANT_A, TENANT_A]


class TestCrossTenantIsolation:
    """A key stored for another tenant is a first-time request (Req 11.4)."""

    def test_same_key_other_tenant_is_processed(self, client, fake_es):
        client.post("/api/driver/echo", headers=_headers(TENANT_A, KEY))
        response = client.post("/api/driver/echo", headers=_headers(TENANT_B, KEY))
        assert response.status_code == 201
        assert response.headers.get("X-Idempotent-Replayed") is None
        assert response.json()["tenant_id"] == TENANT_B
        assert client.calls == [TENANT_A, TENANT_B]
        assert sorted(fake_es.docs) == [f"{TENANT_A}:{KEY}", f"{TENANT_B}:{KEY}"]


class TestNoHeaderNoDeduplication:
    """No ``X-Idempotency-Key`` header means no deduplication (Req 11.5)."""

    def test_requests_without_the_header_are_never_replayed(self, client, fake_es):
        first = client.post("/api/driver/echo", headers=_headers(TENANT_A))
        second = client.post("/api/driver/echo", headers=_headers(TENANT_A))
        assert first.status_code == second.status_code == 201
        assert second.headers.get("X-Idempotent-Replayed") is None
        assert client.calls == [TENANT_A, TENANT_A]
        assert fake_es.docs == {}


class TestExpiry:
    """A stored entry stops replaying 24 hours after creation (Req 11.3)."""

    @pytest.mark.asyncio
    async def test_ttl_is_twenty_four_hours_with_no_grace_period(self, fake_es):
        middleware = IdempotencyMiddleware(es_service=fake_es)
        await middleware.store_response(KEY, TENANT_A, {"ok": True}, 201)
        doc = fake_es.docs[f"{TENANT_A}:{KEY}"]
        created = datetime.fromisoformat(doc["created_at"])
        expires = datetime.fromisoformat(doc["expires_at"])
        assert expires - created == timedelta(hours=24)
