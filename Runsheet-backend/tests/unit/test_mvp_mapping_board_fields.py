"""Board plan and route documents are fully declared in the MVP mappings (P10, K2.4).

The store does not enforce ``dynamic: strict``, so the mapping is the reference
for the exact keys readers use and for the field policy. This builds one board
plan and one board route exactly as design K7.3a writes them (plus the recovery
markers of freeze rule 10) and checks every key path against the create-time
mapping, and every field the board adds against ``MVP_ADDITIVE_MAPPING_UPDATES``
as well, the way ``product_code`` and ``window_misses`` were added.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Set

import pytest

from Agents.support.mvp_es_mappings import (
    MVP_ADDITIVE_MAPPING_UPDATES,
    MVP_LOAD_PLANS_INDEX,
    MVP_LOAD_PLANS_MAPPING,
    MVP_PLAN_EXECUTIONS_INDEX,
    MVP_PLAN_EXECUTIONS_MAPPING,
    MVP_ROUTES_INDEX,
    MVP_ROUTES_MAPPING,
)

NOW = "2026-10-06T07:00:00+00:00"

BOARD_PLAN_FIELDS = {
    "source", "board_draft_id", "board_load_id", "service_date", "shift_id",
    "load_seq", "driver_id", "revision", "supersedes_plan_id", "superseded_by_plan_id",
}
BOARD_ROUTE_FIELDS = {"source", "board_load_id", "service_date", "revision", "supersedes_route_id"}
RECOVERY_FIELDS = {"superseded_by_attempt", "status_before_retire", "created_by_attempt"}


def _board_plan() -> Dict[str, Any]:
    """``mvp_load_plans/{plan_id}`` as K7.3a writes it, revision 2."""
    plan_id = "bp-load1-r2"
    return {
        "plan_id": plan_id,
        "run_id": plan_id,
        "tenant_id": "tenant-1",
        "truck_id": "truck-1",
        "terminal_id": "term-1",
        "status": "draft",
        "source": "dispatch_board",
        "board_draft_id": "tenant-1:2026-10-06",
        "board_load_id": "load1",
        "service_date": "2026-10-06",
        "shift_id": "day",
        "load_seq": 1,
        "driver_id": "drv-1",
        "revision": 2,
        "supersedes_plan_id": "bp-load1-r1",
        "superseded_by_plan_id": None,
        "created_at": NOW,
        "updated_at": NOW,
        "assignments": [
            {
                "order_id": "ord-1",
                "station_id": "cust-1",
                "product_code": "DIESEL_2",
                "fuel_grade": "DIESEL_2",
                "compartment_id": "c1",
                "quantity_liters": 3785.0,
                "compartment_capacity_liters": 5000.0,
            }
        ],
        # Recovery markers (freeze rule 10).
        "superseded_by_attempt": "att-1",
        "status_before_retire": "dispatched",
        "created_by_attempt": "att-2",
    }


def _board_route() -> Dict[str, Any]:
    """``mvp_routes/{route_id}`` as K7.3a writes it, revision 2."""
    return {
        "route_id": "br-load1-r2",
        "plan_id": "bp-load1-r2",
        "run_id": "bp-load1-r2",
        "tenant_id": "tenant-1",
        "truck_id": "truck-1",
        "status": "planned",
        "timestamp": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "distance_km": 42.5,
        "source": "dispatch_board",
        "board_load_id": "load1",
        "service_date": "2026-10-06",
        "revision": 2,
        "supersedes_route_id": "br-load1-r1",
        "stops": [
            {
                "sequence": 1,
                "station_id": "tank-1",
                "order_ids": ["ord-1"],
                "eta": "2026-10-06T09:00:00+00:00",
                "drop": {"DIESEL_2": 3785.0},
            }
        ],
        "superseded_by_attempt": "att-1",
        "status_before_retire": "dispatched",
        "created_by_attempt": "att-2",
    }


def _paths(doc: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(doc, list):
        for item in doc:
            yield from _paths(item, prefix)
        return
    if not isinstance(doc, dict):
        return
    for key, value in doc.items():
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, (dict, list)):
            yield from _paths(value, f"{path}.")


def _declared(properties: Dict[str, Any], prefix: str = "") -> Set[str]:
    """Declared paths; a ``dynamic: True`` object declares its whole subtree."""
    out: Set[str] = set()
    for name, spec in properties.items():
        path = f"{prefix}{name}"
        out.add(path)
        if spec.get("dynamic") is True:
            out.add(f"{path}.*")
        if "properties" in spec:
            out |= _declared(spec["properties"], f"{path}.")
    return out


def _covered(path: str, declared: Set[str]) -> bool:
    if path in declared:
        return True
    parts = path.split(".")
    return any(".".join(parts[:i]) + ".*" in declared for i in range(1, len(parts)))


@pytest.mark.parametrize(
    "doc, mapping",
    [
        (_board_plan(), MVP_LOAD_PLANS_MAPPING),
        (_board_route(), MVP_ROUTES_MAPPING),
    ],
    ids=["plan", "route"],
)
def test_every_key_path_is_declared_in_the_create_time_mapping(doc, mapping):
    declared = _declared(mapping["mappings"]["properties"])
    missing = sorted(p for p in set(_paths(doc)) if not _covered(p, declared))
    assert missing == []


@pytest.mark.parametrize(
    "index, fields",
    [
        (MVP_LOAD_PLANS_INDEX, BOARD_PLAN_FIELDS | RECOVERY_FIELDS),
        (MVP_ROUTES_INDEX, BOARD_ROUTE_FIELDS | RECOVERY_FIELDS),
        (MVP_PLAN_EXECUTIONS_INDEX, RECOVERY_FIELDS),
    ],
)
def test_board_fields_are_in_the_additive_updates(index, fields):
    additive = MVP_ADDITIVE_MAPPING_UPDATES[index]["properties"]
    assert fields <= set(additive)


@pytest.mark.parametrize(
    "index, mapping, fields",
    [
        (MVP_LOAD_PLANS_INDEX, MVP_LOAD_PLANS_MAPPING, BOARD_PLAN_FIELDS | RECOVERY_FIELDS),
        (MVP_ROUTES_INDEX, MVP_ROUTES_MAPPING, BOARD_ROUTE_FIELDS | RECOVERY_FIELDS),
        (MVP_PLAN_EXECUTIONS_INDEX, MVP_PLAN_EXECUTIONS_MAPPING, RECOVERY_FIELDS),
    ],
)
def test_create_time_and_additive_types_agree(index, mapping, fields):
    create = mapping["mappings"]["properties"]
    additive = MVP_ADDITIVE_MAPPING_UPDATES[index]["properties"]
    for field in fields:
        assert create[field] == additive[field], field


def test_service_date_and_revision_types():
    plan = MVP_LOAD_PLANS_MAPPING["mappings"]["properties"]
    assert plan["service_date"] == {"type": "date"}
    assert plan["revision"] == {"type": "integer"}
    assert plan["load_seq"] == {"type": "integer"}
    assert plan["source"] == {"type": "keyword"}
