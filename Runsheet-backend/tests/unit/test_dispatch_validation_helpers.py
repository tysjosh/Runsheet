"""Golden tests for the helpers extracted from ``RoutePlanningAgent`` (dispatch-board task 6).

The expected values were captured from the agent's private methods before the
extraction. The agent now delegates to ``fuel.services.dispatch_validation``,
so both paths must keep producing exactly these outputs (N7: agent behaviour
unchanged; R8.1: the board and the agent compute the same requirements).
"""
from __future__ import annotations

import pytest

from Agents.overlay.route_planning_agent import RoutePlanningAgent
from Agents.support import route_solver
from fuel.services.dispatch_validation import (
    build_route_requirements,
    estimate_route_hours,
    resolve_stop_locations,
)

_A = {"requires_hazmat": True, "requires_tanker": True, "min_cdl_class": "A"}
_TANKER_ONLY = {"requires_hazmat": False, "requires_tanker": True, "min_cdl_class": "A"}

REQUIREMENT_GOLDENS = [
    ([], {"requires_hazmat": False, "requires_tanker": False, "min_cdl_class": None}),
    ([{"fuel_grade": ""}], _TANKER_ONLY),
    ([{"fuel_grade": "DIESEL_2"}], _A),
    ([{"fuel_grade": "DEF"}], _TANKER_ONLY),
    ([{"fuel_grade": "nonsense-grade"}], _A),
    ([{"fuel_grade": "DEF"}, {"fuel_grade": "UNL_87"}], _A),
    ([{"fuel_grade": "PROPANE"}], _A),
    ([{"fuel_grade": "AGO"}], _A),
    ([{"fuel_grade": "PMS"}], _A),
]

HOURS_GOLDENS = [
    (0, (0.0, 0.0)),
    (1, (1.2, 1.7)),
    (2, (1.8, 2.8)),
    (5, (3.6, 6.1)),
    (13, (8.4, 14.9)),
]

LOCATION_INPUT = dict(
    station_ids=["s1", "s2", "s3", "s4"],
    station_locations={"s1": {"lat": 1, "lon": 1}, "s2": {"lat": 2, "lon": 2}, "s4": {"lat": 4, "lon": 4}},
    order_ids_by_station={"s1": ["o1"], "s2": ["o2", "o3"], "s3": ["o4"]},
    order_stop_locations={"o1": {"lat": 10, "lon": 10}, "o3": {"lat": 30, "lon": 30}},
)
LOCATION_GOLDEN = {"s1": {"lat": 10, "lon": 10}, "s2": {"lat": 30, "lon": 30}, "s4": {"lat": 4, "lon": 4}}


@pytest.fixture
def agent():
    return RoutePlanningAgent.__new__(RoutePlanningAgent)


@pytest.mark.parametrize("assignments, expected", REQUIREMENT_GOLDENS)
def test_route_requirements_golden(agent, assignments, expected):
    assert build_route_requirements(assignments) == expected
    assert agent._build_route_requirements(assignments) == expected


@pytest.mark.parametrize("stops, expected", HOURS_GOLDENS)
def test_route_hours_golden(agent, stops, expected):
    assignments = [{}] * stops
    assert estimate_route_hours(assignments) == pytest.approx(expected)
    assert agent._estimate_route_hours(assignments) == pytest.approx(expected)


def test_stop_locations_golden():
    assert resolve_stop_locations(**LOCATION_INPUT) == LOCATION_GOLDEN
    assert RoutePlanningAgent._resolve_stop_locations(**LOCATION_INPUT) == LOCATION_GOLDEN


def test_resolved_locations_are_copies():
    resolved = resolve_stop_locations(**LOCATION_INPUT)
    resolved["s1"]["lat"] = 99
    assert LOCATION_INPUT["order_stop_locations"]["o1"]["lat"] == 10


def test_agent_constants_still_drive_its_estimate(agent, monkeypatch):
    monkeypatch.setattr(RoutePlanningAgent, "_AVERAGE_SPEED_MPH", 50.0)
    assert agent._estimate_route_hours([{}]) == pytest.approx((0.6, 1.1))


def test_recompute_etas_public_alias():
    assert route_solver.recompute_etas is route_solver._recompute_etas
