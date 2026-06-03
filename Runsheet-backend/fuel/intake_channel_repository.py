"""
Intake Channel Repository — tenant-scoped CRUD for ``intake_channels``
Elasticsearch index.

Implements :class:`IntakeChannelRepository` with:

* ``create`` — persist a new IntakeChannel, generate and vault the HMAC
  secret, return the plaintext exactly once.
* ``get`` — single channel by ID, tenant-scoped.
* ``list_for_tenant`` — paginated listing with tenant isolation.
* ``update`` — partial update of a channel document.
* ``delete`` — remove a channel (and its vault credential).
* ``rotate_secret`` — generate a fresh HMAC secret, persist it in the
  vault, bump ``secret_version``, and return the new plaintext exactly once.

``create`` and ``rotate_secret`` use
:class:`services.credentials_vault.TenantCredentialsVault` to persist the
HMAC secret and return the plaintext to the caller exactly once. A
``secret_version: int`` on every channel increments on rotate so the webhook
receiver can invalidate old secrets within 60 seconds.

Every method wraps reads through
:func:`ops.middleware.tenant_guard.inject_tenant_filter` and validates
returned documents re-match the caller's tenant before crossing the
repository boundary. Cross-tenant reads degrade to ``None`` (for ``get``)
or empty lists (for ``list_for_tenant``).
Cross-tenant writes raise :class:`IntakeChannelCrossTenantAccessError`.

Validates: Requirements 2.1.3, 2.1.4, 2.1.6.
"""
from __future__ import annotations

import logging
import re
import secrets as stdlib_secrets
from typing import Any, Dict, List, Optional, Tuple

from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_es_mappings import INTAKE_CHANNELS_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IntakeChannelCrossTenantAccessError(PermissionError):
    """Raised when a write targets a channel owned by another tenant.

    Cross-tenant reads degrade silently to ``None`` / empty lists so the
    REST layer can return a uniform HTTP 404 without leaking existence.
    Cross-tenant writes are a security violation and MUST raise.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.channel_id = channel_id
        self.owning_tenant_id = owning_tenant_id
        super().__init__(
            f"Tenant {tenant_id!r} attempted cross-tenant access on "
            f"intake channel {channel_id!r} (owner={owning_tenant_id!r})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not resp:
        return []
    # Handle both dict and ObjectApiResponse (which has .get() but isn't a dict)
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _safe_channel_load(source: Dict[str, Any]) -> Optional[IntakeChannel]:
    """Build an :class:`IntakeChannel` from a raw ES source, logging on failure."""
    try:
        return IntakeChannel(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "IntakeChannelRepository: dropping intake_channels doc that "
            "failed model validation (channel_id=%s): %s",
            source.get("channel_id"),
            exc,
        )
        return None


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class IntakeChannelRepository:
    """Tenant-scoped CRUD repository for intake channels.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The interfaces relied upon:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``
        * ``await es.delete_document(index, doc_id)``

    which matches :class:`services.elasticsearch_service.ElasticsearchService`.

    * ``await credentials_vault.put(tenant_id, key, plaintext, provider_name)``
    * ``await credentials_vault.get(tenant_id, ref)``
    * ``await credentials_vault.delete(tenant_id, ref)``

    which matches :class:`services.credentials_vault.TenantCredentialsVault`.

    Tenant isolation is enforced at two points for defense-in-depth:
        1. Every ES query is wrapped through
           :func:`ops.middleware.tenant_guard.inject_tenant_filter`.
        2. Every returned document is re-validated against the caller's
           ``tenant_id`` before it crosses the repository boundary.
    """

    DEFAULT_LIST_SIZE: int = 500

    def __init__(
        self,
        es_service: Any,
        credentials_vault: Any,
        *,
        channels_index: str = INTAKE_CHANNELS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if credentials_vault is None:
            raise ValueError("credentials_vault must not be None")
        self._es = es_service
        self._vault = credentials_vault
        self._channels_index = channels_index

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

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
        """Create a new intake channel with a freshly minted HMAC secret.

        Generates a cryptographically random HMAC-SHA256 secret, stores it
        in the :class:`TenantCredentialsVault`, persists the channel
        document in ES, and returns both the channel model and the
        plaintext secret (returned exactly once — never again).

        Args:
            tenant_id: Owning tenant.
            channel_id: Unique channel identifier (validated by model).
            channel_type: One of the registrable channel types.
            display_name: Human-readable name for the channel.
            supported_schema_versions: Non-empty list of supported versions.
            rate_limit_per_minute: Optional rate limit override.
            enabled: Whether the channel is active (default True).

        Returns:
            A tuple of ``(IntakeChannel, plaintext_secret)``. The plaintext
            is returned exactly once and MUST NOT be logged or persisted
            outside the vault.

        Raises:
            :class:`IntakeChannelCrossTenantAccessError` if tenant mismatch.
            ValueError: If required fields are invalid.
        """
        self._require_tenant(tenant_id)

        # Generate a fresh HMAC secret
        plaintext_secret = stdlib_secrets.token_urlsafe(32)

        # Store in the credentials vault
        vault_ref = await self._vault.put(
            tenant_id=tenant_id,
            key=f"intake_channel_hmac:{channel_id}",
            plaintext={"secret": plaintext_secret},
            provider_name="intake_channel",
        )

        now = _utcnow_iso()
        payload: Dict[str, Any] = {
            "channel_id": channel_id,
            "tenant_id": tenant_id,
            "channel_type": channel_type,
            "display_name": display_name,
            "hmac_secret_ref": vault_ref,
            "supported_schema_versions": supported_schema_versions,
            "rate_limit_per_minute": rate_limit_per_minute,
            "secret_version": 1,
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
        }

        # Validate through the Pydantic model before touching ES
        model = IntakeChannel(**payload)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._channels_index, model.channel_id, doc
        )

        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("intake_channel", doc)

        logger.info(
            "IntakeChannelRepository.create: created channel=%s "
            "tenant=%s type=%s",
            channel_id,
            tenant_id,
            channel_type,
        )

        return model, plaintext_secret

    # ------------------------------------------------------------------
    # Get (single channel)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, channel_id: str
    ) -> Optional[IntakeChannel]:
        """Return the channel or ``None`` if it does not exist / is not owned.

        Cross-tenant fetches degrade to ``None`` so the REST layer can
        return a uniform HTTP 404 without leaking existence.
        """
        self._require_tenant(tenant_id)
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        # Read-cutover: serve from Postgres when enabled.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_get,
        )
        pg = await read_hybrid_get("intake_channel", tenant_id, channel_id)
        if pg is not _NOT_CUT_OVER:
            return _safe_channel_load(pg) if pg is not None else None

        query = inject_tenant_filter(
            {"query": {"term": {"channel_id": channel_id}}},
            tenant_id,
        )
        query["size"] = 1

        try:
            resp = await self._es.search_documents(
                self._channels_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntakeChannelRepository.get: search failed for "
                "channel=%s: %s",
                channel_id,
                exc,
            )
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        source = sources[0]
        # Defense-in-depth: re-validate tenant ownership
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "IntakeChannelRepository.get: suppressing cross-tenant hit "
                "for channel=%s (owner=%s, requester=%s)",
                channel_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None

        return _safe_channel_load(source)

    # ------------------------------------------------------------------
    # List for tenant
    # ------------------------------------------------------------------

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[IntakeChannel]:
        """List all intake channels for the tenant (up to ``size``).

        Results are tenant-scoped and re-validated before returning.
        """
        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        # Read-cutover: serve from Postgres when enabled (created_at DESC).
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_list,
        )
        pg = await read_hybrid_list("intake_channel", tenant_id, limit=size)
        if pg is not _NOT_CUT_OVER:
            out_pg: List[IntakeChannel] = []
            for source in pg["items"]:
                model = _safe_channel_load(source)
                if model is not None:
                    out_pg.append(model)
            return out_pg

        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant_id,
        )
        query["size"] = size
        query["sort"] = [{"created_at": {"order": "desc"}}]

        resp = await self._es.search_documents(
            self._channels_index, query, size
        )
        sources = _extract_sources(resp)

        out: List[IntakeChannel] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "IntakeChannelRepository.list_for_tenant: dropping doc "
                    "with mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_channel_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Update (partial)
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        channel_id: str,
        updates: Dict[str, Any],
    ) -> Optional[IntakeChannel]:
        """Partially update a channel document.

        Fetches the existing channel first to validate tenant ownership,
        then applies the partial update. Returns the updated IntakeChannel
        model or ``None`` if the channel does not exist for this tenant.

        Raises :class:`IntakeChannelCrossTenantAccessError` if the channel
        belongs to another tenant.

        Note: ``hmac_secret_ref``, ``secret_version``, ``tenant_id``, and
        ``channel_id`` cannot be mutated through this method. Use
        ``rotate_secret`` to change the HMAC secret.
        """
        self._require_tenant(tenant_id)
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        # Prevent mutation of protected fields
        protected = {"hmac_secret_ref", "secret_version", "tenant_id", "channel_id"}
        for field in protected:
            updates.pop(field, None)

        # Fetch existing to validate ownership
        existing = await self.get(tenant_id, channel_id)
        if existing is None:
            return None

        # Build the updated payload
        existing_dict = existing.model_dump(mode="python")
        existing_dict.update(updates)
        existing_dict["updated_at"] = _utcnow_iso()

        # Prevent tenant_id mutation
        if existing_dict.get("tenant_id") != tenant_id:
            raise IntakeChannelCrossTenantAccessError(
                tenant_id=tenant_id,
                channel_id=channel_id,
                owning_tenant_id=existing_dict.get("tenant_id"),
            )

        # Validate through the Pydantic model before touching ES
        model = IntakeChannel(**existing_dict)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._channels_index, model.channel_id, doc
        )

        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("intake_channel", doc)

        logger.info(
            "IntakeChannelRepository.update: updated channel=%s tenant=%s",
            channel_id,
            tenant_id,
        )
        return model

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(
        self,
        tenant_id: str,
        channel_id: str,
    ) -> bool:
        """Delete a channel and its vault credential.

        Returns ``True`` if the channel was deleted, ``False`` if it did
        not exist for this tenant.

        Raises :class:`IntakeChannelCrossTenantAccessError` if the channel
        belongs to another tenant.
        """
        self._require_tenant(tenant_id)
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        # Fetch existing to validate ownership
        existing = await self.get(tenant_id, channel_id)
        if existing is None:
            return False

        # Delete the vault credential (best-effort — channel deletion
        # proceeds even if vault cleanup fails)
        try:
            await self._vault.delete(tenant_id, existing.hmac_secret_ref)
        except Exception as exc:
            logger.warning(
                "IntakeChannelRepository.delete: failed to delete vault "
                "credential ref=%s for channel=%s: %s",
                existing.hmac_secret_ref,
                channel_id,
                exc,
            )

        # Delete the channel document from ES (best-effort: once the
        # intake_channels index is dropped in Phase 6 this is a no-op, so a
        # missing-index error must not fail the delete — Postgres is the
        # source-of-truth).
        try:
            await self._es.delete_document(self._channels_index, channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "IntakeChannelRepository.delete: ES delete skipped/failed for "
                "channel=%s (continuing; Postgres is source-of-truth): %s",
                channel_id, exc,
            )

        # Delete the authoritative Postgres row so the read-cutover path stops
        # serving the channel.
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_delete,
        )
        await mirror_current_state_delete("intake_channel", tenant_id, channel_id)

        logger.info(
            "IntakeChannelRepository.delete: deleted channel=%s tenant=%s",
            channel_id,
            tenant_id,
        )
        return True

    # ------------------------------------------------------------------
    # Rotate secret
    # ------------------------------------------------------------------

    async def rotate_secret(
        self,
        tenant_id: str,
        channel_id: str,
    ) -> Tuple[IntakeChannel, str]:
        """Generate a fresh HMAC secret, persist it in the vault, bump
        ``secret_version``, and return the new plaintext exactly once.

        The old secret is invalidated within 60 seconds by bumping the
        ``secret_version`` stored on the channel document. The webhook
        receiver verifies against the current version only.

        Args:
            tenant_id: The caller's tenant.
            channel_id: The channel whose secret to rotate.

        Returns:
            A tuple of ``(updated IntakeChannel, new_plaintext_secret)``.
            The plaintext is returned exactly once and MUST NOT be logged
            or persisted outside the vault.

        Raises:
            :class:`IntakeChannelCrossTenantAccessError` if the channel
            belongs to another tenant.
            ValueError: If the channel does not exist for this tenant.
        """
        self._require_tenant(tenant_id)
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        # Fetch existing to validate ownership
        existing = await self.get(tenant_id, channel_id)
        if existing is None:
            raise ValueError(
                f"Intake channel {channel_id!r} not found for tenant "
                f"{tenant_id!r}"
            )

        # Generate a fresh HMAC secret
        new_plaintext_secret = stdlib_secrets.token_urlsafe(32)

        # Store the new secret in the vault under a new key
        new_vault_ref = await self._vault.put(
            tenant_id=tenant_id,
            key=f"intake_channel_hmac:{channel_id}",
            plaintext={"secret": new_plaintext_secret},
            provider_name="intake_channel",
        )

        # Delete the old vault credential (best-effort)
        old_ref = existing.hmac_secret_ref
        try:
            await self._vault.delete(tenant_id, old_ref)
        except Exception as exc:
            logger.warning(
                "IntakeChannelRepository.rotate_secret: failed to delete "
                "old vault credential ref=%s for channel=%s: %s",
                old_ref,
                channel_id,
                exc,
            )

        # Bump secret_version and update the channel document
        new_version = existing.secret_version + 1
        now = _utcnow_iso()

        existing_dict = existing.model_dump(mode="python")
        existing_dict["hmac_secret_ref"] = new_vault_ref
        existing_dict["secret_version"] = new_version
        existing_dict["updated_at"] = now

        # Validate through the Pydantic model before touching ES
        model = IntakeChannel(**existing_dict)
        doc = model.model_dump(mode="json", exclude_none=False)

        await self._es.index_document(
            self._channels_index, model.channel_id, doc
        )

        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("intake_channel", doc)

        logger.info(
            "IntakeChannelRepository.rotate_secret: rotated secret for "
            "channel=%s tenant=%s new_version=%d",
            channel_id,
            tenant_id,
            new_version,
        )

        return model, new_plaintext_secret

    # ------------------------------------------------------------------
    # Cross-tenant lookups (for webhook channel resolution)
    # ------------------------------------------------------------------

    async def get_by_channel_id(
        self, channel_id: str
    ) -> Optional[IntakeChannel]:
        """Look up a channel by ``channel_id`` without requiring a tenant.

        Used by the webhook intake path where the tenant is derived FROM
        the channel (the channel_id is globally unique). No tenant guard
        is applied — the caller is the pipeline which has not yet
        established tenant identity.

        Returns ``None`` if the channel does not exist.
        """
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id must be a non-empty string")

        # Read-cutover: serve from Postgres when enabled. Tenant-agnostic
        # get-by-id (the webhook path derives the tenant FROM the channel).
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_get_any,
        )
        pg = await read_hybrid_get_any("intake_channel", channel_id)
        if pg is not _NOT_CUT_OVER:
            return _safe_channel_load(pg) if pg is not None else None

        query = {
            "query": {"term": {"channel_id": channel_id}},
            "size": 1,
        }

        try:
            resp = await self._es.search_documents(
                self._channels_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntakeChannelRepository.get_by_channel_id: search failed "
                "for channel=%s: %s",
                channel_id,
                exc,
            )
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        return _safe_channel_load(sources[0])

    async def get_dispatcher_channel(
        self, tenant_id: str
    ) -> Optional[IntakeChannel]:
        """Look up the tenant's single dispatcher channel.

        Returns the first channel with ``channel_type="dispatcher"`` for
        the given tenant, or ``None`` if none exists.
        """
        self._require_tenant(tenant_id)

        # Read-cutover: serve from Postgres when enabled.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_find_one,
        )
        pg = await read_hybrid_find_one(
            "intake_channel", tenant_id,
            term_filters={"channel_type": "dispatcher"},
        )
        if pg is not _NOT_CUT_OVER:
            return _safe_channel_load(pg) if pg is not None else None

        query = inject_tenant_filter(
            {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"channel_type": "dispatcher"}},
                        ]
                    }
                }
            },
            tenant_id,
        )
        query["size"] = 1

        try:
            resp = await self._es.search_documents(
                self._channels_index, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntakeChannelRepository.get_dispatcher_channel: search "
                "failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return None

        sources = _extract_sources(resp)
        if not sources:
            return None

        source = sources[0]
        # Defense-in-depth: re-validate tenant ownership
        if source.get("tenant_id") != tenant_id:
            return None

        return _safe_channel_load(source)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def ensure_dispatcher_channel(
        self, tenant_id: str
    ) -> IntakeChannel:
        """Return the tenant's dispatcher channel, creating it if absent.

        The dispatcher keyboard intake path (``ingest_dispatcher``) resolves a
        per-tenant ``channel_type="dispatcher"`` channel. Unlike webhook/EDI
        channels — which an admin registers explicitly — the dispatcher channel
        is an implicit, always-present surface (every tenant's operators can key
        in orders). Historically this was described as "seeded at first use" but
        no code created it, so a tenant that never ran the seeder hit a hard 404
        on every dispatcher order. This method makes "first use" real: it looks
        the channel up and, only when missing, provisions a stable default
        (``{tenant}-dispatcher``) idempotently.

        Concurrency: if two requests race to create it, the second create may
        land a duplicate; we tolerate that by re-reading and returning whatever
        the lookup yields.
        """
        self._require_tenant(tenant_id)

        existing = await self.get_dispatcher_channel(tenant_id)
        if existing is not None:
            return existing

        # Provision a stable default dispatcher channel for this tenant.
        channel_id = f"{tenant_id}-dispatcher"
        # channel_id must match ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ — normalise.
        channel_id = re.sub(r"[^a-z0-9-]", "-", channel_id.lower()).strip("-")
        if len(channel_id) < 3:
            channel_id = f"{channel_id}-dispatcher"
        channel_id = channel_id[:64].rstrip("-")

        try:
            channel, _secret = await self.create(
                tenant_id=tenant_id,
                channel_id=channel_id,
                channel_type="dispatcher",
                display_name="Dispatcher Keyboard",
                supported_schema_versions=["1.0"],
                enabled=True,
            )
            logger.info(
                "IntakeChannelRepository: auto-provisioned dispatcher channel "
                "%s for tenant=%s",
                channel_id,
                tenant_id,
            )
            return channel
        except Exception as exc:
            # Likely a race (another request created it) or a transient write
            # error — re-read and return if it now exists.
            logger.warning(
                "ensure_dispatcher_channel: create failed for tenant=%s (%s); "
                "re-reading",
                tenant_id,
                exc,
            )
            existing = await self.get_dispatcher_channel(tenant_id)
            if existing is not None:
                return existing
            raise

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        """Validate that tenant_id is a non-empty string."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "IntakeChannelRepository",
    "IntakeChannelCrossTenantAccessError",
]
