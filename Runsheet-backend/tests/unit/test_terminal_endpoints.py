"""
Unit tests for the Terminal CRUD + proposed-load REST endpoints added by
Task 7.2.

Covers the endpoints mounted on the primary
:data:`fuel.api.fuel_ops_endpoints.router` under the ``/api/fuel``
prefix:

* ``GET    /api/fuel/terminals``                          — list with filters
* ``POST   /api/fuel/terminals``                          — create
* ``GET    /api/fuel/terminals/{terminal_id}``            — fetch
* ``PATCH  /api/fuel/terminals/{terminal_id}``            — partial update
* ``DELETE /api/fuel/terminals/{terminal_id}``            — hard delete
* ``POST   /api/fuel/terminals/{terminal_id}/proposed-load``
  — operating-hours / supported-product validator (Req 8.1.4)

Each test uses :class:`_FakeESService` — a minimal in-memory stub that
implements the subset of :class:`ElasticsearchService` the repository
relies on. Same pattern as ``test_fuel_ops_depot_endpoints.py`` so the
Terminal surface exercises the full ``TerminalRepository`` + router
wiring without a real Elasticsearch backend.

Validates: Requirements 8.1.2, 8.1.4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from errors.handlers import handle_app_exception
from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    router,
)
from fuel.terminal_models import TerminalRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub used by :class:`TerminalRepository`.

    Implements ``index_document``, ``search_documents``,
    ``update_document``, and ``delete_document`` over an in-memory dict
    keyed by ``doc_id``. Queries with a ``term`` on ``terminal_id``
    resolve to a single-document lookup; queries with ``bool.must`` on
    ``tenant_id`` and optional equality filters (``status``,
    ``supported_products``, ``operator``, ``branded``) are honoured so
    the endpoint-level filters exercise the real code paths.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}

    # -------- index_document ---------------------------------------------
    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)

    # -------- search_documents -------------------------------------------
    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        must = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id: Optional[str] = None
        equality: Dict[str, Any] = {}
        product_filter: Optional[str] = None
        id_lookup: Optional[str] = None

        # Shorthand single-term query (``_fetch_source``) uses a
        # top-level ``{"query": {"term": {...}}}`` shape.
        inner = query.get("query", {})
        if not must and "term" in inner:
            must = [inner]

        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                if field == "tenant_id":
                    tenant_id = value
                elif field == "terminal_id":
                    id_lookup = value
                elif field == "supported_products":
                    product_filter = value
                else:
                    equality[field] = value

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)}],
                    "total": {"value": 1},
                }
            }

        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            if product_filter is not None:
                supported = doc.get("supported_products") or []
                if product_filter not in supported:
                    continue
            matches.append({"_source": dict(doc)})

        matches = matches[:size]
        return {"hits": {"hits": matches, "total": {"value": len(matches)}}}

    # -------- update_document --------------------------------------------
    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)

    # -------- delete_document --------------------------------------------
    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(
    tenant_id: str = "tenant-1", region: str = "US", roles: Optional[List[str]] = None
):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            # The Terminal write surface is admin-gated (Task 8.1). Default
            # the harness to an admin caller so the existing create/update/
            # delete tests exercise the success path; the admin-gating tests
            # override ``roles`` to assert the 403.
            roles=roles if roles is not None else ["dispatcher", "admin"],
            region=region,
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    tenant_id: str = "tenant-1", roles: Optional[List[str]] = None
) -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    repo = TerminalRepository(es_service=es)
    configure_fuel_ops_endpoints(es_service=es, terminal_repository=repo)

    app = FastAPI()
    app.include_router(router)

    # The shared Role_Authorizer and the deactivate handler both raise
    # AppException; register the exact handler main.py installs so responses
    # render as the canonical top-level ``ErrorResponse`` envelope rather
    # than a test-only shape.
    app.add_exception_handler(AppException, handle_app_exception)

    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id, roles=roles
    )
    return app, es


def _base_create_payload(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "terminal_id": "term_001",
        "name": "Newark Rack",
        "operator": "Buckeye",
        "location_lat": 40.7357,
        "location_lon": -74.1724,
        "address": "1 Fuel Lane, Newark, NJ",
        "timezone": "America/New_York",
        "operating_hours": [
            {"day_of_week": "mon", "open": "06:00", "close": "22:00"},
            {"day_of_week": "tue", "open": "06:00", "close": "22:00"},
            {"day_of_week": "wed", "open": "06:00", "close": "22:00"},
            {"day_of_week": "thu", "open": "06:00", "close": "22:00"},
            {"day_of_week": "fri", "open": "06:00", "close": "22:00"},
        ],
        "supported_products": ["DIESEL_2", "GASOLINE_REG"],
        "branded": False,
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _seed_terminal(
    es: _FakeESService,
    terminal_id: str = "term_001",
    tenant_id: str = "tenant-1",
    **overrides: Any,
) -> None:
    """Insert a well-formed Terminal source directly into the fake index.

    Used to exercise read / update / delete paths without going through
    ``POST``. Callers can override any field via ``**overrides``.
    """

    payload = _base_create_payload(terminal_id=terminal_id, **overrides)
    payload["tenant_id"] = tenant_id
    payload.setdefault("created_at", "2025-01-01T00:00:00+00:00")
    payload.setdefault("updated_at", "2025-01-01T00:00:00+00:00")
    es.docs[terminal_id] = payload


# ---------------------------------------------------------------------------
# POST /api/fuel/terminals (Req 8.1.2)
# ---------------------------------------------------------------------------


class TestCreateTerminal:
    def test_creates_terminal_and_stamps_tenant_from_jwt(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        body = _base_create_payload()
        resp = client.post("/api/fuel/terminals", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["terminal_id"] == "term_001"
        # Router stamps tenant_id from the verified JWT context.
        assert data["tenant_id"] == "tenant-1"
        assert data["operator"] == "Buckeye"
        assert data["supported_products"] == ["DIESEL_2", "GASOLINE_REG"]
        assert "term_001" in es.docs

    def test_mints_id_when_omitted(self):
        app, es = _build_app()
        client = TestClient(app)

        body = _base_create_payload()
        body.pop("terminal_id")
        resp = client.post("/api/fuel/terminals", json=body)

        assert resp.status_code == 201
        minted = resp.json()["terminal_id"]
        assert minted.startswith("term_")
        assert minted in es.docs

    def test_canonicalizes_legacy_aliases(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(supported_products=["AGO", "LPG"])
        resp = client.post("/api/fuel/terminals", json=body)

        assert resp.status_code == 201
        assert resp.json()["supported_products"] == ["DIESEL_2", "PROPANE"]

    def test_branded_without_supplier_brand_fails(self):
        app, _ = _build_app()
        client = TestClient(app)
        body = _base_create_payload(branded=True)
        resp = client.post("/api/fuel/terminals", json=body)
        assert resp.status_code == 422

    def test_rejects_extra_fields(self):
        app, _ = _build_app()
        client = TestClient(app)
        body = _base_create_payload()
        body["sneaky"] = "nope"
        resp = client.post("/api/fuel/terminals", json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/fuel/terminals (Req 8.1.2)
# ---------------------------------------------------------------------------


class TestListTerminals:
    def test_lists_only_current_tenants_terminals(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_terminal(es, terminal_id="mine", tenant_id="tenant-1")
        _seed_terminal(es, terminal_id="other", tenant_id="tenant-2")

        resp = client.get("/api/fuel/terminals")
        assert resp.status_code == 200
        data = resp.json()
        ids = [t["terminal_id"] for t in data["items"]]
        assert ids == ["mine"]
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert data["has_next"] is False

    def test_status_filter(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es, terminal_id="active_one", status="active")
        _seed_terminal(es, terminal_id="inactive_one", status="inactive")

        resp = client.get("/api/fuel/terminals", params={"status": "inactive"})
        assert resp.status_code == 200
        assert [
            t["terminal_id"] for t in resp.json()["items"]
        ] == ["inactive_one"]

    def test_operator_substring_filter_case_insensitive(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es, terminal_id="a", operator="Buckeye Terminals")
        _seed_terminal(es, terminal_id="b", operator="Kinder Morgan")
        _seed_terminal(es, terminal_id="c", operator="Buckeye")

        resp = client.get("/api/fuel/terminals", params={"operator": "buckeye"})
        assert resp.status_code == 200
        ids = sorted(t["terminal_id"] for t in resp.json()["items"])
        assert ids == ["a", "c"]

    def test_product_code_filter_with_alias(self):
        """Filtering by ``AGO`` matches terminals that persist ``DIESEL_2``."""

        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(
            es, terminal_id="d", supported_products=["DIESEL_2"]
        )
        _seed_terminal(
            es, terminal_id="g", supported_products=["GASOLINE_REG"]
        )

        resp = client.get("/api/fuel/terminals", params={"product_code": "AGO"})
        assert resp.status_code == 200
        assert [t["terminal_id"] for t in resp.json()["items"]] == ["d"]

    def test_rejects_invalid_status_filter(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/terminals", params={"status": "nope"})
        assert resp.status_code == 422

    def test_pagination_has_next(self):
        app, es = _build_app()
        client = TestClient(app)
        for i in range(3):
            _seed_terminal(es, terminal_id=f"t{i}")

        resp = client.get("/api/fuel/terminals", params={"size": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["has_next"] is True

        resp2 = client.get(
            "/api/fuel/terminals", params={"size": 2, "page": 2}
        )
        assert resp2.status_code == 200
        assert resp2.json()["has_next"] is False


# ---------------------------------------------------------------------------
# GET /api/fuel/terminals/{id} (Req 8.1.2)
# ---------------------------------------------------------------------------


class TestGetTerminal:
    def test_returns_owned_terminal(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.get("/api/fuel/terminals/term_001")
        assert resp.status_code == 200
        assert resp.json()["terminal_id"] == "term_001"

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_returns_404_for_cross_tenant(self):
        """Cross-tenant gets are 404 (not 403) to avoid leaking existence."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_terminal(es, terminal_id="term_001", tenant_id="tenant-2")

        resp = client.get("/api/fuel/terminals/term_001")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"


# ---------------------------------------------------------------------------
# PATCH /api/fuel/terminals/{id} (Req 8.1.2)
# ---------------------------------------------------------------------------


class TestUpdateTerminal:
    def test_applies_partial_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.patch(
            "/api/fuel/terminals/term_001",
            json={"name": "Renamed Rack", "status": "inactive"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Renamed Rack"
        assert data["status"] == "inactive"
        # Untouched fields preserved.
        assert data["operator"] == "Buckeye"

    def test_canonicalizes_supported_products_on_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.patch(
            "/api/fuel/terminals/term_001",
            json={"supported_products": ["LPG"]},
        )
        assert resp.status_code == 200
        assert resp.json()["supported_products"] == ["PROPANE"]

    def test_rejects_immutable_fields(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.patch(
            "/api/fuel/terminals/term_001",
            json={"tenant_id": "tenant-evil"},
        )
        assert resp.status_code == 422

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.patch(
            "/api/fuel/terminals/does-not-exist",
            json={"status": "inactive"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_returns_403_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_terminal(es, terminal_id="term_001", tenant_id="tenant-2")

        resp = client.patch(
            "/api/fuel/terminals/term_001",
            json={"status": "inactive"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["error_code"] == "cross_tenant_access_denied"

    def test_empty_patch_returns_current_model(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.patch("/api/fuel/terminals/term_001", json={})
        assert resp.status_code == 200
        assert resp.json()["terminal_id"] == "term_001"


# ---------------------------------------------------------------------------
# DELETE /api/fuel/terminals/{id} (Req 8.1.2)
# ---------------------------------------------------------------------------


class TestDeleteTerminal:
    def test_deletes_owned_terminal(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.delete("/api/fuel/terminals/term_001")
        assert resp.status_code == 204
        assert resp.content == b""
        assert "term_001" not in es.docs

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.delete("/api/fuel/terminals/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_returns_403_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_terminal(es, terminal_id="term_001", tenant_id="tenant-2")

        resp = client.delete("/api/fuel/terminals/term_001")
        assert resp.status_code == 403
        # Terminal must remain in the store — cross-tenant delete is a no-op.
        assert "term_001" in es.docs


# ---------------------------------------------------------------------------
# POST /api/fuel/terminals/{id}/proposed-load (Req 8.1.4)
# ---------------------------------------------------------------------------


class TestProposedLoad:
    def test_open_terminal_returns_200(self):
        app, es = _build_app()
        client = TestClient(app)
        # Seed a terminal that is open Monday 06:00-22:00 in America/New_York.
        _seed_terminal(es)

        # Monday 2025-03-10 15:00 UTC → 11:00 EDT → inside the window.
        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "DIESEL_2",
                "volume_gallons": 5000.0,
                "as_of": "2025-03-10T15:00:00+00:00",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        assert data["terminal_id"] == "term_001"
        assert data["product_code"] == "DIESEL_2"
        assert data["volume_gallons"] == 5000.0

    def test_closed_terminal_returns_400_with_next_open_window(self):
        app, es = _build_app()
        client = TestClient(app)
        # Weekday-only schedule (mon-fri) already seeded via _base_create_payload.
        _seed_terminal(es)

        # Saturday 2025-03-15 at 10:00 local (14:00 UTC) — closed.
        # Next open window should land on Monday 2025-03-17 06:00 local
        # (10:00 UTC since EDT started 2025-03-09).
        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "DIESEL_2",
                "volume_gallons": 5000.0,
                "as_of": "2025-03-15T14:00:00+00:00",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "terminal_closed"
        assert detail["terminal_id"] == "term_001"
        # as_of is echoed verbatim so the caller can reconcile.
        assert detail["as_of"].startswith("2025-03-15T14:00:00")
        window = detail["next_open_window"]
        assert window is not None
        assert window["day_of_week"] == "mon"
        assert window["open_local"] == "06:00"
        assert window["close_local"] == "22:00"
        # EDT is UTC-4 in mid-March so 06:00 local ≡ 10:00 UTC.
        assert window["starts_at_utc"].startswith("2025-03-17T10:00:00")

    def test_later_same_day_walks_to_next_open(self):
        """An ``as_of`` that's later in the day than the window must
        walk to the next day's window rather than reporting today as
        the next open."""

        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        # Monday 2025-03-10 at 23:00 EDT → 03:00 UTC on 2025-03-11.
        # Monday window has already closed (closes 22:00), so the next
        # open should be Tuesday 2025-03-11 06:00 EDT → 10:00 UTC.
        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "DIESEL_2",
                "volume_gallons": 5000.0,
                "as_of": "2025-03-11T03:00:00+00:00",
            },
        )
        assert resp.status_code == 400
        window = resp.json()["detail"]["next_open_window"]
        assert window["day_of_week"] == "tue"
        assert window["starts_at_utc"].startswith("2025-03-11T10:00:00")

    def test_unsupported_product_returns_400(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)  # supports DIESEL_2 + GASOLINE_REG

        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "PROPANE",
                "volume_gallons": 1000.0,
                "as_of": "2025-03-10T15:00:00+00:00",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "product_not_supported"
        assert detail["terminal_id"] == "term_001"
        assert detail["product_code"] == "PROPANE"
        assert "DIESEL_2" in detail["supported_products"]

    def test_product_alias_canonicalized_before_membership_check(self):
        """Submitting ``AGO`` against a terminal that stores ``DIESEL_2``
        must resolve the alias before the supported-products lookup."""

        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "AGO",
                "volume_gallons": 1000.0,
                "as_of": "2025-03-10T15:00:00+00:00",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["product_code"] == "DIESEL_2"

    def test_unknown_product_returns_400(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={
                "product_code": "UNOBTAINIUM",
                "volume_gallons": 1000.0,
                "as_of": "2025-03-10T15:00:00+00:00",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "unknown_product_code"

    def test_missing_terminal_returns_404(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/does-not-exist/proposed-load",
            json={"product_code": "DIESEL_2", "volume_gallons": 1000.0},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_as_of_defaults_to_now(self):
        """Omitting ``as_of`` evaluates against the current wall-clock;
        a 24/7 terminal must always respond 200."""

        app, es = _build_app()
        client = TestClient(app)
        # 24/7 terminal: empty operating_hours means always open.
        _seed_terminal(es, operating_hours=[])

        resp = client.post(
            "/api/fuel/terminals/term_001/proposed-load",
            json={"product_code": "DIESEL_2", "volume_gallons": 1000.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        # The server stamps an ``as_of`` close to now.
        stamped = datetime.fromisoformat(data["as_of"])
        now = datetime.now(timezone.utc)
        assert abs((now - stamped).total_seconds()) < 30


# ---------------------------------------------------------------------------
# Admin-gating on the Terminal write surface (Task 8.1, Req 9.1, 9.2)
# ---------------------------------------------------------------------------


class TestTerminalAdminGating:
    """The thin management surface (Task 8.1) restricts every state-changing
    Terminal operation to the canonical ``admin`` role. Reads stay open so
    the Sourcing UI / ``<EntityLink>`` resolver can still resolve a
    reference for a non-admin caller."""

    def test_non_admin_cannot_create(self):
        app, _ = _build_app(roles=["dispatcher"])
        client = TestClient(app)

        resp = client.post("/api/fuel/terminals", json=_base_create_payload())

        assert resp.status_code == 403

    def test_non_admin_cannot_update(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.patch(
            "/api/fuel/terminals/term_001", json={"status": "inactive"}
        )

        assert resp.status_code == 403
        # The terminal is untouched — still active.
        assert es.docs["term_001"]["status"] == "active"

    def test_non_admin_cannot_delete(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.delete("/api/fuel/terminals/term_001")

        assert resp.status_code == 403
        assert "term_001" in es.docs

    def test_non_admin_cannot_deactivate(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.post("/api/fuel/terminals/term_001/deactivate")

        assert resp.status_code == 403
        assert es.docs["term_001"]["status"] == "active"

    def test_reads_stay_open_to_non_admin(self):
        app, es = _build_app(roles=["dispatcher"])
        client = TestClient(app)
        _seed_terminal(es)

        list_resp = client.get("/api/fuel/terminals")
        get_resp = client.get("/api/fuel/terminals/term_001")

        assert list_resp.status_code == 200
        assert get_resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/fuel/terminals/{id}/deactivate (Task 8.1, Req 9.1, 9.2)
# ---------------------------------------------------------------------------


class TestDeactivateTerminal:
    def test_deactivates_owned_terminal(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es)

        resp = client.post("/api/fuel/terminals/term_001/deactivate")

        assert resp.status_code == 200
        assert resp.json()["status"] == "inactive"
        assert es.docs["term_001"]["status"] == "inactive"

    def test_deactivate_is_idempotent(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_terminal(es, status="inactive")

        resp = client.post("/api/fuel/terminals/term_001/deactivate")

        assert resp.status_code == 200
        assert resp.json()["status"] == "inactive"

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.post("/api/fuel/terminals/does-not-exist/deactivate")

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_returns_404_for_cross_tenant(self):
        # A cross-tenant id is indistinguishable from "missing" on the read
        # path, so the load-or-404 guard surfaces 404 (never leaks existence).
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_terminal(es, terminal_id="term_001", tenant_id="tenant-2")

        resp = client.post("/api/fuel/terminals/term_001/deactivate")

        assert resp.status_code == 404
        # Cross-tenant terminal is untouched.
        assert es.docs["term_001"]["status"] == "active"
