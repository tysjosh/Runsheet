"""Elasticsearch index mappings for Fuel Compliance Backbone indices.

Defines strict mappings for the 11 new indices introduced by the
fuel-compliance-backbone spec covering tax jurisdictions, customer tax
exemptions, sell-side price-protection contracts, driver qualification files,
vehicle/tanker certifications, meter registry + per-meter audit trail,
inbound terminal BOLs, sales pricing rules, IFTA per-jurisdiction mileage,
and K-factor adjustment history.

All mappings use ``dynamic: strict`` to reject unexpected fields and every
index carries ``tenant_id``, ``created_at``, and ``updated_at`` fields so
tenant isolation and temporal auditing can be enforced at the query layer.

Follows the same tenant-prefixed / shared-settings strategy as
``fuel/services/fuel_ops_es_mappings.py`` and
``commerce/services/commerce_es_mappings.py``.

Validates: Requirements 1.5, 1.6, 1.7, 1.8, 3.1, 3.2, 5.1, 7.1, 7.2, 8.2,
8.3, 9.5, 9.6, 10.1, 11.1, 13.1
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index name constants
# ---------------------------------------------------------------------------

# Tax Engine (Req 1)
TAX_JURISDICTIONS_INDEX = "tax_jurisdictions"
TAX_EXEMPTIONS_INDEX = "tax_exemptions"

# Price Protection (Req 3)
PRICE_PROTECTION_CONTRACTS_INDEX = "price_protection_contracts"

# Driver Qualification (Req 5)
DRIVERS_INDEX = "drivers"

# Asset Certification (Req 13)
ASSET_CERTIFICATIONS_INDEX = "asset_certifications"

# Meter Audit (Req 8)
METER_REGISTRY_INDEX = "meter_registry"
METER_AUDIT_TRAIL_INDEX = "meter_audit_trail"

# Terminal BOL Ingestion (Req 10)
TERMINAL_BOLS_INDEX = "terminal_bols"

# Sales Pricing Engine (Req 11)
# Distinct from commerce.commerce_es_mappings.PRICING_RULES_CURRENT_INDEX
# ("pricing_rules_current") which belongs to the PriceBook resolver. The
# compliance "pricing_rules" index stores the richer sell-side strategy rules
# (posted_price / rack_plus_margin / tiered_volume / cost_plus) evaluated
# before the PriceBook fan-out.
PRICING_RULES_INDEX = "pricing_rules"

# IFTA Reporter (Req 7)
IFTA_MILEAGE_INDEX = "ifta_mileage"

# K-Factor Calibration (Req 9)
KFACTOR_HISTORY_INDEX = "kfactor_history"

# Dyed Diesel Audit Log (Req 6.7)
DYED_DIESEL_AUDIT_LOG_INDEX = "dyed_diesel_audit_log"


# ---------------------------------------------------------------------------
# Shared settings
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 1,
}


# ---------------------------------------------------------------------------
# Tax Engine mappings (Req 1)
# ---------------------------------------------------------------------------

TAX_JURISDICTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "jurisdiction_id":        {"type": "keyword"},
            "tenant_id":              {"type": "keyword"},
            # FIPS code: 2-digit state, 5-digit county, 7-digit city.
            "fips_code":              {"type": "keyword"},
            "jurisdiction_level":     {"type": "keyword"},
            "jurisdiction_name":      {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            # excise | ust | spcc | environmental
            "tax_type":               {"type": "keyword"},
            "product_codes":          {"type": "keyword"},
            # Integer cents for precision — no float drift in tax totals.
            "rate_cents_per_gallon":  {"type": "long"},
            "effective_date":         {"type": "date"},
            "expiry_date":            {"type": "date"},
            "source":                 {"type": "keyword"},
            "updated_at":             {"type": "date"},
            "created_at":             {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

TAX_EXEMPTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "exemption_id":       {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "customer_id":        {"type": "keyword"},
            "account_id":         {"type": "keyword"},
            # dyed_diesel | off_road | farm | 637M | government | resale
            "exemption_type":     {"type": "keyword"},
            "certificate_number": {"type": "keyword"},
            # IRS 637 letter suffix (e.g. "M" for dyed-diesel blenders).
            "letter_suffix":      {"type": "keyword"},
            "issuing_authority":  {"type": "keyword"},
            "product_codes":      {"type": "keyword"},
            "jurisdiction_fips":  {"type": "keyword"},
            "issued_date":        {"type": "date"},
            "expiry_date":        {"type": "date"},
            # valid | expired | revoked
            "status":             {"type": "keyword"},
            "document_ref":       {"type": "keyword"},
            "updated_at":         {"type": "date"},
            "created_at":         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Price Protection mapping (Req 3)
# ---------------------------------------------------------------------------

PRICE_PROTECTION_CONTRACTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "contract_id":          {"type": "keyword"},
            "tenant_id":            {"type": "keyword"},
            "customer_id":          {"type": "keyword"},
            "account_id":           {"type": "keyword"},
            "product_code":         {"type": "keyword"},
            # fixed_price | cap_price | collar
            "contract_type":        {"type": "keyword"},
            "start_date":           {"type": "date"},
            "end_date":             {"type": "date"},
            "contracted_gallons":   {"type": "double"},
            "remaining_gallons":    {"type": "double"},
            "price_cap_cents":      {"type": "long"},
            "price_floor_cents":    {"type": "long"},
            "fixed_price_cents":    {"type": "long"},
            # active | exhausted | expired | cancelled
            "status":               {"type": "keyword"},
            # Optimistic-concurrency counter used by decrement_gallons (Task 4.3).
            "version":              {"type": "long"},
            "notes":                {"type": "text"},
            "updated_at":           {"type": "date"},
            "created_at":           {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Driver Qualification mapping (Req 5)
# ---------------------------------------------------------------------------

DRIVERS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "driver_id":                          {"type": "keyword"},
            "tenant_id":                          {"type": "keyword"},
            "full_name":                          {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "cdl_number":                         {"type": "keyword"},
            "cdl_state":                          {"type": "keyword"},
            # A | B | C
            "cdl_class":                          {"type": "keyword"},
            "cdl_expiry_date":                    {"type": "date"},
            "medical_card_expiry_date":           {"type": "date"},
            "hazmat_endorsement_expiry_date":     {"type": "date"},
            "tanker_endorsement_expiry_date":     {"type": "date"},
            "last_drug_test_date":                {"type": "date"},
            "last_mvr_date":                      {"type": "date"},
            # active | suspended | expired
            "status":                             {"type": "keyword"},
            "suspension_reason":                  {"type": "keyword"},
            "external_refs":                      {"type": "object"},
            "updated_at":                         {"type": "date"},
            "created_at":                         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Asset Certification mapping (Req 13)
# ---------------------------------------------------------------------------

ASSET_CERTIFICATIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "cert_id":              {"type": "keyword"},
            "tenant_id":            {"type": "keyword"},
            "asset_id":             {"type": "keyword"},
            # V_test | K_test | I_test | P_test | UT_test | meter_seal | fire_extinguisher
            "certification_type":   {"type": "keyword"},
            "certification_date":   {"type": "date"},
            "expiry_date":          {"type": "date"},
            "inspector_name":       {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "certificate_number":   {"type": "keyword"},
            "issuing_authority":    {"type": "keyword"},
            # valid | expiring_soon | expired
            "status":               {"type": "keyword"},
            # Tracks the 3-year retest requirement for cargo tank certs
            # (Req 13.6). Independent of expiry_date so short-cycle
            # certifications (e.g. annual V-test) can still be flagged
            # against the 3-year retest horizon.
            "retest_due_date":      {"type": "date"},
            "document_ref":         {"type": "keyword"},
            "updated_at":           {"type": "date"},
            "created_at":           {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Meter Audit mappings (Req 8)
# ---------------------------------------------------------------------------

METER_REGISTRY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "meter_id":                        {"type": "keyword"},
            "tenant_id":                       {"type": "keyword"},
            "meter_number":                    {"type": "keyword"},
            "truck_id":                        {"type": "keyword"},
            "compartment_id":                  {"type": "keyword"},
            "calibration_certificate_number":  {"type": "keyword"},
            "calibration_date":                {"type": "date"},
            "calibration_expiry_date":         {"type": "date"},
            "weights_measures_authority":      {"type": "keyword"},
            # valid | expiring_soon | expired | retired
            "status":                          {"type": "keyword"},
            "manufacturer":                    {"type": "keyword"},
            "model":                           {"type": "keyword"},
            "serial_number":                   {"type": "keyword"},
            "updated_at":                      {"type": "date"},
            "created_at":                      {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

METER_AUDIT_TRAIL_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "audit_id":        {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "meter_id":        {"type": "keyword"},
            "meter_ticket_id": {"type": "keyword"},
            "invoice_id":      {"type": "keyword"},
            "delivery_id":     {"type": "keyword"},
            "gross_gallons":   {"type": "double"},
            "net_gallons":     {"type": "double"},
            "pod_gallons":     {"type": "double"},
            "variance_pct":    {"type": "float"},
            # meter_pod_variance | meter_calibration_expired | ...
            "variance_flags":  {"type": "keyword"},
            "event_type":      {"type": "keyword"},
            "actor_id":        {"type": "keyword"},
            "occurred_at":     {"type": "date"},
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Terminal BOL Ingestion mapping (Req 10)
# ---------------------------------------------------------------------------

TERMINAL_BOLS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "bol_id":             {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "load_number":        {"type": "keyword"},
            "product_code":       {"type": "keyword"},
            "gross_gallons":      {"type": "double"},
            "net_gallons":        {"type": "double"},
            "temperature_f":      {"type": "float"},
            "api_gravity":        {"type": "float"},
            "supplier_name":      {"type": "keyword"},
            "terminal_id":        {"type": "keyword"},
            "terminal_name":      {"type": "keyword"},
            "driver_id":          {"type": "keyword"},
            "truck_id":           {"type": "keyword"},
            "load_plan_id":       {"type": "keyword"},
            "compartment_id":     {"type": "keyword"},
            # edi | manual
            "source":             {"type": "keyword"},
            # x12_856 | pipe_delimited | pdf | image
            "source_format":      {"type": "keyword"},
            # Immutable raw attachment reference (S3 key) — Req 10.7.
            "raw_payload_ref":    {"type": "keyword"},
            # Signed-difference between terminal-reported and locally
            # computed net gallons (±0.1% tolerance — Req 10.4).
            "vcf_variance_pct":   {"type": "float"},
            # ingested | validated | discrepancy | rejected
            "status":             {"type": "keyword"},
            "rejection_reason":   {"type": "keyword"},
            "issued_at":          {"type": "date"},
            "ingested_at":        {"type": "date"},
            "updated_at":         {"type": "date"},
            "created_at":         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Sales Pricing Engine mapping (Req 11)
# ---------------------------------------------------------------------------

PRICING_RULES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "rule_id":                      {"type": "keyword"},
            "tenant_id":                    {"type": "keyword"},
            # Null = product-default rule (Req 11.2 resolution order).
            "customer_id":                  {"type": "keyword"},
            "account_id":                   {"type": "keyword"},
            "product_code":                 {"type": "keyword"},
            # posted_price | rack_plus_margin | tiered_volume | cost_plus
            "strategy":                     {"type": "keyword"},
            "posted_price_cents":           {"type": "long"},
            "margin_cents":                 {"type": "long"},
            "freight_rate_cents_per_mile":  {"type": "long"},
            "terminal_id":                  {"type": "keyword"},
            "tier_thresholds": {
                "type": "nested",
                "properties": {
                    "min_gallons":      {"type": "double"},
                    "max_gallons":      {"type": "double"},
                    "unit_price_cents": {"type": "long"},
                },
            },
            # Lower = higher priority (Req 11.2).
            "priority":                     {"type": "integer"},
            "effective_date":               {"type": "date"},
            "expiry_date":                  {"type": "date"},
            # active | inactive
            "status":                       {"type": "keyword"},
            "updated_at":                   {"type": "date"},
            "created_at":                   {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# IFTA Reporter mapping (Req 7)
# ---------------------------------------------------------------------------

IFTA_MILEAGE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "record_id":           {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "truck_id":            {"type": "keyword"},
            # Two-letter US state / CA province abbreviation.
            "jurisdiction":        {"type": "keyword"},
            "jurisdiction_fips":   {"type": "keyword"},
            # Calendar quarter identifier, e.g. "2026-Q1".
            "quarter":             {"type": "keyword"},
            "miles":               {"type": "double"},
            "taxable_miles":       {"type": "double"},
            "tax_paid_gallons":    {"type": "double"},
            "net_taxable_gallons": {"type": "double"},
            "segment_start_at":    {"type": "date"},
            "segment_end_at":      {"type": "date"},
            # geotab | fuel_card | manual_adjustment
            "source":              {"type": "keyword"},
            "adjustment_reason":   {"type": "text"},
            "adjusted_by":         {"type": "keyword"},
            "adjusted_at":         {"type": "date"},
            "updated_at":          {"type": "date"},
            "created_at":          {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# K-Factor Calibration mapping (Req 9)
# ---------------------------------------------------------------------------

KFACTOR_HISTORY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "adjustment_id":      {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "tank_id":            {"type": "keyword"},
            "customer_id":        {"type": "keyword"},
            "old_kfactor":        {"type": "double"},
            "new_kfactor":        {"type": "double"},
            "suggested_kfactor":  {"type": "double"},
            "operator_id":        {"type": "keyword"},
            "variance_percent":   {"type": "float"},
            "accumulated_hdd":    {"type": "float"},
            "actual_gallons":     {"type": "double"},
            "predicted_gallons":  {"type": "double"},
            "delivery_id":        {"type": "keyword"},
            # auto_suggested | operator_approved | rolled_back
            "action":             {"type": "keyword"},
            "notes":              {"type": "text"},
            "approved_at":        {"type": "date"},
            "updated_at":         {"type": "date"},
            "created_at":         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Dyed Diesel Audit Log mapping (Req 6.7)
# ---------------------------------------------------------------------------

DYED_DIESEL_AUDIT_LOG_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "audit_id":            {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "customer_id":         {"type": "keyword"},
            "certificate_id":      {"type": "keyword"},
            "certificate_expiry":  {"type": "date"},
            "gallons":             {"type": "double"},
            "invoice_id":          {"type": "keyword"},
            "product_code":        {"type": "keyword"},
            "timestamp":           {"type": "date"},
            "updated_at":          {"type": "date"},
            "created_at":          {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Index registry
# ---------------------------------------------------------------------------

COMPLIANCE_INDEX_MAPPINGS = {
    # Tax Engine (Req 1)
    TAX_JURISDICTIONS_INDEX:           TAX_JURISDICTIONS_MAPPING,
    TAX_EXEMPTIONS_INDEX:              TAX_EXEMPTIONS_MAPPING,
    # Price Protection (Req 3)
    PRICE_PROTECTION_CONTRACTS_INDEX:  PRICE_PROTECTION_CONTRACTS_MAPPING,
    # Driver Qualification (Req 5)
    DRIVERS_INDEX:                     DRIVERS_MAPPING,
    # Asset Certification (Req 13)
    ASSET_CERTIFICATIONS_INDEX:        ASSET_CERTIFICATIONS_MAPPING,
    # Meter Audit (Req 8)
    METER_REGISTRY_INDEX:              METER_REGISTRY_MAPPING,
    METER_AUDIT_TRAIL_INDEX:           METER_AUDIT_TRAIL_MAPPING,
    # Terminal BOL Ingestion (Req 10)
    TERMINAL_BOLS_INDEX:               TERMINAL_BOLS_MAPPING,
    # Sales Pricing Engine (Req 11)
    PRICING_RULES_INDEX:               PRICING_RULES_MAPPING,
    # IFTA Reporter (Req 7)
    IFTA_MILEAGE_INDEX:                IFTA_MILEAGE_MAPPING,
    # K-Factor Calibration (Req 9)
    KFACTOR_HISTORY_INDEX:             KFACTOR_HISTORY_MAPPING,
    # Dyed Diesel Audit Log (Req 6.7)
    DYED_DIESEL_AUDIT_LOG_INDEX:       DYED_DIESEL_AUDIT_LOG_MAPPING,
}


# ---------------------------------------------------------------------------
# Index setup function
# ---------------------------------------------------------------------------


def setup_compliance_indices(es_service) -> None:
    """Create Fuel Compliance Backbone ES indices if they don't already exist.

    Iterates over :data:`COMPLIANCE_INDEX_MAPPINGS` and creates each index
    idempotently — existing indices are skipped so the bootstrap hook can be
    invoked on every application startup without side effects. On
    Elasticsearch Serverless deployments, shard/replica settings are stripped
    before creation via
    :meth:`services.elasticsearch_service.ElasticsearchService.strip_serverless_incompatible_settings`.

    Follows the same pattern as ``setup_commerce_indices`` in
    ``commerce/services/commerce_es_mappings.py`` and
    ``setup_fuel_ops_indices`` in ``fuel/services/fuel_ops_es_mappings.py``.

    Args:
        es_service: An :class:`ElasticsearchService` instance exposing
            ``.client`` and ``.is_serverless`` attributes.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    for index_name, mapping in COMPLIANCE_INDEX_MAPPINGS.items():
        try:
            if not es_client.indices.exists(index=index_name):
                body = mapping
                if is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(body)
                es_client.indices.create(index=index_name, body=body)
                logger.info(f"Created compliance index: {index_name}")
            else:
                logger.info(f"Compliance index already exists: {index_name}")
        except Exception as e:
            logger.error(f"Failed to create compliance index {index_name}: {e}")
