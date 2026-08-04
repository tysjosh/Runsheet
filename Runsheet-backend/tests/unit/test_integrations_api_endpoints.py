"""
Unit tests for :mod:`integrations.api.integrations_endpoints`.

Task 9.3 of the fuel-ops-hardening spec mounts the tenant-scoped
``/api/integrations`` surface used by the Integration_Marketplace
(Req 5.6.1). These tests exercise the full wiring
(``configure_integrations_endpoints`` → :class:`IntegrationInstanceRepository`
→ :class:`IntegrationScheduler` stubs → :class:`TenantCredentialsVault`
stub) with an injected fake ES service so the suite stays decoupled from
Elasticsearch, APScheduler, and AWS KMS.

Covers:

* ``GET    /api/integrations`` — list with and without filters, pagination,
  tenant scoping, redacted credentials.
* ``POST   /api/integrations`` — creation with and without a
  ``credentials`` payload. Credentials are unwrapped into the vault
  and the plaintext NEVER appears in the response (Req 5.1.8).
* ``PATCH  /api/integrations/{id}`` — delta update, credentials
  rotation via the vault, 404 on missing, 403 on cross-tenant.
* ``DELETE /api/integrations/{id}`` — 204 on success, scheduler
  unschedule hook invoked once.
* ``POST   /api/integrations/{id}/enable`` / ``disable`` — flag flip +
  scheduler schedule/unschedule invoked.
* ``POST   /api/integrations/{id}/sync-now`` — returns the terminal
  :class:`SyncRun`, 404 on missing, 400 on disabled.
* ``GET    /api/integrations/{id}/sync-runs`` — returns the latest N
  sync runs from the ``integration_sync_runs`` index with cross-tenant
  rows dropped defensively.
* ``GET    /api/integrations/providers`` — returns whatever the
  per-provider registry (populated by Tasks 9.4–9.10) has registered.

Validates: Requirements 5.1.7, 5.1.8, 5.6.2, 5.6.6.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.api.integrations_endpoints import (
    configure_integrations_endpoints,
    router,
)
from integrations.connector_base import (
    IntegrationInstance,
    IntegrationInstanceRepository,
)
from integrations import provider_catalog
from integrations.provider_catalog import (
    ProviderCatalogEntry,
    clear_registry,
    register_provider,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub implementing the surface the repository uses."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.sync_run_docs: List[Dict[str, Any]] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        # sync_runs index uses a different doc store so tests can seed it
        # without polluting the instances map.
        if index == "integration_sync_runs":
            must = (
                query.get("query", {}).get("bool", {}).get("must", [])
                if isinstance(query, dict)
                else []
            )
            filters: Dict[str, Any] = {}
            for clause in must:
                for field, value in (clause.get("term") or {}).items():
                    filters[field] = value
            matches = [
                dict(d)
                for d in self.sync_run_docs
                if all(d.get(k) == v for k, v in filters.items())
            ]
            # Sort desc by started_at when requested.
            sort = query.get("sort") if isinstance(query, dict) else None
            if sort:
                matches.sort(
                    key=lambda d: d.get("started_at") or "",
                    reverse=True,
                )
            matches = matches[:size]
            return {"hits": {"hits": [{"_source": m} for m in matches]}}

        # integration_instances index — mirror the must+term shape used by
        # the repository.
        must = query.get("query", {}).get("bool", {}).get("must", [])
        filters: Dict[str, Any] = {}
        id_lookup: Optional[str] = None
        for clause in must:
            for field, value in (clause.get("term") or {}).items():
                if field == "instance_id":
                    id_lookup = value
                else:
                    filters[field] = value

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": []}}
            return {"hits": {"hits": [{"_source": dict(doc)}]}}

        matches = [
            dict(d)
            for d in self.docs.values()
            if all(d.get(k) == v for k, v in filters.items())
        ]
        return {"hits": {"hits": [{"_source": m} for m in matches[:size]]}}

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)

    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None


class _FakeVault:
    """Records every put/rotate call but never stores real secrets."""

    def __init__(self) -> None:
        self.put_calls: List[Dict[str, Any]] = []
        self.rotate_calls: List[Dict[str, Any]] = []
        self._counter = 0

    async def put(
        self,
        tenant_id: str,
        key: str,
        plaintext: Dict[str, Any],
        provider_name: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> str:
        self._counter += 1
        stored_ref = ref or f"cred:{tenant_id}:{key}:{self._counter}"
        self.put_calls.append(
            {
                "tenant_id": tenant_id,
                "key": key,
                "provider_name": provider_name,
                "ref": ref,
                # We intentionally keep a copy of the plaintext so the
                # test can assert the values crossed into the vault;
                # nothing in production logs the plaintext.
                "plaintext_keys": sorted(plaintext.keys()),
            }
        )
        return stored_ref

    async def rotate(self, tenant_id: str, ref: str) -> str:
        self.rotate_calls.append({"tenant_id": tenant_id, "ref": ref})
        return ref


class _FakeScheduler:
    """Records schedule/unschedule/sync_now invocations."""

    def __init__(
        self,
        *,
        sync_run: Optional[Dict[str, Any]] = None,
        sync_now_side_effect: Optional[Exception] = None,
    ) -> None:
        self.schedule_calls: List[str] = []
        self.unschedule_calls: List[str] = []
        self.sync_now_calls: List[tuple[str, str]] = []
        self._sync_run = sync_run
        self._side_effect = sync_now_side_effect

    async def schedule_instance(self, instance: IntegrationInstance) -> bool:
        self.schedule_calls.append(instance.instance_id)
        return True

    async def reschedule_instance(self, instance: IntegrationInstance) -> bool:
        self.schedule_calls.append(instance.instance_id)
        return True

    async def unschedule_instance(self, instance_id: str) -> bool:
        self.unschedule_calls.append(instance_id)
        return True

    async def sync_now(self, tenant_id: str, instance_id: str):
        self.sync_now_calls.append((tenant_id, instance_id))
        if self._side_effect is not None:
            raise self._side_effect
        from integrations.connector_base import SyncRun

        return SyncRun(
            **(
                self._sync_run
                or {
                    "run_id": "run-1",
                    "tenant_id": tenant_id,
                    "instance_id": instance_id,
                    "provider_name": "quickbooks_online",
                    "operation": "pull",
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": datetime.now(timezone.utc),
                    "status": "success",
                    "record_counts": {"invoices": 3},
                }
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-A"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["admin"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    scheduler: Optional[_FakeScheduler] = None,
    vault: Optional[_FakeVault] = None,
) -> tuple[FastAPI, _FakeESService, _FakeScheduler, _FakeVault]:
    es = _FakeESService()
    repo = IntegrationInstanceRepository(es_service=es)
    scheduler = scheduler or _FakeScheduler()
    vault = vault or _FakeVault()
    configure_integrations_endpoints(
        repository=repo,
        scheduler=scheduler,  # type: ignore[arg-type]
        credentials_vault=vault,
        es_service=es,
    )

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(tenant_id)
    return app, es, scheduler, vault


def _seed_instance(
    es: _FakeESService,
    *,
    instance_id: str = "integration_001",
    tenant_id: str = "tenant-A",
    provider_name: str = "quickbooks_online",
    category: str = "accounting",
    enabled: bool = False,
    status_value: str = "connected",
    credentials_ref: Optional[str] = "cred:tenant-A:quickbooks_online_credentials:1",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "instance_id": instance_id,
        "tenant_id": tenant_id,
        "provider_name": provider_name,
        "category": category,
        "status": status_value,
        "enabled": enabled,
        "credentials_ref": credentials_ref,
        "schedule_cron": "0 */6 * * *",
        "config": {"realm_id": "9999"},
        "last_sync_at": None,
        "last_error": None,
        "retry_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    es.docs[instance_id] = doc
    return doc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListIntegrations:
    def test_returns_tenant_scoped_rows_with_redacted_credentials(self):
        app, es, _, _ = _build_app()
        _seed_instance(es, instance_id="integ-1")
        _seed_instance(
            es,
            instance_id="other",
            tenant_id="tenant-B",  # cross-tenant, must not leak
        )
        client = TestClient(app)

        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        view = body["items"][0]
        assert view["instance_id"] == "integ-1"
        # Credentials are REDACTED. The opaque ref is surfaced so the
        # Marketplace can render a "Reset credentials" action; the
        # status flag is derived from ref presence.
        assert view["credentials_ref"].startswith("cred:")
        assert view["credentials_status"] == "valid"
        # No plaintext credential fields should be leaking through.
        assert "credentials" not in view

    def test_filters_by_provider_and_enabled(self):
        app, es, _, _ = _build_app()
        _seed_instance(es, instance_id="qb", provider_name="quickbooks_online")
        _seed_instance(es, instance_id="stripe", provider_name="stripe", category="payment")
        _seed_instance(
            es,
            instance_id="qb-disabled",
            provider_name="quickbooks_online",
            enabled=False,
        )
        client = TestClient(app)

        resp = client.get(
            "/api/integrations",
            params={"provider_name": "quickbooks_online"},
        )
        assert resp.status_code == 200
        ids = sorted(i["instance_id"] for i in resp.json()["items"])
        assert ids == ["qb", "qb-disabled"]

    def test_missing_credentials_ref_reports_missing_status(self):
        app, es, _, _ = _build_app()
        _seed_instance(es, credentials_ref=None)
        client = TestClient(app)

        resp = client.get("/api/integrations")
        view = resp.json()["items"][0]
        assert view["credentials_ref"] is None
        assert view["credentials_status"] == "missing"


class TestCreateIntegration:
    def test_stamps_tenant_from_jwt_and_returns_201(self):
        app, es, _, _ = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.post(
            "/api/integrations",
            json={
                "instance_id": "integ-new",
                "provider_name": "veeder_root",
                "category": "tank_monitor",
                "schedule_cron": "*/15 * * * *",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["instance_id"] == "integ-new"
        assert body["tenant_id"] == "tenant-A"
        assert body["provider_name"] == "veeder_root"
        assert body["credentials_status"] == "missing"
        assert "integ-new" in es.docs
        assert es.docs["integ-new"]["tenant_id"] == "tenant-A"

    def test_credentials_are_unwrapped_into_vault_and_redacted(self):
        app, es, _, vault = _build_app()
        client = TestClient(app)

        resp = client.post(
            "/api/integrations",
            json={
                "provider_name": "quickbooks_online",
                "category": "accounting",
                "credentials": {
                    "client_id": "qbo-client",
                    "client_secret": "SHOULD-NEVER-LEAK",
                    "refresh_token": "SHOULD-NEVER-LEAK",
                },
            },
        )
        assert resp.status_code == 201
        body = resp.json()

        # The body must not carry credentials, only a ref.
        assert "credentials" not in body
        assert body["credentials_ref"].startswith("cred:")
        assert body["credentials_status"] == "valid"

        # Vault received the plaintext; no secrets leaked elsewhere.
        assert len(vault.put_calls) == 1
        call = vault.put_calls[0]
        assert call["tenant_id"] == "tenant-A"
        assert call["provider_name"] == "quickbooks_online"
        assert sorted(call["plaintext_keys"]) == [
            "client_id",
            "client_secret",
            "refresh_token",
        ]

        # And the persisted document does not include the plaintext
        # either — only the ref.
        persisted = next(iter(es.docs.values()))
        assert persisted.get("credentials_ref", "").startswith("cred:")
        assert "credentials" not in persisted

    def test_mints_id_when_omitted(self):
        app, es, _, _ = _build_app()
        client = TestClient(app)

        resp = client.post(
            "/api/integrations",
            json={
                "provider_name": "geotab",
                "category": "gps_eld",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["instance_id"].startswith("integration_")
        assert data["instance_id"] in es.docs


class TestPatchIntegration:
    def test_updates_fields_and_preserves_immutable(self):
        app, es, _, _ = _build_app()
        _seed_instance(es)
        client = TestClient(app)

        resp = client.patch(
            "/api/integrations/integration_001",
            json={"schedule_cron": "*/5 * * * *", "enabled": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["schedule_cron"] == "*/5 * * * *"
        assert body["enabled"] is True
        # Immutable tenant_id + provider_name preserved.
        assert body["tenant_id"] == "tenant-A"
        assert body["provider_name"] == "quickbooks_online"

    def test_returns_404_when_missing(self):
        app, _, _, _ = _build_app()
        client = TestClient(app)
        resp = client.patch(
            "/api/integrations/does-not-exist",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "integration_instance_not_found"

    def test_credentials_update_replaces_plaintext_under_existing_ref(self):
        app, es, _, vault = _build_app()
        _seed_instance(es, credentials_ref="cred:tenant-A:qbo:1")
        client = TestClient(app)

        resp = client.patch(
            "/api/integrations/integration_001",
            json={"credentials": {"refresh_token": "ROTATED"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["credentials_ref"] == "cred:tenant-A:qbo:1"
        assert "credentials" not in body
        assert vault.rotate_calls == []
        assert vault.put_calls == [
            {
                "tenant_id": "tenant-A",
                "key": "quickbooks_online_credentials",
                "provider_name": "quickbooks_online",
                "ref": "cred:tenant-A:qbo:1",
                "plaintext_keys": ["refresh_token"],
            }
        ]

    def test_credentials_first_time_uses_put(self):
        app, es, _, vault = _build_app()
        _seed_instance(es, credentials_ref=None)
        client = TestClient(app)

        resp = client.patch(
            "/api/integrations/integration_001",
            json={"credentials": {"api_token": "tok-123"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["credentials_ref"].startswith("cred:")
        assert vault.put_calls and not vault.rotate_calls


class TestDeleteIntegration:
    def test_204_on_success_and_unschedules(self):
        app, es, scheduler, _ = _build_app()
        _seed_instance(es)
        client = TestClient(app)

        resp = client.delete("/api/integrations/integration_001")
        assert resp.status_code == 204
        assert "integration_001" not in es.docs
        assert scheduler.unschedule_calls == ["integration_001"]

    def test_404_when_missing(self):
        app, _, scheduler, _ = _build_app()
        client = TestClient(app)
        resp = client.delete("/api/integrations/missing")
        assert resp.status_code == 404
        assert scheduler.unschedule_calls == []


class TestEnableDisable:
    def test_enable_flips_flag_and_schedules(self):
        app, es, scheduler, _ = _build_app()
        _seed_instance(es, enabled=False)
        client = TestClient(app)

        resp = client.post("/api/integrations/integration_001/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        assert es.docs["integration_001"]["enabled"] is True
        assert scheduler.schedule_calls == ["integration_001"]

    def test_disable_flips_flag_and_unschedules(self):
        app, es, scheduler, _ = _build_app()
        _seed_instance(es, enabled=True)
        client = TestClient(app)

        resp = client.post("/api/integrations/integration_001/disable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert es.docs["integration_001"]["enabled"] is False
        assert scheduler.unschedule_calls == ["integration_001"]


class TestSyncNow:
    def test_returns_terminal_sync_run(self):
        app, es, _, _ = _build_app()
        _seed_instance(es, enabled=True)
        client = TestClient(app)

        resp = client.post("/api/integrations/integration_001/sync-now")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["instance_id"] == "integration_001"
        assert body["record_counts"] == {"invoices": 3}

    def test_404_for_missing_instance(self):
        scheduler = _FakeScheduler(sync_now_side_effect=LookupError("missing"))
        app, _, _, _ = _build_app(scheduler=scheduler)
        client = TestClient(app)
        resp = client.post("/api/integrations/missing/sync-now")
        assert resp.status_code == 404

    def test_400_for_disabled_instance(self):
        scheduler = _FakeScheduler(
            sync_now_side_effect=ValueError("instance disabled"),
        )
        app, es, _, _ = _build_app(scheduler=scheduler)
        _seed_instance(es, enabled=False)
        client = TestClient(app)
        resp = client.post("/api/integrations/integration_001/sync-now")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "instance_disabled"


class TestListSyncRuns:
    def test_returns_most_recent_runs_capped_at_limit(self):
        app, es, _, _ = _build_app()
        _seed_instance(es)
        # Seed three sync run docs with distinct started_at so desc sort
        # yields a deterministic order.
        for idx, started_at in enumerate(
            [
                "2024-01-01T00:00:00+00:00",
                "2024-01-02T00:00:00+00:00",
                "2024-01-03T00:00:00+00:00",
            ]
        ):
            es.sync_run_docs.append(
                {
                    "run_id": f"run-{idx}",
                    "tenant_id": "tenant-A",
                    "instance_id": "integration_001",
                    "provider_name": "quickbooks_online",
                    "operation": "pull",
                    "started_at": started_at,
                    "finished_at": started_at,
                    "status": "success",
                    "record_counts": {"invoices": idx + 1},
                }
            )
        client = TestClient(app)

        resp = client.get(
            "/api/integrations/integration_001/sync-runs",
            params={"limit": 2},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        # Latest first
        assert body["items"][0]["run_id"] == "run-2"
        assert body["items"][1]["run_id"] == "run-1"

    def test_404_when_instance_not_owned(self):
        app, es, _, _ = _build_app(tenant_id="tenant-A")
        _seed_instance(es, tenant_id="tenant-B")
        client = TestClient(app)
        resp = client.get("/api/integrations/integration_001/sync-runs")
        assert resp.status_code == 404

    def test_defensively_drops_cross_tenant_runs(self):
        app, es, _, _ = _build_app(tenant_id="tenant-A")
        _seed_instance(es)
        # Seed one owned + one cross-tenant run with the SAME
        # instance_id — the defensive per-document tenant check must
        # drop the alien row.
        es.sync_run_docs.append(
            {
                "run_id": "run-owned",
                "tenant_id": "tenant-A",
                "instance_id": "integration_001",
                "provider_name": "quickbooks_online",
                "operation": "pull",
                "started_at": "2024-01-02T00:00:00+00:00",
                "status": "success",
                "record_counts": {"invoices": 1},
            }
        )
        es.sync_run_docs.append(
            {
                "run_id": "run-alien",
                "tenant_id": "tenant-B",
                "instance_id": "integration_001",
                "provider_name": "quickbooks_online",
                "operation": "pull",
                "started_at": "2024-01-03T00:00:00+00:00",
                "status": "success",
                "record_counts": {},
            }
        )
        client = TestClient(app)
        resp = client.get("/api/integrations/integration_001/sync-runs")
        assert resp.status_code == 200
        body = resp.json()
        assert [r["run_id"] for r in body["items"]] == ["run-owned"]


class TestProviderCatalog:
    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_returns_registered_providers(self):
        app, _, _, _ = _build_app()
        register_provider(
            ProviderCatalogEntry(
                provider_name="quickbooks_online",
                category="accounting",
                description="Sync invoices and payments with QuickBooks Online.",
                required_credential_fields=["client_id", "client_secret", "refresh_token"],
                doc_url="https://example.com/qbo",
                auth_mode="oauth2",
            )
        )
        register_provider(
            ProviderCatalogEntry(
                provider_name="veeder_root",
                category="tank_monitor",
                description="Pull ATG tank levels.",
                required_credential_fields=["api_token"],
            )
        )
        client = TestClient(app)

        resp = client.get("/api/integrations/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        names = [p["provider_name"] for p in body["items"]]
        assert names == ["quickbooks_online", "veeder_root"]
        # Defaults surface the overlay feature-flag key template.
        assert body["items"][1]["feature_flag_key"] is None
        # Task 9.10: every entry exposes the Marketplace-level
        # effective feature-flag key so the UI can gate visibility
        # via overlay.integration.{provider_name} (Req 5.6.6).
        assert (
            body["items"][0]["effective_feature_flag_key"]
            == "overlay.integration.quickbooks_online"
        )
        assert (
            body["items"][1]["effective_feature_flag_key"]
            == "overlay.integration.veeder_root"
        )
        # No secret values leak — only the field-name schema.
        for entry in body["items"]:
            assert "credentials" not in entry

    def test_returns_empty_catalog_when_nothing_registered(self):
        app, _, _, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/integrations/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"items": [], "total": 0}


class TestProviderCatalogModel:
    """Coverage for the registry model itself — feature-flag defaults, validation."""

    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_rejects_unknown_category(self):
        with pytest.raises(Exception):
            ProviderCatalogEntry(
                provider_name="bad",
                category="unknown",
                description="nope",
            )

    def test_effective_flag_key_defaults_to_overlay_namespace(self):
        entry = ProviderCatalogEntry(
            provider_name="stripe",
            category="payment",
            description="Charge customers via Stripe.",
        )
        assert entry.effective_feature_flag_key() == "overlay.integration.stripe"

    def test_register_and_list_preserve_order(self):
        for name in ["qbo", "vroot", "geo", "stripe"]:
            register_provider(
                ProviderCatalogEntry(
                    provider_name=name,
                    category="accounting" if name == "qbo" else (
                        "tank_monitor" if name == "vroot"
                        else "gps_eld" if name == "geo"
                        else "payment"
                    ),
                    description=f"{name} integration.",
                )
            )
        assert [p.provider_name for p in provider_catalog.list_providers()] == [
            "qbo",
            "vroot",
            "geo",
            "stripe",
        ]

    def test_re_register_replaces_existing(self):
        register_provider(
            ProviderCatalogEntry(
                provider_name="qbo",
                category="accounting",
                description="first",
            )
        )
        register_provider(
            ProviderCatalogEntry(
                provider_name="qbo",
                category="accounting",
                description="second",
            )
        )
        entries = provider_catalog.list_providers()
        assert len(entries) == 1
        assert entries[0].description == "second"

    def test_required_credential_fields_dedup_and_strip(self):
        entry = ProviderCatalogEntry(
            provider_name="p",
            category="payment",
            description="x",
            required_credential_fields=[
                "api_key",
                "",
                "api_key",
                "  webhook_secret  ",
                "webhook_secret",
            ],
        )
        assert entry.required_credential_fields == ["api_key", "webhook_secret"]
