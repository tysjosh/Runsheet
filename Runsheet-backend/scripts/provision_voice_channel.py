#!/usr/bin/env python3
"""
Provision (or reuse) a Dinee voice intake channel and mint its Surface B API key.

Closes the dinee-voice-integration gap: the admin REST surface
(``POST /api/integrations/intake-channels``) can now mint a Surface B
``voice_api_key`` when a ``channel_type="voice"`` channel is created, but there
was no standalone / break-glass path to obtain a key against a running data
plane (ES + the credentials vault) without a SuperTokens admin session. This CLI
is that path.

Given a tenant id and a channel id it:

  1. constructs the ElasticsearchService + TenantCredentialsVault +
     IntakeChannelRepository + VoiceApiKeyRepository exactly the way bootstrap
     does (LocalKMSClient off-AWS, real KMS in production),
  2. creates a ``channel_type="voice"`` :class:`IntakeChannel` for the tenant
     (or reuses the existing channel when one already exists under the id),
  3. mints a Surface B voice API key via
     :meth:`VoiceApiKeyRepository.provision`, and
  4. prints ``channel_id``, ``tenant_id``, ``hmac_secret`` (only when freshly
     created — the HMAC plaintext is never retrievable again), and
     ``voice_api_key`` to stdout.

    ⚠️  The ``hmac_secret`` and ``voice_api_key`` values are shown EXACTLY ONCE.
    They are never retrievable again — capture them now. They are printed to
    stdout only; keep them out of shared logs.

Usage:
    python -m scripts.provision_voice_channel <tenant_id> <channel_id>
    python -m scripts.provision_voice_channel acme-co voice-acme-01 \
        --display-name "Acme Voice Line" --schema-version 1.0

Prerequisites:
    * DATABASE / Elasticsearch reachable via the standard app configuration
      (the ``elasticsearch_service`` module-level singleton connects on use).
    * VOICE_API_KEY_SALT must be configured (settings.voice_api_key_salt) — the
      salted-hash reverse lookup cannot operate without it.
    * For KMS envelope encryption: production sets FUEL_OPS_KMS_KEY_ID; dev/CI
      transparently fall back to the process-local LocalKMSClient (no AWS).
    * No SuperTokens session is required — this runs directly against ES + vault.

Design reference: ``.kiro/specs/dinee-voice-integration/design.md``
§"API-key storage" / Surface B authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# Ensure the project root is importable when run as a standalone script.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _build_credentials_vault(es_service, settings):
    """Construct the TenantCredentialsVault exactly like bootstrap/agents.py.

    Uses a real KMS key when ``FUEL_OPS_KMS_KEY_ID`` is configured; otherwise —
    outside production — falls back to the process-local ``LocalKMSClient`` so
    envelope encryption works end-to-end off-AWS. Never uses LocalKMS in prod.
    """
    import os

    from services.credentials_vault import TenantCredentialsVault

    kms_key_id = os.environ.get("FUEL_OPS_KMS_KEY_ID")
    kms_client = None
    env = getattr(settings.environment, "value", settings.environment)
    if not kms_key_id and env != "production":
        from services.local_kms import LOCAL_KMS_DEFAULT_KEY_ID, LocalKMSClient

        kms_key_id = LOCAL_KMS_DEFAULT_KEY_ID
        kms_client = LocalKMSClient(key_id=kms_key_id)
        logger.info(
            "No FUEL_OPS_KMS_KEY_ID configured in %s; using LocalKMSClient "
            "for the credentials vault (dev/CI envelope encryption)",
            env,
        )

    return TenantCredentialsVault(
        es_service=es_service,
        kms_key_id=kms_key_id,
        kms_client=kms_client,
    )


async def _run(
    tenant_id: str,
    channel_id: str,
    *,
    display_name: str,
    schema_versions: List[str],
) -> int:
    """Create/reuse the voice channel and mint the Surface B API key."""
    from config.settings import get_settings
    from fuel.intake_channel_repository import IntakeChannelRepository
    from fuel.voice.voice_auth import VoiceApiKeyRepository
    from fuel.voice.voice_es_mappings import setup_voice_indices
    from services.elasticsearch_service import elasticsearch_service

    settings = get_settings()

    # The salt is mandatory — without it the salted-hash reverse lookup cannot
    # be built, so fail closed with a clear message rather than a stack trace.
    salt = getattr(settings, "voice_api_key_salt", "") or ""
    if not salt:
        logger.error(
            "voice_api_key_salt is empty — set VOICE_API_KEY_SALT before "
            "provisioning a voice channel (the Surface B reverse lookup needs "
            "it)."
        )
        return 1

    es_service = elasticsearch_service
    credentials_vault = _build_credentials_vault(es_service, settings)

    # Ensure the voice_api_keys index exists before the reverse-lookup write
    # (idempotent — a no-op if already present).
    try:
        setup_voice_indices(es_service)
    except Exception as exc:  # noqa: BLE001 — non-fatal, provision may still work
        logger.warning("Could not ensure voice ES indices: %s", exc)

    intake_channel_repo = IntakeChannelRepository(
        es_service=es_service,
        credentials_vault=credentials_vault,
    )
    voice_api_key_repo = VoiceApiKeyRepository(
        es_service=es_service,
        credentials_vault=credentials_vault,
        salt=salt,
    )

    # Create (or reuse) the voice channel. If a channel already exists under
    # this id we reuse it — the HMAC plaintext is only available at creation, so
    # it cannot be reprinted for a pre-existing channel.
    hmac_secret: Optional[str] = None
    existing = await intake_channel_repo.get(tenant_id, channel_id)
    if existing is not None:
        if existing.channel_type != "voice":
            logger.error(
                "Channel '%s' already exists for tenant '%s' but its type is "
                "'%s', not 'voice' — refusing to mint a voice key for it.",
                channel_id,
                tenant_id,
                existing.channel_type,
            )
            return 1
        channel = existing
        logger.info(
            "Reusing existing voice channel tenant=%s channel=%s "
            "(HMAC secret not shown — only available at creation)",
            tenant_id,
            channel_id,
        )
    else:
        channel, hmac_secret = await intake_channel_repo.create(
            tenant_id=tenant_id,
            channel_id=channel_id,
            channel_type="voice",
            display_name=display_name,
            supported_schema_versions=schema_versions,
        )
        logger.info(
            "Created voice channel tenant=%s channel=%s", tenant_id, channel_id
        )

    # Mint the Surface B voice API key (plaintext returned exactly once).
    voice_api_key = await voice_api_key_repo.provision(
        channel.tenant_id, channel.channel_id
    )

    # ── Output — one-time plaintext values ─────────────────────────────
    print(f"\n{'=' * 72}")
    print("Voice channel provisioned")
    print(f"{'=' * 72}")
    print(f"channel_id:     {channel.channel_id}")
    print(f"tenant_id:      {channel.tenant_id}")
    if hmac_secret is not None:
        print(f"hmac_secret:    {hmac_secret}")
    else:
        print("hmac_secret:    (existing channel — not retrievable)")
    print(f"voice_api_key:  {voice_api_key}")
    print(
        "\n⚠️  hmac_secret and voice_api_key are shown ONCE and are NOT "
        "retrievable again. Capture them now and keep them out of shared logs."
    )
    print(f"{'=' * 72}\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provision (or reuse) a Dinee voice intake channel and mint its "
            "Surface B voice API key. Prints the one-time secrets to stdout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tenant_id", help="Owning tenant id")
    parser.add_argument(
        "channel_id",
        help="Voice channel id (3-64 chars; reused if it already exists)",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Human-readable channel name (default: 'Voice Channel <channel_id>')",
    )
    parser.add_argument(
        "--schema-version",
        dest="schema_versions",
        action="append",
        default=None,
        help="Supported schema version (repeatable; default: '1.0')",
    )

    args = parser.parse_args(argv)

    display_name = args.display_name or f"Voice Channel {args.channel_id}"
    schema_versions = args.schema_versions or ["1.0"]

    try:
        return asyncio.run(
            _run(
                args.tenant_id,
                args.channel_id,
                display_name=display_name,
                schema_versions=schema_versions,
            )
        )
    except Exception as exc:  # noqa: BLE001 — clean CLI failure
        logger.error("Aborted: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
