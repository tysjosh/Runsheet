"""Unit tests for price-protection contract lifecycle transitions.

Covers Task 4.5 of the Fuel Compliance Backbone spec:
``PriceProtectionService.check_expiry`` /
``PriceProtectionService.check_and_transition_contract`` transition
active contracts to ``exhausted`` (zero ``remaining_gallons``) or
``expired`` (past ``end_date``) per Req 3.6.

The test suite uses an async fake ES service that persists contract
rows keyed on ``contract_id`` and routes ``search_documents`` /
``update_document`` through the same storage, so both the tenant
scan emitted by ``check_expiry`` and the per-contract write emitted
by ``_write_status_transition`` are exercised end-to-end.

Validates: Requirement 3.6
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.services.price_protection_expiry_job import (
    PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS,
    run_price_protection_expiry_cycle,
)
from commerce.services.price_protection_service import PriceProtectionService
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in with per-``contract_id`` storage.

    Supports two query shapes:

    * Active-contracts scan (``check_expiry``): matches rows on
      ``tenant_id`` + ``status == "active"``.
    * Single-contract lookup (``_fetch_contract`` / the
      ``check_and_transition_contract`` path): matches on
      ``tenant_id`` + ``contract_id``.
    * Tenant aggregation (``run_price_protection_expiry_cycle``):
      returns a ``terms`` bucket list of distinct ``tenant_id``
      values stored in the fake.

    ``update_document`` merges the partial-update patch into the
    stored row so a subsequent re-read observes the transition.
    """

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            self._rows[row["contract_id"]] = dict(row)
        self.search_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # ES surface
    # ------------------------------------------------------------------

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
        self.search_calls.append(
            {"index": index, "query": query, "size": size}
        )

        # Tenant filter lives on the outer bool.filter injected by
        # inject_tenant_filter.
        tenant_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        tenant_term: Optional[str] = None
        for clause in tenant_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_term = clause["term"]["tenant_id"]
                break

        # Terms aggregation path (cron: distinct tenants).
        aggs = (query or {}).get("aggs") or {}
        if "tenants" in aggs:
            tenant_ids = {
                row.get("tenant_id")
                for row in self._rows.values()
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

        # Inner filters live on bool.must[0].bool.filter.
        inner_filters = (
            (
                (((query or {}).get("query") or {}).get("bool") or {})
                .get("must", [])
            )
            or []
        )
        inner_bool = (
            inner_filters[0].get("bool", {}) if inner_filters else {}
        )
        inner_filter = inner_bool.get("filter", []) if inner_bool else []

        contract_id: Optional[str] = None
        status: Optional[str] = None
        for clause in inner_filter:
            if "term" in clause and "contract_id" in clause["term"]:
                contract_id = clause["term"]["contract_id"]
            elif "term" in clause and "status" in clause["term"]:
                status = clause["term"]["status"]

        matching: List[Dict[str, Any]] = []
        for row in self._rows.values():
            if (
                tenant_term is not None
                and row.get("tenant_id") != tenant_term
            ):
                continue
            if (
                contract_id is not None
                and row.get("contract_id") != contract_id
            ):
                continue
            if status is not None and row.get("status") != status:
                continue
            matching.append(dict(row))

        return {"hits": {"hits": [{"_source": row} for row in matching]}}

    async def update_document(
        self,
        index: str,
        doc_id: str,
        partial: Dict[str, Any],
    ) -> None:
        self.update_calls.append(
            {"index": index, "id": doc_id, "partial": dict(partial)}
        )
        existing = self._rows.get(doc_id)
        if existing is None:
            raise KeyError(f"doc_id {doc_id} not found")
        existing.update(partial)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def get_row(self, contract_id: str) -> Dict[str, Any]:
        """Return the stored row for inspection in assertions."""
        return dict(self._rows[contract_id])


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------


def _make_contract_row(
    *,
    contract_id: str = "contract-1",
    tenant_id: str = "tenant-1",
    customer_id: str = "cust-1",
    account_id: str = "acct-1",
    product_code: str = "HEATING_OIL",
    contract_type: str = "fixed_price",
    start_date: str = "2026-01-01",
    end_date: str = "2026-12-31",
    contracted_gallons: float = 10_000.0,
    remaining_gallons: Optional[float] = None,
    price_cap_cents: Optional[int] = None,
    price_floor_cents: Optional[int] = None,
    fixed_price_cents: Optional[int] = 325,
    status: str = "active",
    version: int = 0,
) -> Dict[str, Any]:
    """Build a ``price_protection_contracts`` ``_source`` row."""
    if remaining_gallons is None:
        remaining_gallons = contracted_gallons
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


# ---------------------------------------------------------------------------
# check_and_transition_contract — single-contract path
# ---------------------------------------------------------------------------


class TestCheckAndTransitionContractExhausted:
    """Zero ``remaining_gallons`` transitions active contracts to exhausted."""

    @pytest.mark.asyncio
    async def test_zero_remaining_gallons_transitions_to_exhausted(self):
        row = _make_contract_row(
            contract_id="c-exhausted",
            end_date="2099-12-31",  # well in the future
            contracted_gallons=1000.0,
            remaining_gallons=0.0,
            version=2,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-exhausted", today=date(2026, 6, 1)
        )

        assert new_status == "exhausted"
        stored = es.get_row("c-exhausted")
        assert stored["status"] == "exhausted"
        # OCC counter advanced so concurrent decrements observe the
        # transition on their own post-write re-read.
        assert stored["version"] == 3
        # Exactly one write was issued (the transition).
        assert len(es.update_calls) == 1
        assert es.update_calls[0]["id"] == "c-exhausted"
        assert es.update_calls[0]["partial"]["status"] == "exhausted"

    @pytest.mark.asyncio
    async def test_already_exhausted_contract_is_noop(self):
        """Idempotency — already-terminal contracts are not rewritten."""
        row = _make_contract_row(
            contract_id="c-done",
            status="exhausted",
            remaining_gallons=0.0,
            version=5,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-done", today=date(2026, 6, 1)
        )

        assert new_status is None
        assert es.update_calls == []


class TestCheckAndTransitionContractExpired:
    """Past ``end_date`` transitions active contracts to expired."""

    @pytest.mark.asyncio
    async def test_past_end_date_transitions_to_expired(self):
        row = _make_contract_row(
            contract_id="c-expired",
            start_date="2025-01-01",
            end_date="2026-05-31",
            contracted_gallons=1000.0,
            remaining_gallons=750.0,  # still gallons left
            version=1,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-expired", today=date(2026, 6, 1)
        )

        assert new_status == "expired"
        stored = es.get_row("c-expired")
        assert stored["status"] == "expired"
        assert stored["version"] == 2

    @pytest.mark.asyncio
    async def test_end_date_equal_to_today_is_still_active(self):
        """``end_date`` is inclusive — the contract expires the day AFTER."""
        row = _make_contract_row(
            contract_id="c-last-day",
            end_date="2026-06-01",
            remaining_gallons=500.0,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-last-day", today=date(2026, 6, 1)
        )

        assert new_status is None
        assert es.get_row("c-last-day")["status"] == "active"
        assert es.update_calls == []


class TestCheckAndTransitionContractBothConditions:
    """Zero gallons AND past end_date — ``exhausted`` wins per spec preference."""

    @pytest.mark.asyncio
    async def test_zero_gallons_and_expired_prefers_exhausted(self):
        row = _make_contract_row(
            contract_id="c-both",
            start_date="2024-01-01",
            end_date="2025-12-31",
            contracted_gallons=1000.0,
            remaining_gallons=0.0,
            version=4,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-both", today=date(2026, 6, 1)
        )

        # Either terminal state satisfies the spec, but the service
        # prefers ``exhausted`` because it carries more business
        # information (the allotment was actually consumed).
        assert new_status == "exhausted"
        stored = es.get_row("c-both")
        assert stored["status"] == "exhausted"
        assert stored["version"] == 5


class TestCheckAndTransitionContractNoOp:
    """Active contracts that haven't hit either trigger are untouched."""

    @pytest.mark.asyncio
    async def test_non_expired_with_gallons_left_is_unchanged(self):
        row = _make_contract_row(
            contract_id="c-running",
            end_date="2099-12-31",
            contracted_gallons=1000.0,
            remaining_gallons=500.0,
            version=7,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        new_status = await service.check_and_transition_contract(
            "c-running", today=date(2026, 6, 1)
        )

        assert new_status is None
        stored = es.get_row("c-running")
        assert stored["status"] == "active"
        assert stored["version"] == 7  # version NOT bumped
        assert es.update_calls == []


class TestCheckAndTransitionContractInputValidation:
    """Input validation mirrors the rest of the service surface."""

    @pytest.mark.asyncio
    async def test_empty_contract_id_rejected(self):
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")
        with pytest.raises(ValueError):
            await service.check_and_transition_contract("")
        with pytest.raises(ValueError):
            await service.check_and_transition_contract("   ")

    @pytest.mark.asyncio
    async def test_non_date_today_rejected(self):
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")
        with pytest.raises(ValueError):
            await service.check_and_transition_contract(
                "c-1", today="2026-06-01"  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_unknown_contract_raises_not_found(self):
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")
        with pytest.raises(ValueError) as excinfo:
            await service.check_and_transition_contract("c-missing")
        assert "contract_not_found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# check_expiry — tenant-wide scan
# ---------------------------------------------------------------------------


class TestCheckExpiry:
    """Scan every active contract and return the transitioned ids."""

    @pytest.mark.asyncio
    async def test_transitions_zero_gallons_to_exhausted(self):
        rows = [
            _make_contract_row(
                contract_id="c-1",
                end_date="2099-12-31",
                remaining_gallons=0.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        transitioned = await service.check_expiry(today=date(2026, 6, 1))

        assert transitioned == ["c-1"]
        assert es.get_row("c-1")["status"] == "exhausted"

    @pytest.mark.asyncio
    async def test_transitions_past_end_date_to_expired(self):
        rows = [
            _make_contract_row(
                contract_id="c-2",
                start_date="2025-01-01",
                end_date="2025-12-31",
                remaining_gallons=500.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        transitioned = await service.check_expiry(today=date(2026, 6, 1))

        assert transitioned == ["c-2"]
        assert es.get_row("c-2")["status"] == "expired"

    @pytest.mark.asyncio
    async def test_leaves_running_contracts_unchanged(self):
        rows = [
            _make_contract_row(
                contract_id="c-running",
                end_date="2099-12-31",
                remaining_gallons=500.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        transitioned = await service.check_expiry(today=date(2026, 6, 1))

        assert transitioned == []
        assert es.get_row("c-running")["status"] == "active"
        assert es.update_calls == []

    @pytest.mark.asyncio
    async def test_scan_filters_on_tenant_and_active_status(self):
        """Cross-tenant rows and already-terminal rows are excluded."""
        rows = [
            _make_contract_row(
                contract_id="c-mine",
                tenant_id="tenant-1",
                end_date="2099-12-31",
                remaining_gallons=0.0,
            ),
            # Different tenant — must not be touched.
            _make_contract_row(
                contract_id="c-other-tenant",
                tenant_id="tenant-2",
                start_date="2024-01-01",
                end_date="2025-01-01",
                remaining_gallons=0.0,
            ),
            # Already-expired row in our tenant — skipped.
            _make_contract_row(
                contract_id="c-already-done",
                tenant_id="tenant-1",
                status="expired",
                start_date="2024-01-01",
                end_date="2025-01-01",
                remaining_gallons=100.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        transitioned = await service.check_expiry(today=date(2026, 6, 1))

        assert transitioned == ["c-mine"]
        # Cross-tenant row untouched.
        assert es.get_row("c-other-tenant")["status"] == "active"
        # Already-terminal row untouched.
        assert es.get_row("c-already-done")["status"] == "expired"

    @pytest.mark.asyncio
    async def test_mixed_batch_of_transitions(self):
        """Exhausted and expired rows coexist in a single sweep."""
        rows = [
            _make_contract_row(
                contract_id="c-a",  # exhausted
                end_date="2099-12-31",
                remaining_gallons=0.0,
            ),
            _make_contract_row(
                contract_id="c-b",  # expired
                start_date="2025-01-01",
                end_date="2025-12-31",
                remaining_gallons=250.0,
            ),
            _make_contract_row(
                contract_id="c-c",  # no-op
                end_date="2099-12-31",
                remaining_gallons=750.0,
            ),
            _make_contract_row(
                contract_id="c-d",  # both → exhausted
                start_date="2023-01-01",
                end_date="2024-06-01",
                remaining_gallons=0.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        transitioned = await service.check_expiry(today=date(2026, 6, 1))

        assert set(transitioned) == {"c-a", "c-b", "c-d"}
        assert es.get_row("c-a")["status"] == "exhausted"
        assert es.get_row("c-b")["status"] == "expired"
        assert es.get_row("c-c")["status"] == "active"
        assert es.get_row("c-d")["status"] == "exhausted"

    @pytest.mark.asyncio
    async def test_non_date_today_rejected(self):
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")
        with pytest.raises(ValueError):
            await service.check_expiry(today="2026-06-01")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_scan_hits_the_contracts_index(self):
        """Smoke-test the target index and query shape."""
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")
        await service.check_expiry(today=date(2026, 6, 1))

        assert es.search_calls
        assert es.search_calls[0]["index"] == PRICE_PROTECTION_CONTRACTS_INDEX


# ---------------------------------------------------------------------------
# Cron helper — run_price_protection_expiry_cycle
# ---------------------------------------------------------------------------


class TestRunPriceProtectionExpiryCycle:
    """The daily cron iterates over tenants and aggregates totals."""

    def test_interval_is_daily(self):
        assert PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS == 86_400

    @pytest.mark.asyncio
    async def test_iterates_distinct_tenants_and_returns_total(self):
        rows = [
            _make_contract_row(
                contract_id="c-a",
                tenant_id="tenant-1",
                end_date="2099-12-31",
                remaining_gallons=0.0,
            ),
            _make_contract_row(
                contract_id="c-b",
                tenant_id="tenant-2",
                start_date="2025-01-01",
                end_date="2025-12-31",
                remaining_gallons=250.0,
            ),
            _make_contract_row(
                contract_id="c-c",
                tenant_id="tenant-2",
                end_date="2099-12-31",
                remaining_gallons=500.0,
            ),
        ]
        es = _FakeESService(rows)

        total = await run_price_protection_expiry_cycle(es_service=es)

        # tenant-1 had one exhausted contract; tenant-2 had one
        # expired contract (c-b) and one no-op (c-c).
        assert total == 2
        assert es.get_row("c-a")["status"] == "exhausted"
        assert es.get_row("c-b")["status"] == "expired"
        assert es.get_row("c-c")["status"] == "active"

    @pytest.mark.asyncio
    async def test_no_tenants_returns_zero(self):
        es = _FakeESService([])
        total = await run_price_protection_expiry_cycle(es_service=es)
        assert total == 0

    @pytest.mark.asyncio
    async def test_scan_failure_returns_zero_and_does_not_raise(self):
        class _BrokenES(_FakeESService):
            async def search_documents(self, *args, **kwargs):
                raise RuntimeError("ES unavailable")

        es = _BrokenES([])
        total = await run_price_protection_expiry_cycle(es_service=es)
        assert total == 0
