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
"""
from __future__ import annotations

from typing import Dict, Set

import pytest

from Agents.support.compartment_models import CompartmentAssignment, LoadingPlan
from Agents.support.mvp_es_mappings import (
    MVP_ADDITIVE_MAPPING_UPDATES,
    MVP_INDEX_MAPPINGS,
    MVP_LOAD_PLANS_INDEX,
    MVP_LOAD_PLANS_MAPPING,
)


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
