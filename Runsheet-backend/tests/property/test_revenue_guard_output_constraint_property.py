"""
Property-based tests for RevenueGuard Output Constraint.

# Feature: agent-overlay-architecture, Property 15: RevenueGuard Output Constraint
# Re-expressed over margin records for margin-feed (FR5, tasks.md item 22).

**Validates: Requirements 6.5, 6.7; margin-feed FR5.4, FR5.9, FR8.2**

All output proposals from RevenueGuard are PolicyChangeProposals — never
InterventionProposals or direct mutation actions. RevenueGuard reads margin
records from the margin DB queue (never ``jobs_current``) and only advises:
it never changes prices.

Sub-properties tested, for any generated mix of margin records (tenant,
customer, product, flags, origin):

1. Every proposal returned by ``monitor_cycle()`` is a PolicyChangeProposal
   and never an InterventionProposal.
2. No proposal contains direct mutation actions, none reaches the
   ConfirmationProtocol, and none carries a money key.
3. Every proposal has a rollback plan and non-empty evidence made of the
   proposing tenant's own record ids, and the last ``leakage_threshold``
   sales for its (customer, product) are all below floor.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis.strategies import booleans, composite, integers, lists, sampled_from

from Agents.overlay.data_contracts import InterventionProposal, PolicyChangeProposal
from commerce.services.margin_repository import MarginRepository, RecordFilters
from tests.unit.commerce.margin._revenue_guard_support import (
    build_guard,
    clear_margin_tables,
    delivery,
)
from tests.unit.commerce.margin._service_support import assert_no_money
from tests.unit.commerce.margin.conftest import (  # noqa: F401 - fixtures
    TENANT_A,
    TENANT_B,
    flag_on,
    margin_engine,
)

_TENANTS = sampled_from([TENANT_A, TENANT_B])
_CUSTOMERS = sampled_from(["CUST-1", "CUST-2"])
_PRODUCTS = sampled_from(["DIESEL_2", "GASOLINE_87"])
_FLAGS = sampled_from([None, "below", "below", "negative", "missing"])


@composite
def _records(draw) -> List[Tuple[str, str, str, object, bool]]:
    """(tenant, customer, product, flag, recompute_origin) per sale."""
    return draw(lists(
        _composite_record(),
        min_size=0,
        max_size=14,
    ))


@composite
def _composite_record(draw):
    return (
        draw(_TENANTS),
        draw(_CUSTOMERS),
        draw(_PRODUCTS),
        draw(_FLAGS),
        draw(booleans()),
    )


@pytest.fixture
def no_invoicing(monkeypatch):
    from config.settings import clear_settings_cache

    monkeypatch.setenv("COMMERCE_INVOICING_ENABLED", "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.usefixtures("margin_engine", "flag_on", "no_invoicing")
class TestRevenueGuardOutputConstraint:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(records=_records(), threshold=integers(min_value=1, max_value=4))
    # Always exercise the proposal path: three live below-floor sales.
    @example(records=[(TENANT_A, "CUST-1", "DIESEL_2", "below", False)] * 3, threshold=3)
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    async def test_only_advisory_policy_change_proposals(self, records, threshold):
        await clear_margin_tables()
        repo = MarginRepository()
        written: Dict[str, set] = {TENANT_A: set(), TENANT_B: set()}
        for i, (tenant, customer, product, flag, recompute) in enumerate(records):
            result = await repo.write_record(
                tenant,
                delivery(f"ORD-{i}", n=i, flag=flag, customer=customer, product=product),
                "recompute" if recompute else "live",
            )
            written[tenant].add(result.record["record_id"])

        h = build_guard(repo, leakage_threshold=threshold)
        _, proposals = await h.guard.monitor_cycle()

        for proposal in proposals:
            # Property 1: policy proposals only.
            assert isinstance(proposal, PolicyChangeProposal)
            assert not isinstance(proposal, InterventionProposal)
            # Property 2: no mutation actions, nothing to confirm, no money.
            assert not hasattr(proposal, "actions")
            assert proposal.parameter.startswith("margin.review.")
            assert proposal.new_value == {"action": "review_pricing"}
            assert_no_money(proposal.model_dump(mode="json"))
            # Property 3: complete, tenant-scoped, and justified.
            assert proposal.source_agent == "revenue_guard"
            assert proposal.rollback_plan
            assert proposal.evidence
            assert set(proposal.evidence) <= written[proposal.tenant_id]
            sales = await _latest_sales(repo, proposal.tenant_id, proposal.parameter, threshold)
            assert len(sales) == threshold and all(sales)
        h.confirmation.process_mutation.assert_not_called()
        assert h.store.calls == []  # never reads jobs_current or any index


async def _latest_sales(repo, tenant_id, parameter, threshold) -> List[bool]:
    """Below-floor flag of the latest ``threshold`` delivery sales."""
    _, _, customer, product = parameter.split(".", 3)
    rows = (await repo.list_records(
        tenant_id,
        RecordFilters(customer_id=customer, product_code=product, stage="delivery"),
        limit=200,
    )).items
    rows.sort(key=lambda r: (r["as_of"], r["record_id"]), reverse=True)
    return [bool(r["flag_below_floor"]) for r in rows[:threshold]]
