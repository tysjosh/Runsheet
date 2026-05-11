from __future__ import annotations

from fuel.services.allocation_engine import (
    AllocationEngine,
    AllocationPolicy,
    AllocationRequest,
)


def _request(customer_id: str, **overrides):
    payload = {
        "tenant_id": "tenant-A",
        "customer_id": customer_id,
        "product_code": "DIESEL_2",
        "requested_gallons": 500.0,
        "criticality_tier": "standard",
    }
    payload.update(overrides)
    return AllocationRequest(**payload)


def test_critical_infrastructure_is_allocated_before_standard_customer():
    engine = AllocationEngine()
    policy = AllocationPolicy(
        tenant_id="tenant-A",
        product_code="DIESEL_2",
        available_gallons=500.0,
    )

    decisions = engine.allocate(
        policy=policy,
        requests=[
            _request("standard"),
            _request(
                "hospital",
                criticality_tier="medical",
                is_generator_fuel=True,
                requires_continuous_service=True,
            ),
        ],
    )

    assert [d.customer_id for d in decisions] == ["hospital", "standard"]
    assert decisions[0].approved_gallons == 500.0
    assert decisions[0].rationed_gallons == 0.0
    assert "critical_infrastructure" in decisions[0].reason_codes
    assert "generator_fuel" in decisions[0].reason_codes
    assert decisions[1].approved_gallons == 0.0
    assert decisions[1].reason_codes == ["rationed_none"]


def test_runout_risk_breaks_ties_within_same_tier():
    engine = AllocationEngine()
    policy = AllocationPolicy(
        tenant_id="tenant-A",
        product_code="PROPANE",
        available_gallons=300.0,
    )

    decisions = engine.allocate(
        policy=policy,
        requests=[
            _request(
                "safe",
                product_code="LPG",
                requested_gallons=300.0,
                criticality_tier="commercial",
                hours_to_runout_p90=72,
            ),
            _request(
                "runout",
                product_code="PROPANE",
                requested_gallons=300.0,
                criticality_tier="commercial",
                hours_to_runout_p90=8,
            ),
        ],
    )

    assert [d.customer_id for d in decisions] == ["runout", "safe"]
    assert decisions[0].approved_gallons == 300.0
    assert "runout_risk" in decisions[0].reason_codes
    assert decisions[1].approved_gallons == 0.0


def test_filters_to_policy_tenant_and_product():
    engine = AllocationEngine()
    policy = AllocationPolicy(
        tenant_id="tenant-A",
        product_code="DEF",
        available_gallons=100.0,
    )

    decisions = engine.allocate(
        policy=policy,
        requests=[
            _request("wrong-product", product_code="DIESEL_2"),
            _request("wrong-tenant", tenant_id="tenant-B", product_code="DEF"),
            _request("kept", product_code="DEF", requested_gallons=75.0),
        ],
    )

    assert [d.customer_id for d in decisions] == ["kept"]
    assert decisions[0].approved_gallons == 75.0
