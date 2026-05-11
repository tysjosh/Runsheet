"""
Storm, DOT emergency declaration, and Federal HOS exemption tracking models.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


DeclarationSource = Literal["fmcsa", "state_dot", "local", "manual"]
DeclarationStatus = Literal["active", "expired", "cancelled"]
HOSRuleScope = Literal["drive_limit", "on_duty_window", "cycle_limit"]


class EmergencyDeclaration(BaseModel):
    """A storm or DOT emergency declaration affecting fuel operations."""

    model_config = ConfigDict(extra="forbid")

    declaration_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    source: DeclarationSource
    status: DeclarationStatus = "active"
    title: str = Field(..., min_length=1)
    affected_states: List[str] = Field(default_factory=list)
    effective_at: datetime
    expires_at: datetime
    fuel_allocation_enabled: bool = False
    hos_exemption_enabled: bool = False
    description: Optional[str] = None

    @field_validator("declaration_id", "tenant_id", "title", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("affected_states", mode="before")
    @classmethod
    def _normalize_states(cls, value):
        values = value or []
        out: List[str] = []
        for state in values:
            if not isinstance(state, str):
                raise ValueError("affected_states entries must be strings")
            normalized = state.strip().upper()
            if normalized and normalized not in out:
                out.append(normalized)
        return out

    @field_validator("expires_at")
    @classmethod
    def _expires_after_effective(cls, value, info):
        effective = info.data.get("effective_at")
        if isinstance(effective, datetime) and value <= effective:
            raise ValueError("expires_at must be after effective_at")
        return value

    def is_active(self, *, as_of: Optional[datetime] = None, state: Optional[str] = None) -> bool:
        cursor = _ensure_utc(as_of or datetime.now(timezone.utc))
        if self.status != "active":
            return False
        if cursor < _ensure_utc(self.effective_at) or cursor >= _ensure_utc(self.expires_at):
            return False
        if state and self.affected_states:
            return state.strip().upper() in self.affected_states
        return True


class FederalHOSExemption(BaseModel):
    """Federal HOS exemption linked to an emergency declaration."""

    model_config = ConfigDict(extra="forbid")

    exemption_id: str = Field(..., min_length=1)
    declaration_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    affected_states: List[str] = Field(default_factory=list)
    suspended_rules: List[HOSRuleScope] = Field(default_factory=list)
    effective_at: datetime
    expires_at: datetime
    citation_url: Optional[str] = None

    @field_validator("exemption_id", "declaration_id", "tenant_id", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("affected_states", mode="before")
    @classmethod
    def _normalize_states(cls, value):
        values = value or []
        out: List[str] = []
        for state in values:
            if not isinstance(state, str):
                raise ValueError("affected_states entries must be strings")
            normalized = state.strip().upper()
            if normalized and normalized not in out:
                out.append(normalized)
        return out

    @field_validator("suspended_rules")
    @classmethod
    def _dedupe_rules(cls, value: List[HOSRuleScope]) -> List[HOSRuleScope]:
        out: List[HOSRuleScope] = []
        for rule in value or []:
            if rule not in out:
                out.append(rule)
        return out

    @field_validator("expires_at")
    @classmethod
    def _expires_after_effective(cls, value, info):
        effective = info.data.get("effective_at")
        if isinstance(effective, datetime) and value <= effective:
            raise ValueError("expires_at must be after effective_at")
        return value

    def applies_to(
        self, *, as_of: Optional[datetime] = None, state: Optional[str] = None
    ) -> bool:
        cursor = _ensure_utc(as_of or datetime.now(timezone.utc))
        if cursor < _ensure_utc(self.effective_at) or cursor >= _ensure_utc(self.expires_at):
            return False
        if state and self.affected_states:
            return state.strip().upper() in self.affected_states
        return True


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DeclarationSource",
    "DeclarationStatus",
    "EmergencyDeclaration",
    "FederalHOSExemption",
    "HOSRuleScope",
]
