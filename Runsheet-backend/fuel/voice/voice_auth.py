"""
Surface B authentication — per-tenant Bearer + ``X-Runsheet-Tenant``.

This module implements the ``get_voice_tenant`` FastAPI dependency that
authenticates every Dinee voice Surface B read/driver endpoint. It is
deliberately **distinct** from the SuperTokens ``get_tenant_context``
dependency (``ops/middleware/tenant_guard.py``): the voice ws-server
authenticates with a per-tenant API key, not a SuperTokens session.

Authentication decision (Requirement 10):

1. No ``Authorization: Bearer <apiKey>`` header            → 401 (``voice_unauthorized``)
2. Bearer value does not resolve to a configured API key   → 401 (``voice_unauthorized``)
3. No ``X-Runsheet-Tenant`` header                         → 401 (``voice_unauthorized``)
4. ``X-Runsheet-Tenant`` != tenant bound to the key        → 403 (``voice_tenant_mismatch``)
5. Match → authorize; ``VoiceTenantContext.tenant_id`` comes from the
   **credential binding**, never the header/query/path/body (Req 11.4).
6. Rejections carry no tenant data or credential values (Req 10.6): the
   error factories use fixed, non-sensitive default messages and this module
   never attaches tenant identifiers, API keys, or hashes to ``details``.

**API-key storage (design "API-key storage").** The vault is keyed by
``(tenant_id, key)`` and cannot be queried "by API key" to find a tenant.
Authentication needs the reverse lookup (key → tenant), so a **salted hash**
of the API key is stored in the tenant-scoped ``voice_api_keys`` index
(``{api_key_sha256, tenant_id, channel_id, disabled, created_at}``) and the
**plaintext** key is stored in the :class:`TenantCredentialsVault` under
``voice_api_key:{channel_id}`` for rotation/audit. ``get_voice_tenant``
hashes the presented Bearer with the configured salt and looks up
``voice_api_keys`` by ``api_key_sha256`` (constant-time compare), giving an
O(1) reverse lookup while keeping plaintext keys out of the query path.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.4
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as stdlib_secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import Header, Request

from errors.exceptions import voice_tenant_mismatch, voice_unauthorized
from fuel.voice.voice_es_mappings import VOICE_API_KEYS_INDEX
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "VoiceTenantContext",
    "VoiceApiKeyRecord",
    "VoiceApiKeyRepository",
    "configure_voice_auth",
    "get_voice_auth_repository",
    "get_voice_tenant",
]

_BEARER_PREFIX = "bearer "


# ---------------------------------------------------------------------------
# Context + record models
# ---------------------------------------------------------------------------


@dataclass
class VoiceTenantContext:
    """Authenticated Surface B identity for a request.

    ``tenant_id`` is bound to the presented API key and is authoritative for
    all tenant scoping — it is derived from the credential binding, never from
    a client-supplied header/query/path/body (Req 11.4). ``channel_id`` is the
    voice channel the key was minted for (audit/rotation context).
    """

    tenant_id: str
    channel_id: str


@dataclass
class VoiceApiKeyRecord:
    """A resolved ``voice_api_keys`` reverse-lookup record."""

    api_key_sha256: str
    tenant_id: str
    channel_id: str
    disabled: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Return the Bearer token from an ``Authorization`` header, or ``None``.

    Tolerant of surrounding whitespace and any case of the ``Bearer`` scheme.
    Returns ``None`` when the header is absent, malformed, or carries an empty
    token so the caller can respond with a uniform 401.
    """
    if not authorization:
        return None
    value = authorization.strip()
    if not value.lower().startswith(_BEARER_PREFIX):
        return None
    token = value[len(_BEARER_PREFIX):].strip()
    return token or None


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response.

    Handles both plain dicts and the ``ObjectApiResponse`` wrapper the ES
    client returns (which exposes ``.get`` but is not a ``dict``).
    """
    if not resp:
        return []
    hits_outer = resp.get("hits") if hasattr(resp, "get") else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, "get") and hit.get("_source"):
            out.append(hit["_source"])
    return out


# ---------------------------------------------------------------------------
# Reverse-lookup repository
# ---------------------------------------------------------------------------


class VoiceApiKeyRepository:
    """Salted-hash reverse lookup for Surface B API keys.

    Resolves a presented Bearer token to its tenant binding by hashing the
    token with the configured salt and querying ``voice_api_keys`` for the
    matching ``api_key_sha256``. The plaintext key is stored separately in the
    :class:`TenantCredentialsVault` (``voice_api_key:{channel_id}``) for
    rotation/audit — never in this index.

    Dependencies are injected so the repository is trivially testable with a
    recording fake ES service and vault.
    """

    def __init__(
        self,
        es_service: Any,
        credentials_vault: Any,
        salt: str,
        *,
        index: str = VOICE_API_KEYS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if credentials_vault is None:
            raise ValueError("credentials_vault must not be None")
        if not salt:
            # A missing salt would make every stored hash trivially
            # reproducible; refuse to operate without one.
            raise ValueError("voice_api_key_salt must be a non-empty string")
        self._es = es_service
        self._vault = credentials_vault
        self._salt = salt
        self._index = index

    def hash_api_key(self, api_key: str) -> str:
        """Return the salted SHA-256 (HMAC) hex digest of ``api_key``.

        The salt is used as the HMAC key so the stored digest cannot be
        reproduced without both the key and the configured salt.
        """
        return hmac.new(
            self._salt.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    async def resolve(self, api_key: str) -> Optional[VoiceApiKeyRecord]:
        """Resolve a presented Bearer token to its ``VoiceApiKeyRecord``.

        Returns ``None`` when the token does not resolve to an enabled record.
        A constant-time compare against the stored hash is performed as
        defense-in-depth even though the term query already matched.
        """
        if not api_key:
            return None

        digest = self.hash_api_key(api_key)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"api_key_sha256": digest}},
                        {"term": {"disabled": False}},
                    ]
                }
            },
            "size": 1,
        }

        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("VoiceApiKeyRepository.resolve: lookup failed: %s", exc)
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        source = sources[0]
        stored = source.get("api_key_sha256") or ""
        # Defense-in-depth constant-time comparison.
        if not hmac.compare_digest(stored, digest):
            return None
        if source.get("disabled"):
            return None

        tenant_id = source.get("tenant_id")
        channel_id = source.get("channel_id")
        if not tenant_id or not channel_id:
            return None

        return VoiceApiKeyRecord(
            api_key_sha256=stored,
            tenant_id=tenant_id,
            channel_id=channel_id,
            disabled=bool(source.get("disabled", False)),
        )

    async def provision(self, tenant_id: str, channel_id: str) -> str:
        """Mint a new API key for ``(tenant_id, channel_id)`` and persist it.

        Stores the **plaintext** key in the vault under
        ``voice_api_key:{channel_id}`` (rotation/audit) and writes the salted
        hash to ``voice_api_keys`` for reverse lookup. Returns the plaintext
        key **exactly once** — it is never retrievable from the index and
        MUST NOT be logged or persisted elsewhere.
        """
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        api_key = stdlib_secrets.token_urlsafe(32)
        digest = self.hash_api_key(api_key)

        # Plaintext lives only in the vault for rotation/audit.
        await self._vault.put(
            tenant_id=tenant_id,
            key=f"voice_api_key:{channel_id}",
            plaintext={"api_key": api_key},
            provider_name="voice_intake",
        )

        doc = {
            "api_key_sha256": digest,
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "disabled": False,
            "created_at": utcnow().isoformat(),
        }
        # The salted hash is a stable, collision-free document id.
        await self._es.index_document(self._index, digest, doc)

        logger.info(
            "VoiceApiKeyRepository.provision: minted voice API key for "
            "tenant=%s channel=%s",
            tenant_id,
            channel_id,
        )
        return api_key


# ---------------------------------------------------------------------------
# Module-level wiring (mirrors ops.middleware.tenant_guard)
# ---------------------------------------------------------------------------

_voice_api_key_repository: Optional[VoiceApiKeyRepository] = None


def configure_voice_auth(repository: Optional[VoiceApiKeyRepository]) -> None:
    """Register the :class:`VoiceApiKeyRepository` used by ``get_voice_tenant``.

    Called from the bootstrap layer once ES + the vault are available. Passing
    ``None`` resets the wiring (useful for isolated tests).
    """
    global _voice_api_key_repository
    _voice_api_key_repository = repository


def get_voice_auth_repository() -> Optional[VoiceApiKeyRepository]:
    """Return the currently wired :class:`VoiceApiKeyRepository`, or ``None``."""
    return _voice_api_key_repository


# ---------------------------------------------------------------------------
# get_voice_tenant — the Surface B authentication dependency
# ---------------------------------------------------------------------------


async def get_voice_tenant(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_runsheet_tenant: Optional[str] = Header(None, alias="X-Runsheet-Tenant"),
) -> VoiceTenantContext:
    """FastAPI dependency producing an authenticated :class:`VoiceTenantContext`.

    Enforces the Requirement 10 decision table. The resolved ``tenant_id``
    comes exclusively from the credential binding (Req 11.4); the
    ``X-Runsheet-Tenant`` header is only used to confirm the caller's asserted
    tenant matches the bound one. Rejection envelopes exclude tenant data and
    credential values (Req 10.6).

    Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.4
    """
    repository = _voice_api_key_repository
    if repository is None:
        # No credential store wired — fail closed as unauthenticated rather
        # than leak that authentication is unavailable.
        logger.error("get_voice_tenant called before configure_voice_auth wiring")
        raise voice_unauthorized()

    # (1) Bearer must be present and well-formed.
    token = _extract_bearer(authorization)
    if token is None:
        raise voice_unauthorized()

    # (2) Bearer must resolve to a configured, enabled API key.
    record = await repository.resolve(token)
    if record is None:
        raise voice_unauthorized()

    # (3) X-Runsheet-Tenant must be present.
    if not x_runsheet_tenant or not x_runsheet_tenant.strip():
        raise voice_unauthorized()

    # (4) X-Runsheet-Tenant must match the tenant bound to the key.
    if x_runsheet_tenant != record.tenant_id:
        raise voice_tenant_mismatch()

    # (5) Authorized — scope derives from the credential binding.
    return VoiceTenantContext(
        tenant_id=record.tenant_id,
        channel_id=record.channel_id,
    )
