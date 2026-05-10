"""Smoke tests for the Price Protection REST endpoints (Task 4.7).

Covers the five endpoints mounted under
:data:`commerce.api.price_protection_endpoints.router`:

* ``POST   /api/commerce/price-protection-contracts``           — create
* ``GET    /api/commerce/price-protection-contracts``           — list
* ``GET    /api/commerce/price-protection-contracts/{id}``      — fetch single
* ``PUT    /api/commerce/price-protection-contracts/{id}``      — update
* ``GET    /api/commerce/price-protection-contracts/{id}/variance``
                                                                 — report

Mirrors the test pattern established by
``tests/unit/test_tax_endpoints.py``: an in-memory ``_FakeESService``
stands in for the real :class:`ElasticsearchService` so the tests
exercise the router's tenant-filter construction and response shaping
without touching an external cluster. The fake implements only the
``term`` / ``range`` / ``bool`` clauses that the handlers actually
emit.

Validates: Requirement 3 (CRUD + variance)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api.price_protection_endpoints import (
    configure_price_protection_api,
    router,
)
from commerce.services.commerce_es_mappings import INVOICE_EVENTS_INDEX
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# In-memory ES fake
# ---------------------------------------------------------------------------


def _match(doc: Dict[str, Any], clause: Dict[str, Any]) -> bool:
    """Return True if ``doc`` matches a single ES query clause."""
    if not isinstance(clause, dict) or not clause:
        return True
    if "match_all" in clause:
        return True
    if "term" in clause:
        for field, value in clause["term"].items():
            field_value = doc
            for part in field.split("."):
                if not isinstance(field_value, dict):
                    return False
                field_value = field_value.get(part)
            if isinstance(field_value, list):
                if value not in field_value:
                    return False
            elif field_value != value:
                return False
        return True
    if "terms" in clause:
        for field, values in clause["terms"].items():
            if doc.get(field) not in values:
                return False
        return True
    if "range" in clause:
        for field, spec in clause["range"].items():
            value = doc.get(field)
            if value is None:
                return False
            if "lte" in spec and not (str(value) <= str(spec["lte"])):
                return False
            if "gte" in spec and not (str(value) >= str(spec["gte"])):
                return False
        return True
    if "exists" in clause:
        field = clause["exists"]["field"]
        return doc.get(field) is not None
    if "bool" in clause:
        bool_clause = clause["bool"]
        for sub in bool_clause.get("must", []) or []:
            if not _match(doc, sub):
                return False
        for sub in bool_clause.get("filter", []) or []:
            if not _match(doc, sub):
                return False
        must_not = bool_clause.get("must_not", []) or []
        for sub in must_not:
            if _match(doc, sub):
                return False
        should = bool_clause.get("should", []) or []
        if should:
            min_should = bool_clause.get("minimum_should_match", 1)
            matched = sum(1 for sub in should if _match(doc, sub))
            if matched < min_should:
                return False
        return True
    return False


class _FakeESService:
    """Minimal async ES stub for the price-protection endpoint tests."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.index_calls: List[tuple] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.index_calls.append((index, doc_id, dict(document)))
        self.docs.setdefault(index, {})[doc_id] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        bucket = self.docs.get(index, {})
        q = query.get("query", {})
        hits: List[Dict[str, Any]] = []
        for doc in bucket.values():
            if _match(doc, q):
                hits.append({"_source": dict(doc)})
        return {"hits": {"hits": hits[:size]}}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-A"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["operator"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    tenant_id: str = "tenant-A",
) -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    configure_price_protection_api(es_service=es)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _seed_contract(
    es: _FakeESService,
    *,
    contract_id: str = "contract-1",
    tenant_id: str = "tenant-A",
    customer_id: str = "cust-1",
    product_code: str = "HEATING_OIL",
    contract_type: str = "fixed_price",
    status: str = "active",
    contracted_gallons: float = 1000.0,
    remaining_gallons: float = 1000.0,
    fixed_price_cents: int = 300,
    start_date: str = "2026-01-01",
    end_date: str = "2026-12-31",
) -> Dict[str, Any]:
    """Seed a pre-validated contract document into the fake ES."""
    doc: Dict[str, Any] = {
        "contract_id": contract_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": "acct-1",
        "product_code": product_code,
        "contract_type": contract_type,
        "start_date": start_date,
        "end_date": end_date,
        "contracted_gallons": contracted_gallons,
        "remaining_gallons": remaining_gallons,
        "price_cap_cents": None,
        "price_floor_cents": None,
        "fixed_price_cents": fixed_price_cents,
        "status": status,
        "version": 0,
        "notes": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    if contract_type == "cap_price":
        doc["fixed_price_cents"] = None
        doc["price_cap_cents"] = fixed_price_cents
    elif contract_type == "collar":
        doc["fixed_price_cents"] = None
        doc["price_cap_cents"] = fixed_price_cents
        doc["price_floor_cents"] = max(fixed_price_cents - 50, 0)
    es.docs.setdefault(PRICE_PROTECTION_CONTRACTS_INDEX, {})[contract_id] = doc
    return doc


# ---------------------------------------------------------------------------
# POST /api/commerce/price-protection-contracts — create
# ---------------------------------------------------------------------------


class TestCreateContract:
    """Smoke coverage for POST contract create (Req 3.1, 3.2)."""

    def test_creates_fixed_price_contract_and_indexes_to_es(self):
        app, es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/commerce/price-protection-contracts",
            json={
                "customer_id": "cust-1",
                "account_id": "acct-1",
                "product_code": "HEATING_OIL",
                "contract_type": "fixed_price",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "contracted_gallons": 1000.0,
                "fixed_price_cents": 300,
                "notes": "Winter lock-in",
            },
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]

        # Server-assigned fields stamped correctly
        assert data["contract_id"].startswith("contract_")
        assert data["tenant_id"] == "tenant-A"
        assert data["status"] == "active"
        assert data["version"] == 0
        # remaining_gallons defaults to contracted_gallons
        assert data["remaining_gallons"] == 1000.0
        assert data["fixed_price_cents"] == 300
        assert data["notes"] == "Winter lock-in"

        # ES index_document called once with correct doc id
        assert len(es.index_calls) == 1
        index, doc_id, document = es.index_calls[0]
        assert index == PRICE_PROTECTION_CONTRACTS_INDEX
        assert doc_id == data["contract_id"]
        assert document["tenant_id"] == "tenant-A"

    def test_rejects_fixed_price_without_fixed_price_cents(self):
        """Contract-type / pricing-field coherence is enforced (Req 3.1)."""
        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/commerce/price-protection-contracts",
            json={
                "customer_id": "cust-1",
                "account_id": "acct-1",
                "product_code": "HEATING_OIL",
                "contract_type": "fixed_price",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "contracted_gallons": 1000.0,
                # fixed_price_cents omitted on purpose
            },
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        # Custom validator failures return a dict; Pydantic schema
        # failures return a list. Our model validators run post-
        # schema, so the response is a dict.
        if isinstance(detail, dict):
            assert (
                detail["error_code"]
                == "price_protection_contract.invalid_payload"
            )

    def test_rejects_end_before_start(self):
        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/commerce/price-protection-contracts",
            json={
                "customer_id": "cust-1",
                "account_id": "acct-1",
                "product_code": "HEATING_OIL",
                "contract_type": "fixed_price",
                "start_date": "2026-12-31",
                "end_date": "2026-01-01",
                "contracted_gallons": 1000.0,
                "fixed_price_cents": 300,
            },
        )

        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts — list
# ---------------------------------------------------------------------------


class TestListContracts:
    """Smoke coverage for GET list + filters (Req 3.1)."""

    def test_returns_all_rows_for_tenant(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-1", tenant_id="tenant-A"
        )
        _seed_contract(
            es, contract_id="contract-2", tenant_id="tenant-A",
            customer_id="cust-2",
        )
        # Cross-tenant row — must not leak into the response.
        _seed_contract(
            es, contract_id="contract-other", tenant_id="tenant-OTHER"
        )

        client = TestClient(app)
        resp = client.get("/api/commerce/price-protection-contracts")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        tenant_ids = {row["tenant_id"] for row in body["data"]}
        assert tenant_ids == {"tenant-A"}
        assert body["count"] == 2

    def test_filters_by_customer_id(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-1", tenant_id="tenant-A",
            customer_id="cust-1",
        )
        _seed_contract(
            es, contract_id="contract-2", tenant_id="tenant-A",
            customer_id="cust-2",
        )

        client = TestClient(app)
        resp = client.get(
            "/api/commerce/price-protection-contracts",
            params={"customer_id": "cust-1"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert {row["contract_id"] for row in data} == {"contract-1"}

    def test_filters_by_status_and_active_on_date(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-active", tenant_id="tenant-A",
            status="active",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        _seed_contract(
            es, contract_id="contract-expired", tenant_id="tenant-A",
            status="expired",
            start_date="2020-01-01", end_date="2020-12-31",
        )

        client = TestClient(app)
        resp = client.get(
            "/api/commerce/price-protection-contracts",
            params={"status": "active", "active_on_date": "2026-06-15"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert {row["contract_id"] for row in data} == {"contract-active"}


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts/{id} — fetch single
# ---------------------------------------------------------------------------


class TestGetContract:
    """Smoke coverage for single-contract fetch (Req 3.1)."""

    def test_returns_contract_for_matching_tenant(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-1", tenant_id="tenant-A"
        )

        client = TestClient(app)
        resp = client.get(
            "/api/commerce/price-protection-contracts/contract-1"
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["contract_id"] == "contract-1"
        assert data["tenant_id"] == "tenant-A"

    def test_returns_404_for_missing_id(self):
        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/does-not-exist"
        )

        assert resp.status_code == 404, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "price_protection_contract.not_found"

    def test_returns_404_for_cross_tenant_contract(self):
        """Does not leak existence across tenants (Constraint C3)."""
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-other", tenant_id="tenant-OTHER"
        )
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/contract-other"
        )

        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# PUT /api/commerce/price-protection-contracts/{id} — update
# ---------------------------------------------------------------------------


class TestUpdateContract:
    """Smoke coverage for PUT update (Req 3.6)."""

    def test_updates_notes(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/contract-1",
            json={"notes": "Renegotiated per call with customer"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["notes"] == "Renegotiated per call with customer"
        # status untouched
        assert data["status"] == "active"

    def test_cancels_active_contract(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/contract-1",
            json={"status": "cancelled"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "cancelled"

    def test_rejects_invalid_status_transition(self):
        """Only active → cancelled is accepted via the endpoint."""
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/contract-1",
            json={"status": "exhausted"},
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert (
            detail["error_code"]
            == "price_protection_contract.invalid_status_transition"
        )
        assert detail["current_status"] == "active"
        assert detail["requested_status"] == "exhausted"

    def test_rejects_cancel_from_exhausted(self):
        """Cancellation is only allowed from ``active``."""
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-1", tenant_id="tenant-A",
            status="exhausted", remaining_gallons=0.0,
        )
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/contract-1",
            json={"status": "cancelled"},
        )

        assert resp.status_code == 422, resp.text

    def test_rejects_empty_payload(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/contract-1",
            json={},
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert (
            detail["error_code"]
            == "price_protection_contract.no_mutable_fields"
        )

    def test_returns_404_for_missing_contract(self):
        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.put(
            "/api/commerce/price-protection-contracts/nope",
            json={"notes": "x"},
        )

        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts/{id}/variance — report
# ---------------------------------------------------------------------------


class TestVarianceReport:
    """Smoke coverage for portfolio variance report (Req 3.7)."""

    def _seed_invoice_event(
        self,
        es: _FakeESService,
        *,
        tenant_id: str = "tenant-A",
        event_id: str,
        contract_id: str = "contract-1",
        delivery_id: str = "delivery-1",
        market_price_cents: int = 350,
        effective_price_cents: int = 300,
        gallons: float = 100.0,
    ) -> None:
        es.docs.setdefault(INVOICE_EVENTS_INDEX, {})[event_id] = {
            "tenant_id": tenant_id,
            "event_id": event_id,
            "payload": {
                "contract_id": contract_id,
                "delivery_id": delivery_id,
                "market_price_cents": market_price_cents,
                "effective_price_cents": effective_price_cents,
                "gallons": gallons,
            },
        }

    def test_returns_zero_variance_when_no_events(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/contract-1/variance"
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["contract_id"] == "contract-1"
        assert data["total_variance_cents"] == 0
        assert data["total_gallons"] == 0.0
        assert data["delivery_count"] == 0
        assert data["breakdown"] == []
        # Report is enriched with the contract document
        assert data["contract"]["contract_id"] == "contract-1"

    def test_aggregates_variance_across_events(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(
            es, contract_id="contract-1", tenant_id="tenant-A",
            fixed_price_cents=300,
        )
        # Two deliveries at 100 gallons each with a 50¢ spread →
        # per-delivery variance = (350 - 300) * 100 = 5000¢. Total
        # across two deliveries = 10_000¢ from the customer's
        # perspective (customer saved $100).
        self._seed_invoice_event(
            es, event_id="ev-1", delivery_id="delivery-1",
            market_price_cents=350, effective_price_cents=300,
            gallons=100.0,
        )
        self._seed_invoice_event(
            es, event_id="ev-2", delivery_id="delivery-2",
            market_price_cents=350, effective_price_cents=300,
            gallons=100.0,
        )
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/contract-1/variance"
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["contract_id"] == "contract-1"
        assert data["delivery_count"] == 2
        assert data["total_variance_cents"] == 10_000
        assert data["total_gallons"] == 200.0
        breakdown_ids = {row["delivery_id"] for row in data["breakdown"]}
        assert breakdown_ids == {"delivery-1", "delivery-2"}
        for row in data["breakdown"]:
            assert row["variance_cents"] == 5000

    def test_returns_404_for_unknown_contract(self):
        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/nope/variance"
        )

        assert resp.status_code == 404, resp.text

    def test_excludes_cross_tenant_events(self):
        """Tenant isolation (Constraint C3)."""
        app, es = _build_app(tenant_id="tenant-A")
        _seed_contract(es, contract_id="contract-1", tenant_id="tenant-A")
        # Event tagged with the same contract_id but a different
        # tenant — must be invisible to the variance report.
        self._seed_invoice_event(
            es, event_id="ev-cross", tenant_id="tenant-OTHER",
            delivery_id="delivery-x",
            market_price_cents=500, effective_price_cents=300,
            gallons=50.0,
        )
        # Same-tenant event contributes to the report.
        self._seed_invoice_event(
            es, event_id="ev-ok", tenant_id="tenant-A",
            delivery_id="delivery-1",
            market_price_cents=350, effective_price_cents=300,
            gallons=100.0,
        )
        client = TestClient(app)

        resp = client.get(
            "/api/commerce/price-protection-contracts/contract-1/variance"
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["delivery_count"] == 1
        assert data["total_variance_cents"] == 5000
