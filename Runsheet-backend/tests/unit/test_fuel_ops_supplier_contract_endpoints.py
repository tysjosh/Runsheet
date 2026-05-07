"""
Unit tests for the Supplier_Contract CRUD REST endpoints added by Task 7.6.

Covers the endpoints mounted under :data:`fuel.api.fuel_ops_endpoints.router`:

* ``GET    /api/fuel/supplier-contracts``                 — tenant-scoped list
* ``GET    /api/fuel/supplier-contracts/{contract_id}``   — single record
* ``POST   /api/fuel/supplier-contracts``                 — create
* ``PATCH  /api/fuel/supplier-contracts/{contract_id}``   — partial update
* ``DELETE /api/fuel/supplier-contracts/{contract_id}``   — delete

Each test uses an in-memory :class:`_FakeESService` and a
:class:`_FakeRedis` so the repository layer and the :class:`ContractLiftService`
both exercise their real code paths without touching external services. The
fakes deliberately implement only the subset of behaviour the endpoints
exercise (search ``term`` clauses for tenant isolation, ``incrbyfloat`` for
the counter) — the production repositories have their own coverage in
:mod:`tests.unit.test_terminal_models`.

Property-based coverage:

    * The monthly-lift counter key always matches the Task 7.6 key shape,
      regardless of tenant / contract id variations (covered in
      :mod:`tests.unit.test_contract_lift_service`).

Validates: Requirements 8.3.2, 8.3.4.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.services.contract_lift_service import (
    CONTRACT_LIFT_KEY_PATTERN,
    ContractLiftService,
    month_bucket,
)
from fuel.terminal_models import SupplierContractRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub for :class:`SupplierContractRepository`.

    Supports the ``term`` clauses used by the repository
    (``tenant_id``, ``contract_id``, ``status``, ``supplier_name``,
    ``product_code``, ``preferred_terminal_ids``) and mutates the
    in-memory store on write/update/delete.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        must = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id: Optional[str] = None
        equality: Dict[str, Any] = {}
        list_filters: Dict[str, Any] = {}
        id_lookup: Optional[str] = None

        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                if field == "tenant_id":
                    tenant_id = value
                elif field == "contract_id":
                    id_lookup = value
                elif field == "preferred_terminal_ids":
                    list_filters[field] = value
                else:
                    equality[field] = value

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": []}}
            return {"hits": {"hits": [{"_source": dict(doc)}]}}

        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            skip = False
            for k, v in list_filters.items():
                source = doc.get(k) or []
                if v not in source:
                    skip = True
                    break
            if skip:
                continue
            matches.append({"_source": dict(doc)})

        return {"hits": {"hits": matches[:size]}}

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)

    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None


class _FakeRedis:
    """Minimal async Redis mock supporting ``get`` / ``incrbyfloat`` / ``expire``."""

    def __init__(self) -> None:
        self.store: Dict[str, float] = {}
        self.ttls: Dict[str, int] = {}
        self.incr_calls: List[Tuple[str, float]] = []

    async def incrbyfloat(self, key: str, amount: float) -> float:
        self.incr_calls.append((key, float(amount)))
        current = self.store.get(key, 0.0)
        new_value = current + float(amount)
        self.store[key] = new_value
        return new_value

    async def get(self, key: str) -> Optional[str]:
        value = self.store.get(key)
        if value is None:
            return None
        return str(value)

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = int(ttl_seconds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    tenant_id: str = "tenant-1",
) -> tuple[FastAPI, _FakeESService, _FakeRedis]:
    es = _FakeESService()
    redis = _FakeRedis()
    repo = SupplierContractRepository(es_service=es)
    lift_service = ContractLiftService(redis_client=redis)
    configure_fuel_ops_endpoints(
        es_service=es,
        supplier_contract_repository=repo,
        contract_lift_service=lift_service,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es, redis


def _base_create_payload(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "contract_id": "sc_001",
        "supplier_name": "Buckeye Terminals LLC",
        "product_code": "DIESEL_2",
        "preferred_terminal_ids": ["term_001", "term_002"],
        "contract_price_per_gallon_usd": 3.25,
        "branded_required": False,
        "minimum_lift_gallons_per_month": 50000.0,
        "rebate_terms": "net 30",
        "effective_from": "2025-01-01",
        "effective_to": "2026-01-01",
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _seed_contract(
    es: _FakeESService,
    contract_id: str = "sc_001",
    tenant_id: str = "tenant-1",
    **overrides: Any,
) -> None:
    payload = _base_create_payload(contract_id=contract_id, **overrides)
    payload["tenant_id"] = tenant_id
    payload.setdefault("created_at", "2025-01-01T00:00:00+00:00")
    payload.setdefault("updated_at", "2025-01-01T00:00:00+00:00")
    es.docs[contract_id] = payload


# ---------------------------------------------------------------------------
# POST /api/fuel/supplier-contracts
# ---------------------------------------------------------------------------


class TestCreateSupplierContract:
    def test_creates_contract_and_stamps_tenant_from_jwt(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        body = _base_create_payload()
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["contract"]["contract_id"] == "sc_001"
        assert data["contract"]["tenant_id"] == "tenant-1"
        assert data["contract"]["product_code"] == "DIESEL_2"
        assert data["contract"]["supplier_name"] == "Buckeye Terminals LLC"
        assert data["contract"]["minimum_lift_gallons_per_month"] == 50000.0
        # Lift summary is emitted even for a just-created contract — the
        # counter starts at zero and below_minimum is True because the
        # contract has a positive minimum.
        assert data["lift_summary"]["gallons_lifted_this_month"] == 0.0
        assert data["lift_summary"]["below_minimum"] is True
        assert data["lift_summary"]["percent_of_minimum"] == 0.0
        assert "sc_001" in es.docs
        assert es.docs["sc_001"]["tenant_id"] == "tenant-1"

    def test_mints_id_when_omitted(self):
        app, es, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload()
        body.pop("contract_id")
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        assert resp.status_code == 201, resp.json()
        assert resp.json()["contract"]["contract_id"].startswith("sc_")
        assert resp.json()["contract"]["contract_id"] in es.docs

    def test_canonicalizes_legacy_product_alias(self):
        """LPG should be normalized to PROPANE via the fuel catalog."""

        app, _, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(product_code="LPG")
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        assert resp.status_code == 201, resp.json()
        assert resp.json()["contract"]["product_code"] == "PROPANE"

    def test_rejects_unknown_product_code(self):
        app, _, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(product_code="UNOBTAINIUM")
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "unknown_product_code"
        assert detail["fuel_product_code"] == "UNOBTAINIUM"

    def test_rejects_negative_minimum_lift(self):
        app, _, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(minimum_lift_gallons_per_month=-100.0)
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        assert resp.status_code == 422

    def test_rejects_effective_to_before_from(self):
        app, _, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(
            effective_from="2025-06-01", effective_to="2025-01-01"
        )
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        # The contract's validator raises when effective_to < effective_from,
        # which the router maps to 422 via _translate_validation_error.
        assert resp.status_code == 422

    def test_rejects_extra_fields(self):
        app, _, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(mystery_field="boom")
        resp = client.post("/api/fuel/supplier-contracts", json=body)

        # Pydantic's extra="forbid" returns 422 with a detailed shape.
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/fuel/supplier-contracts
# ---------------------------------------------------------------------------


class TestListSupplierContracts:
    def test_lists_only_tenants_contracts(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_a", tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_b", tenant_id="tenant-2")

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["contract"]["contract_id"] == "sc_a"

    def test_filters_by_status(self):
        app, es, _ = _build_app()
        _seed_contract(es, contract_id="sc_active", status="active")
        _seed_contract(es, contract_id="sc_inactive", status="inactive")

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts?status=inactive")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["contract"]["contract_id"] == "sc_inactive"

    def test_filters_by_product_code_with_alias(self):
        """Query by legacy AGO alias should resolve to DIESEL_2."""

        app, es, _ = _build_app()
        _seed_contract(es, contract_id="sc_diesel", product_code="DIESEL_2")
        _seed_contract(es, contract_id="sc_propane", product_code="PROPANE")

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts?product_code=AGO")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["contract"]["contract_id"] == "sc_diesel"

    def test_filters_by_preferred_terminal(self):
        app, es, _ = _build_app()
        _seed_contract(
            es, contract_id="sc_1", preferred_terminal_ids=["term_001", "term_002"]
        )
        _seed_contract(
            es, contract_id="sc_2", preferred_terminal_ids=["term_003"]
        )

        client = TestClient(app)
        resp = client.get(
            "/api/fuel/supplier-contracts?preferred_terminal_id=term_002"
        )

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["contract"]["contract_id"] == "sc_1"

    def test_lift_summary_reflects_redis_counter(self):
        app, es, redis = _build_app(tenant_id="tenant-1")
        _seed_contract(
            es,
            contract_id="sc_001",
            tenant_id="tenant-1",
            minimum_lift_gallons_per_month=1000.0,
        )

        # Pre-seed the rolling counter for the current month.
        key = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="tenant-1",
            contract_id="sc_001",
            yyyy_mm=month_bucket(),
        )
        redis.store[key] = 750.0

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts")
        assert resp.status_code == 200

        item = resp.json()["items"][0]
        assert item["lift_summary"]["gallons_lifted_this_month"] == 750.0
        assert item["lift_summary"]["percent_of_minimum"] == 75.0
        assert item["lift_summary"]["below_minimum"] is True


# ---------------------------------------------------------------------------
# GET /api/fuel/supplier-contracts/{contract_id}
# ---------------------------------------------------------------------------


class TestGetSupplierContract:
    def test_returns_contract_for_owner(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts/sc_001")

        assert resp.status_code == 200
        assert resp.json()["contract"]["contract_id"] == "sc_001"

    def test_404_for_missing(self):
        app, _, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts/missing")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "supplier_contract_not_found"

    def test_masks_cross_tenant_as_404(self):
        """Cross-tenant reads surface as 404 so existence is not leaked."""

        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-other")

        client = TestClient(app)
        resp = client.get("/api/fuel/supplier-contracts/sc_001")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/fuel/supplier-contracts/{contract_id}
# ---------------------------------------------------------------------------


class TestPatchSupplierContract:
    def test_applies_partial_update(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")

        client = TestClient(app)
        resp = client.patch(
            "/api/fuel/supplier-contracts/sc_001",
            json={"contract_price_per_gallon_usd": 4.10, "status": "inactive"},
        )

        assert resp.status_code == 200, resp.json()
        data = resp.json()["contract"]
        assert data["contract_price_per_gallon_usd"] == 4.10
        assert data["status"] == "inactive"
        # Unchanged fields should persist.
        assert data["supplier_name"] == "Buckeye Terminals LLC"

    def test_canonicalizes_product_code_on_patch(self):
        app, es, _ = _build_app()
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")

        client = TestClient(app)
        resp = client.patch(
            "/api/fuel/supplier-contracts/sc_001", json={"product_code": "PMS"}
        )

        assert resp.status_code == 200, resp.json()
        assert resp.json()["contract"]["product_code"] == "GASOLINE_REG"

    def test_empty_patch_returns_current_state(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")

        client = TestClient(app)
        resp = client.patch("/api/fuel/supplier-contracts/sc_001", json={})

        assert resp.status_code == 200
        assert resp.json()["contract"]["contract_id"] == "sc_001"

    def test_404_for_missing(self):
        app, _, _ = _build_app()
        client = TestClient(app)
        resp = client.patch(
            "/api/fuel/supplier-contracts/missing",
            json={"status": "inactive"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/fuel/supplier-contracts/{contract_id}
# ---------------------------------------------------------------------------


class TestDeleteSupplierContract:
    def test_deletes_owned(self):
        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")

        client = TestClient(app)
        resp = client.delete("/api/fuel/supplier-contracts/sc_001")

        assert resp.status_code == 204
        assert "sc_001" not in es.docs

    def test_404_for_missing(self):
        app, _, _ = _build_app()
        client = TestClient(app)
        resp = client.delete("/api/fuel/supplier-contracts/missing")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "supplier_contract_not_found"

    def test_cross_tenant_delete_returns_404(self):
        """Repository masks cross-tenant deletes as 404 (no existence leak)."""

        app, es, _ = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-other")

        client = TestClient(app)
        resp = client.delete("/api/fuel/supplier-contracts/sc_001")

        # The repository raises CrossTenantAccessError for cross-tenant
        # delete attempts; the router maps that to 403.
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "cross_tenant_access_denied"
        assert "sc_001" in es.docs  # not deleted

    def test_delete_does_not_purge_lift_counter(self):
        """Historical lift counters survive contract deletion for reporting."""

        app, es, redis = _build_app(tenant_id="tenant-1")
        _seed_contract(es, contract_id="sc_001", tenant_id="tenant-1")
        key = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="tenant-1",
            contract_id="sc_001",
            yyyy_mm=month_bucket(),
        )
        redis.store[key] = 12345.0

        client = TestClient(app)
        resp = client.delete("/api/fuel/supplier-contracts/sc_001")

        assert resp.status_code == 204
        # The counter key persists so the admin UI can still render a
        # retrospective lift summary for the deleted contract.
        assert redis.store[key] == 12345.0
