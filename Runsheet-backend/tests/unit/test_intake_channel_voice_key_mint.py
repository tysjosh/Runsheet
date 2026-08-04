"""
Unit tests for Surface B voice API-key minting on intake-channel creation.

# Feature: dinee-voice-integration

``POST /api/integrations/intake-channels`` now mints a Surface B
``voice_api_key`` when a ``channel_type="voice"`` channel is created (returned
exactly once, like ``hmac_secret``). These tests assert:

    * creating a ``voice`` channel returns a non-empty ``voice_api_key`` whose
      resolution via the REAL :class:`VoiceApiKeyRepository.resolve` yields the
      same ``(tenant_id, channel_id)`` binding;
    * creating a non-voice channel returns no ``voice_api_key`` (``None``);
    * when no :class:`VoiceApiKeyRepository` is wired, a voice channel still
      creates successfully but returns no ``voice_api_key``.

The intake-channel repository is the in-memory fake used by the endpoint suite;
the voice API-key repository is the REAL ``VoiceApiKeyRepository`` driven over
the recording ES + vault fakes from ``tests/property/test_voice_bearer_auth_property``
so the mint → resolve round-trip exercises the true salted-hash reverse lookup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.intake_channel_models import IntakeChannel
from fuel.voice.voice_auth import VoiceApiKeyRepository
from integrations.api.intake_channel_endpoints import (
    configure_intake_channel_endpoints,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Recording ES + vault fakes (mirror test_voice_bearer_auth_property)
# ---------------------------------------------------------------------------
class FakeES:
    """Recording fake honouring the ``bool.filter`` term clauses so the
    salted-hash reverse lookup behaves like a real exact-match query."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, dict]] = {}

    async def index_document(self, index: str, doc_id: str, document: dict):
        self.docs.setdefault(index, {})[doc_id] = dict(document)

    async def search_documents(self, index, query, size=100, request_timeout=10):
        filters = query.get("query", {}).get("bool", {}).get("filter", [])
        wanted: dict = {}
        for clause in filters:
            wanted.update(clause.get("term", {}))
        hits = []
        for source in self.docs.get(index, {}).values():
            if all(source.get(k) == v for k, v in wanted.items()):
                hits.append({"_source": source})
        return {"hits": {"hits": hits[:size]}}


class FakeVault:
    """Recording fake for the ``TenantCredentialsVault`` ``put`` surface."""

    def __init__(self) -> None:
        self.stored: Dict[Tuple[str, str], dict] = {}

    async def put(self, *, tenant_id, key, plaintext, provider_name=None):
        self.stored[(tenant_id, key)] = plaintext


_SALT = "unit-test-voice-mint-salt"


# ---------------------------------------------------------------------------
# In-memory intake-channel repository fake (mirrors the endpoint suite)
# ---------------------------------------------------------------------------
class FakeIntakeChannelRepository:
    def __init__(self) -> None:
        self._channels: Dict[str, IntakeChannel] = {}
        self._secret_counter = 0

    def _key(self, tenant_id: str, channel_id: str) -> str:
        return f"{tenant_id}::{channel_id}"

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tenant_ctx(tenant_id: str = "tenant-A") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="user-1",
        has_pii_access=False,
        roles=["admin"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _build_client(
    *,
    voice_repo: Optional[VoiceApiKeyRepository],
    tenant_id: str = "tenant-A",
) -> TestClient:
    intake_repo = FakeIntakeChannelRepository()
    configure_intake_channel_endpoints(
        repository=intake_repo,
        voice_api_key_repository=voice_repo,
    )
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})

    app.dependency_overrides[get_tenant_context] = lambda: _tenant_ctx(tenant_id)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestVoiceApiKeyMinting:
    """# Feature: dinee-voice-integration — voice key minted on create."""

    def test_voice_channel_create_mints_resolvable_key(self):
        es = FakeES()
        vault = FakeVault()
        voice_repo = VoiceApiKeyRepository(es, vault, _SALT)
        client = _build_client(voice_repo=voice_repo, tenant_id="tenant-A")

        resp = client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "voice-line-01",
                "channel_type": "voice",
                "display_name": "Voice Line 01",
                "supported_schema_versions": ["1.0"],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        # The voice API key is minted and returned exactly once, non-empty.
        voice_api_key = body.get("voice_api_key")
        assert voice_api_key, "expected a non-empty voice_api_key on voice create"

        # Resolving the minted key yields the same tenant/channel binding.
        record = asyncio.run(voice_repo.resolve(voice_api_key))
        assert record is not None
        assert record.tenant_id == "tenant-A"
        assert record.channel_id == "voice-line-01"
        assert record.disabled is False

    def test_non_voice_channel_create_returns_no_voice_key(self):
        es = FakeES()
        vault = FakeVault()
        voice_repo = VoiceApiKeyRepository(es, vault, _SALT)
        client = _build_client(voice_repo=voice_repo)

        resp = client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "edi-partner-01",
                "channel_type": "edi",
                "display_name": "EDI Partner 01",
                "supported_schema_versions": ["1.0"],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body.get("voice_api_key") is None
        # No key was minted into the reverse-lookup index.
        assert es.docs == {}

    def test_voice_channel_create_without_wired_repo_returns_no_key(self):
        # No VoiceApiKeyRepository wired — the create must still succeed and
        # simply omit the voice_api_key (fail-soft, warning logged).
        client = _build_client(voice_repo=None)

        resp = client.post(
            "/api/integrations/intake-channels",
            json={
                "channel_id": "voice-line-02",
                "channel_type": "voice",
                "display_name": "Voice Line 02",
                "supported_schema_versions": ["1.0"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json().get("voice_api_key") is None


@pytest.fixture(autouse=True)
def _reset_endpoint_wiring():
    """Reset the module-level endpoint wiring after each test."""
    yield
    # Leave a benign repository wired so other suites are unaffected.
    configure_intake_channel_endpoints(
        repository=FakeIntakeChannelRepository(),
        voice_api_key_repository=None,
    )
