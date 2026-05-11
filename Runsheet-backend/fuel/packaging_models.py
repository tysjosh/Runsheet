"""
Fuel packaging hierarchy and propane cylinder-exchange models.

Bulk gallons are not the only sellable unit in fuel operations: DEF moves as
totes, drums, and 2.5-gallon jugs, while propane frequently moves through
cylinder exchange. These strict models provide the shared validation layer for
order intake, inventory, and future warehouse APIs.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from fuel.services.fuel_product_catalog import canonicalize


PackageKind = Literal["bulk", "tote", "drum", "case", "jug", "cylinder_exchange"]
CylinderRequalificationStatus = Literal["valid", "due_soon", "expired"]


class PackageDefinition(BaseModel):
    """Defines a sellable package and its place in a package hierarchy."""

    model_config = ConfigDict(extra="forbid")

    package_code: str = Field(..., min_length=1)
    product_code: str = Field(..., min_length=1)
    kind: PackageKind
    unit_volume_gallons: float = Field(..., gt=0)
    units_per_parent: int = Field(default=1, ge=1)
    parent_package_code: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None

    @field_validator("package_code", "product_code", "parent_package_code", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("string fields must not be blank")
        return stripped

    @field_validator("product_code", mode="after")
    @classmethod
    def _canonical_product(cls, value: str) -> str:
        return canonicalize(value)

    @computed_field
    @property
    def parent_volume_gallons(self) -> float:
        """Total gallons represented by one parent package."""

        return round(self.unit_volume_gallons * self.units_per_parent, 6)


class PropaneCylinder(BaseModel):
    """DOT-tracked propane exchange cylinder."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    cylinder_id: str = Field(..., min_length=1)
    size_lb: float = Field(..., gt=0)
    serial_number: Optional[str] = None
    requalification_due_date: date
    last_exchange_date: Optional[date] = None

    @field_validator("tenant_id", "cylinder_id", "serial_number", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("string fields must not be blank")
        return stripped

    def requalification_status(
        self, *, as_of: date, due_soon_days: int = 90
    ) -> CylinderRequalificationStatus:
        if due_soon_days < 0:
            raise ValueError("due_soon_days must be >= 0")
        days_until_due = (self.requalification_due_date - as_of).days
        if days_until_due < 0:
            return "expired"
        if days_until_due <= due_soon_days:
            return "due_soon"
        return "valid"


class CylinderExchangeLine(BaseModel):
    """One propane cylinder-exchange order line."""

    model_config = ConfigDict(extra="forbid")

    product_code: str = Field(default="PROPANE", min_length=1)
    package_code: str = Field(default="PROPANE_CYL_EXCHANGE_20LB", min_length=1)
    full_cylinders_out: int = Field(..., ge=0)
    empties_returned: int = Field(..., ge=0)
    rejected_cylinders: int = Field(default=0, ge=0)
    rejection_reason: Optional[str] = None

    @field_validator("product_code", mode="after")
    @classmethod
    def _canonical_product(cls, value: str) -> str:
        product = canonicalize(value)
        if product != "PROPANE":
            raise ValueError("cylinder exchange is only valid for PROPANE")
        return product

    @field_validator("package_code", "rejection_reason", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("string fields must not be blank")
        return stripped

    @computed_field
    @property
    def net_cylinder_delta(self) -> int:
        """Full cylinders delivered minus acceptable empty returns."""

        return self.full_cylinders_out - self.empties_returned + self.rejected_cylinders


DEFAULT_DEF_PACKAGES = (
    PackageDefinition(
        package_code="DEF_TOTE_330GAL",
        product_code="DEF",
        kind="tote",
        unit_volume_gallons=330.0,
        description="330-gallon DEF tote",
    ),
    PackageDefinition(
        package_code="DEF_CASE_2X2_5GAL",
        product_code="DEF",
        kind="case",
        unit_volume_gallons=2.5,
        units_per_parent=2,
        parent_package_code="DEF_JUG_2_5GAL",
        description="Case of two 2.5-gallon DEF jugs",
    ),
    PackageDefinition(
        package_code="DEF_JUG_2_5GAL",
        product_code="DEF",
        kind="jug",
        unit_volume_gallons=2.5,
        description="2.5-gallon DEF jug",
    ),
)


__all__ = [
    "CylinderExchangeLine",
    "CylinderRequalificationStatus",
    "DEFAULT_DEF_PACKAGES",
    "PackageDefinition",
    "PackageKind",
    "PropaneCylinder",
]
