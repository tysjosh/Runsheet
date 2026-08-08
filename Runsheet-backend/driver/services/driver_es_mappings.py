"""Elasticsearch index mappings for the Driver Communication module.

Defines index mappings for job_messages, proof_of_delivery, driver_presence,
driver_exceptions, and idempotency_keys indices, plus the driver-mobile-app
indices duty_status_events, driver_devices, driver_push_attempts,
vehicle_inspections, driver_breadcrumbs, and hos_gate_overrides. Each index
uses strict dynamic mapping to prevent unintended field additions
(Requirement 15.12).

Validates: Requirements 6.1, 8.1, 9.5, 7.1, 14.2, 13.11, 9.1, 9.10, 9.18,
8.3, 8.4, 10.3, 17.23, 15.12
"""

import logging

logger = logging.getLogger(__name__)

JOB_MESSAGES_INDEX = "job_messages"
PROOF_OF_DELIVERY_INDEX = "proof_of_delivery"
DRIVER_PRESENCE_INDEX = "driver_presence"
DRIVER_EXCEPTIONS_INDEX = "driver_exceptions"
IDEMPOTENCY_KEYS_INDEX = "idempotency_keys"

# Driver mobile app indices. The Phase 2 pair (driver_breadcrumbs,
# hos_gate_overrides) is declared in Phase 1 so no first write can
# auto-create it with ``dynamic: true`` (Requirement 15.12).
DUTY_STATUS_EVENTS_INDEX = "duty_status_events"
DRIVER_DEVICES_INDEX = "driver_devices"
DRIVER_PUSH_ATTEMPTS_INDEX = "driver_push_attempts"
VEHICLE_INSPECTIONS_INDEX = "vehicle_inspections"
DRIVER_BREADCRUMBS_INDEX = "driver_breadcrumbs"
HOS_GATE_OVERRIDES_INDEX = "hos_gate_overrides"

JOB_MESSAGES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "message_id":  {"type": "keyword"},
            "job_id":      {"type": "keyword"},
            # Order-keyed sibling thread key (Requirement 7.14) and the
            # canonical acting driver on the thread (Requirement 7.13). This
            # mapping is dynamic: strict, so the order-keyed writes cannot
            # land until both are declared (Requirement 15.12).
            "order_id":    {"type": "keyword"},
            "driver_id":   {"type": "keyword"},
            "sender_id":   {"type": "keyword"},
            "sender_role": {"type": "keyword"},
            "body":        {"type": "text"},
            "timestamp":   {"type": "date"},
            "tenant_id":   {"type": "keyword"},
            # Auto-stamped by ElasticsearchService.index_document on every
            # write; a dynamic: strict mapping that omits them rejects all
            # writes (Requirement 15.12).
            "created_at":  {"type": "date"},
            "updated_at":  {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

PROOF_OF_DELIVERY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "pod_id":             {"type": "keyword"},
            "job_id":             {"type": "keyword"},
            "order_id":           {"type": "keyword"},
            "recipient_name":     {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            # Tenant-prefixed file_refs (preferred) + legacy URL fields.
            "signature_ref":      {"type": "keyword"},
            "photo_refs":         {"type": "keyword"},
            "meter_ticket_ref":   {"type": "keyword"},
            "signature_url":      {"type": "keyword"},
            "photo_urls":         {"type": "keyword"},
            "delivered_gallons":          {"type": "double"},
            "delivered_gallons_source":   {"type": "keyword"},
            "delivered_at":       {"type": "date"},
            # OCR metadata (Capability 4, Requirement 4.2.4–4.2.6). Populated
            # when a ``meter_ticket_ref`` is supplied and the POD endpoint
            # invokes MeterTicketOCRService to extract gallons.
            "ocr_result_id":               {"type": "keyword"},
            "ocr_confidence":              {"type": "double"},
            "ocr_requires_manual_review":  {"type": "boolean"},
            "ocr_error":                   {"type": "keyword"},
            "geotag":             {"type": "geo_point"},
            "timestamp":          {"type": "date"},
            "otp_verified":       {"type": "boolean"},
            "location_mismatch":  {"type": "boolean"},
            "status":             {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            # Hash-chain fields (Capability 4, Requirement 4.5).
            "pod_hash":           {"type": "keyword"},
            "previous_pod_hash":  {"type": "keyword"},
            "chain_sequence":     {"type": "long"},
            "persisted_at":       {"type": "date"},
            # Customer validation + refusal fields already written by
            # submit_pod (driver/api/pod_endpoints.py) but never declared
            # against this dynamic: strict mapping (Requirement 15.12).
            "customer_id":                  {"type": "keyword"},
            "expected_customer_id":         {"type": "keyword"},
            "signature_customer_validated": {"type": "boolean"},
            "refused_delivery":             {"type": "boolean"},
            "refusal_reason_code":          {"type": "keyword"},
            "refusal_note":                 {"type": "text"},
            # Canonical acting driver (Requirement 5.16) and the
            # uncompensated status-transition bookkeeping written when a POD
            # persists but its order transition fails (Requirement 4.7).
            "driver_id":                    {"type": "keyword"},
            "pod_status_transition":        {"type": "keyword"},
            "pod_status_transition_error":  {"type": "keyword"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":                   {"type": "date"},
            "updated_at":                   {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

DRIVER_PRESENCE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "driver_id":     {"type": "keyword"},
            "tenant_id":     {"type": "keyword"},
            "status":        {"type": "keyword"},
            "last_seen":     {"type": "date"},
            "last_location": {"type": "geo_point"},
            "connected_at":  {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":    {"type": "date"},
            "updated_at":    {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

DRIVER_EXCEPTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "exception_id":   {"type": "keyword"},
            "job_id":         {"type": "keyword"},
            # Order-keyed sibling work key (Requirement 7.13) and the
            # canonical acting driver (Requirement 7.14). This mapping is
            # dynamic: strict, so the order-keyed writes cannot land until
            # both are declared (Requirement 15.12).
            "order_id":       {"type": "keyword"},
            "driver_id":      {"type": "keyword"},
            "exception_type": {"type": "keyword"},
            "severity":       {"type": "keyword"},
            "note":           {"type": "text"},
            "location":       {"type": "geo_point"},
            "media_refs":     {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "timestamp":      {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":     {"type": "date"},
            "updated_at":     {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

IDEMPOTENCY_KEYS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "idempotency_key": {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "response":        {"type": "object", "enabled": False},
            "created_at":      {"type": "date"},
            "expires_at":      {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "updated_at":      {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


# ---------------------------------------------------------------------------
# Driver mobile app indices — Phase 1
# ---------------------------------------------------------------------------

# Append-only, authoritative history of every duty-status transition.
# Document id: {tenant_id}:{driver_id}:{ulid} so ids sort by creation and
# never collide across tenants (Requirements 13.11-13.14).
# ``new_status`` is constrained to the four DriverStatus values in the
# Pydantic model, not in the mapping. There is deliberately no field
# recording a driver certification, an edit history, or an annotation
# (Requirement 13.22) — those are ELD concepts.
# Retention: 36 months from ``event_timestamp`` (Requirement 10.18).
DUTY_STATUS_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":           {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "driver_id":          {"type": "keyword"},
            # nullable — the first event for a driver has no previous status
            "previous_status":    {"type": "keyword"},
            # active | inactive | on_break | off_duty
            "new_status":         {"type": "keyword"},
            # client-asserted (Requirement 13.12)
            "event_timestamp":    {"type": "date"},
            # projection tiebreak (Requirement 13.15)
            "server_received_at": {"type": "date"},
            # driver_id | admin user_id | "system"
            "actor_id":           {"type": "keyword"},
            # driver | admin | system (Requirement 13.12)
            "source":             {"type": "keyword"},
            # nullable, admin-set transitions
            "reason":             {"type": "keyword"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":         {"type": "date"},
            "updated_at":         {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

# The Device_Registry (Requirements 9.1, 9.2, 9.18). Document id
# {tenant_id}:{driver_id}:{device_id}, so a re-registration for the same
# device replaces the record rather than creating a second one.
DRIVER_DEVICES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "device_registration_id": {"type": "keyword"},
            "tenant_id":     {"type": "keyword"},
            "driver_id":     {"type": "keyword"},
            "device_id":     {"type": "keyword"},
            # Stored verbatim, never parsed or format-validated here, so a
            # provider change needs no registry migration (Requirement 9.18).
            # "index": False keeps the token retrievable but never queryable
            # and out of any inverted index.
            "push_token":    {"type": "keyword", "index": False},
            # ios | android
            "platform":      {"type": "keyword"},
            "app_version":   {"type": "keyword"},
            "registered_at": {"type": "date"},
            "last_seen_at":  {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":    {"type": "date"},
            "updated_at":    {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

# Per-attempt push audit record (Requirement 9.10). No message body, no
# title, no customer identifier — the payload is excluded from the audit
# record for the same reason it is excluded from the push itself (R9.8).
DRIVER_PUSH_ATTEMPTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "attempt_id":          {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "driver_id":           {"type": "keyword"},
            "device_id":           {"type": "keyword"},
            "notification_type":   {"type": "keyword"},
            # sent | failed
            "outcome":             {"type": "keyword"},
            "provider_message_id": {"type": "keyword"},
            "failure_reason":      {"type": "keyword"},
            "attempt_number":      {"type": "integer"},
            "attempted_at":        {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":          {"type": "date"},
            "updated_at":          {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

# Vehicle inspection reports (Requirements 8.3, 8.4, 8.8, 8.9).
# Document id {tenant_id}:{inspection_id}.
# Retention: 15 months from ``inspection_timestamp``.
VEHICLE_INSPECTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "inspection_id":     {"type": "keyword"},
            "tenant_id":         {"type": "keyword"},
            "driver_id":         {"type": "keyword"},
            "asset_id":          {"type": "keyword"},
            # pre_trip | post_trip (Requirement 8.8)
            "inspection_type":   {"type": "keyword"},
            # miles, per Requirement 8.3
            "odometer_miles":    {"type": "double"},
            # client-asserted
            "inspection_timestamp": {"type": "date"},
            "server_received_at":   {"type": "date"},
            # Calendar day in the tenant's timezone, precomputed so the R8.7
            # "first transition in a calendar day" gate is one term filter
            # rather than a range plus a timezone calculation. YYYY-MM-DD.
            "inspection_local_date": {"type": "keyword"},
            "defects": {
                "type": "nested",
                "properties": {
                    # from a defined list (Requirement 8.4)
                    "component":   {"type": "keyword"},
                    # minor | out_of_service
                    "severity":    {"type": "keyword"},
                    "note":        {"type": "text"},
                    "photo_refs":  {"type": "keyword"},
                },
            },
            # Denormalized so the unconditional gate (R8.5, R8.6) is a term
            # filter rather than a nested query on every transition.
            "has_out_of_service_defect": {"type": "boolean"},
            # inspection_timestamp + 15 months
            "expires_at":        {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":        {"type": "date"},
            "updated_at":        {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


# ---------------------------------------------------------------------------
# Driver mobile app indices — Phase 2, declared in Phase 1
#
# Declared now so no first write auto-creates them with ``dynamic: true``.
# ---------------------------------------------------------------------------

# Breadcrumb samples (Requirements 10.1-10.3, 10.8). Document id
# {tenant_id}:{driver_id}:{sample_timestamp_epoch_ms}, which makes the
# (tenant_id, driver_id, sample_timestamp) uniqueness of R10.8 a property of
# the id: an index operation with op_type=create on a duplicate id fails, the
# existing sample is retained, and no duplicate is created.
# Retention: 90 days from ``sample_timestamp`` (Requirement 10.17).
DRIVER_BREADCRUMBS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "breadcrumb_id":     {"type": "keyword"},
            "tenant_id":         {"type": "keyword"},
            "driver_id":         {"type": "keyword"},
            "location":          {"type": "geo_point"},
            # client-asserted
            "sample_timestamp":  {"type": "date"},
            "server_received_at": {"type": "date"},
            "accuracy_meters":   {"type": "float"},
            # miles per hour, per Requirement 10.1
            "speed_mph":         {"type": "float"},
            "heading_degrees":   {"type": "float"},
            "batch_id":          {"type": "keyword"},
            # Auto-stamped by ElasticsearchService.index_document.
            "created_at":        {"type": "date"},
            "updated_at":        {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

# Administrator overrides of the HOS advisory gate (Requirement 17.23).
# Mirrors the storm_mode_overrides pattern: override_id minted server-side,
# actor_id derived from the verified session and never from the body,
# non-blank reason and an expiry required.
HOS_GATE_OVERRIDES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            # hgo_<uuid4hex>
            "override_id":  {"type": "keyword"},
            "tenant_id":    {"type": "keyword"},
            "driver_id":    {"type": "keyword"},
            # from session (Requirement 17.23)
            "actor_id":     {"type": "keyword"},
            # non-blank
            "reason":       {"type": "text"},
            "expires_at":   {"type": "date"},
            "created_at":   {"type": "date"},
            # Auto-stamped by ElasticsearchService.index_document.
            "updated_at":   {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


# ---------------------------------------------------------------------------
# Index registry
#
# Module-level registry matching ``ORDER_INTAKE_INDEX_MAPPINGS`` in
# ``fuel/services/order_es_mappings.py`` so callers other than
# ``setup_driver_indices`` (mapping validator, retention job, tests) can
# enumerate the driver indices and their mappings.
# ---------------------------------------------------------------------------

DRIVER_INDEX_MAPPINGS = {
    JOB_MESSAGES_INDEX: JOB_MESSAGES_MAPPING,
    PROOF_OF_DELIVERY_INDEX: PROOF_OF_DELIVERY_MAPPING,
    DRIVER_PRESENCE_INDEX: DRIVER_PRESENCE_MAPPING,
    DRIVER_EXCEPTIONS_INDEX: DRIVER_EXCEPTIONS_MAPPING,
    IDEMPOTENCY_KEYS_INDEX: IDEMPOTENCY_KEYS_MAPPING,
    DUTY_STATUS_EVENTS_INDEX: DUTY_STATUS_EVENTS_MAPPING,
    DRIVER_DEVICES_INDEX: DRIVER_DEVICES_MAPPING,
    DRIVER_PUSH_ATTEMPTS_INDEX: DRIVER_PUSH_ATTEMPTS_MAPPING,
    VEHICLE_INSPECTIONS_INDEX: VEHICLE_INSPECTIONS_MAPPING,
    DRIVER_BREADCRUMBS_INDEX: DRIVER_BREADCRUMBS_MAPPING,
    HOS_GATE_OVERRIDES_INDEX: HOS_GATE_OVERRIDES_MAPPING,
}


