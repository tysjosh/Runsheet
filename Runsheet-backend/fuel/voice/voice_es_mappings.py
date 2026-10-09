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
            # ``ElasticsearchService.index_document`` auto-stamps ``updated_at``
            # on every write (see TIMESTAMP_SKIP_INDICES for the exclusions).
            # This index is NOT skipped, so the strict mapping MUST allow the
            # field or writes fail with strict_dynamic_mapping_exception.
            "updated_at": {"type": "date"},
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


