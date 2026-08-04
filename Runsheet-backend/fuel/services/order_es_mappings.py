"""
Elasticsearch index mappings for the Order Intake Pipeline.

Defines strict mappings for:
- fuel_orders_current (§1)
- fuel_order_events (§2)
- drivers_current (§3)
- intake_channels (§4)
- pending_legacy_mirrors (§13 — dual-write retry queue)

Every index sets ``"dynamic": "strict"`` so adapters cannot smuggle
arbitrary fields. The ``intake_metadata`` sub-mapping on
``fuel_orders_current`` is also strict.

Every date field MUST be written via ``services.time_utils.utcnow()``.

Validates: Requirements 1.1, 1.1.3, 10.3
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index names
# ---------------------------------------------------------------------------

FUEL_ORDERS_CURRENT_INDEX = "fuel_orders_current"
FUEL_ORDER_EVENTS_INDEX = "fuel_order_events"
DRIVERS_CURRENT_INDEX = "drivers_current"
INTAKE_CHANNELS_INDEX = "intake_channels"
PENDING_LEGACY_MIRRORS_INDEX = "pending_legacy_mirrors"
DRIVER_REPORTS_INDEX = "driver_reports"

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

FUEL_ORDERS_CURRENT_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "order_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "customer_id": {"type": "keyword"},
            "customer_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "customer_phone": {"type": "keyword"},
            "customer_email": {"type": "keyword"},
            "ship_to_address": {"type": "text"},
            "ship_to_lat": {"type": "double"},
            "ship_to_lon": {"type": "double"},
            "ship_to_geo": {"type": "geo_point"},
            "customer_tank_id": {"type": "keyword"},
            "product_code": {"type": "keyword"},
            "gallons_requested": {"type": "double"},
            "unit_price_micros": {"type": "long"},
            "unit_price_cents": {"type": "long"},
            "subtotal_cents": {"type": "long"},
            "tax_cents": {"type": "long"},
            "total_cents": {"type": "long"},
            "fill_to_full": {"type": "boolean"},
            "call_type": {"type": "keyword"},
            "delivery_window_start": {"type": "date"},
            "delivery_window_end": {"type": "date"},
            "hold_reason": {"type": "keyword"},
            "po_number": {"type": "keyword"},
            "special_instructions": {"type": "text"},
            "intake_channel": {"type": "keyword"},
            "intake_channel_id": {"type": "keyword"},
            "intake_metadata": {
                "dynamic": "strict",
                "properties": {
                    "call_id": {"type": "keyword"},
                    "recording_url": {"type": "keyword"},
                    "transcript": {"type": "text"},
                    "agent_confidence": {"type": "float"},
                    "dispatcher_user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "portal_session_id": {"type": "keyword"},
                    "user_agent": {"type": "keyword"},
                    "import_batch_id": {"type": "keyword"},
                    "csv_row_number": {"type": "integer"},
                    "source_system": {"type": "keyword"},
                    "source_record_id": {"type": "keyword"},
                    "source_updated_at": {"type": "date"},
                    "edi_interchange_id": {"type": "keyword"},
                    "partner_ref": {"type": "keyword"},
                    "legacy_shipment_id": {"type": "keyword"},
                },
            },
            "status": {"type": "keyword"},
            "assigned_driver_id": {"type": "keyword"},
            "assigned_asset_id": {"type": "keyword"},
            "assigned_run_id": {"type": "keyword"},
            # POD one-time code, provisioned by PODOTPService when the order
            # transitions to ``dispatched`` in a tenant with otp_required
            # (driver-mobile-app R5.25). The mapping is ``dynamic: strict``, so
            # the write fails outright until both fields are declared.
            # ``pod_otp`` is ``"index": False``: it is only ever read back
            # from the fetched document to compare against the submitted code,
            # never searched, and an unindexed keyword keeps it out of the
            # inverted index (R5.26).
            "pod_otp": {"type": "keyword", "index": False},
            "pod_otp_generated_at": {"type": "date"},
            # Written by PODSubmissionService when a POD records a refusal and
            # the order transitions to ``failed`` (driver-mobile-app R4.6), so
            # the order carries its own failure reason without a POD join.
            "refusal_reason_code": {"type": "keyword"},
            # Immutable POD snapshot written immediately before the delivered
            # transition. Commerce and outbound ERP integrations consume this
            # object from the order.delivered event without a second index read.
            "delivery_result": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "pod_id": {"type": "keyword"},
                    "actual_gallons": {"type": "double"},
                    "actual_gallons_source": {"type": "keyword"},
                    "delivered_at": {"type": "date"},
                    "recipient_name": {"type": "keyword"},
                    "driver_id": {"type": "keyword"},
                    "signature_ref": {"type": "keyword"},
                    "photo_refs": {"type": "keyword"},
                    "meter_ticket_ref": {"type": "keyword"},
                    "bol_id": {"type": "keyword"},
                    "bol_ref": {"type": "keyword"},
                    "pod_hash": {"type": "keyword"},
                    "geotag": {"type": "geo_point"},
                    "otp_verified": {"type": "boolean"},
                    "location_mismatch": {"type": "boolean"},
                    "source_system": {"type": "keyword"},
                    "source_record_id": {"type": "keyword"},
                },
            },
            "legacy_origin_snapshot": {"type": "text"},
            "source_schema_version": {"type": "keyword"},
            "trace_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "last_event_timestamp": {"type": "date"},
        },
    },
}

FUEL_ORDER_EVENTS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id": {"type": "keyword"},
            "order_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "event_type": {"type": "keyword"},
            # ``event_payload`` is a free-form, event-type-specific bag (the
            # order_placed event carries intake_channel/dispatcher_user_id, a
            # status_changed event carries old/new status, etc.). Mapping it as
            # a strict ``nested`` object rejected those dynamic keys with
            # ``strict_dynamic_mapping_exception`` and 503'd EVERY order intake.
            # Store it as a non-indexed object (same pattern as job_events /
            # account_events / invoice_events) — persisted verbatim, never
            # dynamically mapped. We don't query into event_payload here.
            "event_payload": {"type": "object", "enabled": False},
            "event_timestamp": {"type": "date"},
            "ingested_at": {"type": "date"},
            "source_schema_version": {"type": "keyword"},
            "trace_id": {"type": "keyword"},
            "location": {"type": "geo_point"},
            "intake_metadata": {
                "dynamic": "strict",
                "properties": {
                    "call_id": {"type": "keyword"},
                    "recording_url": {"type": "keyword"},
                    "transcript": {"type": "text"},
                    "agent_confidence": {"type": "float"},
                    "dispatcher_user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "portal_session_id": {"type": "keyword"},
                    "user_agent": {"type": "keyword"},
                    "import_batch_id": {"type": "keyword"},
                    "csv_row_number": {"type": "integer"},
                    "edi_interchange_id": {"type": "keyword"},
                    "partner_ref": {"type": "keyword"},
                    "legacy_shipment_id": {"type": "keyword"},
                },
            },
        },
    },
}

DRIVERS_CURRENT_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "driver_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "driver_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "phone": {"type": "keyword"},
            # Current duty-status value. Stays a single keyword carrying one of
            # the four DriverStatus values so every existing reader of
            # drivers_current.status keeps working unchanged.
            "status": {"type": "keyword"},
            # Duty-status projection bookkeeping. Both nullable: absent means
            # the record predates the duty-status event log. duty_status_event_id
            # is the id of the duty_status_events document this value projects
            # and duty_status_updated_at is that event's server_received_at.
            "duty_status_event_id": {"type": "keyword"},
            "duty_status_updated_at": {"type": "date"},
            "availability": {"type": "keyword"},
            "assigned_truck_id": {"type": "keyword"},
            "cdl_class": {"type": "keyword"},
            "hazmat_endorsement": {"type": "boolean"},
            "medical_card_expiry": {"type": "date"},
            "current_location": {"type": "geo_point"},
            "last_seen": {"type": "date"},
            "active_order_count": {"type": "integer"},
            "completed_today": {"type": "integer"},
            "last_event_timestamp": {"type": "date"},
            "source_schema_version": {"type": "keyword"},
            "trace_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "intake_metadata": {
                "dynamic": "strict",
                "properties": {
                    "call_id": {"type": "keyword"},
                    "recording_url": {"type": "keyword"},
                    "transcript": {"type": "text"},
                    "agent_confidence": {"type": "float"},
                    "dispatcher_user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "portal_session_id": {"type": "keyword"},
                    "user_agent": {"type": "keyword"},
                    "import_batch_id": {"type": "keyword"},
                    "csv_row_number": {"type": "integer"},
                    "edi_interchange_id": {"type": "keyword"},
                    "partner_ref": {"type": "keyword"},
                    "legacy_shipment_id": {"type": "keyword"},
                },
            },
        },
    },
}

INTAKE_CHANNELS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "channel_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "channel_type": {"type": "keyword"},
            "display_name": {"type": "text"},
            "hmac_secret_ref": {"type": "keyword"},
            "supported_schema_versions": {"type": "keyword"},
            "rate_limit_per_minute": {"type": "integer"},
            "secret_version": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "intake_metadata": {
                "dynamic": "strict",
                "properties": {
                    "call_id": {"type": "keyword"},
                    "recording_url": {"type": "keyword"},
                    "transcript": {"type": "text"},
                    "agent_confidence": {"type": "float"},
                    "dispatcher_user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "portal_session_id": {"type": "keyword"},
                    "user_agent": {"type": "keyword"},
                    "import_batch_id": {"type": "keyword"},
                    "csv_row_number": {"type": "integer"},
                    "edi_interchange_id": {"type": "keyword"},
                    "partner_ref": {"type": "keyword"},
                    "legacy_shipment_id": {"type": "keyword"},
                },
            },
        },
    },
}

PENDING_LEGACY_MIRRORS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "entry_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "entity_type": {"type": "keyword"},
            "entity_id": {"type": "keyword"},
            "failure_reason": {"type": "text"},
            "retry_count": {"type": "integer"},
            "next_retry_at": {"type": "date"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "intake_metadata": {
                "dynamic": "strict",
                "properties": {
                    "call_id": {"type": "keyword"},
                    "recording_url": {"type": "keyword"},
                    "transcript": {"type": "text"},
                    "agent_confidence": {"type": "float"},
                    "dispatcher_user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "portal_session_id": {"type": "keyword"},
                    "user_agent": {"type": "keyword"},
                    "import_batch_id": {"type": "keyword"},
                    "csv_row_number": {"type": "integer"},
                    "edi_interchange_id": {"type": "keyword"},
                    "partner_ref": {"type": "keyword"},
                    "legacy_shipment_id": {"type": "keyword"},
                },
            },
        },
    },
}

DRIVER_REPORTS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "report_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "driver_id": {"type": "keyword"},
            "assignment_id": {"type": "keyword"},
            "kind": {"type": "keyword"},
            "detail": {"type": "text"},
            "eta_minutes": {"type": "integer"},
            "created_at": {"type": "date"},
        },
    },
}

# ---------------------------------------------------------------------------
# Consolidated index registry
# ---------------------------------------------------------------------------

ORDER_INTAKE_INDEX_MAPPINGS: dict[str, dict] = {
    FUEL_ORDERS_CURRENT_INDEX: FUEL_ORDERS_CURRENT_MAPPING,
    FUEL_ORDER_EVENTS_INDEX: FUEL_ORDER_EVENTS_MAPPING,
    DRIVERS_CURRENT_INDEX: DRIVERS_CURRENT_MAPPING,
    INTAKE_CHANNELS_INDEX: INTAKE_CHANNELS_MAPPING,
    PENDING_LEGACY_MIRRORS_INDEX: PENDING_LEGACY_MIRRORS_MAPPING,
    DRIVER_REPORTS_INDEX: DRIVER_REPORTS_MAPPING,
}


# ---------------------------------------------------------------------------
# Bootstrap helper
# ---------------------------------------------------------------------------


def setup_order_intake_indices(es_service) -> None:
    """Create Order Intake Pipeline ES indices if they don't already exist.

    Follows the same pattern as ``setup_fuel_ops_indices`` in
    ``fuel_ops_es_mappings.py``. On Elasticsearch Serverless deployments,
    shard/replica settings are stripped before creation via
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

    # Skip indices that have been retired (migrated to Postgres + dropped in
    # Phase 6) so startup does not silently recreate a dropped index.
    try:
        from config.settings import get_settings
        retired = set(get_settings().retired_es_indices or [])
    except Exception:  # noqa: BLE001
        retired = set()

    for index_name, mapping in ORDER_INTAKE_INDEX_MAPPINGS.items():
        if index_name in retired:
            logger.info("Skipping retired order-intake index: %s", index_name)
            continue
        try:
            if not es_client.indices.exists(index=index_name):
                body = mapping
                if is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(body)
                es_client.indices.create(index=index_name, body=body)
                logger.info("Created order-intake index: %s", index_name)
            else:
                logger.info("Order-intake index already exists: %s", index_name)
        except Exception as e:
            logger.error("Failed to create order-intake index %s: %s", index_name, e)
