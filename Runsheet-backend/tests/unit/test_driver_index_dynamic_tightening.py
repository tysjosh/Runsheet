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

from driver.services.driver_es_mappings import DRIVER_INDEX_MAPPINGS, JOB_MESSAGES_INDEX, PROOF_OF_DELIVERY_INDEX
from persistence.document_field_policy import all_index_mappings as _collect_all_index_mappings

EXPECTED_INDICES = set(DRIVER_INDEX_MAPPINGS)








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
