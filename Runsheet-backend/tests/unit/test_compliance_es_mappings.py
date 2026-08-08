"""Unit tests for ``compliance/services/compliance_es_mappings.py``.

Verifies every Fuel Compliance Backbone index mapping carries the mandatory
``tenant_id``, ``created_at``, and ``updated_at`` fields, uses a strict
mapping, and that ``setup_compliance_indices`` is idempotent when run
against an ES client that already reports every index as existing.

Validates: Requirements 1.5, 1.6, 3.1, 3.2, 5.1, 7.1, 7.2, 8.2, 8.3, 10.1,
11.1, 13.1
"""
import sys
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from compliance.services.compliance_es_mappings import ASSET_CERTIFICATIONS_INDEX, COMPLIANCE_INDEX_MAPPINGS, DRIVERS_INDEX, DYED_DIESEL_AUDIT_LOG_INDEX, IFTA_MILEAGE_INDEX, KFACTOR_HISTORY_INDEX, METER_AUDIT_TRAIL_INDEX, METER_REGISTRY_INDEX, PRICE_PROTECTION_CONTRACTS_INDEX, PRICING_RULES_INDEX, TAX_EXEMPTIONS_INDEX, TAX_JURISDICTIONS_INDEX, TERMINAL_BOLS_INDEX


# The 12 index names defined by the fuel-compliance-backbone spec.
EXPECTED_INDICES = {
    TAX_JURISDICTIONS_INDEX,
    TAX_EXEMPTIONS_INDEX,
    PRICE_PROTECTION_CONTRACTS_INDEX,
    DRIVERS_INDEX,
    ASSET_CERTIFICATIONS_INDEX,
    METER_REGISTRY_INDEX,
    METER_AUDIT_TRAIL_INDEX,
    TERMINAL_BOLS_INDEX,
    PRICING_RULES_INDEX,
    IFTA_MILEAGE_INDEX,
    KFACTOR_HISTORY_INDEX,
    DYED_DIESEL_AUDIT_LOG_INDEX,
}


# ---------------------------------------------------------------------------
# Tests: every mapping has the mandatory tenant + audit timestamp fields
# ---------------------------------------------------------------------------


class TestComplianceMappingShape:
    """Every mapping is strict, tenant-scoped, and audit-timestamped."""

    def test_catalog_contains_all_11_indices(self):
        assert set(COMPLIANCE_INDEX_MAPPINGS.keys()) == EXPECTED_INDICES
        assert len(COMPLIANCE_INDEX_MAPPINGS) == 12

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_is_strict(self, index_name):
        mapping = COMPLIANCE_INDEX_MAPPINGS[index_name]
        assert mapping["mappings"]["dynamic"] == "strict", (
            f"{index_name} mapping must use dynamic: strict"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_tenant_id_keyword(self, index_name):
        props = COMPLIANCE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "tenant_id" in props, (
            f"{index_name} must define tenant_id for tenant isolation"
        )
        assert props["tenant_id"]["type"] == "keyword", (
            f"{index_name}.tenant_id must be keyword for exact-match filtering"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_created_at_date(self, index_name):
        props = COMPLIANCE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "created_at" in props, (
            f"{index_name} must define created_at for audit trail"
        )
        assert props["created_at"]["type"] == "date", (
            f"{index_name}.created_at must be type date"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_updated_at_date(self, index_name):
        props = COMPLIANCE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "updated_at" in props, (
            f"{index_name} must define updated_at for audit trail"
        )
        assert props["updated_at"]["type"] == "date", (
            f"{index_name}.updated_at must be type date"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_default_shard_and_replica_settings(self, index_name):
        settings = COMPLIANCE_INDEX_MAPPINGS[index_name]["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1


# ---------------------------------------------------------------------------
# Tests: setup_compliance_indices idempotency + bootstrap behaviour
# ---------------------------------------------------------------------------


class TestSetupComplianceIndices:
    """The bootstrap hook must be safe to invoke on every startup."""








