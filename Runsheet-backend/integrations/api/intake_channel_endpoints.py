"""
REST endpoints for Intake Channel administration (Capability 2 / Task 4.3).

Exposes tenant-scoped, admin-gated HTTP routes under
``/api/integrations/intake-channels`` so operations engineers can register,
list, update, delete, and rotate secrets for upstream intake channels.

Routes mounted on :data:`router`:

* ``POST   /api/integrations/intake-channels``
  — create a new :class:`IntakeChannel`. Returns the freshly minted HMAC
  secret ONCE in the response body. Subsequent reads never expose it.

* ``GET    /api/integrations/intake-channels``
  — list all intake channels for the caller's tenant. Responses carry only
  the opaque ``hmac_secret_ref``, never the plaintext secret.

* ``PATCH  /api/integrations/intake-channels/{channel_id}``
  — partial update of a single :class:`IntakeChannel`. Protected fields
  (``hmac_secret_ref``, ``secret_version``, ``tenant_id``, ``channel_id``)
  cannot be mutated through this endpoint.

* ``DELETE /api/integrations/intake-channels/{channel_id}``
  — remove a channel and its vault credential.

* ``POST   /api/integrations/intake-channels/{channel_id}/rotate-secret``
  — generate a fresh HMAC secret, persist it in the vault, bump
  ``secret_version``, and return the new plaintext exactly once.

Every handler depends on :func:`get_tenant_context`, checks
``"admin" in tenant.roles``, and raises ``insufficient_role`` otherwise.

Tenant isolation is enforced at two layers:
    1. Every repository call takes the tenant_id from the verified
       :class:`TenantContext` (JWT-only).
    2. The repository re-validates tenant ownership on every returned
       document before crossing the boundary.

Validates: Requirements 2.1.1, 2.1.4, 2.1.6.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from errors.exceptions import resource_not_found
from auth.authorization import require_role
from fuel.intake_channel_models import IntakeChannel, RegistrableChannelType
from fuel.intake_channel_repository import (
    IntakeChannelCrossTenantAccessError,
    IntakeChannelRepository,
)
from fuel.services.order_intake_metrics import orders_intake_channel_rotations_total
from fuel.voice.voice_auth import VoiceApiKeyRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/integrations/intake-channels",
    tags=["intake-channels"],
)


# ---------------------------------------------------------------------------
# Module-level service wiring (same pattern as integrations_endpoints)
# ---------------------------------------------------------------------------

_repository: Optional[IntakeChannelRepository] = None
_voice_api_key_repository: Optional[VoiceApiKeyRepository] = None


def configure_intake_channel_endpoints(
    *,
    repository: IntakeChannelRepository,
    voice_api_key_repository: Optional[VoiceApiKeyRepository] = None,
) -> None:
    """Wire service dependencies into the intake channel REST module.

    Called once during application startup (from the integrations
    bootstrap). Tests inject a fake :class:`IntakeChannelRepository`
    so the router can be exercised without ES or the credentials vault.

    Args:
        repository: Tenant-scoped :class:`IntakeChannelRepository`
            responsible for CRUD against the ``intake_channels`` ES index.
        voice_api_key_repository: Optional :class:`VoiceApiKeyRepository`
            used to mint a Surface B voice API key when a ``voice`` channel is
            created. When ``None``, creating a voice channel still succeeds but
            returns no ``voice_api_key`` (a warning is logged); non-voice
            channels are unaffected.
    """
    global _repository, _voice_api_key_repository
    if repository is None:
        raise ValueError("repository must not be None")
    _repository = repository
    _voice_api_key_repository = voice_api_key_repository


def _get_repository() -> IntakeChannelRepository:
    if _repository is None:
        raise RuntimeError(
            "Intake channel endpoints not configured. "
            "Call configure_intake_channel_endpoints() during startup."
        )
    return _repository


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _require_admin(tenant: TenantContext) -> None:
    """Raise ``INSUFFICIENT_ROLE`` (HTTP 403) if the caller is not an admin.

    Delegates to the shared :func:`auth.authorization.require_role` helper
    so this router applies the one consistent, exact-match authorization
    mechanism (Req 4.7).
    """
    require_role(tenant, "admin")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateIntakeChannelRequest(BaseModel):
    """Body for ``POST /api/integrations/intake-channels``."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., min_length=3, max_length=64)
    channel_type: RegistrableChannelType
    display_name: str = Field(..., min_length=1)
    supported_schema_versions: List[str] = Field(..., min_length=1)
    rate_limit_per_minute: Optional[int] = Field(default=None)
    enabled: bool = True


class CreateIntakeChannelResponse(BaseModel):
    """Response for ``POST /api/integrations/intake-channels``.

    Includes the plaintext ``hmac_secret`` — returned exactly once.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    tenant_id: str
    channel_type: RegistrableChannelType
    display_name: str
    hmac_secret: str  # plaintext — returned ONCE, never again
    hmac_secret_ref: str
    supported_schema_versions: List[str]
    rate_limit_per_minute: Optional[int] = None
    secret_version: int
    enabled: bool
    created_at: str
    updated_at: str
    #: Surface B voice API key — minted ONCE for ``channel_type == "voice"``
    #: (like ``hmac_secret``), omitted/``None`` for every other channel type.
    voice_api_key: Optional[str] = None


class IntakeChannelView(BaseModel):
    """Response shape for a single :class:`IntakeChannel` in list/get.

    Does NOT include the plaintext secret — only the opaque
    ``hmac_secret_ref``.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    tenant_id: str
    channel_type: RegistrableChannelType
    display_name: str
    hmac_secret_ref: str
    supported_schema_versions: List[str]
    rate_limit_per_minute: Optional[int] = None
    secret_version: int
    enabled: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, model: IntakeChannel) -> "IntakeChannelView":
        dumped = model.model_dump(mode="json")
        return cls(**dumped)


class IntakeChannelListResponse(BaseModel):
    """Envelope for ``GET /api/integrations/intake-channels``."""

    model_config = ConfigDict(extra="forbid")

    items: List[IntakeChannelView]
    total: int


class UpdateIntakeChannelRequest(BaseModel):
    """Body for ``PATCH /api/integrations/intake-channels/{channel_id}``.

    Every field is optional so callers can send just the delta.
    Protected fields (hmac_secret_ref, secret_version, tenant_id,
    channel_id) are stripped by the repository.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, min_length=1)
    channel_type: Optional[RegistrableChannelType] = None
    supported_schema_versions: Optional[List[str]] = Field(default=None, min_length=1)
    rate_limit_per_minute: Optional[int] = None
    enabled: Optional[bool] = None


class RotateSecretResponse(BaseModel):
    """Response for ``POST .../rotate-secret``.

    Includes the new plaintext ``hmac_secret`` — returned exactly once.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    tenant_id: str
    hmac_secret: str  # new plaintext — returned ONCE, never again
    hmac_secret_ref: str
    secret_version: int
    updated_at: str


# ---------------------------------------------------------------------------
# POST /api/integrations/intake-channels (Req 2.1.1, 2.1.4)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateIntakeChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_intake_channel(
    body: CreateIntakeChannelRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> CreateIntakeChannelResponse:
    """Create a new intake channel, returning the freshly minted HMAC
    secret ONCE and storing its ref in the vault. Admin role required.

    Validates: Requirements 2.1.1, 2.1.4.
    """
    _require_admin(tenant)
    repo = _get_repository()

    channel, plaintext_secret = await repo.create(
        tenant_id=tenant.tenant_id,
        channel_id=body.channel_id,
        channel_type=body.channel_type,
        display_name=body.display_name,
        supported_schema_versions=body.supported_schema_versions,
        rate_limit_per_minute=body.rate_limit_per_minute,
        enabled=body.enabled,
    )

    logger.info(
        "intake_channel_endpoints.create: tenant=%s channel=%s type=%s",
        tenant.tenant_id,
        channel.channel_id,
        channel.channel_type,
    )

    # For a voice channel, mint the Surface B per-tenant API key so the Dinee
    # ws-server can authenticate against ``/voice/*``. Returned ONCE (like the
    # HMAC secret); never retrievable again. If the voice repository isn't wired
    # we log a warning and return the channel without a key rather than failing
    # the create.
    voice_api_key: Optional[str] = None
    if channel.channel_type == "voice":
        if _voice_api_key_repository is not None:
            voice_api_key = await _voice_api_key_repository.provision(
                channel.tenant_id, channel.channel_id
            )
            logger.info(
                "intake_channel_endpoints.create: minted voice API key for "
                "tenant=%s channel=%s",
                tenant.tenant_id,
                channel.channel_id,
            )
        else:
            logger.warning(
                "intake_channel_endpoints.create: voice channel created for "
                "tenant=%s channel=%s but VoiceApiKeyRepository is not wired — "
                "returning channel without a voice_api_key",
                tenant.tenant_id,
                channel.channel_id,
            )

    return CreateIntakeChannelResponse(
        channel_id=channel.channel_id,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        display_name=channel.display_name,
        hmac_secret=plaintext_secret,
        hmac_secret_ref=channel.hmac_secret_ref,
        supported_schema_versions=channel.supported_schema_versions,
        rate_limit_per_minute=channel.rate_limit_per_minute,
        secret_version=channel.secret_version,
        enabled=channel.enabled,
        created_at=channel.created_at.isoformat(),
        updated_at=channel.updated_at.isoformat(),
        voice_api_key=voice_api_key,
    )


# ---------------------------------------------------------------------------
# GET /api/integrations/intake-channels (Req 2.1.1)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=IntakeChannelListResponse,
)
async def list_intake_channels(
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntakeChannelListResponse:
    """List all intake channels for the caller's tenant.

    Responses carry only ``hmac_secret_ref`` — never the plaintext secret.
    Admin role required.

    Validates: Requirement 2.1.1.
    """
    _require_admin(tenant)
    repo = _get_repository()

    channels = await repo.list_for_tenant(tenant.tenant_id)

    items = [IntakeChannelView.from_model(ch) for ch in channels]
    logger.debug(
        "intake_channel_endpoints.list: tenant=%s total=%d",
        tenant.tenant_id,
        len(items),
    )

    return IntakeChannelListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# PATCH /api/integrations/intake-channels/{channel_id} (Req 2.1.1)
# ---------------------------------------------------------------------------


@router.patch(
    "/{channel_id}",
    response_model=IntakeChannelView,
)
async def update_intake_channel(
    channel_id: str,
    body: UpdateIntakeChannelRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntakeChannelView:
    """Partially update an intake channel. Admin role required.

    Protected fields (hmac_secret_ref, secret_version, tenant_id,
    channel_id) cannot be mutated through this endpoint. Use
    ``rotate-secret`` to change the HMAC secret.

    Validates: Requirement 2.1.1.
    """
    _require_admin(tenant)
    repo = _get_repository()

    updates: Dict[str, Any] = body.model_dump(exclude_none=True)

    if not updates:
        # Empty patch — fetch and return the existing channel
        existing = await repo.get(tenant.tenant_id, channel_id)
        if existing is None:
            raise resource_not_found(
                message=f"Intake channel '{channel_id}' not found",
                details={"channel_id": channel_id},
            )
        return IntakeChannelView.from_model(existing)

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            channel_id=channel_id,
            updates=updates,
        )
    except IntakeChannelCrossTenantAccessError:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )

    if updated is None:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )

    logger.info(
        "intake_channel_endpoints.update: tenant=%s channel=%s keys=%s",
        tenant.tenant_id,
        channel_id,
        sorted(updates.keys()),
    )

    return IntakeChannelView.from_model(updated)


# ---------------------------------------------------------------------------
# DELETE /api/integrations/intake-channels/{channel_id} (Req 2.1.1)
# ---------------------------------------------------------------------------


@router.delete(
    "/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_intake_channel(
    channel_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> None:
    """Delete an intake channel and its vault credential. Admin role required.

    Validates: Requirement 2.1.1.
    """
    _require_admin(tenant)
    repo = _get_repository()

    try:
        removed = await repo.delete(tenant.tenant_id, channel_id)
    except IntakeChannelCrossTenantAccessError:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )

    if not removed:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )

    logger.info(
        "intake_channel_endpoints.delete: tenant=%s channel=%s",
        tenant.tenant_id,
        channel_id,
    )


# ---------------------------------------------------------------------------
# POST /api/integrations/intake-channels/{channel_id}/rotate-secret
# (Req 2.1.6)
# ---------------------------------------------------------------------------


@router.post(
    "/{channel_id}/rotate-secret",
    response_model=RotateSecretResponse,
)
async def rotate_intake_channel_secret(
    channel_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> RotateSecretResponse:
    """Rotate the HMAC secret for an intake channel.

    Generates a fresh secret, persists it in the vault, bumps
    ``secret_version``, and returns the new plaintext exactly once.
    The old secret is invalidated within 60 seconds.
    Admin role required.

    Validates: Requirement 2.1.6.
    """
    _require_admin(tenant)
    repo = _get_repository()

    try:
        channel, new_plaintext = await repo.rotate_secret(
            tenant_id=tenant.tenant_id,
            channel_id=channel_id,
        )
    except ValueError:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )
    except IntakeChannelCrossTenantAccessError:
        raise resource_not_found(
            message=f"Intake channel '{channel_id}' not found",
            details={"channel_id": channel_id},
        )

    logger.info(
        "intake_channel_endpoints.rotate_secret: tenant=%s channel=%s "
        "new_version=%d",
        tenant.tenant_id,
        channel_id,
        channel.secret_version,
    )

    # Increment prometheus metric for secret rotation observability
    orders_intake_channel_rotations_total.labels(
        tenant_id=tenant.tenant_id,
    ).inc()

    return RotateSecretResponse(
        channel_id=channel.channel_id,
        tenant_id=channel.tenant_id,
        hmac_secret=new_plaintext,
        hmac_secret_ref=channel.hmac_secret_ref,
        secret_version=channel.secret_version,
        updated_at=channel.updated_at.isoformat(),
    )
