"""
Tests for the PIIMasker class and PII masking integration.

Validates: Requirements 22.1, 22.2, 22.3, 22.4, 22.5
"""

import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.middleware.pii_masker import PIIMasker, log_pii_access


# ---------------------------------------------------------------------------
# Unit tests for PIIMasker
# ---------------------------------------------------------------------------


class TestMaskPhone:
    """Test phone number masking retains last 2 digits. Validates: Req 22.3"""

    def test_standard_phone(self):
        masker = PIIMasker()
        assert masker.mask_phone("+1-555-123-4567") == "+XX-XXXX-XX67"

    def test_phone_with_spaces(self):
        masker = PIIMasker()
        assert masker.mask_phone("+44 20 7946 0958") == "+XX-XXXX-XX58"

    def test_short_phone(self):
        masker = PIIMasker()
        assert masker.mask_phone("+1234567") == "+XX-XXXX-XX67"

    def test_phone_only_digits(self):
        masker = PIIMasker()
        assert masker.mask_phone("5551234567") == "+XX-XXXX-XX67"

    def test_phone_single_digit_returns_stars(self):
        masker = PIIMasker()
        assert masker.mask_phone("5") == "***"


class TestMaskEmail:
    """Test email masking produces ***@***.tld. Validates: Req 22.3"""

    def test_standard_email(self):
        masker = PIIMasker()
        assert masker.mask_email("john@example.com") == "***@***.com"

    def test_email_with_subdomain(self):
        masker = PIIMasker()
        assert masker.mask_email("user@mail.example.org") == "***@***.org"

    def test_email_with_plus(self):
        masker = PIIMasker()
        assert masker.mask_email("user+tag@domain.net") == "***@***.net"

    def test_email_no_tld(self):
        masker = PIIMasker()
        # Edge case: no dot in email
        assert masker.mask_email("user@localhost") == "***@***.com"


class TestMaskResponse:
    """Test recursive PII masking in response dicts. Validates: Req 22.1, 22.4"""

    def setup_method(self):
        self.masker = PIIMasker()

    def test_masks_name_fields(self):
        data = {
            "shipment_id": "SH-001",
            "customer_name": "John Doe",
            "recipient_name": "Jane Smith",
            "sender_name": "Bob Wilson",
        }
        result = self.masker.mask_response(data)
        assert result["customer_name"] == "***"
        assert result["recipient_name"] == "***"
        assert result["sender_name"] == "***"
        assert result["shipment_id"] == "SH-001"

    def test_masks_phone_in_value(self):
        data = {"phone": "+1-555-123-4567"}
        result = self.masker.mask_response(data)
        assert result["phone"] == "+XX-XXXX-XX67"

    def test_masks_email_in_value(self):
        data = {"email": "john@example.com"}
        result = self.masker.mask_response(data)
        assert result["email"] == "***@***.com"

    def test_masks_nested_dict(self):
        data = {
            "shipment": {
                "customer_name": "Alice",
                "contact": {"email": "alice@test.com"},
            }
        }
        result = self.masker.mask_response(data)
        assert result["shipment"]["customer_name"] == "***"
        assert result["shipment"]["contact"]["email"] == "***@***.com"

    def test_masks_list_of_dicts(self):
        data = [
            {"customer_name": "Alice", "status": "delivered"},
            {"customer_name": "Bob", "status": "pending"},
        ]
        result = self.masker.mask_response(data)
        assert result[0]["customer_name"] == "***"
        assert result[1]["customer_name"] == "***"
        assert result[0]["status"] == "delivered"

    def test_pii_access_bypasses_masking(self):
        data = {"customer_name": "John Doe", "email": "john@example.com"}
        result = self.masker.mask_response(data, has_pii_access=True)
        assert result["customer_name"] == "John Doe"
        assert result["email"] == "john@example.com"

    def test_does_not_mutate_original(self):
        data = {"customer_name": "John Doe"}
        self.masker.mask_response(data)
        assert data["customer_name"] == "John Doe"

    def test_non_pii_fields_unchanged(self):
        data = {
            "shipment_id": "SH-001",
            "status": "delivered",
            "rider_id": "R-001",
            "tenant_id": "T-001",
        }
        result = self.masker.mask_response(data)
        assert result == data

    def test_empty_dict(self):
        assert self.masker.mask_response({}) == {}

    def test_empty_list(self):
        assert self.masker.mask_response([]) == []


class TestLogPiiAccess:
    """Test PII access logging. Validates: Req 22.5"""

    def test_logs_pii_access_event(self, caplog):
        with caplog.at_level(logging.INFO, logger="ops.middleware.pii_masker"):
            log_pii_access(
                user_id="u1",
                tenant_id="t1",
                fields_accessed=["customer_name", "email"],
                endpoint="GET /ops/shipments",
            )
        assert "PII access event" in caplog.text
        assert "u1" in caplog.text
        assert "t1" in caplog.text
        assert "customer_name" in caplog.text


# ---------------------------------------------------------------------------
# Integration tests: PII masking in Ops API responses
# ---------------------------------------------------------------------------

# Prevent the real ElasticsearchService from connecting on import
_mock_es_module = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.api.endpoints import configure_ops_api, router  # noqa: E402
from ops.middleware.tenant_guard import TenantContext, get_tenant_context  # noqa: E402
from ops.services.ops_es_service import OpsElasticsearchService  # noqa: E402


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _tenant_ctx_no_pii() -> TenantContext:
    return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=False)


def _tenant_ctx_with_pii() -> TenantContext:
    return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=True)


def _es_response(hits: list, total: int | None = None) -> dict:
    if total is None:
        total = len(hits)
    return {
        "hits": {
            "total": {"value": total},
            "hits": [{"_source": h} for h in hits],
        }
    }


SAMPLE_SHIPMENT = {
    "shipment_id": "SH-001",
    "status": "delivered",
    "tenant_id": "t1",
    "rider_id": "R-001",
    "customer_name": "John Doe",
    "recipient_name": "Jane Smith",
    "sender_name": "Bob Wilson",
}

SAMPLE_SHIPMENT_WITH_CONTACT = {
    "shipment_id": "SH-002",
    "status": "in_transit",
    "tenant_id": "t1",
    "customer_name": "Alice",
    "phone": "+1-555-987-6543",
    "email": "alice@example.com",
}


@pytest.fixture()
def mock_es():
    svc = MagicMock(spec=OpsElasticsearchService)
    svc.client = MagicMock()
    svc.client.search = AsyncMock()
    svc.SHIPMENTS_CURRENT = OpsElasticsearchService.SHIPMENTS_CURRENT
    svc.SHIPMENT_EVENTS = OpsElasticsearchService.SHIPMENT_EVENTS
    svc.RIDERS_CURRENT = OpsElasticsearchService.RIDERS_CURRENT
    svc.POISON_QUEUE = OpsElasticsearchService.POISON_QUEUE
    return svc


def _build_client(mock_es, tenant_factory):
    app = _make_app()
    configure_ops_api(ops_es_service=mock_es)
    app.dependency_overrides[get_tenant_context] = tenant_factory

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "test-req-id"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    return TestClient(app)


class TestPIIMaskingInShipments:
    """Verify PII masking in /ops/shipments responses. Validates: Req 22.1, 22.2"""

    def test_masks_pii_when_no_access(self, mock_es):
        mock_es.client.search.return_value = _es_response([SAMPLE_SHIPMENT_WITH_CONTACT])
        client = _build_client(mock_es, _tenant_ctx_no_pii)

        resp = client.get("/ops/shipments")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["customer_name"] == "***"
        assert data[0]["phone"] == "+XX-XXXX-XX43"
        assert data[0]["email"] == "***@***.com"
        # Non-PII fields unchanged
        assert data[0]["shipment_id"] == "SH-002"
        assert data[0]["status"] == "in_transit"

    def test_unmasked_with_pii_access(self, mock_es):
        mock_es.client.search.return_value = _es_response([SAMPLE_SHIPMENT_WITH_CONTACT])
        client = _build_client(mock_es, _tenant_ctx_with_pii)

        resp = client.get("/ops/shipments")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data[0]["customer_name"] == "Alice"
        assert data[0]["phone"] == "+1-555-987-6543"
        assert data[0]["email"] == "alice@example.com"


class TestPIIMaskingInSingleShipment:
    """Verify PII masking in /ops/shipments/{id} responses."""

    def test_masks_pii_in_single_shipment(self, mock_es):
        mock_es.client.search.side_effect = [
            _es_response([SAMPLE_SHIPMENT_WITH_CONTACT]),  # shipment
            _es_response([]),  # events
        ]
        client = _build_client(mock_es, _tenant_ctx_no_pii)

        resp = client.get("/ops/shipments/SH-002")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["customer_name"] == "***"
        assert data["email"] == "***@***.com"


class TestPIIMaskingInRiders:
    """Verify PII masking in /ops/riders responses."""

    def test_masks_rider_name_fields(self, mock_es):
        rider = {
            "rider_id": "R-001",
            "status": "active",
            "tenant_id": "t1",
            "customer_name": "Rider Name",  # if present
        }
        mock_es.client.search.return_value = _es_response([rider])
        client = _build_client(mock_es, _tenant_ctx_no_pii)

        resp = client.get("/ops/riders")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data[0]["customer_name"] == "***"


class TestPIIAccessLogging:
    """Verify PII access events are logged. Validates: Req 22.5"""

    def test_logs_when_pii_access_granted(self, mock_es, caplog):
        mock_es.client.search.return_value = _es_response([SAMPLE_SHIPMENT_WITH_CONTACT])
        client = _build_client(mock_es, _tenant_ctx_with_pii)

        with caplog.at_level(logging.INFO, logger="ops.middleware.pii_masker"):
            resp = client.get("/ops/shipments")
            assert resp.status_code == 200

        assert "PII access event" in caplog.text
        assert "u1" in caplog.text
        assert "t1" in caplog.text
