"""Portal response models (design §2.1 E10, §3).

Every model is ``extra="forbid"`` and is built only from explicit projection
code, so a response can never carry a field that isn't listed here.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _PortalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalMeasurementUnits(_PortalModel):
    volume: str
    distance: str


class PortalMe(_PortalModel):
    """``GET /api/portal/me`` (design §3.1)."""

    email: str
    customer_display_name: str
    #: Falls back to the tenant id until tenants have a display name (B1, DV1).
    supplier_name: str
    ordering_available: bool
    invoices_available: bool
    payments_available: bool
    measurement_units: PortalMeasurementUnits


class PortalMeEnvelope(_PortalModel):
    data: PortalMe
    request_id: str


__all__ = ["PortalMe", "PortalMeEnvelope", "PortalMeasurementUnits"]
