"""Smoke tests for the Tax Engine REST endpoints (Task 3.9).

Covers the five endpoints mounted under :data:`compliance.api.tax_endpoints.router`:

* ``POST /api/compliance/tax/compute`` — breakdown happy path + the
  ``tax.jurisdiction_not_found`` HTTP 400 mapping (Req 1.9).
* ``GET  /api/compliance/tax-jurisdictions`` — list + filter.
* ``POST /api/compliance/tax-jurisdictions`` — create + ES index.
* ``GET  /api/compliance/exemptions`` — list + filter.
* ``POST /api/compliance/exemptions`` — create + ES index.

An in-memory ``_FakeESService`` stands in for the real
:class:`ElasticsearchService` so the tests exercise the router's
tenant-filter construction and response shaping without touching an
external cluster. The fake implements only the ``term`` / ``terms`` /
``range`` / ``exists`` / ``bool`` clauses that the handlers actually
emit.

Validates: Requirements 1.1, 1.5, 1.9
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api.tax_endpoints import configure_tax_api, router
from compliance.services.compliance_es_mappings import (
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
)
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# In-memory ES fake
# ---------------------------------------------------------------------------


def _match(doc: Dict[str, Any], clause: Dict[str, Any]) -> bool:
    """Return True if ``doc`` matches a single ES query clause.

    Supports the subset of query DSL emitted by the tax endpoints and
    the TaxEngine: ``term``, ``terms``, ``range`` (lte/gte on dates),
    ``exists``, ``match_all``, and nested ``bool`` clauses with
    ``must`` / ``filter`` / ``should`` / ``must_not``.
    """

    if not isinstance(clause, dict) or not clause:
        return True
    if "match_all" in clause:
        return True
    if "term" in clause:
        for field, value in clause["term"].items():
            field_value = doc.get(field)
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
    """Minimal async ES stub for the tax endpoint tests.

    Stores documents keyed by (index, doc_id). The ``search_documents``
    implementation walks the ``bool.filter`` / ``bool.must`` tree via
    :func:`_match` so tenant isolation, fips/tax-type/effective-date
    filters, and exemption expiry windows are all honored.
    """

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


def _tenant_ctx_factory(
    tenant_id: str = "tenant-A",
    roles: list[str] | None = None,
):
    # Tax jurisdictions and exemptions are a compliance surface, gated to the
    # operations roles. These tests previously used `roles=["operator"]`, which is
    # not a canonical role at all and passed only because the router had no role
    # gate; see tests/unit/test_compliance_api_authz.py.
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=list(roles) if roles is not None else ["admin"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    tenant_id: str = "tenant-A",
    roles: list[str] | None = None,
) -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    configure_tax_api(es_service=es)

    app = FastAPI()
    # The role gate raises AppException, which only becomes a 403 once the app's
    # handlers are registered. Without this a rejected request would surface as a
    # raised exception rather than an HTTP status.
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id, roles=roles
    )
    return app, es


def _seed_federal_diesel_rate(es: _FakeESService, tenant_id: str) -> str:
    """Seed a federal excise row for diesel so compute_tax picks it up."""

    # 244 tenths-of-cent = 24.4¢/gal — matches statutory diesel rate.
    doc = {
        "jurisdiction_id": "juris_fed_diesel",
        "tenant_id": tenant_id,
        "fips_code": "00",
        "jurisdiction_level": "federal",
        "jurisdiction_name": "United States",
        "tax_type": "excise",
        "product_codes": ["DIESEL_2"],
        "rate_cents_per_gallon": 244,
        "effective_date": "2020-01-01",
        "expiry_date": None,
        "source": "irs_form_720",
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    es.docs.setdefault(TAX_JURISDICTIONS_INDEX, {})[doc["jurisdiction_id"]] = doc
    return doc["jurisdiction_id"]


def _seed_state_ca_diesel_rate(es: _FakeESService, tenant_id: str) -> str:
    """Seed a California state excise row so compute_tax does not 400."""

    doc = {
        "jurisdiction_id": "juris_ca_state",
        "tenant_id": tenant_id,
        "fips_code": "06",
        "jurisdiction_level": "state",
        "jurisdiction_name": "CA",
        "tax_type": "excise",
        "product_codes": ["DIESEL_2"],
        "rate_cents_per_gallon": 400,  # 40.0¢/gal in RATE_SCALE units
        "effective_date": "2020-01-01",
        "expiry_date": None,
        "source": "ca_cdtfa",
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    es.docs.setdefault(TAX_JURISDICTIONS_INDEX, {})[doc["jurisdiction_id"]] = doc
    return doc["jurisdiction_id"]


# ---------------------------------------------------------------------------
# POST /api/compliance/tax/compute
# ---------------------------------------------------------------------------


class TestComputeTaxEndpoint:
    """Smoke coverage for POST /api/compliance/tax/compute (Req 1.1, 1.9)."""

    def test_returns_breakdown_structure_for_valid_request(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_federal_diesel_rate(es, tenant_id="tenant-A")
        _seed_state_ca_diesel_rate(es, tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/compliance/tax/compute",
            json={
                "product_code": "DIESEL_2",
                "net_gallons": 1000.0,
                "destination_fips": "06",
                "customer_id": "cust-1",
                "effective_date": "2026-01-15",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        # Breakdown structure: every component bucket present and the
        # computed total agrees with the sum.
        for key in (
            "federal_cents",
            "state_cents",
            "county_cents",
            "city_cents",
            "ust_cents",
            "spcc_cents",
            "environmental_cents",
            "total_tax_cents",
            "line_items",
            "exemptions_applied",
        ):
            assert key in data, f"breakdown missing {key}: {data}"

        # Federal: 244 × 1000 / 10 = 24_400¢ = $244.00
        assert data["federal_cents"] == 24_400
        # State: 400 × 1000 / 10 = 40_000¢ = $400.00
        assert data["state_cents"] == 40_000
        assert data["total_tax_cents"] == (
            data["federal_cents"]
            + data["state_cents"]
            + data["county_cents"]
            + data["city_cents"]
            + data["ust_cents"]
            + data["spcc_cents"]
            + data["environmental_cents"]
        )
        # At least one line item per non-zero bucket.
        assert len(data["line_items"]) >= 2

    def test_missing_state_row_maps_to_http_400_with_error_code(self):
        """Req 1.9 — TaxJurisdictionNotFoundError → HTTP 400 with error_code."""

        app, es = _build_app(tenant_id="tenant-A")
        # Seed only the federal row — state excise row is missing on
        # purpose so compute_tax raises TaxJurisdictionNotFoundError.
        _seed_federal_diesel_rate(es, tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/compliance/tax/compute",
            json={
                "product_code": "DIESEL_2",
                "net_gallons": 1000.0,
                "destination_fips": "06",
                "customer_id": "cust-1",
                "effective_date": "2026-01-15",
            },
        )

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "tax.jurisdiction_not_found"
        assert detail["fips_code"] == "06"
        assert detail["jurisdiction_level"] == "state"
        assert detail["tax_type"] == "excise"
        assert detail["product_code"] == "DIESEL_2"
        assert detail["effective_date"] == "2026-01-15"
        assert "message" in detail and detail["message"]

    def test_invalid_input_maps_to_http_422(self):
        app, es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        # Pydantic rejects net_gallons < 0 at schema validation time
        # (422 response, with ``detail`` as a list of validation errors).
        resp = client.post(
            "/api/compliance/tax/compute",
            json={
                "product_code": "DIESEL_2",
                "net_gallons": -1.0,
                "destination_fips": "06",
                "customer_id": "cust-1",
            },
        )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/compliance/tax-jurisdictions
# ---------------------------------------------------------------------------


class TestCreateTaxJurisdiction:
    """Smoke coverage for POST /api/compliance/tax-jurisdictions (Req 1.5)."""

    def test_creates_row_and_indexes_to_es(self):
        app, es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/compliance/tax-jurisdictions",
            json={
                "fips_code": "06",
                "jurisdiction_level": "state",
                "jurisdiction_name": "CA",
                "tax_type": "excise",
                "product_codes": ["DIESEL_2"],
                "rate_cents_per_gallon": 400,
                "effective_date": "2026-01-01",
                "source": "manual_csv_import",
            },
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]

        # Server-assigned id, tenant stamp, timestamps
        assert data["jurisdiction_id"].startswith("juris_")
        assert data["tenant_id"] == "tenant-A"
        assert data["created_at"] is not None
        assert data["updated_at"] is not None

        # Values echoed back unchanged
        assert data["fips_code"] == "06"
        assert data["tax_type"] == "excise"
        assert data["rate_cents_per_gallon"] == 400

        # ES index_document was called exactly once against the right
        # index, using the generated jurisdiction_id as doc id.
        assert len(es.index_calls) == 1
        index, doc_id, document = es.index_calls[0]
        assert index == TAX_JURISDICTIONS_INDEX
        assert doc_id == data["jurisdiction_id"]
        assert document["tenant_id"] == "tenant-A"

    def test_rejects_malformed_payload_with_422(self):
        """fips_code length must match jurisdiction_level (Req 1.5)."""

        app, _es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        # 5-digit FIPS with level=state is rejected by the model
        # validator. The handler surfaces this as HTTP 422 with a
        # structured error_code so the UI can route on the code.
        resp = client.post(
            "/api/compliance/tax-jurisdictions",
            json={
                "fips_code": "06037",
                "jurisdiction_level": "state",
                "tax_type": "excise",
                "product_codes": ["DIESEL_2"],
                "rate_cents_per_gallon": 400,
                "effective_date": "2026-01-01",
            },
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        # Our handler returns a dict for custom validation failures;
        # Pydantic schema-level failures return a list. Accept either
        # shape so long as the error_code / message is present when
        # it's a dict.
        if isinstance(detail, dict):
            assert detail["error_code"] == "tax_jurisdiction.invalid_payload"
            assert "fips_code" in detail["message"].lower() or "length" in detail["message"].lower()


# ---------------------------------------------------------------------------
# GET /api/compliance/tax-jurisdictions
# ---------------------------------------------------------------------------


class TestListTaxJurisdictions:
    """Smoke coverage for GET /api/compliance/tax-jurisdictions filtering (Req 1.5)."""

    def test_returns_all_rows_when_no_filters(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_federal_diesel_rate(es, tenant_id="tenant-A")
        _seed_state_ca_diesel_rate(es, tenant_id="tenant-A")
        # Cross-tenant row — must not leak into the response.
        _seed_state_ca_diesel_rate(es, tenant_id="tenant-OTHER")
        es.docs[TAX_JURISDICTIONS_INDEX]["juris_ca_state"]["tenant_id"] = "tenant-A"
        # Overwrite the cross-tenant doc with a distinct id.
        es.docs[TAX_JURISDICTIONS_INDEX]["juris_other"] = {
            **es.docs[TAX_JURISDICTIONS_INDEX]["juris_ca_state"],
            "jurisdiction_id": "juris_other",
            "tenant_id": "tenant-OTHER",
        }

        client = TestClient(app)

        resp = client.get("/api/compliance/tax-jurisdictions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        tenant_ids = {row["tenant_id"] for row in body["data"]}
        assert tenant_ids == {"tenant-A"}
        assert body["count"] == len(body["data"])
        assert body["count"] >= 2

    def test_filters_by_fips_code_and_tax_type(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_federal_diesel_rate(es, tenant_id="tenant-A")
        _seed_state_ca_diesel_rate(es, tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get(
            "/api/compliance/tax-jurisdictions",
            params={"fips_code": "06", "tax_type": "excise"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # Only the CA state row has fips_code="06" — the federal row
        # uses the "00" sentinel.
        assert len(data) == 1
        assert data[0]["fips_code"] == "06"
        assert data[0]["tax_type"] == "excise"
        assert data[0]["jurisdiction_level"] == "state"

    def test_filters_by_effective_date(self):
        app, es = _build_app(tenant_id="tenant-A")
        # Seed a row that expired in 2020 so the effective_date filter
        # excludes it.
        es.docs.setdefault(TAX_JURISDICTIONS_INDEX, {})["juris_expired"] = {
            "jurisdiction_id": "juris_expired",
            "tenant_id": "tenant-A",
            "fips_code": "06",
            "jurisdiction_level": "state",
            "jurisdiction_name": "CA",
            "tax_type": "excise",
            "product_codes": ["DIESEL_2"],
            "rate_cents_per_gallon": 200,
            "effective_date": "2015-01-01",
            "expiry_date": "2020-12-31",
            "created_at": "2015-01-01T00:00:00+00:00",
            "updated_at": "2015-01-01T00:00:00+00:00",
        }
        _seed_state_ca_diesel_rate(es, tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get(
            "/api/compliance/tax-jurisdictions",
            params={"effective_date": "2026-01-15"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # Expired row excluded, active row returned.
        ids = {row["jurisdiction_id"] for row in data}
        assert "juris_ca_state" in ids
        assert "juris_expired" not in ids


# ---------------------------------------------------------------------------
# POST /api/compliance/exemptions
# ---------------------------------------------------------------------------


class TestCreateExemption:
    """Smoke coverage for POST /api/compliance/exemptions (Req 1.6, 1.7)."""

    def test_creates_row_and_indexes_to_es(self):
        app, es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/compliance/exemptions",
            json={
                "customer_id": "cust-1",
                "exemption_type": "dyed_diesel",
                "certificate_number": "CERT-12345",
                "issuing_authority": "IRS",
                "expiry_date": "2030-12-31",
            },
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["exemption_id"].startswith("exempt_")
        assert data["tenant_id"] == "tenant-A"
        assert data["exemption_type"] == "dyed_diesel"
        assert data["certificate_number"] == "CERT-12345"

        assert len(es.index_calls) == 1
        index, doc_id, _document = es.index_calls[0]
        assert index == TAX_EXEMPTIONS_INDEX
        assert doc_id == data["exemption_id"]


# ---------------------------------------------------------------------------
# GET /api/compliance/exemptions
# ---------------------------------------------------------------------------


class TestListExemptions:
    """Smoke coverage for GET /api/compliance/exemptions (Req 1.6, 1.7)."""

    def test_filters_by_customer_id(self):
        app, es = _build_app(tenant_id="tenant-A")
        # Seed two exemptions for different customers.
        es.docs.setdefault(TAX_EXEMPTIONS_INDEX, {})["exempt_a"] = {
            "exemption_id": "exempt_a",
            "tenant_id": "tenant-A",
            "customer_id": "cust-1",
            "exemption_type": "dyed_diesel",
            "certificate_number": "CERT-A",
            "expiry_date": "2030-12-31",
            "status": "valid",
            "created_at": "2020-01-01T00:00:00+00:00",
            "updated_at": "2020-01-01T00:00:00+00:00",
        }
        es.docs[TAX_EXEMPTIONS_INDEX]["exempt_b"] = {
            "exemption_id": "exempt_b",
            "tenant_id": "tenant-A",
            "customer_id": "cust-2",
            "exemption_type": "farm",
            "certificate_number": "CERT-B",
            "expiry_date": "2030-12-31",
            "status": "valid",
            "created_at": "2020-01-01T00:00:00+00:00",
            "updated_at": "2020-01-01T00:00:00+00:00",
        }
        client = TestClient(app)

        resp = client.get(
            "/api/compliance/exemptions",
            params={"customer_id": "cust-1"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert {row["exemption_id"] for row in data} == {"exempt_a"}


class TestRouterLevelAuthorization:
    """The compliance gate is attached to this router, so prove it through HTTP.

    ``tests/unit/test_compliance_api_authz.py`` covers the policy and the drift
    guard. This covers the wiring on *this* router: that removing
    ``dependencies=[Depends(compliance_ops_dependency)]`` from the ``APIRouter``
    would fail a test rather than pass silently.
    """

    def test_driver_is_refused(self) -> None:
        app, _ = _build_app(roles=["driver"])
        response = TestClient(app).get("/api/compliance/tax-jurisdictions")
        assert response.status_code == 403, response.text

    def test_platform_admin_alone_is_refused(self) -> None:
        app, _ = _build_app(roles=["platform_admin"])
        response = TestClient(app).get("/api/compliance/tax-jurisdictions")
        assert response.status_code == 403, response.text

    def test_dispatcher_is_allowed(self) -> None:
        # A dispatcher works compliance during a shift; this is the customer's own
        # regulatory obligation, not a staff-only surface.
        app, _ = _build_app(roles=["dispatcher"])
        response = TestClient(app).get("/api/compliance/tax-jurisdictions")
        assert response.status_code == 200, response.text

    def test_rejection_does_not_echo_held_roles(self) -> None:
        app, _ = _build_app(roles=["some-internal-role-name"])
        response = TestClient(app).get("/api/compliance/tax-jurisdictions")
        assert response.status_code == 403
        assert "some-internal-role-name" not in response.text
