"""A rebuildable index whose mapping cannot be looked up rebuilds broken.

``rebuild_from_postgres._ensure_index`` recreates a missing index before writing
to it, and asks ``_lookup_mapping`` for the declared mapping. When that returns
``None`` it lets Elasticsearch create the index dynamically — which types
``tenant_id`` as ``text`` (with a ``.keyword`` subfield) instead of ``keyword``.
A ``term: {"tenant_id": ...}`` query against a ``text`` field matches nothing, so
every tenant-scoped read of the rebuilt index returns empty.

Nothing about that failure is loud. The rebuild logs "Indexed 9 doc(s)" and exits
0; the documents really are there; only the queries stop working. It was found by
dropping ``truck_compartments`` and rebuilding it: ``_lookup_mapping`` consulted
five registries and the ``truck_compartments`` mapping lives in a sixth
(``Agents.support.mvp_es_mappings``). ``fuel_stations`` had the same gap, and its
module had no registry dict to consult at all.

This test closes the class rather than the two instances: every aggregate the
rebuild claims to restore must resolve to a real mapping.
"""
from __future__ import annotations

import pytest

from persistence.projections import PROJECTORS
from persistence.rebuild_from_postgres import _REBUILD_SPECS, _lookup_mapping

#: Indices legitimately created with dynamic mapping. These two are legacy
#: generic-ES indices that were never declared in any mapping registry — the
#: application post-filters ``tenant_id`` in Python for exactly that reason, and
#: ``parity_check._fetch_es_all`` carries a ``tenant_id.keyword`` retry for them.
#: Declaring a strict mapping for them is a separate change with its own
#: reindex; until then they are excluded explicitly rather than by omission.
_DYNAMIC_BY_DESIGN = {"trucks", "locations"}

_REBUILDABLE_INDICES = sorted(
    {
        PROJECTORS[aggregate][0]
        for aggregate in _REBUILD_SPECS
        if aggregate in PROJECTORS
    }
    - _DYNAMIC_BY_DESIGN
)


@pytest.mark.parametrize("index", _REBUILDABLE_INDICES)
def test_every_rebuildable_index_has_a_resolvable_mapping(index):
    mapping = _lookup_mapping(index)
    assert mapping is not None, (
        f"{index} is rebuilt by rebuild_from_postgres but _lookup_mapping "
        "cannot find its mapping, so a rebuild after a drop creates it with "
        "dynamic typing. Add its registry to _lookup_mapping."
    )


@pytest.mark.parametrize("index", _REBUILDABLE_INDICES)
def test_tenant_id_is_a_keyword_in_every_rebuildable_mapping(index):
    """The specific field whose mistyping breaks every tenant-scoped read."""
    mapping = _lookup_mapping(index)
    assert mapping is not None
    properties = (mapping.get("mappings") or {}).get("properties") or {}
    tenant = properties.get("tenant_id")
    assert tenant is not None, f"{index} mapping declares no tenant_id"
    assert tenant.get("type") == "keyword", (
        f"{index}.tenant_id is {tenant.get('type')!r}, not 'keyword' — "
        "term queries on it will not match"
    )


def test_the_three_fuel_asset_indices_are_covered():
    """Named explicitly: these are the two that were broken plus their sibling.

    The parametrised tests above would pass if a future edit removed all three
    from ``_REBUILD_SPECS``, which is the other way to lose them.
    """
    for index in ("customer_tanks", "truck_compartments", "fuel_stations"):
        assert index in _REBUILDABLE_INDICES
