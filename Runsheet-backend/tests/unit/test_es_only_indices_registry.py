"""The rebuild tool must not claim to cover indices it cannot rebuild (Bug 4).

``persistence.rebuild_from_postgres`` ended its docstring with "so dropping an
index is safe: it can always be reconstructed here". Acting on that during an
end-to-end test of the MVP pipeline destroyed the contents of three indices that
had no Postgres table, no ORM model and no projector —  ``customer_tanks``,
``truck_compartments`` and ``fuel_stations``. The A1 tank forecasting and A3
compartment loading stages then had no input whatsoever, which is the data half
of the "plan generated, zero output" bug.

Those three were never seed data. ``KFactorCalibrationService`` writes calibrated
``k_factor`` values back into ``customer_tanks``, the Veeder-Root ATG connector
updates tank levels, and ``CompartmentLoadingAgent`` writes
``last_loaded_product`` into ``truck_compartments`` — the history the
cross-contamination guard reads before allowing a product into a compartment.
While Elasticsearch held the only copy, losing the cluster lost all of it.

They now have Postgres tables, passthrough projectors, a backfill and
``_REBUILD_SPECS`` entries, so ``ES_ONLY_INDICES`` is empty. These tests keep
both halves of that honest:

  * whatever is listed in ``ES_ONLY_INDICES`` really is unrebuildable, so the
    list cannot rot into scaremongering about something since fixed;
  * nothing appears in both ``ES_ONLY_INDICES`` and the rebuildable set;
  * the three that were fixed stay fixed — each keeps its ORM model, projector
    and rebuild spec, so the gap cannot silently reopen;
  * the docstring no longer carries the unconditional claim.

The first two are generic: a fourth index added without a projector belongs on
the list, and these tests then hold it to the same standard.
"""
from __future__ import annotations

import pytest

import persistence.rebuild_from_postgres as rebuild_mod
from persistence.projections import PROJECTORS
from persistence.rebuild_from_postgres import (
    ES_ONLY_INDICES,
    _REBUILD_SPECS,
)

#: The three that produced the empty plan run, and the Postgres homes they were
#: given. ``(aggregate_type, es_index, ORM class name)``.
_FORMERLY_ES_ONLY = (
    ("customer_tank", "customer_tanks", "CustomerTankORM"),
    ("truck_compartment", "truck_compartments", "TruckCompartmentORM"),
    ("fuel_station", "fuel_stations", "FuelStationORM"),
)


def _rebuildable_indices() -> set:
    """The ES indices ``rebuild_all`` actually restores.

    An aggregate is only rebuildable when it appears in *both* tables: the spec
    supplies the ORM model to read and ``PROJECTORS`` supplies the target index
    and the document shape. ``rebuild`` enforces exactly that pairing.
    """
    return {
        PROJECTORS[aggregate][0]
        for aggregate in _REBUILD_SPECS
        if aggregate in PROJECTORS
    }


class TestEsOnlyIndicesAreReallyUnrebuildable:
    def test_none_of_them_has_a_projector(self):
        """A ratchet: adding a projector should force removal from the list."""
        projected = {index for index, _ in PROJECTORS.values()}
        overlap = sorted(set(ES_ONLY_INDICES) & projected)

        assert not overlap, (
            f"{overlap} now has a projector, so it is no longer ES-only. "
            "Remove it from ES_ONLY_INDICES and add its aggregate to "
            "_REBUILD_SPECS."
        )

    def test_none_of_them_is_rebuilt_by_rebuild_all(self):
        overlap = sorted(set(ES_ONLY_INDICES) & _rebuildable_indices())

        assert not overlap, (
            f"{overlap} is both listed as ES-only and rebuilt by rebuild_all — "
            "the two registries contradict each other."
        )

    def test_the_list_is_empty_now_that_the_three_gaps_are_closed(self):
        assert ES_ONLY_INDICES == (), (
            "an index is listed as having no Postgres source of truth. If that "
            "is genuinely true, extend _FORMERLY_ES_ONLY reasoning to it and "
            "back it up with scripts.es_only_backup; if it is stale, remove it."
        )


class TestTheThreeClosedGapsStayClosed:
    """Each of the three keeps a Postgres home, a projector and a rebuild spec.

    Any one of the three missing is enough to reopen the original bug, and the
    failure mode is silent: the rebuild simply skips the aggregate and the fuel
    planning stages run on an empty index.
    """

    @pytest.mark.parametrize("aggregate,index,model_name", _FORMERLY_ES_ONLY)
    def test_orm_model_exists(self, aggregate, index, model_name):
        from persistence import models

        assert hasattr(models, model_name), (
            f"{model_name} is gone, so {index} has no Postgres table again"
        )

    @pytest.mark.parametrize("aggregate,index,model_name", _FORMERLY_ES_ONLY)
    def test_projector_targets_the_right_index(self, aggregate, index, model_name):
        assert aggregate in PROJECTORS, f"{aggregate} lost its projector"
        assert PROJECTORS[aggregate][0] == index

    @pytest.mark.parametrize("aggregate,index,model_name", _FORMERLY_ES_ONLY)
    def test_rebuild_spec_names_the_same_model(self, aggregate, index, model_name):
        assert aggregate in _REBUILD_SPECS, f"{aggregate} lost its rebuild spec"
        assert _REBUILD_SPECS[aggregate][0] == model_name

    @pytest.mark.parametrize("aggregate,index,model_name", _FORMERLY_ES_ONLY)
    def test_rebuild_spec_pk_is_the_write_path_pk(self, aggregate, index, model_name):
        """The rebuild must index under the same id the writers use.

        A mismatch would rebuild the index under a different ``_id`` than the
        application fetches by — every document present, every lookup a 404.
        Two of the three are exposed to this: ``truck_compartments`` is keyed by
        the composite ``truck_id_compartment_id`` and ``fuel_stations`` by an
        ``_id`` that may or may not carry a ``::fuel_type`` suffix.
        """
        from persistence.repositories import hybrid_spec_for

        _model, write_pk, _typed = hybrid_spec_for(aggregate)
        assert _REBUILD_SPECS[aggregate][1] == write_pk

    @pytest.mark.parametrize("aggregate,index,model_name", _FORMERLY_ES_ONLY)
    def test_read_path_is_registered(self, aggregate, index, model_name):
        """Without this the read cutover silently falls back to Elasticsearch."""
        from persistence.read_repositories import HybridReadRepository

        assert HybridReadRepository.is_registered(aggregate)

    def test_last_loaded_product_is_a_typed_column_not_only_json(self):
        """The cross-contamination guard's input must survive as a column.

        It is the one field whose loss is worse than losing data: the guard
        reads it to decide whether a compartment may take a product, and an
        absent value reads as "no history", i.e. permitted.
        """
        from persistence.models import TruckCompartmentORM

        assert hasattr(TruckCompartmentORM, "last_loaded_product")

        from persistence.repositories import hybrid_spec_for

        _model, _pk, typed_cols = hybrid_spec_for("truck_compartment")
        assert "last_loaded_product" in typed_cols, (
            "the column exists but no writer populates it, so every "
            "filter on it returns empty — the depot/status failure mode"
        )


class TestTheFalsePromiseIsGone:
    def test_the_docstring_no_longer_claims_dropping_any_index_is_safe(self):
        doc = rebuild_mod.__doc__ or ""

        assert "it can always be reconstructed here" not in doc, (
            "the unconditional claim is back; it is false for the indices in "
            "ES_ONLY_INDICES and acting on it loses data"
        )

    def test_the_docstring_points_at_the_registry(self):
        doc = rebuild_mod.__doc__ or ""
        assert "ES_ONLY_INDICES" in doc


class TestRebuildAllWarnsAboutTheGap:
    def test_rebuild_all_warns_only_when_there_is_a_gap(self, caplog, monkeypatch):
        """``--all`` finishing cleanly reads as "the cluster is whole" otherwise.

        With the list empty there is nothing to warn about and warning anyway
        would train operators to ignore it, so the assertion is conditional on
        the registry rather than pinned to today's empty tuple.
        """
        import asyncio
        import logging

        # Stub the per-aggregate rebuild: without DATABASE_URL every call raises
        # and rebuild_all logs a traceback for each aggregate, which would bury
        # this assertion's own output in unrelated noise. The warning under test
        # fires before the loop, so the loop can be a no-op.
        async def _noop(aggregate_type, tenant_id=None, *, dry_run=False):
            return 0

        monkeypatch.setattr(rebuild_mod, "rebuild", _noop)

        with caplog.at_level(logging.WARNING, logger=rebuild_mod.logger.name):
            asyncio.run(rebuild_mod.rebuild_all(tenant_id="t", dry_run=True))

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        if ES_ONLY_INDICES:
            assert any(
                all(index in message for index in ES_ONLY_INDICES)
                for message in warnings
            ), warnings
        else:
            assert not any(
                "ES-only index" in message for message in warnings
            ), warnings
