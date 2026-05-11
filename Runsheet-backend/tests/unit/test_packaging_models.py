from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from fuel.packaging_models import (
    CylinderExchangeLine,
    DEFAULT_DEF_PACKAGES,
    PackageDefinition,
    PropaneCylinder,
)


def test_def_case_hierarchy_computes_parent_gallons():
    case = PackageDefinition(
        package_code="DEF_CASE_2X2_5GAL",
        product_code="DEF",
        kind="case",
        unit_volume_gallons=2.5,
        units_per_parent=2,
        parent_package_code="DEF_JUG_2_5GAL",
    )

    assert case.product_code == "DEF"
    assert case.parent_volume_gallons == 5.0


def test_default_def_packages_include_tote_case_and_jug():
    codes = {package.package_code for package in DEFAULT_DEF_PACKAGES}

    assert {"DEF_TOTE_330GAL", "DEF_CASE_2X2_5GAL", "DEF_JUG_2_5GAL"} <= codes


def test_propane_cylinder_requalification_statuses():
    cylinder = PropaneCylinder(
        tenant_id="tenant-A",
        cylinder_id="cyl-1",
        size_lb=20,
        requalification_due_date=date(2026, 7, 1),
    )

    assert cylinder.requalification_status(as_of=date(2026, 1, 1)) == "valid"
    assert cylinder.requalification_status(as_of=date(2026, 5, 1)) == "due_soon"
    assert cylinder.requalification_status(as_of=date(2026, 7, 2)) == "expired"


def test_cylinder_exchange_only_accepts_propane_aliases():
    line = CylinderExchangeLine(
        product_code="LPG",
        full_cylinders_out=10,
        empties_returned=8,
        rejected_cylinders=1,
        rejection_reason="expired requalification",
    )

    assert line.product_code == "PROPANE"
    assert line.net_cylinder_delta == 3

    with pytest.raises(ValidationError):
        CylinderExchangeLine(
            product_code="DEF",
            full_cylinders_out=1,
            empties_returned=1,
        )
