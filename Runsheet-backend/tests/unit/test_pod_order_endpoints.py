"""
Unit tests for the order-keyed POD sibling ``POST /api/driver/orders/{order_id}/pod``.

The handler carries no rule of its own: it resolves the path parameter through
:meth:`WorkRefResolver.resolve_order`, delegates to the same
:class:`PODSubmissionService` the job-keyed handler calls, and stores the
idempotency response. These tests cover what the order-keyed path adds — the
``order_id`` taken from the path parameter rather than from a job document, the
resolver's 404/403 outcomes, and the shared ``X-Idempotency-Key`` handling.

Validates: Requirements 5.20, 5.21, 5.23, 7.19
"""

from __future__ import annotations

import sys
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.api.pod_endpoints import configure_pod_endpoints, router as pod_router
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.driver_es_mappings import IDEMPOTENCY_KEYS_INDEX
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT_ID = "t1"
DRIVER_ID = "driver-1"
OTHER_DRIVER_ID = "driver-2"
ORDER_ID = "ord_00000000000000000000000000000001"

_SIGNATURE_REF = (
    f"tenants/{TENANT_ID}/signature/2024/01/15/"
    "11111111-1111-1111-1111-111111111111.png"
)
_PHOTO_REF = (
    f"tenants/{TENANT_ID}/photo/2024/01/15/"
    "22222222-2222-2222-2222-222222222222.jpg"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(tenant_id: str = TENANT_ID, **extra) -> dict:
    headers = auth_headers(
        tenant_id, sub=DRIVER_ID, roles=["driver"], driver_id=DRIVER_ID
    )
    headers.update(extra)
    return headers


def _make_es_service() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _make_file_storage() -> MagicMock:
    def _validate_ref(tenant_id: str, file_ref: str, actor=None) -> bool:
        if not file_ref or not file_ref.startswith(f"tenants/{tenant_id}/"):
            raise PermissionError("cross_tenant_file_ref")
        return True

    svc = MagicMock()
    svc.validate_ref = MagicMock(side_effect=_validate_ref)
    return svc


class FakeOrderRepository:
    """Minimal stand-in for ``FuelOrderRepository.get``."""

    def __init__(self, order: Optional[dict]) -> None:
        self._order = order
        self.calls: list[tuple[str, str]] = []

    async def get(self, tenant_id: str, order_id: str):
        self.calls.append((tenant_id, order_id))
        if self._order is None:
            return None
        if self._order.get("order_id") != order_id:
            return None
        return dict(self._order)


def _order_doc(
    assigned_driver_id: Optional[str] = DRIVER_ID,
    tenant_id: str = TENANT_ID,
) -> dict:
    return {
        "order_id": ORDER_ID,
        "tenant_id": tenant_id,
        "assigned_driver_id": assigned_driver_id,
        "status": "in_transit",
    }


def _make_app(
    es_service=None,
    order_repository=None,
    job_service=None,
    scheduling_ws=None,
    driver_ws=None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pod_router)

    configure_pod_endpoints(
        es_service=es_service or _make_es_service(),
        job_service=job_service,
        order_repository=order_repository,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
        file_storage_service=_make_file_storage(),
    )
    install_test_auth(app)
    return app


def _pod_payload() -> dict:
    """A complete order-keyed POD submission.

    ``delivered_gallons`` is present because a non-refusal that resolves no
    gallon count is rejected (R5.12) — the shared service applies that rule on
    both paths.
    """
    return {
        "recipient_name": "John Doe",
        "signature_ref": _SIGNATURE_REF,
        "photo_refs": [_PHOTO_REF],
        "delivered_gallons": 500.0,
        "geotag": {"lat": -33.8688, "lng": 151.2093},
        "timestamp": "2024-01-15T10:30:00Z",
    }


class FakeIdempotencyES:
    """In-memory stand-in for the ``idempotency_keys`` index."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        assert index == IDEMPOTENCY_KEYS_INDEX
        self.docs[doc_id] = document

    async def get_document(self, index: str, doc_id: str):
        assert index == IDEMPOTENCY_KEYS_INDEX
        return self.docs.get(doc_id)


@pytest.fixture
def idempotency_store():
    """Register a module-global idempotency middleware, then restore."""
    previous = idempotency_module.get_idempotency_middleware()
    store = FakeIdempotencyES()
    configure_idempotency_middleware(es_service=store)
    try:
        yield store
    finally:
        idempotency_module._idempotency_middleware = previous


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubmitPodForOrder:
    """Tests for POST /api/driver/orders/{order_id}/pod."""

    def test_stores_pod_with_order_id_from_path(self):
        """The POD's order_id is the path parameter, and no job doc is read.

        Validates: Requirements 5.20, 5.21
        """
        es = _make_es_service()
        job_svc = MagicMock()
        job_svc._get_job_doc = AsyncMock(return_value=None)
        job_svc._append_event = AsyncMock(return_value="evt-1")
        repo = FakeOrderRepository(_order_doc())
        app = _make_app(es_service=es, order_repository=repo, job_service=job_svc)

        resp = TestClient(app).post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=_pod_payload(),
            headers=_headers(),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["order_id"] == ORDER_ID
        assert data["job_id"] is None
        assert data["driver_id"] == DRIVER_ID
        assert data["tenant_id"] == TENANT_ID
        assert repo.calls == [(TENANT_ID, ORDER_ID)]
        job_svc._get_job_doc.assert_not_called()

        pod_index_calls = [
            call
            for call in es.index_document.call_args_list
            if "proof_of_delivery" in str(call.args[0])
        ]
        assert pod_index_calls, es.index_document.call_args_list

    def test_unknown_order_returns_404(self):
        """An order absent from the caller's tenant is a 404.

        Validates: Requirement 5.21
        """
        app = _make_app(order_repository=FakeOrderRepository(None))

        resp = TestClient(app).post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=_pod_payload(),
            headers=_headers(),
        )

        assert resp.status_code == 404, resp.text

    def test_order_assigned_to_another_driver_returns_403(self):
        """assigned_driver_id must equal the caller's canonical driver_id.

        Validates: Requirement 5.21
        """
        repo = FakeOrderRepository(_order_doc(assigned_driver_id=OTHER_DRIVER_ID))
        app = _make_app(order_repository=repo)

        resp = TestClient(app).post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=_pod_payload(),
            headers=_headers(),
        )

        assert resp.status_code == 403, resp.text

    def test_shares_job_keyed_validation_rules(self):
        """A non-refusal without a signature is rejected here exactly as on the
        job-keyed path, because there is one implementation.

        Validates: Requirements 5.23, 7.19
        """
        repo = FakeOrderRepository(_order_doc())
        app = _make_app(order_repository=repo)
        payload = _pod_payload()
        payload.pop("signature_ref")

        resp = TestClient(app).post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=payload,
            headers=_headers(),
        )

        assert resp.status_code == 400, resp.text
        assert "signature_ref" in resp.text

    def test_idempotency_key_replays_stored_response(self, idempotency_store):
        """A repeated X-Idempotency-Key replays the stored response.

        Validates: Requirement 5.20
        """
        repo = FakeOrderRepository(_order_doc())
        app = _make_app(order_repository=repo)
        client = TestClient(app)
        headers = _headers(**{"X-Idempotency-Key": "idem-order-pod-1"})

        first = client.post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=_pod_payload(),
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert idempotency_store.docs

        second = client.post(
            f"/api/driver/orders/{ORDER_ID}/pod",
            json=_pod_payload(),
            headers=headers,
        )

        assert second.status_code == 200, second.text
        assert second.headers.get("X-Idempotent-Replayed") == "true"
        assert second.json()["data"]["pod_id"] == first.json()["data"]["pod_id"]
