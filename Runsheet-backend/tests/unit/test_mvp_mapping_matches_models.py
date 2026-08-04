"""Every field a model persists must be declared in its strict ES mapping.

Why this exists. ``mvp_load_plans`` is ``dynamic: strict``. A field present on
the Pydantic model but absent from the mapping is not ignored — Elasticsearch
rejects the entire document with ``strict_dynamic_mapping_exception``. The
agent catches that, logs "failed to persist loading plan", and carries on
returning the plan it built in memory, so ``FuelDistributionPipeline`` still
reports ``state: complete`` with nothing stored. A dispatcher then has no plan
to dispatch and no error to look at.

This is not hypothetical. The product-level density fix added
``product_code`` to :class:`CompartmentAssignment`, and sourcing added
``contract_id`` and ``terminal_id`` to :class:`LoadingPlan`, without touching
the mapping. Every load-plan write failed on a live cluster. The whole unit
suite passed throughout, because those tests mock the ES service and therefore
never exercise mapping validation. Only a run against a real cluster surfaced
it, which is exactly the gap this guard closes.

Two things are checked:

* Create-time mapping (:data:`MVP_INDEX_MAPPINGS`) — covers brand-new indices.
* Additive updates (:data:`MVP_ADDITIVE_MAPPING_UPDATES`) — the only path that
  reaches an index that already exists. A field added to the create-time
  mapping alone silently fails to deploy to every existing cluster, which is a
  distinct and easier mistake to make.

Then it happened a third time, on an index this file did not name. ``mvp_routes``
gained ``RoutePlan.window_misses`` (Req 5.2.3) with no mapping entry, so every
route document was rejected 400 while the agent logged "produced 4 route plans"
and the pipeline reported ``complete``; ``GET /plan/{run_id}`` returned
``route_plan: null`` and four dispatcher approvals pointed at routes that were
never stored. The guard existed and was correct — it just covered two indices out
of four, and an enumerated list of indices is a list somebody has to remember to
extend. :data:`_PERSISTED` below drives the same checks off every model/index pair
so the next index is covered by default rather than by diligence.
"""
from __future__ import annotations

from typing import Dict, Set

import pytest

from Agents.support.compartment_models import (
    Compartment,
    CompartmentAssignment,
    LoadingPlan,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriorityList,
    RoutePlan,
    TankForecast,
)
from Agents.support.mvp_es_mappings import (
    MVP_ADDITIVE_MAPPING_UPDATES,
    MVP_DELIVERY_PRIORITIES_INDEX,
    MVP_INDEX_MAPPINGS,
    MVP_LOAD_PLANS_INDEX,
    MVP_LOAD_PLANS_MAPPING,
    MVP_ROUTES_INDEX,
    MVP_TANK_FORECASTS_INDEX,
    TRUCK_COMPARTMENTS_INDEX,
    TRUCK_COMPARTMENTS_MAPPING,
)

#: Every index an agent writes a whole ``model_dump()`` into, and the model it
#: writes. Adding a persisted entity here is what puts it under the guard; the
#: assertions below are all parametrised over it.
_PERSISTED = {
    MVP_TANK_FORECASTS_INDEX: TankForecast,
    MVP_DELIVERY_PRIORITIES_INDEX: DeliveryPriorityList,
    MVP_LOAD_PLANS_INDEX: LoadingPlan,
    MVP_ROUTES_INDEX: RoutePlan,
    TRUCK_COMPARTMENTS_INDEX: Compartment,
}

_PERSISTED_PARAMS = sorted(_PERSISTED.items(), key=lambda kv: kv[0])


def _declared(index: str, mapping: Dict) -> Set[str]:
    """Top-level field names declared for ``index``, create-time + additive."""
    names = set(mapping.get("mappings", {}).get("properties", {}))
    extra = MVP_ADDITIVE_MAPPING_UPDATES.get(index, {}).get("properties", {})
    return names | set(extra)


def _declared_nested(index: str, mapping: Dict, parent: str) -> Set[str]:
    """Child field names of nested ``parent``, create-time + additive."""
    props = mapping.get("mappings", {}).get("properties", {})
    names = set(props.get(parent, {}).get("properties", {}))
    extra = (
        MVP_ADDITIVE_MAPPING_UPDATES.get(index, {})
        .get("properties", {})
        .get(parent, {})
        .get("properties", {})
    )
    return names | set(extra)


def test_load_plans_index_is_strict() -> None:
    """Guard the premise: if the index stopped being strict this test is moot.

    A non-strict index tolerates undeclared fields, so the failure mode
    described above would not occur and the assertions below would be
    enforcing a rule that no longer protects anything.
    """
    assert MVP_LOAD_PLANS_MAPPING["mappings"].get("dynamic") == "strict"


def test_loading_plan_fields_are_all_declared() -> None:
    """Every :class:`LoadingPlan` field exists in the mvp_load_plans mapping."""
    declared = _declared(MVP_LOAD_PLANS_INDEX, MVP_LOAD_PLANS_MAPPING)
    missing = sorted(set(LoadingPlan.model_fields) - declared)
    assert not missing, (
        f"LoadingPlan persists {missing} but mvp_load_plans does not declare "
        f"them. The index is dynamic:strict, so the ENTIRE write is rejected "
        f"and the agent only logs it — plans silently stop being stored while "
        f"the pipeline still reports success. Add them to "
        f"MVP_LOAD_PLANS_MAPPING and to MVP_ADDITIVE_MAPPING_UPDATES."
    )


def test_compartment_assignment_fields_are_all_declared() -> None:
    """Every :class:`CompartmentAssignment` field exists under ``assignments``."""
    declared = _declared_nested(
        MVP_LOAD_PLANS_INDEX, MVP_LOAD_PLANS_MAPPING, "assignments"
    )
    missing = sorted(set(CompartmentAssignment.model_fields) - declared)
    assert not missing, (
        f"CompartmentAssignment persists {missing} but the nested "
        f"'assignments' mapping does not declare them. ``product_code`` in "
        f"particular is what the axle-weight density lookup keys on, so "
        f"losing the write loses the DEF/diesel distinction entirely."
    )


def test_truck_compartments_index_is_strict() -> None:
    """Guard the premise for the compartment assertions below."""
    assert TRUCK_COMPARTMENTS_MAPPING["mappings"].get("dynamic") == "strict"


def test_compartment_fields_are_all_declared() -> None:
    """Every :class:`Compartment` field exists in the truck_compartments mapping.

    This index was outside the guard until ``allowed_product_codes`` was added
    for US product segregation — which is how the same class of defect the
    module docstring describes recurred on a second index.
    """
    declared = _declared(TRUCK_COMPARTMENTS_INDEX, TRUCK_COMPARTMENTS_MAPPING)
    missing = sorted(set(Compartment.model_fields) - declared)
    assert not missing, (
        f"Compartment persists {missing} but truck_compartments does not "
        f"declare them. The index is dynamic:strict, so the ENTIRE compartment "
        f"write is rejected — the operator loses the whole record, not just "
        f"the new field. Add them to TRUCK_COMPARTMENTS_MAPPING and to "
        f"MVP_ADDITIVE_MAPPING_UPDATES."
    )


def test_compartment_fields_reach_a_brand_new_index() -> None:
    """Create-time mapping specifically, not create-time-or-additive.

    :func:`_declared` unions both tables, so a field present in only one of them
    satisfies it. That is deliberately lenient for the older assertions, but it
    cannot catch either single-sided mistake, and the two fail on opposite
    deployments:

    * additive only  -> missing on a brand-new cluster, because
      ``setup_mvp_indices`` creates the index from the create-time mapping and
      applies additive updates only on the ``else`` branch.
    * create-time only -> missing on every existing cluster.

    This pins the first direction; ``test_allowed_product_codes_reaches_``
    ``existing_indices`` pins the second.
    """
    create_time = set(
        TRUCK_COMPARTMENTS_MAPPING["mappings"]["properties"]
    )
    missing = sorted(set(Compartment.model_fields) - create_time)
    assert not missing, (
        f"Compartment persists {missing} but TRUCK_COMPARTMENTS_MAPPING does "
        f"not declare them, so a brand-new cluster gets an index without them. "
        f"Being in MVP_ADDITIVE_MAPPING_UPDATES does not help: additive updates "
        f"only run when the index already exists."
    )


def test_allowed_product_codes_reaches_existing_indices() -> None:
    """The create-time mapping alone never reaches a deployed cluster.

    ``allowed_product_codes`` is what lets a tenant say "this compartment takes
    heating oil but not road diesel". Those two carry different tax classes, so
    a silently-rejected write is not cosmetic: the compartment reverts to
    family eligibility and the segregation rule stops distinguishing them.
    """
    additive = (
        MVP_ADDITIVE_MAPPING_UPDATES.get(TRUCK_COMPARTMENTS_INDEX, {})
        .get("properties", {})
    )
    assert "allowed_product_codes" in additive, (
        "allowed_product_codes is missing from MVP_ADDITIVE_MAPPING_UPDATES"
        f"[{TRUCK_COMPARTMENTS_INDEX!r}]. Every existing cluster already has "
        "truck_compartments, so only the additive path can add the field there."
    )


@pytest.mark.parametrize(
    "field",
    ["product_code", "contract_id", "terminal_id"],
)
def test_regressed_fields_reach_existing_indices(field: str) -> None:
    """The three fields that broke production must be in the ADDITIVE table.

    Declaring a field only in the create-time mapping is the subtle half of
    this bug: ``indices.create`` is skipped when the index already exists, so
    every deployed cluster keeps the old mapping and keeps rejecting writes.
    Pinning these three by name documents the specific regression.
    """
    additive = MVP_ADDITIVE_MAPPING_UPDATES.get(MVP_LOAD_PLANS_INDEX, {})
    props = additive.get("properties", {})
    nested = props.get("assignments", {}).get("properties", {})
    assert field in props or field in nested, (
        f"{field!r} is missing from MVP_ADDITIVE_MAPPING_UPDATES for "
        f"{MVP_LOAD_PLANS_INDEX}. Without it, existing clusters never gain the "
        f"field and load-plan writes keep failing there even though a fresh "
        f"index would work."
    )


def test_every_strict_mvp_mapping_declares_its_nested_children() -> None:
    """No strict MVP index has a nested object with zero declared children.

    A nested field with no properties accepts nothing under a strict index, so
    it is always a mistake rather than a deliberate "open" object.
    """
    offenders = []
    for index, mapping in MVP_INDEX_MAPPINGS.items():
        mappings = mapping.get("mappings", {})
        if mappings.get("dynamic") != "strict":
            continue
        for name, spec in mappings.get("properties", {}).items():
            if spec.get("type") == "nested" and not spec.get("properties"):
                offenders.append(f"{index}.{name}")
    assert not offenders, (
        "Nested field(s) with no declared children under a dynamic:strict "
        f"index: {offenders}. Every write touching them will be rejected."
    )


# ---------------------------------------------------------------------------
# The same three checks, driven off _PERSISTED so no index is left out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index,model", _PERSISTED_PARAMS)
def test_every_persisted_index_is_strict(index, model) -> None:
    """Guard the premise for each index rather than for two of them.

    A non-strict index tolerates undeclared fields, so the assertions below
    would be enforcing a rule that no longer protects anything. If an index is
    deliberately made dynamic, remove it from ``_PERSISTED`` and say why.
    """
    assert MVP_INDEX_MAPPINGS[index]["mappings"].get("dynamic") == "strict", (
        f"{index} is no longer dynamic:strict — this file is not measuring "
        f"anything for {model.__name__}"
    )


@pytest.mark.parametrize("index,model", _PERSISTED_PARAMS)
def test_every_persisted_model_field_is_declared(index, model) -> None:
    """Create-time or additive — the field must be declared somewhere."""
    declared = _declared(index, MVP_INDEX_MAPPINGS[index])
    missing = sorted(set(model.model_fields) - declared)
    assert not missing, (
        f"{model.__name__} persists {missing} but {index} does not declare "
        f"them. {index} is dynamic:strict, so ES rejects the ENTIRE document: "
        f"the whole entity stops being stored while the agent logs the error "
        f"and the pipeline still reports success. Add them to the index "
        f"mapping AND to MVP_ADDITIVE_MAPPING_UPDATES."
    )


@pytest.mark.parametrize("index,model", _PERSISTED_PARAMS)
def test_every_persisted_model_field_reaches_a_brand_new_index(
    index, model
) -> None:
    """Create-time specifically: additive updates never run on a fresh index."""
    create_time = set(MVP_INDEX_MAPPINGS[index]["mappings"]["properties"])
    missing = sorted(set(model.model_fields) - create_time)
    assert not missing, (
        f"{model.__name__} persists {missing} but the create-time mapping for "
        f"{index} does not declare them, so a brand-new cluster gets an index "
        f"without them. Being in MVP_ADDITIVE_MAPPING_UPDATES does not help: "
        f"setup_mvp_indices applies those only on the already-exists branch."
    )


# The additive half cannot be checked generically: only fields absent from a
# *deployed* mapping need an entry, and this file cannot know what a given
# cluster has. It stays pinned by name, for the fields that actually broke —
# below for mvp_routes, and above for mvp_load_plans and truck_compartments.


def test_window_misses_reaches_existing_indices() -> None:
    """The field that broke mvp_routes, pinned by name on the additive path.

    Every cluster already has ``mvp_routes``, so the create-time mapping alone
    would leave all of them rejecting every route write.
    """
    additive = MVP_ADDITIVE_MAPPING_UPDATES.get(MVP_ROUTES_INDEX, {}).get(
        "properties", {}
    )
    assert "window_misses" in additive, (
        f"window_misses is missing from MVP_ADDITIVE_MAPPING_UPDATES"
        f"[{MVP_ROUTES_INDEX!r}]. Without it every deployed cluster keeps the "
        f"strict mapping it was created with and keeps discarding whole route "
        f"documents."
    )


def test_additive_updates_agree_with_the_create_time_types() -> None:
    """Counterweight: a contradicting put-mapping turns a fix into a boot failure.

    ES accepts new fields but rejects a type change on an existing one, so a
    mismatch here would make ``setup_mvp_indices`` raise on startup for every
    cluster that already has the index.
    """
    for index, update in MVP_ADDITIVE_MAPPING_UPDATES.items():
        if index not in MVP_INDEX_MAPPINGS:
            continue
        created = MVP_INDEX_MAPPINGS[index]["mappings"]["properties"]
        for field, spec in update.get("properties", {}).items():
            if field not in created:
                continue
            assert created[field].get("type") == spec.get("type"), (
                f"{index}.{field}: create-time mapping says "
                f"{created[field].get('type')!r} but the additive update says "
                f"{spec.get('type')!r} — put_mapping will be rejected"
            )
