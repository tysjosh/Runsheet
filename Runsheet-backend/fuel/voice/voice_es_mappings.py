"""
Elasticsearch index mappings for the Dinee voice integration (Surface B).

Defines strict mappings for:
- ``voice_api_keys`` (Surface B authentication reverse-lookup index)

The ``voice_api_keys`` index is the O(1) reverse lookup that
``fuel/voice/voice_auth.py::get_voice_tenant`` consults to resolve a
presented ``Authorization: Bearer <apiKey>`` to a tenant. It stores only a
**salted hash** of the API key (``api_key_sha256``) — never the plaintext —
alongside the ``tenant_id``/``channel_id`` binding. The plaintext key lives
in the :class:`TenantCredentialsVault` under ``voice_api_key:{channel_id}``
for rotation/audit (see ``voice_auth.py``).

Every index sets ``"dynamic": "strict"`` so callers cannot smuggle arbitrary
fields, mirroring ``fuel/services/order_es_mappings.py``. Every date field
MUST be written via ``services.time_utils.utcnow()``.

Requirements: 10.1, 10.2, 10.4, 11.4
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index names
# ---------------------------------------------------------------------------

VOICE_API_KEYS_INDEX = "voice_api_keys"

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

VOICE_API_KEYS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            # Salted SHA-256 hash of the plaintext API key. This is the
            # lookup key — the plaintext is never stored here.
            "api_key_sha256": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "channel_id": {"type": "keyword"},
            "disabled": {"type": "boolean"},
            "created_at": {"type": "date"},
        },
    },
}

# ---------------------------------------------------------------------------
# Consolidated index registry
# ---------------------------------------------------------------------------

VOICE_INDEX_MAPPINGS: dict[str, dict] = {
    VOICE_API_KEYS_INDEX: VOICE_API_KEYS_MAPPING,
}


# ---------------------------------------------------------------------------
# Bootstrap helper
# ---------------------------------------------------------------------------


def setup_voice_indices(es_service) -> None:
    """Create Dinee voice ES indices if they don't already exist.

    Follows the same pattern as ``setup_order_intake_indices`` in
    ``fuel/services/order_es_mappings.py``. On Elasticsearch Serverless
    deployments, shard/replica settings are stripped before creation via
    ``ElasticsearchService.strip_serverless_incompatible_settings``.

    Every date field in these indices MUST be written via
    ``services.time_utils.utcnow()`` at the application layer.

    Args:
        es_service: An ElasticsearchService instance with ``.client`` and
            ``.is_serverless`` attributes.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    # Skip indices that have been retired so startup does not silently
    # recreate a dropped index.
    try:
        from config.settings import get_settings
        retired = set(get_settings().retired_es_indices or [])
    except Exception:  # noqa: BLE001
        retired = set()

    for index_name, mapping in VOICE_INDEX_MAPPINGS.items():
        if index_name in retired:
            logger.info("Skipping retired voice index: %s", index_name)
            continue
        try:
            if not es_client.indices.exists(index=index_name):
                body = mapping
                if is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(body)
                es_client.indices.create(index=index_name, body=body)
                logger.info("Created voice index: %s", index_name)
            else:
                logger.info("Voice index already exists: %s", index_name)
        except Exception as e:
            logger.error("Failed to create voice index %s: %s", index_name, e)
