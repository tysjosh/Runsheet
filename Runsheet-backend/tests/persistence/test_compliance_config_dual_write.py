"""Dual-write tests for compliance-config aggregates.

Covers the hybrid document tables (tax jurisdictions / exemptions, price
protection contracts, sell-side pricing rules, supplier contracts): each
upsert stores typed index columns + the verbatim ES document and enqueues an
outbox projection event; the projector round-trips the document byte-for-byte.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from persistence.database import session_scope
from persistence.models import (
    CompliancePricingRuleORM,
    OutboxEventORM,
    PriceProtectionContractORM,
    SupplierContractORM,
    TaxExemptionORM,
    TaxJurisdictionORM,
)
from persistence.projections import _document_passthrough
from persistence.repositories import ComplianceConfigRepository

TENANT = "demo-tenant"


async def test_tax_jurisdiction_upsert_and_passthrough(engine):
    repo = ComplianceConfigRepository("tax_jurisdiction")
    doc = {
        "jurisdiction_id": "tj_1", "tenant_id": TENANT, "fips_code": "06",
        "jurisdiction_level": "state", "tax_type": "excise",
        "product_codes": ["DSL", "GAS"], "rate_cents_per_gallon": 18,
        "effective_date": "2026-01-01", "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        # Typed columns lifted for indexing.
        assert row.fips_code == "06"
        assert row.tax_type == "excise"
        # Projection is the verbatim document (incl. nested product_codes).
        assert _document_passthrough(row) == doc

    async with session_scope() as s:
        outbox = (await s.execute(select(OutboxEventORM))).scalars().all()
    assert len(outbox) == 1
    assert outbox[0].aggregate_type == "tax_jurisdiction"
    assert outbox[0].target_index == "tax_jurisdictions"
    assert outbox[0].payload == doc


async def test_pricing_rule_with_nested_tiers_roundtrips(engine):
    repo = ComplianceConfigRepository("compliance_pricing_rule")
    doc = {
        "rule_id": "rule_1", "tenant_id": TENANT, "customer_id": None,
        "product_code": "DSL", "strategy": "tiered_volume",
        "tier_thresholds": [
            {"min_gallons": 0, "max_gallons": 1000, "unit_price_cents": 350},
            {"min_gallons": 1000, "max_gallons": None, "unit_price_cents": 340},
        ],
        "priority": 0, "effective_date": "2026-01-01", "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.strategy == "tiered_volume"
        assert _document_passthrough(row)["tier_thresholds"] == doc["tier_thresholds"]


async def test_price_protection_version_lifted(engine):
    repo = ComplianceConfigRepository("price_protection_contract")
    doc = {
        "contract_id": "ppc_1", "tenant_id": TENANT, "customer_id": "cust_1",
        "product_code": "DSL", "contract_type": "fixed_price",
        "contracted_gallons": 1000.0, "remaining_gallons": 1000.0,
        "fixed_price_cents": 300, "status": "active", "version": 3,
    }
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.version == 3
        assert row.status == "active"
    # Upsert again with a bumped version -> same row updated, no duplicate.
    doc["version"] = 4
    doc["remaining_gallons"] = 900.0
    async with session_scope() as s:
        await repo.upsert(s, doc=doc)
    async with session_scope() as s:
        count = await s.scalar(select(func.count()).select_from(PriceProtectionContractORM))
        row = await s.get(PriceProtectionContractORM, "ppc_1")
    assert count == 1
    assert row.version == 4
    assert row.document["remaining_gallons"] == 900.0


async def test_supplier_contract_and_exemption_upsert(engine):
    sc = ComplianceConfigRepository("supplier_contract")
    ex = ComplianceConfigRepository("tax_exemption")
    async with session_scope() as s:
        await sc.upsert(s, doc={
            "contract_id": "sc_1", "tenant_id": TENANT, "supplier_name": "BigOil",
            "product_code": "DSL", "status": "active",
        })
        await ex.upsert(s, doc={
            "exemption_id": "ex_1", "tenant_id": TENANT, "customer_id": "cust_1",
            "exemption_type": "dyed_diesel", "certificate_number": "CERT-9",
            "status": "valid",
        })
    async with session_scope() as s:
        assert (await s.get(SupplierContractORM, "sc_1")).supplier_name == "BigOil"
        assert (await s.get(TaxExemptionORM, "ex_1")).certificate_number == "CERT-9"


async def test_delete_removes_row(engine):
    repo = ComplianceConfigRepository("tax_jurisdiction")
    async with session_scope() as s:
        await repo.upsert(s, doc={"jurisdiction_id": "tj_x", "tenant_id": TENANT})
    async with session_scope() as s:
        deleted = await repo.delete(s, TENANT, "tj_x")
        assert deleted is True
    async with session_scope() as s:
        assert await s.get(TaxJurisdictionORM, "tj_x") is None


async def test_unknown_aggregate_type_rejected(engine):
    with pytest.raises(ValueError):
        ComplianceConfigRepository("not_a_real_type")


async def test_tenant_scope_isolation(engine):
    repo = ComplianceConfigRepository("tax_jurisdiction")
    async with session_scope() as s:
        await repo.upsert(s, doc={"jurisdiction_id": "tj_t", "tenant_id": TENANT})
    # A different tenant cannot read it.
    async with session_scope() as s:
        assert await repo.get(s, "other-tenant", "tj_t") is None
        assert await repo.get(s, TENANT, "tj_t") is not None
