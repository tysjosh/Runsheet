"""Unit + property tests for the uniform compliance ``subject_ref``.

Covers cross-module-entity-linkage task 10:
* the :class:`SubjectRef` value object + per-kind derivation
  (:func:`subject_ref_for_kind`),
* the model-side ``subject_ref`` properties on every compliance record kind
  (certification → asset, meter → asset, IFTA → asset, terminal BOL → driver,
  exemption → customer/account, k-factor → tank),
* write-time validation against the shared ``RefResolver``
  (:func:`validate_subject_ref`) and read-time resolution
  (:func:`resolve_subject_ref`),
* a property test asserting tenant containment + validation soundness for the
  derived subject (Property 2 / Property 5).

Feature: cross-module-entity-linkage, Requirement 11.1
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Dict, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from errors.exceptions import AppException
from services.ref_resolver import RefResolver
from compliance.services.compliance_subject_ref import (
    SUBJECT_TYPE_BY_KIND,
    SubjectRef,
    resolve_subject_ref,
    subject_ref_for_kind,
    validate_subject_ref,
)


# ---------------------------------------------------------------------------
# In-memory fake store + resolver (mirrors test_ref_resolver.py)
# ---------------------------------------------------------------------------


class FakeStore:
    """A tiny tenant-scoped store: {tenant_id: {entity_id: summary}}."""

    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def put(self, tenant_id: str, entity_id: str, summary: Dict[str, Any]) -> None:
        self.data.setdefault(tenant_id, {})[entity_id] = summary

    def loader(self):
        async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
            return self.data.get(tenant_id, {}).get(entity_id)

        return _load


def _resolver_with(*types: str, tenant: str = "t1") -> tuple[RefResolver, FakeStore]:
    store = FakeStore()
    resolver = RefResolver()
    for t in types:
        resolver.register(t, store.loader())
    return resolver, store


# ---------------------------------------------------------------------------
# SubjectRef value object
# ---------------------------------------------------------------------------


def test_subject_ref_to_dict_round_trips_fields():
    ref = SubjectRef(subject_type="asset", subject_id="AST-1")
    assert ref.to_dict() == {"subject_type": "asset", "subject_id": "AST-1"}


def test_subject_ref_strips_whitespace_id():
    ref = SubjectRef(subject_type="driver", subject_id="  DRV-9  ")
    assert ref.subject_id == "DRV-9"


def test_subject_ref_rejects_empty_id():
    with pytest.raises(ValueError):
        SubjectRef(subject_type="tank", subject_id="   ")


def test_subject_ref_is_frozen():
    ref = SubjectRef(subject_type="asset", subject_id="AST-1")
    with pytest.raises(Exception):
        ref.subject_id = "AST-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# subject_ref_for_kind — per record-kind mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected_type",
    [
        ("certification", "asset"),
        ("meter", "asset"),
        ("ifta", "asset"),
        ("terminal_bol", "driver"),
        ("exemption", "customer"),
        ("pricing", "customer"),
        ("contract", "customer"),
        ("kfactor", "tank"),
    ],
)
def test_subject_ref_for_kind_maps_default_type(kind, expected_type):
    ref = subject_ref_for_kind(kind, subject_id="ID-1")
    assert ref is not None
    assert ref.subject_type == expected_type
    assert ref.subject_id == "ID-1"


def test_account_scopable_prefers_account_when_present():
    ref = subject_ref_for_kind("exemption", subject_id="CUST-1", account_id="ACCT-9")
    assert ref == SubjectRef(subject_type="account", subject_id="ACCT-9")


def test_account_scopable_falls_back_to_customer_without_account():
    ref = subject_ref_for_kind("exemption", subject_id="CUST-1", account_id="  ")
    assert ref == SubjectRef(subject_type="customer", subject_id="CUST-1")


def test_non_account_kind_ignores_account_id():
    # An asset-subject record never becomes account-scoped.
    ref = subject_ref_for_kind("certification", subject_id="AST-1", account_id="ACCT-9")
    assert ref == SubjectRef(subject_type="asset", subject_id="AST-1")


def test_subject_ref_for_kind_returns_none_without_id():
    assert subject_ref_for_kind("certification", subject_id=None) is None
    assert subject_ref_for_kind("certification", subject_id="   ") is None


def test_subject_ref_for_unknown_kind_raises():
    with pytest.raises(KeyError):
        subject_ref_for_kind("nonsense", subject_id="X")


def test_every_known_kind_has_a_registered_loader_type():
    # The subject types must line up with the resolver's loader entity types so
    # no compliance-specific loader is needed (design §Data Models).
    valid_loader_types = {"asset", "driver", "customer", "account", "tank"}
    assert set(SUBJECT_TYPE_BY_KIND.values()) <= valid_loader_types


# ---------------------------------------------------------------------------
# Model-side subject_ref properties
# ---------------------------------------------------------------------------


def test_asset_certification_subject_ref_is_asset():
    from compliance.models.asset_certification import AssetCertification

    cert = AssetCertification(
        tenant_id="t1",
        asset_id="AST-1",
        certification_type="V_test",
        certification_date=date(2026, 1, 1),
        expiry_date=date(2027, 1, 1),
        inspector_name="Jane",
        certificate_number="C-1",
    )
    assert cert.subject_ref == SubjectRef(subject_type="asset", subject_id="AST-1")


def test_meter_registration_subject_ref_is_asset_via_truck_id():
    from compliance.models.meter import MeterRegistration

    meter = MeterRegistration(
        tenant_id="t1",
        meter_number="M-1",
        truck_id="AST-7",
        calibration_certificate_number="CAL-1",
        calibration_date=date(2026, 1, 1),
        calibration_expiry_date=date(2027, 1, 1),
        weights_measures_authority="NIST",
    )
    assert meter.subject_ref == SubjectRef(subject_type="asset", subject_id="AST-7")


def test_terminal_bol_subject_ref_is_driver():
    from compliance.models.terminal_bol import TerminalBOL

    bol = TerminalBOL(
        tenant_id="t1",
        load_number="L-1",
        product_code="ULSD",
        gross_gallons=1000.0,
        net_gallons=998.0,
        observed_temperature_f=60.0,
        api_gravity=35.0,
        supplier_name="Acme Supply",
        terminal_name="Newark Rack",
        driver_id="DRV-3",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    assert bol.subject_ref == SubjectRef(subject_type="driver", subject_id="DRV-3")


def test_tax_exemption_subject_ref_is_customer_by_default():
    from compliance.models.tax_exemption import TaxExemption

    ex = TaxExemption(
        tenant_id="t1",
        customer_id="CUST-1",
        exemption_type="farm",
        certificate_number="E-1",
        expiry_date=date(2030, 1, 1),
    )
    assert ex.subject_ref == SubjectRef(subject_type="customer", subject_id="CUST-1")


def test_tax_exemption_subject_ref_is_account_when_account_scoped():
    from compliance.models.tax_exemption import TaxExemption

    ex = TaxExemption(
        tenant_id="t1",
        customer_id="CUST-1",
        account_id="ACCT-9",
        exemption_type="farm",
        certificate_number="E-1",
        expiry_date=date(2030, 1, 1),
    )
    assert ex.subject_ref == SubjectRef(subject_type="account", subject_id="ACCT-9")


def test_ifta_trip_segment_subject_ref_is_asset():
    from compliance.services.ifta_reporter import TripSegment

    seg = TripSegment(
        tenant_id="t1",
        truck_id="AST-2",
        jurisdiction="TX",
        miles=120.0,
        quarter="2026-Q1",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
    )
    assert seg.subject_ref == SubjectRef(subject_type="asset", subject_id="AST-2")


def test_kfactor_adjustment_subject_ref_is_tank():
    from compliance.services.kfactor_calibration_service import KFactorAdjustment

    adj = KFactorAdjustment(
        tank_id="tank_5",
        tenant_id="t1",
        old_kfactor=1.0,
        new_kfactor=1.2,
        operator_id="op-1",
    )
    assert adj.subject_ref == SubjectRef(subject_type="tank", subject_id="tank_5")


# ---------------------------------------------------------------------------
# validate_subject_ref — write-time guard
# ---------------------------------------------------------------------------


def test_validate_subject_ref_passes_for_existing_same_tenant():
    resolver, store = _resolver_with("asset")
    store.put("t1", "AST-1", {"name": "Truck 1"})
    ref = SubjectRef(subject_type="asset", subject_id="AST-1")
    # Should not raise.
    asyncio.run(validate_subject_ref(resolver, "t1", ref))


def test_validate_subject_ref_rejects_missing_subject():
    resolver, _ = _resolver_with("asset")
    ref = SubjectRef(subject_type="asset", subject_id="AST-NOPE")
    with pytest.raises(AppException) as exc:
        asyncio.run(validate_subject_ref(resolver, "t1", ref))
    assert exc.value.details["reason"] == "asset_not_found"


def test_validate_subject_ref_rejects_cross_tenant_subject():
    resolver, store = _resolver_with("driver")
    store.put("t1", "DRV-1", {"driver_name": "Jane"})
    ref = SubjectRef(subject_type="driver", subject_id="DRV-1")
    # Caller in another tenant must not resolve t1's driver (Property 2).
    with pytest.raises(AppException) as exc:
        asyncio.run(validate_subject_ref(resolver, "t2", ref))
    assert exc.value.details["reason"] == "driver_not_found"


def test_validate_subject_ref_none_required_raises():
    resolver, _ = _resolver_with("asset")
    with pytest.raises(AppException) as exc:
        asyncio.run(validate_subject_ref(resolver, "t1", None, required=True))
    assert exc.value.details["reason"] == "subject_ref_required"


def test_validate_subject_ref_none_optional_ok():
    resolver, _ = _resolver_with("asset")
    asyncio.run(validate_subject_ref(resolver, "t1", None, required=False))


def test_validate_subject_ref_skips_when_loader_unregistered():
    # No 'tank' loader registered → partially-wired environment skips validation
    # so the reference persists unvalidated rather than failing (additive).
    resolver, _ = _resolver_with("asset")
    ref = SubjectRef(subject_type="tank", subject_id="tank_x")
    asyncio.run(validate_subject_ref(resolver, "t1", ref))  # no raise


def test_validate_subject_ref_skips_when_no_resolver():
    ref = SubjectRef(subject_type="asset", subject_id="AST-1")
    asyncio.run(validate_subject_ref(None, "t1", ref))  # no raise


# ---------------------------------------------------------------------------
# resolve_subject_ref — read-time resolution
# ---------------------------------------------------------------------------


def test_resolve_subject_ref_returns_summary():
    resolver, store = _resolver_with("tank")
    store.put("t1", "tank_1", {"customer_id": "CUST-1", "status": "active"})
    ref = SubjectRef(subject_type="tank", subject_id="tank_1")
    resolved = asyncio.run(resolve_subject_ref(resolver, "t1", ref))
    assert resolved.is_resolved
    assert resolved.to_dict() == {
        "status": "resolved",
        "id": "tank_1",
        "summary": {"customer_id": "CUST-1", "status": "active"},
    }


def test_resolve_subject_ref_unresolved_marker_not_dropped():
    resolver, _ = _resolver_with("asset")
    ref = SubjectRef(subject_type="asset", subject_id="AST-GONE")
    resolved = asyncio.run(resolve_subject_ref(resolver, "t1", ref))
    assert not resolved.is_resolved
    assert resolved.to_dict() == {"status": "unresolved", "id": "AST-GONE"}


def test_resolve_subject_ref_none_is_empty():
    resolver, _ = _resolver_with("asset")
    resolved = asyncio.run(resolve_subject_ref(resolver, "t1", None))
    assert resolved.status == "empty"
    assert resolved.to_dict() == {"status": "empty", "id": None}


# ---------------------------------------------------------------------------
# Property: tenant containment + validation soundness for the derived subject
# Feature: cross-module-entity-linkage, Property 2 & 5
# ---------------------------------------------------------------------------

_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=12
)
_tenants = st.sampled_from(["tenant-a", "tenant-b", "tenant-c"])
_kinds = st.sampled_from(sorted(SUBJECT_TYPE_BY_KIND))


@settings(max_examples=100)
@given(owner=_tenants, caller=_tenants, entity_id=_ids, kind=_kinds)
def test_property_subject_ref_tenant_containment(owner, caller, entity_id, kind):
    """A derived subject_ref validates iff caller tenant == owner tenant, and
    resolution never leaks a subject across tenants (Property 2 / Property 5)."""
    subject_ref = subject_ref_for_kind(kind, subject_id=entity_id)
    assert subject_ref is not None
    subject_type = subject_ref.subject_type

    store = FakeStore()
    store.put(owner, entity_id, {"display_name": "X"})
    resolver = RefResolver()
    resolver.register(subject_type, store.loader())

    resolved = asyncio.run(resolve_subject_ref(resolver, caller, subject_ref))

    if caller == owner:
        assert resolved.is_resolved  # Property 5: soundness
        asyncio.run(validate_subject_ref(resolver, caller, subject_ref))  # no raise
    else:
        assert not resolved.is_resolved  # Property 2: containment
        with pytest.raises(AppException):
            asyncio.run(validate_subject_ref(resolver, caller, subject_ref))
