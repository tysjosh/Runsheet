"""
Contract test: strict ES mappings must declare the auto-stamped timestamps.

``ElasticsearchService.index_document`` auto-stamps ``updated_at`` (and
``created_at`` when absent) onto every document it writes, EXCEPT for indices
in ``TIMESTAMP_SKIP_INDICES`` (event streams that carry their own domain
timestamps). Because a ``dynamic: strict`` index rejects any field not declared
in its mapping, a strict index that receives auto-stamped writes but does not
declare ``created_at``/``updated_at`` will fail every write with a
``strict_dynamic_mapping_exception``.

This is exactly the class of production bug that took down fuel_events this
cycle (``status`` + auto-stamped timestamps were missing from a strict mapping).
This test pins the invariant so a newly-added or edited strict mapping can't
silently reintroduce it — it fails at CI time instead of at runtime.

Coverage note: the source of truth is ``mapping_validator._collect_all_index_mappings()``,
the same canonical (index, mapping) list the startup MappingValidator uses.
Adding a domain field a writer persists that is not auto-stamped (e.g. a seed
writing ``template_key``) is a separate class this test intentionally does not
attempt to cover.
"""

import pytest

from services.elasticsearch_service import TIMESTAMP_SKIP_INDICES
from services.mapping_validator import _collect_all_index_mappings

# Fields ElasticsearchService.index_document auto-stamps onto every non-skipped
# document.
_AUTO_STAMPED_FIELDS = ("created_at", "updated_at")


def _is_strict(mapping: dict) -> bool:
    return mapping.get("mappings", {}).get("dynamic") == "strict"


def _properties(mapping: dict) -> dict:
    return mapping.get("mappings", {}).get("properties", {})


# Build the parameter set once at import time so each index is a named case.
_STRICT_INDEX_MAPPINGS = [
    (index_name, mapping)
    for index_name, mapping in _collect_all_index_mappings()
    if _is_strict(mapping) and index_name not in TIMESTAMP_SKIP_INDICES
]


def test_collection_is_non_empty():
    """Guard against the collection silently returning nothing (which would
    make the parametrized test vacuously pass)."""
    assert _STRICT_INDEX_MAPPINGS, (
        "No strict index mappings were collected — the contract test would "
        "pass vacuously. Check _collect_all_index_mappings()."
    )


@pytest.mark.parametrize(
    "index_name,mapping",
    _STRICT_INDEX_MAPPINGS,
    ids=[name for name, _ in _STRICT_INDEX_MAPPINGS],
)
def test_strict_index_declares_autostamped_timestamps(index_name, mapping):
    """Every strict index that receives auto-stamped writes must declare the
    auto-stamped timestamp fields, or writes will 400 with
    strict_dynamic_mapping_exception."""
    props = _properties(mapping)
    missing = [f for f in _AUTO_STAMPED_FIELDS if f not in props]
    assert not missing, (
        f"Strict index '{index_name}' is missing auto-stamped field(s) "
        f"{missing}. ElasticsearchService.index_document stamps these on every "
        f"write, so a strict mapping without them rejects all writes. Either "
        f"add them to the mapping or add '{index_name}' to "
        f"TIMESTAMP_SKIP_INDICES if it carries its own domain timestamps."
    )
