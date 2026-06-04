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

from compliance.services.compliance_es_mappings import (
    ASSET_CERTIFICATIONS_INDEX,
    COMPLIANCE_INDEX_MAPPINGS,
    DRIVERS_INDEX,
    DYED_DIESEL_AUDIT_LOG_INDEX,
    IFTA_MILEAGE_INDEX,
    KFACTOR_HISTORY_INDEX,
    METER_AUDIT_TRAIL_INDEX,
    METER_REGISTRY_INDEX,
    PRICE_PROTECTION_CONTRACTS_INDEX,
    PRICING_RULES_INDEX,
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
    TERMINAL_BOLS_INDEX,
    setup_compliance_indices,
)


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

    def _make_es_service(self, existing_indices=None, is_serverless=False):
        existing = existing_indices or set()
        es_service = MagicMock()
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: index in existing
        es_service.client = client
        type(es_service).is_serverless = PropertyMock(return_value=is_serverless)
        return es_service

    def _patch_es_module(self):
        """Patch the lazy import inside ``setup_compliance_indices``."""
        fake_module = MagicMock()
        fake_module.ElasticsearchService = MagicMock()
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            lambda mapping: mapping
        )
        return patch.dict(
            sys.modules, {"services.elasticsearch_service": fake_module}
        )

    def test_creates_all_missing_indices_on_fresh_cluster(self):
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_compliance_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert created == EXPECTED_INDICES

    def test_is_idempotent_when_all_indices_already_exist(self):
        """Re-running the bootstrap with every index present must not error
        and must not issue any create calls."""
        es_service = self._make_es_service(existing_indices=EXPECTED_INDICES)
        with self._patch_es_module():
            setup_compliance_indices(es_service)  # must not raise

        assert es_service.client.indices.create.call_count == 0

    def test_idempotent_across_repeated_invocations(self):
        """Invoking the bootstrap twice back-to-back is safe: the second call
        sees every index as existing and skips all creates."""
        es_service = self._make_es_service()

        # First invocation — cluster is empty, everything gets created.
        with self._patch_es_module():
            setup_compliance_indices(es_service)
        first_create_count = es_service.client.indices.create.call_count
        assert first_create_count == len(EXPECTED_INDICES)

        # Simulate the cluster now reflecting the created indices.
        es_service.client.indices.exists.side_effect = lambda index: True

        # Second invocation — everything already exists, no new creates.
        with self._patch_es_module():
            setup_compliance_indices(es_service)
        assert es_service.client.indices.create.call_count == first_create_count

    def test_partial_existing_indices_creates_only_missing(self):
        already_there = {DRIVERS_INDEX, TAX_JURISDICTIONS_INDEX}
        es_service = self._make_es_service(existing_indices=already_there)
        with self._patch_es_module():
            setup_compliance_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert created.isdisjoint(already_there)
        assert created == EXPECTED_INDICES - already_there

    def test_errors_on_one_index_do_not_abort_others(self):
        """A failure on one index must not prevent the remaining indices from
        being attempted — preserves partial-recovery behaviour on startup."""
        es_service = self._make_es_service()

        def flaky_create(**kwargs):
            if kwargs["index"] == DRIVERS_INDEX:
                raise RuntimeError("simulated ES failure")
            return {"acknowledged": True}

        es_service.client.indices.create.side_effect = flaky_create
        with self._patch_es_module():
            setup_compliance_indices(es_service)  # must not raise

        attempted = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert attempted == EXPECTED_INDICES

    def test_skips_retired_indices(self, monkeypatch):
        """A Phase-6 retired index must NOT be recreated at startup."""
        from config.settings import clear_settings_cache

        monkeypatch.setenv("RETIRED_ES_INDICES", "pricing_rules")
        clear_settings_cache()
        try:
            es_service = self._make_es_service()
            with self._patch_es_module():
                setup_compliance_indices(es_service)

            created = {
                call.kwargs["index"]
                for call in es_service.client.indices.create.call_args_list
            }
            assert PRICING_RULES_INDEX not in created
            assert created == EXPECTED_INDICES - {PRICING_RULES_INDEX}
        finally:
            clear_settings_cache()
