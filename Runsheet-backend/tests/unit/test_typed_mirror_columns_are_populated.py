"""A hybrid mirror column no writer populates turns a filter into a silent empty.

The hybrid document tables keep the full ES document in ``document`` plus a few
typed *mirror* columns lifted out of it for indexing. ``HybridReadRepository.list``
resolves a filter key against the model::

    col = getattr(self.model, key, None)
    if col is not None:
        where.append(col == value)

So a column that exists on the model but is never written is worse than a
missing one: the filter compiles, runs, matches ``NULL`` against the value, and
returns zero rows. No error, no log — the caller just sees an empty list and
concludes there is no data.

That is exactly how route planning lost its depot. ``DepotORM`` declares
``status`` (inherited) and ``is_default``, but the writer spec listed only
``is_default``. ``DepotRepository.list_for_tenant(status="active")`` routes
through ``read_hybrid_list("depot", filters={"status": "active"})`` once
``COMMERCE_READ_FROM_POSTGRES`` is on, matched nothing, and the depot resolver
fell through to ``no_depot_configured`` on every plan — while an unfiltered list
happily returned all three depots. Five other aggregates had the same hole:
``fuel_order.assigned_asset_id`` and ``status`` on ``intake_channel``,
``location``, ``tenant_job_policy`` and ``truck``.

This pins the invariant for every registered aggregate rather than for depot
alone, because the trap is in the shape of the read, not in one spec.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect

import persistence.models as models
from persistence.backfill import (
    _CONFIG_BACKFILL,
    _CURRENT_STATE_BACKFILL,
    _MASTER_DATA_BACKFILL,
)
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import (
    ComplianceConfigRepository,
    CurrentStateRepository,
    hybrid_spec_for,
)

# Structural columns every hybrid table carries; not filter targets lifted from
# the document, so a writer is not expected to list them.
_INFRASTRUCTURE = {"document", "created_at", "updated_at", "tenant_id"}

_AGGREGATES = sorted(HybridReadRepository._SPECS)


def _mirror_columns(aggregate_type: str) -> set[str]:
    model_name, pk_attr, _tenant_optional = HybridReadRepository._SPECS[
        aggregate_type
    ]
    model = getattr(models, model_name)
    columns = {c.key for c in sa_inspect(model).columns}
    return columns - _INFRASTRUCTURE - {pk_attr}


@pytest.mark.parametrize("aggregate_type", _AGGREGATES)
def test_every_mirror_column_is_written_by_the_writer(aggregate_type):
    _model, _pk, typed_cols = hybrid_spec_for(aggregate_type)
    unwritten = _mirror_columns(aggregate_type) - set(typed_cols)
    assert not unwritten, (
        f"{aggregate_type}: {sorted(unwritten)} exist on the ORM model but are "
        "not in the writer's typed columns, so HybridReadRepository.list will "
        "filter on a permanently NULL column and silently return zero rows. "
        "Add them to the spec (and re-backfill existing rows)."
    )


@pytest.mark.parametrize("aggregate_type", _AGGREGATES)
def test_the_read_and_write_sides_agree_on_the_primary_key(aggregate_type):
    """Counterweight: the columns must line up on a model both sides mean.

    Comparing typed columns is only meaningful if the read spec and the write
    spec name the same table; a pk mismatch would mean this file is comparing
    two different aggregates and passing for the wrong reason.
    """
    read_model_name, read_pk, _ = HybridReadRepository._SPECS[aggregate_type]
    write_model, write_pk, _ = hybrid_spec_for(aggregate_type)
    assert write_model is getattr(models, read_model_name)
    assert write_pk == read_pk


class TestBackfillSharesTheSpec:
    """Backfill must not carry its own copy of the typed columns.

    It used to, and the depot entry went stale in both places at once. Deriving
    from ``hybrid_spec_for`` is what makes the test above sufficient — otherwise
    a fixed spec and a stale backfill would still write NULL mirror columns for
    every row a backfill creates.
    """

    @pytest.mark.parametrize(
        "entry",
        [e for e in _CONFIG_BACKFILL]
        + [e[:2] for e in _CURRENT_STATE_BACKFILL]
        + [e for e in _MASTER_DATA_BACKFILL],
    )
    def test_each_entry_names_a_registered_aggregate(self, entry):
        aggregate_type = entry[0]
        assert HybridReadRepository.is_registered(aggregate_type), (
            f"{aggregate_type!r} is in a backfill list but has no read spec"
        )
        hybrid_spec_for(aggregate_type)  # raises if no writer spec either

    def test_the_backfill_lists_carry_no_typed_column_tuples(self):
        """The shape itself is the guard: no room left for a divergent copy."""
        for entry in _CONFIG_BACKFILL + _MASTER_DATA_BACKFILL:
            assert len(entry) == 2, (
                f"{entry!r} should be (aggregate_type, es_index) only — a third "
                "element is how the duplicated typed columns crept back in"
            )
        for entry in _CURRENT_STATE_BACKFILL:
            assert len(entry) == 3, (
                f"{entry!r} should be (aggregate_type, es_index, tenant_keyed)"
            )
            assert isinstance(entry[2], bool)

    def test_every_writable_aggregate_is_backfillable(self):
        """No hybrid aggregate may be write-only.

        A cutover reads from Postgres, so an aggregate the backfill skips serves
        an empty result set for pre-existing data.
        """
        covered = {e[0] for e in _CONFIG_BACKFILL}
        covered |= {e[0] for e in _CURRENT_STATE_BACKFILL}
        covered |= {e[0] for e in _MASTER_DATA_BACKFILL}
        writable = set(CurrentStateRepository._SPECS) | set(
            ComplianceConfigRepository._SPECS
        )
        assert writable - covered == set(), (
            f"not backfillable: {sorted(writable - covered)}"
        )
