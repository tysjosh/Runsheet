"""``customer_tanks``, ``truck_compartments`` and ``fuel_stations`` keep their
Postgres homes (Bug 4).

``persistence.rebuild_from_postgres`` — the tool that reprojected documents from the
relational tables — ended its docstring with "so dropping an index is safe: it can
always be reconstructed here". Acting on that during an end-to-end test of the MVP
pipeline destroyed the contents of three indices that had no Postgres table, no ORM
model and no projector: ``customer_tanks``, ``truck_compartments`` and
``fuel_stations``. The A1 tank forecasting and A3 compartment loading stages then had
no input whatsoever, which is the data half of the "plan generated, zero output" bug.

Those three were never seed data. ``KFactorCalibrationService`` writes calibrated
``k_factor`` values back into ``customer_tanks``, the Veeder-Root ATG connector
updates tank levels, and ``CompartmentLoadingAgent`` writes ``last_loaded_product``
into ``truck_compartments`` — the history the cross-contamination guard reads before
allowing a product into a compartment. While Elasticsearch held the only copy, losing
the cluster lost all of it.

Migration ``0008_fuel_asset_tables`` gave all three Postgres tables, passthrough
projectors, a backfill and rebuild specs. These tests hold that fixed: any one of
them missing reopens the original bug, and the failure mode is silent — the rebuild
simply skips the aggregate and the fuel planning stages run on an empty index.

Formerly ``test_es_only_indices_registry.py``. Three of its classes were about the
``ES_ONLY_INDICES`` registry itself: that whatever it listed really was
unrebuildable, that nothing appeared in both it and the rebuildable set, that it was
empty, and that the module docstring's false promise stayed gone. Phase 6 deleted the
registry with the cluster — there is no Elasticsearch to recreate, so "this index
cannot survive a cluster rebuild" has no referent, and the empty tuple was pure
bookkeeping for ``scripts.es_only_backup``, which went too. What is left is the part
that was never about Elasticsearch: these three aggregates must keep a relational
table, a projector, a rebuild spec, a registered read path and a typed
``last_loaded_product`` column.
"""
from __future__ import annotations

import pytest

from persistence.projections import PROJECTORS
from persistence.rebuild_document_store import _REBUILD_SPECS

#: The three that produced the empty plan run, and the Postgres homes they were
#: given. ``(aggregate_type, index, ORM class name)``.
_FUEL_ASSETS = (
    ("customer_tank", "customer_tanks", "CustomerTankORM"),
    ("truck_compartment", "truck_compartments", "TruckCompartmentORM"),
    ("fuel_station", "fuel_stations", "FuelStationORM"),
)


class TestTheThreeClosedGapsStayClosed:
    @pytest.mark.parametrize("aggregate,index,model_name", _FUEL_ASSETS)
    def test_orm_model_exists(self, aggregate, index, model_name):
        from persistence import models

        assert hasattr(models, model_name), (
            f"{model_name} is gone, so {index} has no Postgres table again"
        )

    @pytest.mark.parametrize("aggregate,index,model_name", _FUEL_ASSETS)
    def test_projector_targets_the_right_index(self, aggregate, index, model_name):
        assert aggregate in PROJECTORS, f"{aggregate} lost its projector"
        assert PROJECTORS[aggregate][0] == index

    @pytest.mark.parametrize("aggregate,index,model_name", _FUEL_ASSETS)
    def test_rebuild_spec_names_the_same_model(self, aggregate, index, model_name):
        assert aggregate in _REBUILD_SPECS, f"{aggregate} lost its rebuild spec"
        assert _REBUILD_SPECS[aggregate][0] == model_name

    @pytest.mark.parametrize("aggregate,index,model_name", _FUEL_ASSETS)
    def test_rebuild_spec_pk_is_the_write_path_pk(self, aggregate, index, model_name):
        """The rebuild must write under the same id the writers use.

        A mismatch would rebuild the index under a different id than the
        application fetches by — every document present, every lookup a 404.
        Two of the three are exposed to this: ``truck_compartments`` is keyed by
        the composite ``truck_id_compartment_id`` and ``fuel_stations`` by an
        id that may or may not carry a ``::fuel_type`` suffix.
        """
        from persistence.repositories import hybrid_spec_for

        _model, write_pk, _typed = hybrid_spec_for(aggregate)
        assert _REBUILD_SPECS[aggregate][1] == write_pk

    @pytest.mark.parametrize("aggregate,index,model_name", _FUEL_ASSETS)
    def test_read_path_is_registered(self, aggregate, index, model_name):
        """Without this a read silently falls back to the document plane."""
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
