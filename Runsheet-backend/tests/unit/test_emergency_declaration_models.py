from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fuel.emergency_declaration_models import (
    EmergencyDeclaration,
    FederalHOSExemption,
)


def _dt(day: int) -> datetime:
    return datetime(2026, 5, day, 12, 0, tzinfo=timezone.utc)


def test_emergency_declaration_tracks_active_window_and_state_scope():
    declaration = EmergencyDeclaration(
        declaration_id="dot-1",
        tenant_id="tenant-A",
        source="state_dot",
        title="Hurricane emergency fuel allocation",
        affected_states=[" tx ", "LA", "tx"],
        effective_at=_dt(10),
        expires_at=_dt(20),
        fuel_allocation_enabled=True,
        hos_exemption_enabled=True,
    )

    assert declaration.affected_states == ["TX", "LA"]
    assert declaration.is_active(as_of=_dt(11), state="TX") is True
    assert declaration.is_active(as_of=_dt(11), state="OK") is False
    assert declaration.is_active(as_of=_dt(21), state="TX") is False


def test_federal_hos_exemption_dedupes_rules_and_state_scope():
    exemption = FederalHOSExemption(
        exemption_id="hos-1",
        declaration_id="dot-1",
        tenant_id="tenant-A",
        affected_states=["fl", "GA"],
        suspended_rules=["drive_limit", "drive_limit", "cycle_limit"],
        effective_at=_dt(10),
        expires_at=_dt(20),
        citation_url="https://example.test/fmcsa",
    )

    assert exemption.affected_states == ["FL", "GA"]
    assert exemption.suspended_rules == ["drive_limit", "cycle_limit"]
    assert exemption.applies_to(as_of=_dt(12), state="GA") is True
    assert exemption.applies_to(as_of=_dt(12), state="AL") is False


def test_declaration_and_exemption_reject_inverted_windows():
    with pytest.raises(ValidationError):
        EmergencyDeclaration(
            declaration_id="dot-1",
            tenant_id="tenant-A",
            source="manual",
            title="bad",
            effective_at=_dt(20),
            expires_at=_dt(10),
        )

    with pytest.raises(ValidationError):
        FederalHOSExemption(
            exemption_id="hos-1",
            declaration_id="dot-1",
            tenant_id="tenant-A",
            effective_at=_dt(20),
            expires_at=_dt(10),
        )
