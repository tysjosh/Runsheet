"""Document-store mappings for the Dispatch Board (dispatch-board design K2.1, K2.2).

Two indices:

* ``dispatch_board_drafts`` — one draft per tenant and service day, written
  only through ``atomic_update``. ``order_ids`` is a flat keyword list kept
  equal to the keys of ``order_index`` so the order listener and the cross-day
  check can find a draft by order with one ``term`` query (K10.4, K5.1).
* ``dispatch_board_commands`` — the append-only command log, written only with
  ``create_document`` (K2.2).

The Postgres document store keeps these documents in ``jsonb`` and enforces no
mapping; the mappings are the reference for the exact keys and feed the field
policy (``persistence/document_field_policy.py`` ``_REGISTRIES``). Fields that
hold whole responses or lane snapshots are ``enabled: false`` so they are never
filterable or sortable: ``applied_commands`` on drafts, and ``response``,
``content_before`` and ``content_after`` on commands.
"""
from __future__ import annotations

DISPATCH_BOARD_DRAFTS_INDEX = "dispatch_board_drafts"
DISPATCH_BOARD_COMMANDS_INDEX = "dispatch_board_commands"

DISPATCH_BOARD_DRAFTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":     {"type": "keyword"},
            "service_date":  {"type": "date"},
            "timezone":      {"type": "keyword"},
            "draft_version": {"type": "integer"},
            # Keyed by truck_id / order_id / warning_id / plan_id /
            # client_request_id, so the shapes below are maps, not schemas.
            "lanes":                 {"type": "object", "dynamic": True},
            "order_index":           {"type": "object", "dynamic": True},
            "order_ids":             {"type": "keyword"},
            "acknowledged":          {"type": "object", "dynamic": True},
            "applied_commands":      {"type": "object", "enabled": False},
            "dismissed_suggestions": {"type": "object", "dynamic": True},
            "publishes":             {"type": "object", "dynamic": True},
            "created_at":    {"type": "date"},
            "updated_at":    {"type": "date"},
        },
    },
}

DISPATCH_BOARD_COMMANDS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":      {"type": "keyword"},
            "service_date":   {"type": "date"},
            "command_id":     {"type": "keyword"},
            "type":           {"type": "keyword"},
            "payload":        {"type": "object", "dynamic": True},
            "payload_hash":   {"type": "keyword"},
            "actor_user_id":  {"type": "keyword"},
            "actor_name":     {"type": "keyword"},
            "input_modality": {"type": "keyword"},
            "result":         {"type": "keyword"},
            "response":       {"type": "object", "enabled": False},
            "lanes": {
                "type": "object",
                "properties": {
                    "truck_id":       {"type": "keyword"},
                    "version_before": {"type": "integer"},
                    "version_after":  {"type": "integer"},
                    "hash_before":    {"type": "keyword"},
                    "hash_after":     {"type": "keyword"},
                },
            },
            "content_before": {"type": "object", "enabled": False},
            "content_after":  {"type": "object", "enabled": False},
            "checks_summary": {"type": "object", "dynamic": True},
            "overrides":      {"type": "object", "dynamic": True},
            "created_at":     {"type": "date"},
            "updated_at":     {"type": "date"},
        },
    },
}

#: Registered in ``persistence/document_field_policy.py`` ``_REGISTRIES``.
DISPATCH_BOARD_INDEX_MAPPINGS = {
    DISPATCH_BOARD_DRAFTS_INDEX: DISPATCH_BOARD_DRAFTS_MAPPING,
    DISPATCH_BOARD_COMMANDS_INDEX: DISPATCH_BOARD_COMMANDS_MAPPING,
}
