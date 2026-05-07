"""
Unit tests for ``GET /api/fuel/pod/{pod_id}/bol``.

Covers Task 8.6 of the fuel-ops-hardening spec:

* Req 4.3.4 — returns a presigned download URL for the BOL PDF associated
  with the given POD, scoped by the JWT-derived tenant_id.
* Req 4.3.5 — surfaces ``pending_regeneration`` rows without failing (no
  ``download_url`` is issued because no PDF exists yet).

The tests wire a fake ES service whose ``search_documents`` returns
preconfigured ``bill_of_lading`` rows, and a fake FileStorageService that
echoes a deterministic presigned URL so assertions don't need to stub
boto3.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    BOL_DOWNLOAD_PRESIGN_TTL_SECONDS,
    configure_fuel_ops_endpoints,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


TENANT_ID = "tenant-a"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = TENANT_ID):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="dispatcher-1",
            has_pii_access=True,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


class _BolESService:
    """ES stub that returns the preconfigured bill_of_lading rows when
    queried with the pod_id / tenant_id filter used by the endpoint."""

    def __init__(self, rows_by_pod: Dict[str, List[Dict[str, Any]]]) -> None:
        self._rows = rows_by_pod
        self.calls: List[Dict[str, Any]] = []

    async def search_documents(self, index: str, query: dict, size: int):
        self.calls.append({"index": index, "query": query, "size": size})
        if index != "bill_of_lading":
            return {"hits": {"hits": [], "total": {"value": 0}}}
        # Extract the pod_id filter.
        must = query.get("query", {}).get("bool", {}).get("must", [])
        pod_id = None
        tenant_id = None
        for clause in must:
            term = clause.get("term", {})
            if "pod_id" in term:
                pod_id = term["pod_id"]
            elif "tenant_id" in term:
                tenant_id = term["tenant_id"]
        rows = list(self._rows.get(pod_id or "", []))
        # Honor tenant filter — drop rows whose tenant_id doesn't match.
        rows = [r for r in rows if r.get("tenant_id") == tenant_id]
        return {
            "hits": {
                "hits": [{"_source": r} for r in rows],
                "total": {"value": len(rows)},
            }
        }


class _FakeFileStorage:
    """Fake FileStorageService that echoes a deterministic presigned URL."""

    def __init__(self, *, raises: Optional[Exception] = None) -> None:
        self._raises = raises
        self.calls: List[Dict[str, Any]] = []

    def presign_get(
        self,
        *,
        tenant_id: str,
        file_ref: str,
        ttl_seconds: int,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "file_ref": file_ref,
                "ttl_seconds": ttl_seconds,
                "actor": actor,
            }
        )
        if self._raises is not None:
            raise self._raises
        return {
            "file_ref": file_ref,
            "download_url": f"https://s3.test/{file_ref}?sig=fake",
            "expires_at": "2025-01-15T14:45:00+00:00",
        }


def _build_app(
    *,
    rows_by_pod: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    file_storage: Optional[_FakeFileStorage] = None,
    tenant_id: str = TENANT_ID,
):
    app = FastAPI()
    app.include_router(router)

    es = _BolESService(rows_by_pod or {})
    configure_fuel_ops_endpoints(
        es_service=es,
        file_storage_service=file_storage or _FakeFileStorage(),
    )

    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(tenant_id)
    return app, es, file_storage


def _bol_row(
    *,
    pod_id: str = "pod-1",
    tenant_id: str = TENANT_ID,
    status: str = "generated",
    file_ref: str = "tenants/tenant-a/bol/2025/01/15/abc.pdf",
    hash_value: str = "deadbeef" * 8,
    generated_at: str = "2025-01-15T14:30:00+00:00",
    bol_id: Optional[str] = None,
    **extra,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "bol_id": bol_id or f"bol-{tenant_id}-{pod_id}",
        "tenant_id": tenant_id,
        "pod_id": pod_id,
        "order_id": "order-1",
        "file_ref": file_ref,
        "hash": hash_value,
        "status": status,
        "fields": {"bol_number": "BOL-TEST"},
        "generated_at": generated_at,
        "created_at": generated_at,
        "updated_at": generated_at,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Happy path (Req 4.3.4)
# ---------------------------------------------------------------------------


class TestGetPodBol:
    def test_returns_presigned_download_url_for_generated_bol(self):
        row = _bol_row()
        fs = _FakeFileStorage()
        app, _, _ = _build_app(
            rows_by_pod={"pod-1": [row]},
            file_storage=fs,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-1/bol")

        assert resp.status_code == 200
        data = resp.json()
        assert data["pod_id"] == "pod-1"
        assert data["tenant_id"] == TENANT_ID
        assert data["status"] == "generated"
        assert data["file_ref"] == row["file_ref"]
        assert data["hash"] == row["hash"]
        assert data["download_url"] == f"https://s3.test/{row['file_ref']}?sig=fake"
        assert data["expires_at"] == "2025-01-15T14:45:00+00:00"
        assert data["generated_at"] == "2025-01-15T14:30:00+00:00"
        # FileStorageService was called with the tenant-scoped ref and
        # platform TTL.
        assert len(fs.calls) == 1
        call = fs.calls[0]
        assert call["tenant_id"] == TENANT_ID
        assert call["file_ref"] == row["file_ref"]
        assert call["ttl_seconds"] == BOL_DOWNLOAD_PRESIGN_TTL_SECONDS
        assert call["actor"] == "dispatcher-1"

    def test_returns_most_recent_row_when_multiple_regenerations(self):
        """A POD may accumulate pending + generated rows across retries; the
        endpoint must surface the most recent one."""
        older = _bol_row(
            generated_at="2025-01-15T14:30:00+00:00",
            status="pending_regeneration",
            file_ref="",
            hash_value="",
            bol_id="bol-old",
        )
        newer = _bol_row(
            generated_at="2025-01-15T14:45:00+00:00",
            status="generated",
            bol_id="bol-new",
        )
        # Return the newer row first to simulate an ES ``sort: desc``.
        app, _, _ = _build_app(rows_by_pod={"pod-1": [newer, older]})
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-1/bol")

        assert resp.status_code == 200
        assert resp.json()["bol_id"] == "bol-new"
        assert resp.json()["status"] == "generated"


# ---------------------------------------------------------------------------
# Pending regeneration (Req 4.3.5)
# ---------------------------------------------------------------------------


class TestPendingRegeneration:
    def test_returns_200_without_download_url_for_pending(self):
        """Req 4.3.5 — pending_regeneration rows are discoverable but carry
        no download URL (no PDF exists yet)."""
        row = _bol_row(status="pending_regeneration", file_ref="", hash_value="")
        fs = _FakeFileStorage()
        app, _, _ = _build_app(
            rows_by_pod={"pod-1": [row]},
            file_storage=fs,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-1/bol")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending_regeneration"
        assert data["download_url"] is None
        assert data["expires_at"] is None
        # FileStorageService must not be asked to presign an empty ref.
        assert fs.calls == []


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_cross_tenant_pod_returns_404(self):
        """A POD that exists but belongs to another tenant must not leak."""
        other_tenant_row = _bol_row(tenant_id="tenant-b")
        app, _, _ = _build_app(
            rows_by_pod={"pod-1": [other_tenant_row]},
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-1/bol")

        assert resp.status_code == 404
        body = resp.json()
        # errors.handlers wraps the detail; check for the core error_code.
        assert "bol_not_found" in str(body).replace("'", '"')

    def test_missing_pod_returns_404(self):
        app, _, _ = _build_app(rows_by_pod={})
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-missing/bol")

        assert resp.status_code == 404

    def test_empty_pod_id_returns_400(self):
        """A blank pod_id (which FastAPI should route through but surfaces
        as trim == '') is rejected with 400. The router naturally rejects
        the empty-segment URL itself, so we use whitespace-only here."""
        app, _, _ = _build_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/   /bol")

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# File storage wiring
# ---------------------------------------------------------------------------


class TestFileStorageWiring:
    def test_returns_503_when_file_storage_not_wired_but_bol_exists(self):
        """A generated BOL row must have a presignable URL. If the endpoint
        is not wired with a FileStorageService (misconfigured bootstrap),
        surface a 503 instead of silently returning a row without a URL."""
        row = _bol_row()
        # Wire without file_storage_service by overriding module state
        # after configure() completes.
        app, _, _ = _build_app(rows_by_pod={"pod-1": [row]})
        # Forcibly unset the FileStorageService to simulate bootstrap gap.
        from fuel.api import fuel_ops_endpoints as mod

        prior = mod._file_storage_service
        mod._file_storage_service = None
        try:
            client = TestClient(app)
            resp = client.get("/api/fuel/pod/pod-1/bol")
        finally:
            mod._file_storage_service = prior

        assert resp.status_code == 503

    def test_presign_permission_error_returns_500(self):
        row = _bol_row()
        fs = _FakeFileStorage(raises=PermissionError("cross_tenant_file_ref"))
        app, _, _ = _build_app(
            rows_by_pod={"pod-1": [row]},
            file_storage=fs,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/pod/pod-1/bol")

        assert resp.status_code == 500
        assert "bol_file_ref_corrupt" in str(resp.json()).replace("'", '"')
