"""
Unit tests for :mod:`integrations.api.intake_channel_endpoints`.

Task 4.5 of the order-intake-pipeline spec. Exercises the admin-gated
intake channel REST surface with a mocked :class:`IntakeChannelRepository`
so the suite stays decoupled from Elasticsearch and the credentials vault.

Covers:

* Non-admin caller gets 403 ``insufficient_role`` on every endpoint.
* Admin caller can create, list, get (via list), update, delete, rotate.
* Create response includes ``hmac_secret`` (plaintext).
* List/get responses do NOT include ``hmac_secret`` field.
* Rotate response includes new ``hmac_secret`` and bumped ``secret_version``.
* Cross-tenant access returns 404 (not 403) to avoid leaking existence.

Validates: Requirements 2.1.3, 2.1.5, 10.2.1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.intake_channel_models import IntakeChannel
from fuel.intake_channel_repository import (
    IntakeChannelCrossTenantAccessError,
    IntakeChannelRepository,
)
from integrations.api.intake_channel_endpoints import (
    configure_intake_channel_endpoints,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes / Mocks
# ---------------------------------------------------------------------------


class FakeIntakeChannelRepository:
    """In-memory fake of :class:`IntakeChannelRepository`.

    Stores channels in a dict keyed by ``(tenant_id, channel_id)`` and
    simulates the tenant-isolation behaviour of the real repository.
    """

    def __init__(self) -> None:
        self._channels: Dict[str, IntakeChannel] = {}
        self._secret_counter: int = 0

    def _key(self, tenant_id: str, channel_id: str) -> str:
        return f"{tenant_id}::{channel_id}"

    def _seed(self, channel: IntakeChannel) -> None:
        """Seed a channel directly (for test setup)."""
        self._channels[self._key(channel.tenant_id, channel.channel_id)] = channel

    async def create(
        self,
        tenant_id: str,
        channel_id: str,
        channel_type: str,
        display_name: str,
        supported_schema_versions: List[str],
        *,
        rate_limit_per_minute: Optional[int] = None,
        enabled: bool = True,
    ) -> Tuple[IntakeChannel, str]:
        self._secret_counter += 1
        now = datetime.now(timezone.utc)
        plaintext = f"secret-plaintext-{self._secret_counter}"
        ref = f"vault-ref:{tenant_id}:{channel_id}:{self._secret_counter}"

        channel = IntakeChannel(
            channel_id=channel_id,
            tenant_id=tenant_id,
            channel_type=channel_type,
            display_name=display_name,
            hmac_secret_ref=ref,
            supported_schema_versions=supported_schema_versions,
            rate_limit_per_minute=rate_limit_per_minute,
            secret_version=1,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._channels[self._key(tenant_id, channel_id)] = channel
        return channel, plaintext

    async def get(
        self, tenant_id: str, channel_id: str
    ) -> Optional[IntakeChannel]:
        key = self._key(tenant_id, channel_id)
        ch = self._channels.get(key)
        if ch is None:
            return None
        # Simulate tenant isolation: cross-tenant reads return None
        if ch.tenant_id != tenant_id:
            return None
        return ch

    async def list_for_tenant(
        self, tenant_id: str, *, size: int = 500
    ) -> List[IntakeChannel]:
        return [
            ch
            for ch in self._channels.values()
            if ch.tenant_id == tenant_id
        ]

    async def update(
        self,
        tenant_id: str,
        channel_id: str,
        updates: Dict[str, Any],
    ) -> Optional[IntakeChannel]:
        key = self._key(tenant_id, channel_id)
        existing = self._channels.get(key)
        if existing is None:
            return None
        if existing.tenant_id != tenant_id:
            raise IntakeChannelCrossTenantAccessError(
                tenant_id=tenant_id,
                channel_id=channel_id,
                owning_tenant_id=existing.tenant_id,
            )
        existing_dict = existing.model_dump(mode="python")
        # Strip protected fields
        for field in ("hmac_secret_ref", "secret_version", "tenant_id", "channel_id"):
            updates.pop(field, None)
        existing_dict.update(updates)
        existing_dict["updated_at"] = datetime.now(timezone.utc)
        updated = IntakeChannel(**existing_dict)
        self._channels[key] = updated
        return updated

    async def delete(self, tenant_id: str, channel_id: str) -> bool:
        key = self._key(tenant_id, channel_id)
        existing = self._channels.get(key)
        if existing is None:
            return False
        if existing.tenant_id != tenant_id:
            raise IntakeChannelCrossTenantAccessError(
                tenant_id=tenant_id,
                channel_id=channel_id,
                owning_tenant_id=existing.tenant_id,
            )
        del self._channels[key]
        return True

    async def rotate_secret(
        self, tenant_id: str, channel_id: str
    ) -> Tuple[IntakeChannel, str]:
        key = self._key(tenant_id, channel_id)
        existing = self._channels.get(key)
        if existing is None:
            raise ValueError(
                f"Intake channel {channel_id!r} not found for tenant {tenant_id!r}"
            )
        if existing.tenant_id != tenant_id:
            raise IntakeChannelCrossTenantAccessError(
                tenant_id=tenant_id,
                channel_id=channel_id,
                owning_tenant_id=existing.tenant_id,
            )
        self._secret_counter += 1
        new_plaintext = f"rotated-secret-{self._secret_counter}"
        new_ref = f"vault-ref:{tenant_id}:{channel_id}:{self._secret_counter}"
        new_version = existing.secret_version + 1
        now = datetime.now(timezone.utc)

        updated_dict = existing.model_dump(mode="python")
        updated_dict["hmac_secret_ref"] = new_ref
        updated_dict["secret_version"] = new_version
        updated_dict["updated_at"] = now
        updated = IntakeChannel(**updated_dict)
        self._channels[key] = updated
        return updated, new_plaintext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx(
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="user-1",
        has_pii_access=False,
        roles=roles if roles is not None else ["admin"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
    repo: Optional[FakeIntakeChannelRepository] = None,
) -> Tuple[FastAPI, TestClient, FakeIntakeChannelRepository]:
    """Build a FastAPI app with the intake channel router wired in."""
    repo = repo or FakeIntakeChannelRepository()
    configure_intake_channel_endpoints(repository=repo)

    app = FastAPI()
    app.include_router(router)

    # Register the AppException handler so errors come back as JSON
    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )

    ctx = _tenant_ctx(tenant_id=tenant_id, roles=roles)
    app.dependency_overrides[get_tenant_context] = lambda: ctx

    client = TestClient(app)
    return app, client, repo


def _seed_channel(
    repo: FakeIntakeChannelRepository,
    *,
    channel_id: str = "voice-provider-1",
    tenant_id: str = "tenant-A",
    channel_type: str = "voice",
    display_name: str = "Voice Provider 1",
    secret_version: int = 1,
) -> IntakeChannel:
    """Seed a channel directly into the fake repository."""
    now = datetime.now(timezone.utc)
    channel = IntakeChannel(
        channel_id=channel_id,
        tenant_id=tenant_id,
        channel_type=channel_type,
        display_name=display_name,
        hmac_secret_ref=f"vault-ref:{tenant_id}:{channel_id}:seed",
        supported_schema_versions=["1.0"],
        rate_limit_per_minute=100,
        secret_version=secret_version,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    repo._seed(channel)
    return channel


# ---------------------------------------------------------------------------
# Tests — Admin-only access (Req 2.1.5, 10.2.1)
# ---------------------------------------------------------------------------


class TestAdminOnlyAccess:
    """Non-admin callers get 403 ``insufficient_role`` on every endpoint."""

    @pytest.fixture
    def non_admin_client(self):
        """Client with a dispatcher role (not admin)."""
        _, client, repo = _build_app(roles=["dispatcher"])
        _seed_channel(repo)
        return client

    def test_create_returns_403_for_non_admin(self, non_admin_client):
        resp = non_admin_client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "new-channel-01",
                "channel_type": "voice",
                "display_name": "New Channel",
                "supported_schema_versions": ["1.0"],
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_list_returns_403_for_non_admin(self, non_admin_client):
        resp = non_admin_client.get("/api/integrations/intake-channels")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_update_returns_403_for_non_admin(self, non_admin_client):
        resp = non_admin_client.patch(
            "/api/integrations/intake-channels/voice-provider-1",
            json={"display_name": "Updated"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_delete_returns_403_for_non_admin(self, non_admin_client):
        resp = non_admin_client.delete(
            "/api/integrations/intake-channels/voice-provider-1"
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_rotate_returns_403_for_non_admin(self, non_admin_client):
        resp = non_admin_client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"


# ---------------------------------------------------------------------------
# Tests — Admin CRUD operations (Req 2.1.1, 2.1.3, 2.1.4, 2.1.6)
# ---------------------------------------------------------------------------


class TestAdminCRUD:
    """Admin caller can create, list, update, delete, and rotate."""

    def test_create_returns_201_with_hmac_secret(self):
        _, client, _ = _build_app()
        resp = client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "partner-edi-01",
                "channel_type": "edi",
                "display_name": "Partner EDI Feed",
                "supported_schema_versions": ["1.0", "2.0"],
                "rate_limit_per_minute": 60,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["channel_id"] == "partner-edi-01"
        assert body["tenant_id"] == "tenant-A"
        assert body["channel_type"] == "edi"
        assert body["display_name"] == "Partner EDI Feed"
        assert body["supported_schema_versions"] == ["1.0", "2.0"]
        assert body["rate_limit_per_minute"] == 60
        assert body["secret_version"] == 1
        assert body["enabled"] is True
        # Plaintext secret returned exactly once on create
        assert "hmac_secret" in body
        assert len(body["hmac_secret"]) > 0
        assert body["hmac_secret_ref"].startswith("vault-ref:")

    def test_list_returns_channels_without_hmac_secret(self):
        _, client, repo = _build_app()
        _seed_channel(repo, channel_id="channel-aaa")
        _seed_channel(repo, channel_id="channel-bbb")

        resp = client.get("/api/integrations/intake-channels")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        for item in body["items"]:
            # hmac_secret MUST NOT appear in list responses
            assert "hmac_secret" not in item
            # hmac_secret_ref is present (opaque reference)
            assert "hmac_secret_ref" in item
            assert item["hmac_secret_ref"].startswith("vault-ref:")

    def test_update_returns_channel_without_hmac_secret(self):
        _, client, repo = _build_app()
        _seed_channel(repo)

        resp = client.patch(
            "/api/integrations/intake-channels/voice-provider-1",
            json={"display_name": "Updated Voice Provider"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Updated Voice Provider"
        # hmac_secret MUST NOT appear in update responses
        assert "hmac_secret" not in body
        assert "hmac_secret_ref" in body

    def test_delete_returns_204(self):
        _, client, repo = _build_app()
        _seed_channel(repo)

        resp = client.delete(
            "/api/integrations/intake-channels/voice-provider-1"
        )
        assert resp.status_code == 204

    def test_rotate_returns_new_secret_and_bumped_version(self):
        _, client, repo = _build_app()
        _seed_channel(repo, secret_version=1)

        resp = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        assert resp.status_code == 200
        body = resp.json()
        # New plaintext secret returned exactly once
        assert "hmac_secret" in body
        assert len(body["hmac_secret"]) > 0
        # Secret version bumped
        assert body["secret_version"] == 2
        # New ref
        assert body["hmac_secret_ref"].startswith("vault-ref:")
        assert body["channel_id"] == "voice-provider-1"
        assert body["tenant_id"] == "tenant-A"


# ---------------------------------------------------------------------------
# Tests — Plaintext secret returned exactly once on create (Req 2.1.4)
# ---------------------------------------------------------------------------


class TestSecretReturnedOnce:
    """Plaintext HMAC secret is returned exactly once on create and rotate,
    and never appears in list/get responses."""

    def test_create_returns_plaintext_secret(self):
        _, client, _ = _build_app()
        resp = client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "test-channel-01",
                "channel_type": "api_partner",
                "display_name": "Test Channel",
                "supported_schema_versions": ["1.0"],
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "hmac_secret" in body
        assert body["hmac_secret"] != ""

    def test_list_never_exposes_plaintext_secret(self):
        _, client, repo = _build_app()
        # Create a channel (which would have a secret)
        _seed_channel(repo)

        resp = client.get("/api/integrations/intake-channels")
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert "hmac_secret" not in item

    def test_rotate_returns_new_plaintext_secret(self):
        _, client, repo = _build_app()
        _seed_channel(repo)

        resp = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "hmac_secret" in body
        assert body["hmac_secret"] != ""


# ---------------------------------------------------------------------------
# Tests — Rotate invalidates previous secret (Req 2.1.6)
# ---------------------------------------------------------------------------


class TestRotateInvalidatesPrevious:
    """Rotate bumps secret_version so the old secret is invalidated."""

    def test_rotate_bumps_secret_version(self):
        _, client, repo = _build_app()
        _seed_channel(repo, secret_version=3)

        resp = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["secret_version"] == 4

    def test_rotate_changes_hmac_secret_ref(self):
        _, client, repo = _build_app()
        original = _seed_channel(repo)
        original_ref = original.hmac_secret_ref

        resp = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        assert resp.status_code == 200
        body = resp.json()
        # The ref should be different from the original
        assert body["hmac_secret_ref"] != original_ref

    def test_consecutive_rotates_produce_different_secrets(self):
        _, client, repo = _build_app()
        _seed_channel(repo)

        resp1 = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        secret1 = resp1.json()["hmac_secret"]

        resp2 = client.post(
            "/api/integrations/intake-channels/voice-provider-1/rotate-secret"
        )
        secret2 = resp2.json()["hmac_secret"]

        assert secret1 != secret2
        assert resp2.json()["secret_version"] == 3


# ---------------------------------------------------------------------------
# Tests — Tenant isolation / cross-tenant access (Req 2.1.5)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Cross-tenant access returns 404 on reads and 403 on writes
    (mapped to 404 by the endpoint to avoid leaking existence)."""

    def test_cross_tenant_read_returns_404(self):
        """Tenant B's channel is invisible to Tenant A (returns 404)."""
        repo = FakeIntakeChannelRepository()
        # Seed a channel owned by tenant-B
        _seed_channel(repo, channel_id="tenant-b-channel", tenant_id="tenant-B")

        # Build app as tenant-A
        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        # Attempt to update (which does a get first) — should get 404
        resp = client.patch(
            "/api/integrations/intake-channels/tenant-b-channel",
            json={"display_name": "Hacked"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_delete_returns_404(self):
        """Tenant A cannot delete Tenant B's channel — gets 404."""
        repo = FakeIntakeChannelRepository()
        _seed_channel(repo, channel_id="tenant-b-channel", tenant_id="tenant-B")

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.delete(
            "/api/integrations/intake-channels/tenant-b-channel"
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_rotate_returns_404(self):
        """Tenant A cannot rotate Tenant B's secret — gets 404."""
        repo = FakeIntakeChannelRepository()
        _seed_channel(repo, channel_id="tenant-b-channel", tenant_id="tenant-B")

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.post(
            "/api/integrations/intake-channels/tenant-b-channel/rotate-secret"
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_list_only_shows_own_tenant_channels(self):
        """List only returns channels belonging to the caller's tenant."""
        repo = FakeIntakeChannelRepository()
        _seed_channel(repo, channel_id="my-channel-01", tenant_id="tenant-A")
        _seed_channel(repo, channel_id="their-channel", tenant_id="tenant-B")
        _seed_channel(repo, channel_id="my-channel-02", tenant_id="tenant-A")

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/integrations/intake-channels")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        channel_ids = [item["channel_id"] for item in body["items"]]
        assert "my-channel-01" in channel_ids
        assert "my-channel-02" in channel_ids
        assert "their-channel" not in channel_ids

    def test_nonexistent_channel_returns_404_not_403(self):
        """A channel that doesn't exist returns 404, not 403."""
        _, client, _ = _build_app()

        resp = client.patch(
            "/api/integrations/intake-channels/does-not-exist",
            json={"display_name": "Ghost"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_delete_nonexistent_returns_404(self):
        _, client, _ = _build_app()

        resp = client.delete(
            "/api/integrations/intake-channels/does-not-exist"
        )
        assert resp.status_code == 404

    def test_rotate_nonexistent_returns_404(self):
        _, client, _ = _build_app()

        resp = client.post(
            "/api/integrations/intake-channels/does-not-exist/rotate-secret"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — HMAC secret never in list/get responses (Req 2.1.3)
# ---------------------------------------------------------------------------


class TestHmacSecretNeverInListGet:
    """The plaintext HMAC secret MUST never appear in list or get responses."""

    def test_list_response_has_no_hmac_secret_field(self):
        _, client, repo = _build_app()
        _seed_channel(repo, channel_id="channel-aaa")
        _seed_channel(repo, channel_id="channel-bbb")
        _seed_channel(repo, channel_id="channel-ccc")

        resp = client.get("/api/integrations/intake-channels")
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            # The key "hmac_secret" must not exist in the response
            assert "hmac_secret" not in item
            # But hmac_secret_ref (the opaque vault reference) is present
            assert "hmac_secret_ref" in item

    def test_update_response_has_no_hmac_secret_field(self):
        """PATCH response (which acts as a get) must not expose the secret."""
        _, client, repo = _build_app()
        _seed_channel(repo)

        resp = client.patch(
            "/api/integrations/intake-channels/voice-provider-1",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "hmac_secret" not in body
        assert "hmac_secret_ref" in body
