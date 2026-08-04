"""The rebuild tool must not claim to cover indices it cannot rebuild (Bug 4).

``persistence.rebuild_from_postgres`` ended its docstring with "so dropping an
index is safe: it can always be reconstructed here". Acting on that during an
end-to-end test of the MVP pipeline destroyed the contents of three indices that
have no Postgres table, no ORM model and no projector behind them —
``customer_tanks``, ``truck_compartments`` and ``fuel_stations``. The A1 tank
forecasting and A3 compartment loading stages then had no input whatsoever, which
is the data half of the "plan generated, zero output" bug.

Those three are not seed data. ``KFactorCalibrationService`` writes calibrated
``k_factor`` values back into ``customer_tanks``, the Veeder-Root ATG connector
updates tank levels, and ``CompartmentLoadingAgent`` writes
``last_loaded_product`` into ``truck_compartments`` — the history the
cross-contamination guard reads before allowing a product into a compartment. The
copy in Elasticsearch is the only copy.

Giving them a Postgres source of truth is a migration plus ORM models,
projectors and repository rewiring, and is not done. What is done is removing the
false promise. These tests hold that honest:

  * every index named in ``ES_ONLY_INDICES`` really is unrebuildable, so the
    list cannot rot into scaremongering about something since fixed;
  * nothing appears in both ``ES_ONLY_INDICES`` and the rebuildable set, so the
    two cannot contradict each other;
  * the docstring no longer carries the unconditional claim.

The first is a ratchet: the day ``customer_tanks`` gets a projector, this test
fails and the fix is to delete it from the list.
"""
from __future__ import annotations

import persistence.rebuild_from_postgres as rebuild_mod
from persistence.projections import PROJECTORS
from persistence.rebuild_from_postgres import (
    ES_ONLY_INDICES,
    _REBUILD_SPECS,
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

    def test_the_list_is_not_empty_and_names_the_three_known_gaps(self):
        """Pins the specific indices whose loss produced the empty plan run."""
        assert set(ES_ONLY_INDICES) == {
            "customer_tanks",
            "truck_compartments",
            "fuel_stations",
        }

    def test_no_orm_model_exists_for_them(self):
        """The underlying reason they cannot be rebuilt: nothing to read from."""
        from persistence import models

        for name in ("TruckCompartmentORM", "CustomerTankORM", "FuelStationORM"):
            assert not hasattr(models, name), (
                f"{name} exists now, so this entity may be rebuildable — "
                "reassess ES_ONLY_INDICES."
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
    def test_rebuild_all_names_the_es_only_indices_in_a_warning(
        self, caplog, monkeypatch
    ):
        """``--all`` finishing cleanly reads as "the cluster is whole" otherwise."""
        import asyncio
        import logging

        # Stub the per-aggregate rebuild: without DATABASE_URL every call raises
        # and rebuild_all logs a traceback for each of the 25 aggregates, which
        # would bury this assertion's own output in unrelated noise. The warning
        # under test fires before the loop, so the loop can be a no-op.
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
        assert any(
            all(index in message for index in ES_ONLY_INDICES)
            for message in warnings
        ), warnings
