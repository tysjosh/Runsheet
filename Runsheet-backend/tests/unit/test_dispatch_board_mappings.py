"""Dispatch Board index mappings and their field policy (dispatch-board K2.1, K2.2, K2.4).

The draft's ``applied_commands`` and the command log's ``response``,
``content_before`` and ``content_after`` hold whole responses and lane
snapshots; they are declared ``enabled: false`` so the Postgres store refuses to
filter or sort on them. The indices reach the policy through ``_REGISTRIES``.
"""
from __future__ import annotations

import pytest

from fuel.services.dispatch_board_es_mappings import (
    DISPATCH_BOARD_COMMANDS_INDEX,
    DISPATCH_BOARD_DRAFTS_INDEX,
    DISPATCH_BOARD_INDEX_MAPPINGS,
)
from persistence import document_field_policy
from persistence.document_field_policy import (
    UnsearchableFieldError,
    all_index_mappings,
    assert_searchable,
    unsearchable_fields,
)


@pytest.fixture(autouse=True)
def _fresh_policy_cache():
    document_field_policy._cache.clear()
    yield
    document_field_policy._cache.clear()


def test_registered_in_field_policy_registries():
    assert (
        "fuel.services.dispatch_board_es_mappings",
        "DISPATCH_BOARD_INDEX_MAPPINGS",
    ) in document_field_policy._REGISTRIES
    names = {name for name, _ in all_index_mappings()}
    assert {DISPATCH_BOARD_DRAFTS_INDEX, DISPATCH_BOARD_COMMANDS_INDEX} <= names


def test_drafts_applied_commands_is_unsearchable():
    assert unsearchable_fields(DISPATCH_BOARD_DRAFTS_INDEX) == frozenset({"applied_commands"})


def test_commands_snapshots_and_response_are_unsearchable():
    assert unsearchable_fields(DISPATCH_BOARD_COMMANDS_INDEX) == frozenset(
        {"response", "content_before", "content_after"}
    )


def test_lookup_fields_stay_searchable():
    # The listener and cross-day lookups (K10.4, K5.1) and history (R22.3).
    assert_searchable(DISPATCH_BOARD_DRAFTS_INDEX, ["tenant_id", "service_date", "order_ids"])
    assert_searchable(
        DISPATCH_BOARD_COMMANDS_INDEX,
        ["tenant_id", "service_date", "command_id", "lanes.truck_id", "created_at"],
    )
    with pytest.raises(UnsearchableFieldError):
        assert_searchable(DISPATCH_BOARD_COMMANDS_INDEX, ["content_after.loads"])


@pytest.mark.parametrize("index", sorted(DISPATCH_BOARD_INDEX_MAPPINGS))
def test_declared_types(index):
    props = DISPATCH_BOARD_INDEX_MAPPINGS[index]["mappings"]["properties"]
    assert props["service_date"] == {"type": "date"}
    assert props["created_at"] == {"type": "date"}
    assert props["updated_at"] == {"type": "date"}
    if index == DISPATCH_BOARD_DRAFTS_INDEX:
        assert props["order_ids"] == {"type": "keyword"}
