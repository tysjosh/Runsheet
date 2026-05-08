"""
Unit tests for Task 8.8 of the fuel-ops-hardening spec:

* ``GET /api/fuel/mvp/reconciliation`` — paginated list of
  :class:`ReconciliationRecord` documents served out of the
  ``mvp_reconciliation`` ES index, scoped by ``tenant_id`` and
  filtered by ``order_id`` / ``plan_id`` / ``pod_id`` / ``min_variance_pct``.

* :meth:`ReconciliationService.update_invoice_fields` — the seam the
  QuickBooks Online Connector (Phase 9) calls to attach
  ``invoiced_gallons`` and recompute
  ``variance_invoiced_vs_delivered_pct`` on a finalized record within
  60 seconds of an invoice event (Req 4.4.5).

The tests use an in-memory Elasticsearch stub plus fake Redis so the
full router + service wiring is exercised without external
dependencies.

Validates: Requirements 4.4.4, 4.4.5.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.services.fuel_ops_es_mappings import MVP_RECONCILIATION_INDEX
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.reconciliation_service import (
    ReconciliationRecord,
    ReconciliationService,
    VARIANCE_ALERT_FLAG,
)


# ---------------------------------------------------------------------------
# Fake Elasticsearch service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Tiny async ES stub backing ``mvp_reconciliation`` for these tests.

    Supports the query shape issued by the endpoint (``bool.must`` with
    ``term`` clauses on ``tenant_id``/``order_id``/``plan_id``/``pod_id``
    plus ``sort`` on ``generated_at`` desc and ``from`` / ``size``
    pagination). Also supports the CRUD ops used by the update seam:
    ``index_document`` / ``get_document`` / ``update_document``.
    """

    def __init__(self) -> None:
        # Keyed by ``(index, doc_id)`` so the stub could be extended to
        # host multiple indices without collision. The task surface only
        # uses ``mvp_reconciliation`` but mirroring the other fuel-ops
        # test stubs keeps the shape familiar.
        self.docs: Dict[tuple, Dict[str, Any]] = {}

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[(index, doc_id)] = dict(document)

    async def get_document(
        self, index: str, doc_id: str
    ) -> Optional[Dict[str, Any]]:
        source = self.docs.get((index, doc_id))
        if source is None:
            return None
        # Mirror the ES hit-shape the real service returns so the
        # ``_source`` unwrap branch in the seam is exercised.
        return {"_source": dict(source), "_id": doc_id, "found": True}

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        current = self.docs.get((index, doc_id))
        if current is None:
            raise KeyError(doc_id)
        updated = dict(current)
        updated.update(partial)
        self.docs[(index, doc_id)] = updated

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        must = query.get("query", {}).get("bool", {}).get("must", [])
        equality: Dict[str, Any] = {}
        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                equality[field] = value

        matches: List[Dict[str, Any]] = []
        for (doc_index, _), doc in self.docs.items():
            if doc_index != index:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            matches.append(dict(doc))

        # Honour ``generated_at`` desc sort.
        sort = query.get("sort") or []
        if sort:
            def _key(row: Dict[str, Any]) -> str:
                for spec in sort:
                    if not isinstance(spec, dict):
                        continue
                    for field in spec.keys():
                        return str(row.get(field) or "")
                return ""

            matches.sort(key=_key, reverse=True)

        total = len(matches)
        start = int(query.get("from", 0) or 0)
        window_size = int(query.get("size", size) or size)
        window = matches[start : start + window_size]

        return {
            "hits": {
                "hits": [{"_source": dict(row)} for row in window],
                "total": {"value": total},
            }
        }


class _FakeRedis:
    """Minimal async Redis stub returning configured values on ``get``."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self._values = dict(values or {})

    async def get(self, key: str):
        return self._values.get(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-A"):
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


def _build_app(tenant_id: str = "tenant-A") -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    configure_fuel_ops_endpoints(es_service=es)

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _seed_record(
    es: _FakeESService,
    *,
    reconciliation_id: str,
    tenant_id: str,
    order_id: str = "order-1",
    plan_id: str = "plan-1",
    pod_id: str = "pod-1",
    ordered_gallons: float = 500.0,
    loaded_gallons: float = 500.0,
    delivered_gallons: float = 500.0,
    invoiced_gallons: Optional[float] = None,
    invoice_id: Optional[str] = None,
    variance_load_vs_order_pct: float = 0.0,
    variance_delivered_vs_loaded_pct: float = 0.0,
    variance_invoiced_vs_delivered_pct: Optional[float] = None,
    alert_flags: Optional[List[str]] = None,
    generated_at: Optional[datetime] = None,
) -> None:
    """Insert a well-formed ReconciliationRecord source document."""

    if generated_at is None:
        generated_at = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    doc: Dict[str, Any] = {
        "reconciliation_id": reconciliation_id,
        "tenant_id": tenant_id,
        "order_id": order_id,
        "plan_id": plan_id,
        "pod_id": pod_id,
        "invoice_id": invoice_id,
        "ordered_gallons": ordered_gallons,
        "loaded_gallons": loaded_gallons,
        "delivered_gallons": delivered_gallons,
        "invoiced_gallons": invoiced_gallons,
        "variance_load_vs_order_pct": variance_load_vs_order_pct,
        "variance_delivered_vs_loaded_pct": variance_delivered_vs_loaded_pct,
        "variance_invoiced_vs_delivered_pct": variance_invoiced_vs_delivered_pct,
        "alert_flags": list(alert_flags or []),
        "generated_at": generated_at.isoformat(),
        "created_at": generated_at.isoformat(),
        "updated_at": generated_at.isoformat(),
    }
    es.docs[(MVP_RECONCILIATION_INDEX, reconciliation_id)] = doc


# ===========================================================================
# GET /api/fuel/mvp/reconciliation — listing
# ===========================================================================


class TestListReconciliationEmpty:
    def test_empty_index_returns_empty_envelope(self):
        app, _ = _build_app()

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["page"] == 1
        assert body["page_size"] == 50
        assert body["has_next"] is False
        # Legacy aliases surface alongside the unified fields so the
        # existing ``fuelApi.ts`` consumer keeps working during the
        # deprecation window (Req 4.6).
        assert body["data"] == []
        assert body["pagination"] == {
            "page": 1,
            "size": 50,
            "total": 0,
            "total_pages": 0,
        }

    def test_populated_index_returns_records(self):
        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            order_id="order-1",
            plan_id="plan-1",
            pod_id="pod-1",
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        row = body["items"][0]
        assert row["reconciliation_id"] == "rec-1"
        assert row["order_id"] == "order-1"
        assert row["delivered_gallons"] == 500.0
        # Persistence-only fields must not leak through.
        assert "created_at" not in row
        assert "updated_at" not in row
        assert "payment_status" not in row
        # ``_id`` / ``_source`` wrapping from ES must never surface.
        assert "_id" not in row
        assert "_source" not in row


# ===========================================================================
# Tenant scoping
# ===========================================================================


class TestListReconciliationTenantScoping:
    def test_returns_only_calling_tenants_rows(self):
        app, es = _build_app(tenant_id="tenant-A")
        _seed_record(
            es,
            reconciliation_id="rec-A1",
            tenant_id="tenant-A",
        )
        _seed_record(
            es,
            reconciliation_id="rec-B1",
            tenant_id="tenant-B",
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-A1"]

    def test_cross_tenant_leak_dropped_defensively(self):
        """Even if the ES filter were bypassed, the endpoint re-validates
        every row's tenant_id before surfacing it."""

        app, es = _build_app(tenant_id="tenant-A")
        # Seed a row that tenant-A does not own.
        _seed_record(
            es,
            reconciliation_id="rec-B2",
            tenant_id="tenant-B",
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        ids = {row["reconciliation_id"] for row in resp.json()["items"]}
        assert "rec-B2" not in ids


# ===========================================================================
# Filters
# ===========================================================================


class TestListReconciliationOrderIdFilter:
    def test_order_id_filter(self):
        app, es = _build_app()
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A", order_id="order-1")
        _seed_record(es, reconciliation_id="rec-2", tenant_id="tenant-A", order_id="order-2")

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"order_id": "order-1"},
            )

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-1"]


class TestListReconciliationPlanIdFilter:
    def test_plan_id_filter(self):
        app, es = _build_app()
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A", plan_id="plan-alpha")
        _seed_record(es, reconciliation_id="rec-2", tenant_id="tenant-A", plan_id="plan-beta")

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"plan_id": "plan-alpha"},
            )

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-1"]


class TestListReconciliationPodIdFilter:
    def test_pod_id_filter(self):
        app, es = _build_app()
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A", pod_id="pod-X")
        _seed_record(es, reconciliation_id="rec-2", tenant_id="tenant-A", pod_id="pod-Y")

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"pod_id": "pod-Y"},
            )

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-2"]


class TestListReconciliationMinVariancePctFilter:
    def test_min_variance_matches_load_variance(self):
        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-below",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=1.5,
            variance_delivered_vs_loaded_pct=0.5,
        )
        _seed_record(
            es,
            reconciliation_id="rec-above",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=4.0,
            variance_delivered_vs_loaded_pct=0.0,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 3.0},
            )

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-above"]

    def test_min_variance_matches_delivered_variance(self):
        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-below",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=0.5,
            variance_delivered_vs_loaded_pct=0.5,
        )
        _seed_record(
            es,
            reconciliation_id="rec-above",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=0.0,
            variance_delivered_vs_loaded_pct=5.0,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 3.0},
            )

        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-above"]

    def test_min_variance_matches_invoice_variance(self):
        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-low-delivered",
            tenant_id="tenant-A",
            invoice_id="INV-1",
            invoiced_gallons=515.0,
            variance_load_vs_order_pct=0.0,
            variance_delivered_vs_loaded_pct=0.0,
            variance_invoiced_vs_delivered_pct=3.0,
        )
        _seed_record(
            es,
            reconciliation_id="rec-high-invoice",
            tenant_id="tenant-A",
            invoice_id="INV-2",
            invoiced_gallons=550.0,
            variance_load_vs_order_pct=0.0,
            variance_delivered_vs_loaded_pct=0.0,
            variance_invoiced_vs_delivered_pct=10.0,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 5.0},
            )

        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-high-invoice"]

    def test_min_variance_zero_matches_everything(self):
        app, es = _build_app()
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A")
        _seed_record(
            es,
            reconciliation_id="rec-2",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=7.0,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 0.0},
            )

        assert resp.status_code == 200
        ids = sorted(row["reconciliation_id"] for row in resp.json()["items"])
        assert ids == ["rec-1", "rec-2"]

    def test_negative_min_variance_rejected(self):
        app, _ = _build_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": -1.0},
            )
        # FastAPI's ``ge=0.0`` validator rejects this at request time.
        assert resp.status_code == 422

    def test_min_variance_partial_record_without_invoice_not_matched_on_invoice_alone(self):
        """A record with ``None`` invoice variance must be matched only
        by the two available variances — the missing invoice variance
        must not contribute to the OR."""

        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-below",
            tenant_id="tenant-A",
            variance_load_vs_order_pct=0.0,
            variance_delivered_vs_loaded_pct=1.0,
            variance_invoiced_vs_delivered_pct=None,
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 3.0},
            )

        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ===========================================================================
# Pagination & ordering
# ===========================================================================


class TestListReconciliationPaginationAndOrdering:
    def test_orders_by_generated_at_descending(self):
        app, es = _build_app()
        base = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
        _seed_record(
            es,
            reconciliation_id="rec-old",
            tenant_id="tenant-A",
            generated_at=base - timedelta(hours=2),
        )
        _seed_record(
            es,
            reconciliation_id="rec-new",
            tenant_id="tenant-A",
            generated_at=base,
        )

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-new", "rec-old"]

    def test_pagination_reports_has_next(self):
        app, es = _build_app()
        base = datetime(2025, 4, 1, 12, 0, tzinfo=timezone.utc)
        for i in range(3):
            _seed_record(
                es,
                reconciliation_id=f"rec-{i}",
                tenant_id="tenant-A",
                generated_at=base - timedelta(minutes=i),
            )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"page": 1, "size": 2},
            )

        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert body["total"] == 3
        assert body["has_next"] is True
        assert [row["reconciliation_id"] for row in body["items"]] == [
            "rec-0",
            "rec-1",
        ]

    def test_pagination_has_next_false_on_last_page(self):
        app, es = _build_app()
        for i in range(2):
            _seed_record(es, reconciliation_id=f"rec-{i}", tenant_id="tenant-A")

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"page": 1, "size": 5},
            )

        body = resp.json()
        assert body["total"] == 2
        assert body["has_next"] is False


# ===========================================================================
# Robustness
# ===========================================================================


class TestListReconciliationRobustness:
    def test_corrupt_row_is_dropped_not_fatal(self):
        app, es = _build_app()
        _seed_record(es, reconciliation_id="rec-good", tenant_id="tenant-A")
        # Seed a corrupt row missing required fields.
        es.docs[(MVP_RECONCILIATION_INDEX, "rec-bad")] = {
            "reconciliation_id": "rec-bad",
            "tenant_id": "tenant-A",
            # order_id intentionally missing — model validation fails
            "plan_id": "plan-1",
            "pod_id": "pod-1",
        }

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/reconciliation")

        assert resp.status_code == 200
        ids = [row["reconciliation_id"] for row in resp.json()["items"]]
        assert ids == ["rec-good"]

    def test_size_out_of_bounds_rejected(self):
        app, _ = _build_app()

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"size": 99999},
            )

        assert resp.status_code == 422


# ===========================================================================
# QBO invoice-update seam (Req 4.4.5, Phase 9 integration contract)
# ===========================================================================


class TestQBOInvoiceUpdateSeamHappyPath:
    @pytest.mark.asyncio
    async def test_seam_updates_invoice_fields_and_variance(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        # Seed a finalized record (no invoice yet).
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            delivered_gallons=500.0,
            variance_load_vs_order_pct=0.0,
            variance_delivered_vs_loaded_pct=0.0,
        )

        updated = await svc.update_invoice_fields(
            tenant_id="tenant-A",
            reconciliation_id="rec-1",
            invoice_id="INV-42",
            invoiced_gallons=515.0,
        )

        assert updated.invoice_id == "INV-42"
        assert updated.invoiced_gallons == 515.0
        # |515 - 500| / 500 * 100 = 3%
        assert updated.variance_invoiced_vs_delivered_pct == pytest.approx(3.0)
        # 3% is exactly at the default 3% threshold — no alert raised.
        assert updated.alert_flags == []
        # The persisted document should carry the new fields.
        persisted = es.docs[(MVP_RECONCILIATION_INDEX, "rec-1")]
        assert persisted["invoice_id"] == "INV-42"
        assert persisted["invoiced_gallons"] == 515.0
        assert persisted["variance_invoiced_vs_delivered_pct"] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_seam_raises_alert_flag_when_variance_exceeds_threshold(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            delivered_gallons=500.0,
        )

        updated = await svc.update_invoice_fields(
            tenant_id="tenant-A",
            reconciliation_id="rec-1",
            invoice_id="INV-9",
            invoiced_gallons=550.0,  # 10% over delivered
        )

        assert updated.variance_invoiced_vs_delivered_pct == pytest.approx(10.0)
        assert VARIANCE_ALERT_FLAG in updated.alert_flags

    @pytest.mark.asyncio
    async def test_seam_accepts_payment_status_when_supplied(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            delivered_gallons=500.0,
        )

        await svc.update_invoice_fields(
            tenant_id="tenant-A",
            reconciliation_id="rec-1",
            invoice_id="INV-1",
            invoiced_gallons=500.0,
            payment_status="paid",
        )

        persisted = es.docs[(MVP_RECONCILIATION_INDEX, "rec-1")]
        assert persisted["payment_status"] == "paid"

    @pytest.mark.asyncio
    async def test_seam_tenant_override_raises_threshold(self):
        es = _FakeESService()
        redis = _FakeRedis({"variance_alert_pct:tenant-A": "15.0"})
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            delivered_gallons=500.0,
        )

        updated = await svc.update_invoice_fields(
            tenant_id="tenant-A",
            reconciliation_id="rec-1",
            invoice_id="INV-1",
            invoiced_gallons=550.0,  # 10% — under tenant-configured 15%
        )

        assert updated.variance_invoiced_vs_delivered_pct == pytest.approx(10.0)
        assert updated.alert_flags == []


class TestQBOInvoiceUpdateSeamValidation:
    @pytest.mark.asyncio
    async def test_seam_rejects_missing_record(self):
        svc = ReconciliationService(es_service=_FakeESService())
        with pytest.raises(LookupError):
            await svc.update_invoice_fields(
                tenant_id="tenant-A",
                reconciliation_id="missing",
                invoice_id="INV-1",
                invoiced_gallons=100.0,
            )

    @pytest.mark.asyncio
    async def test_seam_rejects_cross_tenant_update(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
        )

        with pytest.raises(PermissionError):
            await svc.update_invoice_fields(
                tenant_id="tenant-B",  # wrong tenant
                reconciliation_id="rec-1",
                invoice_id="INV-1",
                invoiced_gallons=500.0,
            )

        # The persisted row must be untouched.
        persisted = es.docs[(MVP_RECONCILIATION_INDEX, "rec-1")]
        assert persisted.get("invoice_id") is None
        assert persisted.get("invoiced_gallons") is None

    @pytest.mark.asyncio
    async def test_seam_rejects_negative_invoiced_gallons(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
        )

        with pytest.raises(ValueError, match="invoiced_gallons"):
            await svc.update_invoice_fields(
                tenant_id="tenant-A",
                reconciliation_id="rec-1",
                invoice_id="INV-1",
                invoiced_gallons=-10.0,
            )

    @pytest.mark.asyncio
    async def test_seam_rejects_non_numeric_invoiced_gallons(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A")

        with pytest.raises(ValueError, match="invoiced_gallons"):
            await svc.update_invoice_fields(
                tenant_id="tenant-A",
                reconciliation_id="rec-1",
                invoice_id="INV-1",
                invoiced_gallons="not-a-number",  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_seam_rejects_empty_invoice_id(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A")

        with pytest.raises(ValueError, match="invoice_id"):
            await svc.update_invoice_fields(
                tenant_id="tenant-A",
                reconciliation_id="rec-1",
                invoice_id="",
                invoiced_gallons=500.0,
            )

    @pytest.mark.asyncio
    async def test_seam_rejects_empty_tenant_id(self):
        es = _FakeESService()
        svc = ReconciliationService(es_service=es)
        _seed_record(es, reconciliation_id="rec-1", tenant_id="tenant-A")

        with pytest.raises(ValueError, match="tenant_id"):
            await svc.update_invoice_fields(
                tenant_id="",
                reconciliation_id="rec-1",
                invoice_id="INV-1",
                invoiced_gallons=500.0,
            )


class TestQBOInvoiceUpdateSeamEndToEndWithEndpoint:
    """After the QBO connector writes invoice fields via the seam the
    subsequent GET must surface the new invoice variance and carry it
    through the ``min_variance_pct`` filter."""

    def test_updated_record_surfaces_invoice_variance_via_endpoint(self):
        app, es = _build_app()
        _seed_record(
            es,
            reconciliation_id="rec-1",
            tenant_id="tenant-A",
            delivered_gallons=500.0,
        )

        # Simulate the QBO connector callback on the same index.
        import asyncio

        svc = ReconciliationService(es_service=es)
        asyncio.run(
            svc.update_invoice_fields(
                tenant_id="tenant-A",
                reconciliation_id="rec-1",
                invoice_id="INV-42",
                invoiced_gallons=600.0,  # 20% over delivered
            )
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/mvp/reconciliation",
                params={"min_variance_pct": 10.0},
            )

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        row = items[0]
        assert row["invoice_id"] == "INV-42"
        assert row["invoiced_gallons"] == 600.0
        assert row["variance_invoiced_vs_delivered_pct"] == pytest.approx(20.0)
        assert VARIANCE_ALERT_FLAG in row["alert_flags"]
