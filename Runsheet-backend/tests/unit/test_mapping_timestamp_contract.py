"""A retired contract, kept as a record of what it found on its way out.

The contract was: ``ElasticsearchService.index_document`` auto-stamps
``updated_at`` and ``created_at``, a ``dynamic: strict`` index rejects any
undeclared field, so a strict index that receives auto-stamped writes without
declaring those two fields fails **every** write with
``strict_dynamic_mapping_exception``. It was written after exactly that took down
``fuel_events``.

It no longer holds, because nothing rejects an undeclared field: the document store
is a ``jsonb`` column. The declarations survive — ``document_field_policy`` reads
them to decide which fields stay unqueryable — but ``dynamic: strict`` is now a
statement of intent with no enforcement behind it.

**What retiring it surfaced.** The test's own docstring warned that its coverage
depended on the collector, and it was right. The old collector named ~70
``*_INDEX`` / ``*_MAPPING`` pairs by hand; the replacement walks the registries, so
it sees 86 indices instead. Ten strict indices lack the auto-stamped fields, and
after excluding the three legitimate event streams in ``TIMESTAMP_SKIP_INDICES``,
seven were unguarded:

    payments_current, ar_aging_snapshots, dunning_events, driver_reports,
    tenant_job_policies, riders_current, ops_poison_queue

``payments_current`` is written by ``PaymentService.ingest`` through
``index_document``. On Elasticsearch, with that strict mapping, every one of those
writes would have been rejected — the exact failure this test existed to prevent,
sitting behind a collector that could not see it. It was invisible because the unit
tests mock the client, so nothing exercised the real rejection.

That is moot now rather than fixed: ``jsonb`` accepts the fields. It is recorded
because it is the argument for the migration having removed a class of latent
failure, and because if the cluster is ever restored these seven need mappings
before they are written to.

The surviving assertion is only that the collector still finds mappings, which is
what the field policy depends on.
"""

import pytest

from services.elasticsearch_service import TIMESTAMP_SKIP_INDICES
from persistence.document_field_policy import all_index_mappings as _collect_all_index_mappings

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

#: The seven indices whose strict mappings never declared the auto-stamped fields.
#: Frozen as a list rather than skipped silently: shrinking it is an improvement,
#: growing it means a new strict mapping repeated the mistake, and either way the
#: number is visible.
_KNOWN_MISSING_AUTO_STAMPS = {
    "payments_current",
    "ar_aging_snapshots",
    "dunning_events",
    "driver_reports",
    "tenant_job_policies",
    "riders_current",
    "ops_poison_queue",
}


def test_collection_is_non_empty():
    """Guard against the collection silently returning nothing.

    Load-bearing beyond this file: ``document_field_policy`` uses the same walk to
    decide which fields are unqueryable, so an empty collection would quietly make
    ``pod_otp`` and ``push_token`` filterable.
    """
    assert _STRICT_INDEX_MAPPINGS, (
        "No strict index mappings were collected — the contract test would "
        "pass vacuously. Check persistence.document_field_policy.all_index_mappings()."
    )


def test_the_set_of_indices_missing_auto_stamps_has_not_grown():
    """The finding, pinned so it cannot quietly get worse.

    Seven strict mappings omit fields ``index_document`` writes. Harmless against
    ``jsonb`` and fatal against Elasticsearch, so this is the list to consult before
    anyone points a strict-mapped store at these indices again.
    """
    actual = {
        index_name
        for index_name, mapping in _STRICT_INDEX_MAPPINGS
        if [f for f in _AUTO_STAMPED_FIELDS if f not in _properties(mapping)]
    }

    assert actual <= _KNOWN_MISSING_AUTO_STAMPS, (
        "these strict mappings newly omit an auto-stamped timestamp: "
        f"{sorted(actual - _KNOWN_MISSING_AUTO_STAMPS)}. Harmless on jsonb, but it "
        "is the shape that took down fuel_events on Elasticsearch."
    )


@pytest.mark.parametrize(
    "index_name,mapping",
    [
        (name, mapping)
        for name, mapping in _STRICT_INDEX_MAPPINGS
        if name not in _KNOWN_MISSING_AUTO_STAMPS
    ],
    ids=[
        name
        for name, _ in _STRICT_INDEX_MAPPINGS
        if name not in _KNOWN_MISSING_AUTO_STAMPS
    ],
)
def test_strict_index_declares_autostamped_timestamps(index_name, mapping):
    """Every strict mapping still declares what ``index_document`` stamps.

    Kept for the 70-odd indices that get it right, so an edit that drops
    ``updated_at`` from one of them is still noticed. The seven that never declared
    it are excluded and tracked in
    :func:`test_the_set_of_indices_missing_auto_stamps_has_not_grown`, rather than
    left to fail here and be silenced by someone widening the skip list.
    """
    props = _properties(mapping)
    missing = [f for f in _AUTO_STAMPED_FIELDS if f not in props]
    assert not missing, (
        f"Strict index '{index_name}' is missing auto-stamped field(s) "
        f"{missing}. ElasticsearchService.index_document stamps these on every "
        f"write. Harmless against jsonb, fatal against a strict Elasticsearch "
        f"mapping — which is what took down fuel_events. Either add them or add "
        f"'{index_name}' to TIMESTAMP_SKIP_INDICES if it carries its own "
        f"domain timestamps."
    )
