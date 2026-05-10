"""End-to-end integration tests for the Price Protection surface (Task 4.9).

Phase 4 of the Fuel Compliance Backbone spec ships the price-protection
contract: a model (Task 4.1), a per-contract-type dispatch
(Task 4.2), an OCC-guarded decrement (Task 4.3), split-line semantics
(Task 4.4), a lifecycle transition cron (Task 4.5), settlement-variance
reporting (Task 4.6), CRUD endpoints (Task 4.7), and wiring into the
:class:`SalesPricingEngine` (Task 4.8). The unit tests that ship with
each task exercise that task's surface in isolation — this file is the
task-4.9 integration complement that chains the operations together
across an invoice's lifecycle so regressions at the seams (resolve →
decrement → transition → split) surface without needing the
:class:`InvoiceService`.

Scenarios covered (Req 3 — comprehensive coverage):

* **Drain-to-exhausted** — create a contract, resolve a delivery,
  decrement the contract, repeat until ``remaining_gallons == 0``,
  then transition via ``check_expiry``.
* **Split-line exhaustion** — decrement a contract to a sub-delivery
  remainder, then trigger a delivery that exceeds the remainder so
  the split-line partition is produced. Follow-through decrement
  draws only the contracted portion; the excess is the invoice
  caller's responsibility. A final transition moves the contract to
  ``exhausted``.
* **Contract-type coverage** — run the drain cycle for each of the
  three supported ``contract_type`` values (``fixed_price``,
  ``cap_price``, ``collar``) so regressions in
  :meth:`PriceProtectionService._dispatch_contract_price` surface at
  the integration seam.
* **Lifecycle expiry** — create a contract that runs out of time
  (``end_date`` in the past) and verify the cron transitions it to
  ``expired`` rather than ``exhausted`` when gallons remain.
* **Settlement variance** — after the drain, feed the synthesised
  delivery history into :meth:`compute_portfolio_variance` to ensure
  the per-delivery variance lines up with the contract-driven
  dispatch.

The tests run against an in-memory fake Elasticsearch service that
implements the five query shapes the service emits:

1. Active-contract scan (``resolve_price``).
2. Single-contract lookup (``_fetch_contract``).
3. Tenant-scoped active scan (``check_expiry``).
4. Tenant-terms aggregation (``run_price_protection_expiry_cycle``).
5. ``invoice_events`` tag scan (``iter_contract_invoice_events``).

The fake is deliberately stricter than a real ES cluster in one way:
it applies the tenant filter by inspecting the query's outer
``bool.filter`` exactly the way
:func:`ops.middleware.tenant_guard.inject_tenant_filter` emits it,
so a regression that drops the filter would fail here as loudly as
it would in production.

Validates: Requirement 3 (comprehensive coverage)
"""

from __future__ import annotations

from datetime import date
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from commerce.models.price_protection_contract import PriceProtectionContract
from commerce.services.commerce_es_mappings import INVOICE_EVENTS_INDEX
from commerce.services.price_protection_service import (
    PriceProtectionService,
    PriceResolution,
)
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)


# ---------------------------------------------------------------------------
# In-memory fake ES — understands every query the service emits.
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that persists documents by ``(index, doc_id)``.

    Supports the exact query shapes the service emits:

    * ``price_protection_contracts`` — single-contract lookup by
      ``contract_id``, active-contract scan by customer/product/date,
      tenant-scoped active scan for the lifecycle cron, and tenant
      ``terms`` aggregation for the cron dispatcher.
    * ``invoice_events`` — tag scan by ``payload.contract_id`` for
      the settlement-variance iterator.

    Every clause is interpreted in-Python so the outer
    :func:`ops.middleware.tenant_guard.inject_tenant_filter` is
    enforced end-to-end (cross-tenant rows never leak).
    """

    def __init__(self) -> None:
        self._docs: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Seeding / inspection
    # ------------------------------------------------------------------

    def seed(self, index: str, doc_id: str, document: Dict[str, Any]) -> None:
        """Seed a document without going through ``index_document``.

        Used by the split-line scenario to insert a separate contract
        in the same tenant without routing through the service.
        """
        self._docs.setdefault(index, {})[doc_id] = dict(document)

    def get(self, index: str, doc_id: str) -> Dict[str, Any]:
        return dict(self._docs[index][doc_id])

    # ------------------------------------------------------------------
    # ES surface
    # ------------------------------------------------------------------

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self._docs.setdefault(index, {})[doc_id] = dict(document)

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        row = self._docs.get(index, {}).get(doc_id)
        if row is None:
            raise KeyError(f"doc_id {doc_id} not found in {index}")
        row.update(partial)

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
        bucket = self._docs.get(index, {})

        tenant_term = self._extract_tenant_filter(query)
        aggs = (query or {}).get("aggs") or {}
        if "tenants" in aggs:
            tenant_ids = {
                row.get("tenant_id")
                for row in bucket.values()
                if row.get("tenant_id")
            }
            return {
                "aggregations": {
                    "tenants": {
                        "buckets": [
                            {"key": tid, "doc_count": 1}
                            for tid in sorted(t for t in tenant_ids if t)
                        ]
                    }
                }
            }

        inner = self._extract_inner_filters(query)
        hits: List[Dict[str, Any]] = []
        for row in bucket.values():
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if not self._row_matches_inner_filters(row, inner):
                continue
            hits.append({"_source": dict(row)})

        return {"hits": {"hits": hits[:size]}}

    # ------------------------------------------------------------------
    # Query-shape helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tenant_filter(query: Dict[str, Any]) -> Optional[str]:
        outer_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        for clause in outer_filter:
            term = clause.get("term") if isinstance(clause, dict) else None
            if isinstance(term, dict) and "tenant_id" in term:
                return term["tenant_id"]
        return None

    @staticmethod
    def _extract_inner_filters(query: Dict[str, Any]) -> List[Dict[str, Any]]:
        inner_must = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("must", [])
        ) or []
        if not inner_must:
            return []
        inner_bool = inner_must[0].get("bool") if isinstance(inner_must[0], dict) else None
        if not isinstance(inner_bool, dict):
            return []
        return inner_bool.get("filter", []) or []

    @staticmethod
    def _get_nested(doc: Dict[str, Any], dotted: str) -> Any:
        current: Any = doc
        for part in dotted.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _row_matches_inner_filters(
        self, row: Dict[str, Any], filters: List[Dict[str, Any]]
    ) -> bool:
        for clause in filters:
            if not isinstance(clause, dict):
                return False
            if "term" in clause:
                for field, value in clause["term"].items():
                    if self._get_nested(row, field) != value:
                        return False
                continue
            if "range" in clause:
                for field, spec in clause["range"].items():
                    value = self._get_nested(row, field)
                    if value is None:
                        return False
                    if "lte" in spec and not (str(value) <= str(spec["lte"])):
                        return False
                    if "gte" in spec and not (str(value) >= str(spec["gte"])):
                        return False
                continue
            # Unknown clause shapes conservatively exclude the row —
            # the service never emits anything else, so this surfaces
            # a regression rather than silently passing.
            return False
        return True


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_contract_row(
    *,
    contract_id: str,
    tenant_id: str = "tenant-INT",
    customer_id: str = "cust-INT",
    account_id: str = "acct-INT",
    product_code: str = "HEATING_OIL",
    contract_type: str = "fixed_price",
    start_date: str = "2026-01-01",
    end_date: str = "2026-12-31",
    contracted_gallons: float = 1_000.0,
    remaining_gallons: Optional[float] = None,
    price_cap_cents: Optional[int] = None,
    price_floor_cents: Optional[int] = None,
    fixed_price_cents: Optional[int] = None,
    status: str = "active",
    version: int = 0,
) -> Dict[str, Any]:
    """Build a valid ``price_protection_contracts`` ``_source`` row.

    Mirrors the defaults used by the per-task unit tests so integration
    seeds read identically to the isolated ones. Contract-type-specific
    price defaults are filled in so tests only override what they care
    about.
    """
    if remaining_gallons is None:
        remaining_gallons = contracted_gallons

    if contract_type == "fixed_price" and fixed_price_cents is None:
        fixed_price_cents = 325
    if contract_type == "cap_price" and price_cap_cents is None:
        price_cap_cents = 340
    if contract_type == "collar":
        if price_cap_cents is None:
            price_cap_cents = 340
        if price_floor_cents is None:
            price_floor_cents = 290

    return {
        "contract_id": contract_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "contract_type": contract_type,
        "start_date": start_date,
        "end_date": end_date,
        "contracted_gallons": contracted_gallons,
        "remaining_gallons": remaining_gallons,
        "price_cap_cents": price_cap_cents,
        "price_floor_cents": price_floor_cents,
        "fixed_price_cents": fixed_price_cents,
        "status": status,
        "version": version,
        "notes": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _seed_invoice_event(
    es: _FakeESService,
    *,
    event_id: str,
    tenant_id: str,
    contract_id: str,
    delivery_id: str,
    market_price_cents: int,
    effective_price_cents: int,
    gallons: float,
) -> None:
    """Seed an ``invoice_events`` row shaped for ``iter_contract_invoice_events``."""
    es.seed(
        INVOICE_EVENTS_INDEX,
        event_id,
        {
            "tenant_id": tenant_id,
            "event_id": event_id,
            "payload": {
                "contract_id": contract_id,
                "delivery_id": delivery_id,
                "market_price_cents": market_price_cents,
                "effective_price_cents": effective_price_cents,
                "gallons": gallons,
            },
        },
    )


# ---------------------------------------------------------------------------
# Helper: drive a full delivery round-trip
# ---------------------------------------------------------------------------


async def _bill_delivery(
    service: PriceProtectionService,
    *,
    customer_id: str,
    product_code: str,
    market_price_cents: int,
    gallons: float,
    effective_date: date,
) -> PriceResolution:
    """Resolve a delivery and decrement the contract in one go.

    Mirrors what :class:`SalesPricingEngine` and the downstream
    :class:`InvoiceService` will do once Task 5.11 lands: ask the
    resolver for the effective price, then (when a contract matched)
    decrement that contract by the contracted portion of the delivery.
    Split-line handling is deferred to the caller — this helper only
    decrements the contracted portion, because the excess is not
    drawn from the contract.
    """
    resolution = await service.resolve_price(
        customer_id=customer_id,
        product_code=product_code,
        market_price_cents=market_price_cents,
        gallons=gallons,
        effective_date=effective_date,
    )

    if resolution.contract_id is None:
        # No contract matched — caller falls through to
        # SalesPricingEngine strategy dispatch, nothing to decrement.
        return resolution

    # Contracted portion equals the full delivery unless a split was
    # produced (delivery exceeded remaining_gallons).
    contracted_gallons = (
        resolution.split_gallons_at_contract_price
        if resolution.split_gallons_at_contract_price is not None
        else gallons
    )
    if contracted_gallons > 0:
        await service.decrement_gallons(
            resolution.contract_id, contracted_gallons
        )

    return resolution


# ---------------------------------------------------------------------------
# Scenario: create → resolve → decrement → … → exhausted
# ---------------------------------------------------------------------------


class TestDrainToExhaustedFixedPrice:
    """Chain resolve+decrement until the contract is fully consumed."""

    @pytest.mark.asyncio
    async def test_fixed_price_contract_drains_and_transitions(self):
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-fx-drain",
            _make_contract_row(
                contract_id="contract-fx-drain",
                contract_type="fixed_price",
                fixed_price_cents=325,
                contracted_gallons=1_000.0,
                remaining_gallons=1_000.0,
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Two 400-gallon deliveries and a final 200-gallon delivery
        # consume the entire 1000-gallon contract. The contract's
        # fixed_price_cents wins regardless of market price.
        for gallons in (400.0, 400.0, 200.0):
            resolution = await _bill_delivery(
                service,
                customer_id="cust-INT",
                product_code="HEATING_OIL",
                market_price_cents=410,
                gallons=gallons,
                effective_date=date(2026, 6, 1),
            )
            assert resolution.contract_id == "contract-fx-drain"
            assert resolution.contract_type == "fixed_price"
            assert resolution.effective_price_cents == 325
            assert resolution.market_price_cents == 410
            assert resolution.split_gallons_at_contract_price is None
            assert resolution.split_gallons_at_market_price is None

        stored = es.get(
            PRICE_PROTECTION_CONTRACTS_INDEX, "contract-fx-drain"
        )
        assert stored["remaining_gallons"] == pytest.approx(0.0)
        # Three decrements → version bumped three times from the
        # initial zero. One transition write will add another bump.
        assert stored["version"] == 3
        assert stored["status"] == "active"

        # Cron pass transitions the drained contract to ``exhausted``.
        transitioned = await service.check_expiry(today=date(2026, 6, 1))
        assert transitioned == ["contract-fx-drain"]

        final = es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-fx-drain")
        assert final["status"] == "exhausted"
        assert final["version"] == 4

        # Post-transition resolver calls fall through to market price
        # — no active contract remains for the customer/product.
        followup = await service.resolve_price(
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=100.0,
            effective_date=date(2026, 6, 2),
        )
        assert followup.contract_id is None
        assert followup.effective_price_cents == 410


# ---------------------------------------------------------------------------
# Scenario: split-line edge case → exhausted contract + rollover
# ---------------------------------------------------------------------------


class TestSplitLineExhaustion:
    """Decrement remaining → split on next delivery → only contracted portion drains."""

    @pytest.mark.asyncio
    async def test_split_line_drains_only_contracted_portion(self):
        """Reproduces the Task 4.4 scenario spelled out in Task 4.9.

        Step 1 — decrement the contract so only 150 gallons remain.
        Step 2 — resolve a 500-gallon delivery; the service returns a
        :class:`PriceResolution` with the split-line fields populated
        (150 gallons at the contract price, 350 gallons at market).
        Step 3 — decrement the contract by the contracted portion
        (150 gallons) only. The excess is the caller's responsibility
        — the ``SalesPricingEngine`` will bill it under the strategy
        dispatch, and the contract must not lose gallons it was not
        allowed to cover.
        Step 4 — the lifecycle cron observes ``remaining_gallons ==
        0`` and transitions the contract to ``exhausted``.
        Step 5 — the follow-up delivery on the same day falls through
        to the market price because no active contract remains.
        """
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-split",
            _make_contract_row(
                contract_id="contract-split",
                contract_type="cap_price",
                price_cap_cents=340,
                contracted_gallons=1_000.0,
                remaining_gallons=1_000.0,
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Step 1 — burn the contract down to 150 gallons remaining.
        first = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,  # above cap → contract price wins
            gallons=850.0,
            effective_date=date(2026, 6, 1),
        )
        assert first.contract_id == "contract-split"
        assert first.effective_price_cents == 340  # capped
        assert first.split_gallons_at_contract_price is None
        stored = es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-split")
        assert stored["remaining_gallons"] == pytest.approx(150.0)

        # Step 2 — a 500-gallon delivery exceeds the 150-gallon
        # remainder. The resolver must split: 150 at the contract
        # price, 350 at the market price, with the contract price
        # still reflecting the cap dispatch for the contracted portion.
        split = await service.resolve_price(
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert split.contract_id == "contract-split"
        assert split.contract_type == "cap_price"
        assert split.effective_price_cents == 340
        assert split.market_price_cents == 410
        assert split.split_gallons_at_contract_price == pytest.approx(150.0)
        assert split.split_gallons_at_market_price == pytest.approx(350.0)
        # Partition must sum back to the delivered volume.
        assert (
            split.split_gallons_at_contract_price
            + split.split_gallons_at_market_price
            == pytest.approx(500.0)
        )

        # Step 3 — decrement only the contracted portion. The excess
        # is billed by the SalesPricingEngine under the strategy
        # dispatch; it must not leak into the contract's remaining
        # gallons.
        await service.decrement_gallons(
            split.contract_id, split.split_gallons_at_contract_price
        )
        drained = es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-split")
        assert drained["remaining_gallons"] == pytest.approx(0.0)
        # Defensive check: a caller that tries to decrement the full
        # 500 gallons would over-draw the contract.
        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-split", 500.0)
        assert "insufficient_remaining_gallons" in str(excinfo.value)

        # Step 4 — cron transitions the drained contract to
        # ``exhausted`` on the next pass.
        transitioned = await service.check_expiry(today=date(2026, 6, 1))
        assert transitioned == ["contract-split"]
        assert (
            es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-split")["status"]
            == "exhausted"
        )

        # Step 5 — a subsequent delivery on the same day falls through
        # to the raw market price because the contract is terminal.
        followup = await service.resolve_price(
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=100.0,
            effective_date=date(2026, 6, 1),
        )
        assert followup.contract_id is None
        assert followup.effective_price_cents == 410
        assert followup.split_gallons_at_contract_price is None
        assert followup.split_gallons_at_market_price is None


# ---------------------------------------------------------------------------
# Scenario: contract-type coverage across the drain cycle
# ---------------------------------------------------------------------------


class TestContractTypeCoverageAcrossLifecycle:
    """Each supported ``contract_type`` survives the create/drain cycle."""

    @pytest.mark.asyncio
    async def test_cap_price_draws_min_of_cap_and_market(self):
        """``cap_price`` returns ``min(market, cap)`` for every delivery."""
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-cap-drain",
            _make_contract_row(
                contract_id="contract-cap-drain",
                contract_type="cap_price",
                price_cap_cents=340,
                contracted_gallons=600.0,
                remaining_gallons=600.0,
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Delivery 1: market below cap → market wins, contract still
        # drives the bill.
        r1 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=300,
            gallons=200.0,
            effective_date=date(2026, 6, 1),
        )
        assert r1.contract_id == "contract-cap-drain"
        assert r1.effective_price_cents == 300

        # Delivery 2: market above cap → cap wins.
        r2 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=200.0,
            effective_date=date(2026, 6, 1),
        )
        assert r2.effective_price_cents == 340

        # Delivery 3: drains the contract exactly. No split triggered
        # (gallons == remaining_gallons falls on the single-price path).
        r3 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=200.0,
            effective_date=date(2026, 6, 1),
        )
        assert r3.effective_price_cents == 340
        assert r3.split_gallons_at_contract_price is None

        final_pre_cron = es.get(
            PRICE_PROTECTION_CONTRACTS_INDEX, "contract-cap-drain"
        )
        assert final_pre_cron["remaining_gallons"] == pytest.approx(0.0)
        assert final_pre_cron["status"] == "active"

        await service.check_expiry(today=date(2026, 6, 1))
        assert (
            es.get(
                PRICE_PROTECTION_CONTRACTS_INDEX, "contract-cap-drain"
            )["status"]
            == "exhausted"
        )

    @pytest.mark.asyncio
    async def test_collar_clamps_within_band_and_transitions(self):
        """``collar`` clamps market between floor and cap per delivery."""
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-collar-drain",
            _make_contract_row(
                contract_id="contract-collar-drain",
                contract_type="collar",
                price_cap_cents=340,
                price_floor_cents=290,
                contracted_gallons=450.0,
                remaining_gallons=450.0,
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Market below floor → floor wins.
        r1 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=250,
            gallons=150.0,
            effective_date=date(2026, 6, 1),
        )
        assert r1.contract_id == "contract-collar-drain"
        assert r1.effective_price_cents == 290

        # Market inside band → market wins.
        r2 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=310,
            gallons=150.0,
            effective_date=date(2026, 6, 1),
        )
        assert r2.effective_price_cents == 310

        # Market above cap → cap wins; drains the contract.
        r3 = await _bill_delivery(
            service,
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=150.0,
            effective_date=date(2026, 6, 1),
        )
        assert r3.effective_price_cents == 340
        assert r3.split_gallons_at_contract_price is None

        await service.check_expiry(today=date(2026, 6, 1))
        assert (
            es.get(
                PRICE_PROTECTION_CONTRACTS_INDEX, "contract-collar-drain"
            )["status"]
            == "exhausted"
        )


# ---------------------------------------------------------------------------
# Scenario: time-based expiry transitions (Req 3.6)
# ---------------------------------------------------------------------------


class TestExpiryLifecycle:
    """Contracts that run out of time transition to ``expired``."""

    @pytest.mark.asyncio
    async def test_past_end_date_with_gallons_left_transitions_to_expired(
        self,
    ):
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-ran-out-time",
            _make_contract_row(
                contract_id="contract-ran-out-time",
                start_date="2025-01-01",
                end_date="2025-12-31",  # already in the past
                contracted_gallons=1_000.0,
                remaining_gallons=750.0,  # 75% unused
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Resolver must not surface a contract whose window has closed.
        before = await service.resolve_price(
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=100.0,
            effective_date=date(2026, 6, 1),
        )
        assert before.contract_id is None
        assert before.effective_price_cents == 410

        # Cron pass moves the status to ``expired`` (time-out, not
        # exhaustion — gallons remain).
        transitioned = await service.check_expiry(today=date(2026, 6, 1))
        assert transitioned == ["contract-ran-out-time"]
        final = es.get(
            PRICE_PROTECTION_CONTRACTS_INDEX, "contract-ran-out-time"
        )
        assert final["status"] == "expired"
        # Gallons preserved — the contract expired with unused coverage.
        assert final["remaining_gallons"] == pytest.approx(750.0)


# ---------------------------------------------------------------------------
# Scenario: settlement variance after a full drain
# ---------------------------------------------------------------------------


class TestSettlementVarianceAfterDrain:
    """Settlement variance lines up with the dispatch across a drain."""

    @pytest.mark.asyncio
    async def test_portfolio_variance_matches_dispatch_history(self):
        """Drain a contract, stream the invoice events back, and verify
        the aggregated variance matches the per-delivery dispatch.

        This exercises the full Task 4.6 surface
        (``iter_contract_invoice_events`` + ``compute_portfolio_variance``)
        on top of the drain cycle the rest of the file stresses.
        """
        es = _FakeESService()
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-variance",
            _make_contract_row(
                contract_id="contract-variance",
                contract_type="fixed_price",
                fixed_price_cents=300,
                contracted_gallons=500.0,
                remaining_gallons=500.0,
            ),
        )
        service = PriceProtectionService(es, "tenant-INT")

        # Three deliveries at different market prices drain the
        # contract. Fixed_price locks the customer at 300¢ — the
        # variance reflects the gain/loss vs the day's market.
        deliveries = [
            ("del-01", 410, 200.0),  # +(410-300)*200 = +22_000
            ("del-02", 275, 200.0),  # +(275-300)*200 = -5_000
            ("del-03", 320, 100.0),  # +(320-300)*100 = +2_000
        ]
        for delivery_id, market, gallons in deliveries:
            resolution = await _bill_delivery(
                service,
                customer_id="cust-INT",
                product_code="HEATING_OIL",
                market_price_cents=market,
                gallons=gallons,
                effective_date=date(2026, 6, 1),
            )
            assert resolution.contract_id == "contract-variance"
            assert resolution.effective_price_cents == 300
            _seed_invoice_event(
                es,
                event_id=f"event_{delivery_id}",
                tenant_id="tenant-INT",
                contract_id="contract-variance",
                delivery_id=delivery_id,
                market_price_cents=market,
                effective_price_cents=300,
                gallons=gallons,
            )

        # Stream the events back through the service helper.
        events: List[Dict[str, Any]] = [
            event
            async for event in service.iter_contract_invoice_events(
                "contract-variance"
            )
        ]
        assert len(events) == 3
        assert {event["delivery_id"] for event in events} == {
            "del-01",
            "del-02",
            "del-03",
        }

        # Aggregate variance via the Task 4.6 helper.
        report = await service.compute_portfolio_variance(
            contract_id="contract-variance",
            deliveries=events,
        )
        assert report["contract_id"] == "contract-variance"
        assert report["delivery_count"] == 3
        assert report["total_gallons"] == pytest.approx(500.0)
        # Net: 22_000 - 5_000 + 2_000 = 19_000 cents saved.
        assert report["total_variance_cents"] == 19_000

        # Sanity: the per-delivery breakdown matches the static
        # computation.
        by_delivery = {
            row["delivery_id"]: row["variance_cents"]
            for row in report["breakdown"]
        }
        assert by_delivery["del-01"] == 22_000
        assert by_delivery["del-02"] == -5_000
        assert by_delivery["del-03"] == 2_000

        # Drained + transition — end-of-scenario cleanup.
        await service.check_expiry(today=date(2026, 6, 1))
        assert (
            es.get(
                PRICE_PROTECTION_CONTRACTS_INDEX, "contract-variance"
            )["status"]
            == "exhausted"
        )


# ---------------------------------------------------------------------------
# Scenario: tenant isolation survives the full chain
# ---------------------------------------------------------------------------


class TestTenantIsolationAcrossChain:
    """Cross-tenant rows must never surface at any step of the chain."""

    @pytest.mark.asyncio
    async def test_other_tenant_contracts_are_invisible(self):
        es = _FakeESService()
        # Seed a contract for our tenant plus an identically-shaped
        # contract for a different tenant. The service bound to
        # ``tenant-A`` must never see the ``tenant-B`` row.
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-mine",
            _make_contract_row(
                contract_id="contract-mine",
                tenant_id="tenant-A",
                contract_type="fixed_price",
                fixed_price_cents=325,
                contracted_gallons=200.0,
                remaining_gallons=200.0,
            ),
        )
        await es.index_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            "contract-theirs",
            _make_contract_row(
                contract_id="contract-theirs",
                tenant_id="tenant-B",
                contract_type="fixed_price",
                fixed_price_cents=200,  # much cheaper — tempting leak
                contracted_gallons=200.0,
                remaining_gallons=0.0,  # already drained for tenant-B
            ),
        )
        service_a = PriceProtectionService(es, "tenant-A")

        # Resolver sees only tenant-A's contract and its 325¢ price.
        resolution = await service_a.resolve_price(
            customer_id="cust-INT",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=50.0,
            effective_date=date(2026, 6, 1),
        )
        assert resolution.contract_id == "contract-mine"
        assert resolution.effective_price_cents == 325

        # Decrement targets tenant-A's contract only.
        await service_a.decrement_gallons("contract-mine", 50.0)
        assert (
            es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-mine")[
                "remaining_gallons"
            ]
            == pytest.approx(150.0)
        )
        # tenant-B's row is untouched.
        theirs = es.get(PRICE_PROTECTION_CONTRACTS_INDEX, "contract-theirs")
        assert theirs["tenant_id"] == "tenant-B"
        assert theirs["remaining_gallons"] == pytest.approx(0.0)
        assert theirs["status"] == "active"

        # Attempting to decrement the cross-tenant contract from our
        # service raises ``contract_not_found`` — the ES filter hides
        # it entirely so we cannot even probe its existence.
        with pytest.raises(ValueError) as excinfo:
            await service_a.decrement_gallons("contract-theirs", 10.0)
        assert "contract_not_found" in str(excinfo.value)

        # ``check_expiry`` for tenant-A must not transition tenant-B's
        # drained contract, even though it has zero remaining gallons.
        transitioned = await service_a.check_expiry(today=date(2026, 6, 1))
        assert transitioned == []
        assert (
            es.get(
                PRICE_PROTECTION_CONTRACTS_INDEX, "contract-theirs"
            )["status"]
            == "active"
        )
