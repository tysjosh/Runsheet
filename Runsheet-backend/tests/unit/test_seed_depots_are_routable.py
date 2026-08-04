"""A freshly seeded tenant must be able to plan a route.

``Route_Planning_Agent`` resolves its start position through
``make_depot_start_resolver``, whose chain is:

    truck.assigned_depot_id → tenant_settings.default_depot_id →
    a tenant depot with ``is_default`` → None

Returning ``None`` makes the agent report ``no_depot_configured`` and skip the
loading plan. On a freshly seeded environment all three steps missed: the seeded
trucks carry no ``assigned_depot_id``, nothing writes
``tenant_settings.default_depot_id``, and none of the three seeded depots set
``is_default``. So ``POST /plan/generate`` came back ``degraded`` with
``route_planning: no_depot_configured`` every time, and the only way to get a
route was to hand-patch a depot document.

That is a seed gap, not agent behaviour, and it belongs in the fixture where a
fresh clone picks it up. This pins it: exactly one seeded depot is the default,
and it is routable — active, with real coordinates. ``location_lat``/
``location_lon`` matter because ``(0.0, 0.0)`` is the null-island sentinel the
resolver exists to avoid.

The "exactly one" half is not decoration: ``DepotRepository`` enforces
single-default-per-tenant on create and update, so a fixture with two defaults
seeds a state the repository would never produce, and which depot wins becomes
ES result-order luck.
"""
from __future__ import annotations

import json
import pathlib

import pytest

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "data"
    / "fuel_ops_seeds.json"
)


@pytest.fixture(scope="module")
def depots():
    with _FIXTURE.open() as fh:
        seeds = json.load(fh)
    assert "depots" in seeds, f"no depots key in {_FIXTURE.name}"
    return seeds["depots"]


@pytest.fixture(scope="module")
def default_depot(depots):
    defaults = [d for d in depots if d.get("is_default") is True]
    assert len(defaults) == 1, (
        "route planning resolves its origin from the single is_default depot; "
        f"fixture has {len(defaults)}: "
        f"{[d.get('depot_id') for d in defaults]}"
    )
    return defaults[0]


def test_exactly_one_seeded_depot_is_the_default(default_depot):
    assert default_depot.get("depot_id")


def test_the_default_depot_is_active(default_depot):
    """The resolver skips non-active depots, so an inactive default is no default."""
    default = default_depot
    assert default.get("status") == "active", (
        f"{default.get('depot_id')} is the default but status="
        f"{default.get('status')!r}; the resolver only routes from active depots"
    )


def test_the_default_depot_has_real_coordinates(default_depot):
    default = default_depot
    lat, lon = default.get("location_lat"), default.get("location_lon")
    assert isinstance(lat, (int, float)) and isinstance(lon, (int, float)), (
        f"{default.get('depot_id')}: location_lat/location_lon must be numeric, "
        f"got {lat!r}/{lon!r}"
    )
    assert (lat, lon) != (0.0, 0.0), "null island is the sentinel, not a depot"
    assert -90 <= lat <= 90 and -180 <= lon <= 180


def test_every_seeded_depot_carries_the_fields_the_resolver_reads(depots):
    """Counterweight: the default is not special-cased into being the only valid one.

    Any of these depots can be reached via ``truck.assigned_depot_id`` or
    ``tenant_settings.default_depot_id``, so all of them need coordinates.
    """
    assert depots, "fixture seeds no depots at all"
    for depot in depots:
        assert depot.get("depot_id"), f"depot without an id: {depot}"
        for field in ("location_lat", "location_lon", "status"):
            assert field in depot, f"{depot['depot_id']} is missing {field}"
