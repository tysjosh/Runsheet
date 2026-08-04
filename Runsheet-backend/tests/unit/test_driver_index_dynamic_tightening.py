"""
Unit tests: driver index setup tightens ``dynamic`` and is validator-registered.

``setup_driver_indices`` creates only absent indices, which does nothing for an
index Elasticsearch auto-created on first write with ``dynamic: true`` — the
state of every driver index in a deployment that never ran the seeder. The
already-exists branch therefore issues ``put_mapping`` with
``{"dynamic": "strict"}``, and the driver pairs are registered with
``MappingValidator`` so additive field drift is detected and repaired.

Validates: Requirements 15.12
"""

import sys
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from driver.services.driver_es_mappings import (
    DRIVER_INDEX_MAPPINGS,
    JOB_MESSAGES_INDEX,
    PROOF_OF_DELIVERY_INDEX,
    setup_driver_indices,
)
from services.mapping_validator import _collect_all_index_mappings

EXPECTED_INDICES = set(DRIVER_INDEX_MAPPINGS)


def _make_es_service(existing_indices=None, is_serverless=False):
    existing = existing_indices or set()
    es_service = MagicMock()
    client = MagicMock()
    client.indices.exists.side_effect = lambda index: index in existing
    es_service.client = client
    type(es_service).is_serverless = PropertyMock(return_value=is_serverless)
    return es_service


def _patch_es_module():
    """Patch the lazy import inside ``setup_driver_indices``."""
    fake_module = MagicMock()
    fake_module.ElasticsearchService = MagicMock()
    fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
        lambda mapping: mapping
    )
    return patch.dict(sys.modules, {"services.elasticsearch_service": fake_module})


class TestDynamicTightening:
    def test_absent_index_is_created_and_not_put_mapped(self):
        es_service = _make_es_service()
        with _patch_es_module():
            setup_driver_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert created == EXPECTED_INDICES
        assert es_service.client.indices.put_mapping.call_count == 0

    def test_existing_index_is_tightened_to_strict(self):
        es_service = _make_es_service(existing_indices=EXPECTED_INDICES)
        with _patch_es_module():
            setup_driver_indices(es_service)

        assert es_service.client.indices.create.call_count == 0
        tightened = {
            call.kwargs["index"]: call.kwargs["body"]
            for call in es_service.client.indices.put_mapping.call_args_list
        }
        assert set(tightened) == EXPECTED_INDICES
        assert all(body == {"dynamic": "strict"} for body in tightened.values())

    def test_partial_cluster_creates_missing_and_tightens_existing(self):
        already_there = {JOB_MESSAGES_INDEX, PROOF_OF_DELIVERY_INDEX}
        es_service = _make_es_service(existing_indices=already_there)
        with _patch_es_module():
            setup_driver_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        tightened = {
            call.kwargs["index"]
            for call in es_service.client.indices.put_mapping.call_args_list
        }
        assert created == EXPECTED_INDICES - already_there
        assert tightened == already_there

    def test_tightening_failure_on_one_index_does_not_abort_others(self):
        es_service = _make_es_service(existing_indices=EXPECTED_INDICES)

        def flaky_put_mapping(**kwargs):
            if kwargs["index"] == JOB_MESSAGES_INDEX:
                raise RuntimeError("simulated ES failure")
            return {"acknowledged": True}

        es_service.client.indices.put_mapping.side_effect = flaky_put_mapping
        with _patch_es_module():
            setup_driver_indices(es_service)  # must not raise

        attempted = {
            call.kwargs["index"]
            for call in es_service.client.indices.put_mapping.call_args_list
        }
        assert attempted == EXPECTED_INDICES

    def test_serverless_settings_are_stripped_on_create(self):
        es_service = _make_es_service(is_serverless=True)
        stripped = {"mappings": {"dynamic": "strict", "properties": {}}}

        fake_module = MagicMock()
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            lambda mapping: stripped
        )
        with patch.dict(
            sys.modules, {"services.elasticsearch_service": fake_module}
        ):
            setup_driver_indices(es_service)

        bodies = [
            call.kwargs["body"]
            for call in es_service.client.indices.create.call_args_list
        ]
        assert bodies and all(body is stripped for body in bodies)


class TestValidatorRegistration:
    def test_every_driver_index_is_registered_with_the_validator(self):
        collected = dict(_collect_all_index_mappings())
        missing = EXPECTED_INDICES - set(collected)
        assert not missing, (
            f"Driver index/indices {sorted(missing)} are not registered with "
            f"MappingValidator, so their drift is neither detected nor repaired."
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_registered_mapping_is_the_code_defined_one(self, index_name):
        collected = dict(_collect_all_index_mappings())
        assert collected[index_name] is DRIVER_INDEX_MAPPINGS[index_name]

    def test_collector_has_no_duplicate_index_entries(self):
        names = [name for name, _ in _collect_all_index_mappings()]
        duplicates = {name for name in names if names.count(name) > 1}
        assert not duplicates, f"Duplicate collector entries: {sorted(duplicates)}"
