"""Unit tests for :meth:`PriceProtectionService.decrement_gallons`.

Covers Task 4.3 of the Fuel Compliance Backbone spec: atomic
decrement of a contract's ``remaining_gallons`` with version-based
optimistic concurrency control.

The test suite uses an async fake ES service that persists contract
rows keyed on ``contract_id``, routes ``search_documents`` and
``update_document`` calls through the same storage, and simulates
concurrent writers via a ``force_conflicts(n)`` hook that flips
``version`` / ``remaining_gallons`` on the n first writes so the
CAS retry loop observes a mismatch.

Validates: Requirement 3.4
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.models.price_protection_contract import PriceProtectionContract
from commerce.services.price_protection_service import PriceProtectionService
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in with per-``contract_id`` storage.

    ``search_documents`` emulates the tenant-scoped ``contract_id``
    term query emitted by ``_fetch_contract``.
    ``update_document`` merges the partial-update patch into the
    stored row. A configurable ``force_conflicts(n)`` hook simulates
    a concurrent writer by tweaking the stored row's ``version`` /
    ``remaining_gallons`` immediately after our write so the post-write
    verification re-read observes a mismatch.
    """

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            self._rows[row["contract_id"]] = dict(row)
        self.search_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []
        self._force_conflicts_remaining = 0

    # -- configuration hooks -------------------------------------------------

    def force_conflicts(self, n: int) -> None:
        """Flip the stored row on the next ``n`` successful writes."""
        self._force_conflicts_remaining = int(n)

    # -- ES surface ----------------------------------------------------------

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})

        tenant_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        tenant_term: Optional[str] = None
        for clause in tenant_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_term = clause["term"]["tenant_id"]
                break

        inner_filters = (
            (
                (((query or {}).get("query") or {}).get("bool") or {})
                .get("must", [])
            )
            or []
        )
        inner_bool = inner_filters[0].get("bool", {}) if inner_filters else {}
        inner_filter = inner_bool.get("filter", []) if inner_bool else []

        contract_id: Optional[str] = None
        for clause in inner_filter:
            if "term" in clause and "contract_id" in clause["term"]:
                contract_id = clause["term"]["contract_id"]
                break

        matching: List[Dict[str, Any]] = []
        for row in self._rows.values():
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if contract_id is not None and row.get("contract_id") != contract_id:
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

        # Simulate a concurrent writer landing right after our commit:
        # bump the stored version past the value we just wrote and
        # tweak remaining_gallons so the post-write re-read observes a
        # mismatch and triggers the CAS retry.
        if self._force_conflicts_remaining > 0:
            self._force_conflicts_remaining -= 1
            existing["version"] = int(existing.get("version", 0)) + 1
            existing["remaining_gallons"] = (
                float(existing.get("remaining_gallons", 0.0)) - 0.5
            )


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
# Tests
# ---------------------------------------------------------------------------


class TestDecrementGallonsHappyPath:
    @pytest.mark.asyncio
    async def test_reduces_remaining_gallons(self):
        """Successful decrement subtracts gallons from remaining_gallons."""
        row = _make_contract_row(
            contract_id="contract-1",
            contracted_gallons=1000.0,
            remaining_gallons=500.0,
            version=3,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        refreshed = await service.decrement_gallons("contract-1", 200.0)

        assert refreshed.remaining_gallons == pytest.approx(300.0)
        # One partial update must have been issued to the right index/id.
        assert es.update_calls
        assert es.update_calls[0]["index"] == PRICE_PROTECTION_CONTRACTS_INDEX
        assert es.update_calls[0]["id"] == "contract-1"
        assert es.update_calls[0]["partial"]["remaining_gallons"] == pytest.approx(
            300.0
        )

    @pytest.mark.asyncio
    async def test_increments_version(self):
        """Successful decrement bumps version by exactly 1."""
        row = _make_contract_row(
            contract_id="contract-2",
            contracted_gallons=1000.0,
            remaining_gallons=800.0,
            version=7,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        refreshed = await service.decrement_gallons("contract-2", 100.0)

        assert refreshed.version == 8

    @pytest.mark.asyncio
    async def test_decrement_exactly_to_zero(self):
        """Decrementing the entire remainder leaves remaining_gallons at 0."""
        row = _make_contract_row(
            contract_id="contract-3",
            contracted_gallons=500.0,
            remaining_gallons=500.0,
            version=0,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        refreshed = await service.decrement_gallons("contract-3", 500.0)

        assert refreshed.remaining_gallons == pytest.approx(0.0)
        assert refreshed.version == 1


class TestDecrementGallonsRejections:
    @pytest.mark.asyncio
    async def test_raises_contract_not_found(self):
        """Missing contract_id → ValueError('contract_not_found')."""
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("missing-contract", 100.0)

        assert "contract_not_found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_raises_on_cross_tenant_contract(self):
        """Cross-tenant contract is invisible to the service → not_found."""
        row = _make_contract_row(
            contract_id="contract-other",
            tenant_id="tenant-other",
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-other", 100.0)

        assert "contract_not_found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_raises_when_gallons_exceed_remaining(self):
        """gallons > remaining_gallons → insufficient_remaining_gallons."""
        row = _make_contract_row(
            contract_id="contract-short",
            contracted_gallons=1000.0,
            remaining_gallons=50.0,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-short", 100.0)

        assert "insufficient_remaining_gallons" in str(excinfo.value)
        # No partial update must have been issued.
        assert not es.update_calls

    @pytest.mark.asyncio
    async def test_raises_on_zero_gallons(self):
        """gallons == 0 → rejected as non-positive."""
        row = _make_contract_row(contract_id="contract-4")
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-4", 0.0)

        assert "gallons" in str(excinfo.value)
        assert not es.update_calls

    @pytest.mark.asyncio
    async def test_raises_on_negative_gallons(self):
        """Negative gallons → rejected before any ES call."""
        row = _make_contract_row(contract_id="contract-5")
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-5", -25.0)

        assert "gallons" in str(excinfo.value)
        assert not es.update_calls

    @pytest.mark.asyncio
    async def test_raises_on_empty_contract_id(self):
        """Blank contract_id is rejected before any ES call."""
        es = _FakeESService([])
        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("   ", 10.0)

        assert "contract_id" in str(excinfo.value)


class TestDecrementGallonsConcurrency:
    """OCC retry-loop behaviour under simulated concurrent writers."""

    @pytest.mark.asyncio
    async def test_recovers_from_single_occ_conflict(self, monkeypatch):
        """One simulated concurrent writer → service retries and succeeds."""
        # Kill the sleep to keep the test fast.
        import commerce.services.price_protection_service as ppm

        async def _fast_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(ppm.asyncio, "sleep", _fast_sleep)

        row = _make_contract_row(
            contract_id="contract-occ",
            contracted_gallons=1000.0,
            remaining_gallons=400.0,
            version=2,
        )
        es = _FakeESService([row])
        es.force_conflicts(1)

        service = PriceProtectionService(es, "tenant-1")

        refreshed = await service.decrement_gallons("contract-occ", 100.0)

        # Two update calls total: the first one was "conflicted" by the
        # fake, the second one succeeded.
        assert len(es.update_calls) == 2
        # Trace (initial version=2, initial remaining=400.0):
        #   attempt 1: fetch v=2 rem=400 → write v=3 rem=300 → fake
        #     conflict bumps stored to v=4 rem=299.5 → verify sees v=4,
        #     retries.
        #   attempt 2: fetch v=4 rem=299.5 → write v=5 rem=199.5 → no
        #     more conflicts → verify matches, success.
        assert refreshed.version == 5
        assert refreshed.remaining_gallons == pytest.approx(199.5)

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_raises(self, monkeypatch):
        """Persistent conflicts → ValueError after retry budget exhausted."""
        import commerce.services.price_protection_service as ppm

        async def _fast_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(ppm.asyncio, "sleep", _fast_sleep)

        row = _make_contract_row(
            contract_id="contract-occ-hot",
            contracted_gallons=1000.0,
            remaining_gallons=400.0,
            version=0,
        )
        es = _FakeESService([row])
        # Force a conflict on every single retry attempt.
        es.force_conflicts(ppm._MAX_DECREMENT_RETRIES)

        service = PriceProtectionService(es, "tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await service.decrement_gallons("contract-occ-hot", 50.0)

        assert "retry exhausted" in str(excinfo.value)
        assert len(es.update_calls) == ppm._MAX_DECREMENT_RETRIES


class TestDecrementGallonsQuery:
    @pytest.mark.asyncio
    async def test_scopes_lookup_to_service_tenant(self):
        """The fetch query includes the tenant filter injected by tenant_guard."""
        row = _make_contract_row(contract_id="contract-scoped")
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        await service.decrement_gallons("contract-scoped", 10.0)

        assert es.search_calls
        first_query = es.search_calls[0]["query"]
        outer_filter = (
            ((first_query.get("query") or {}).get("bool") or {}).get("filter") or []
        )
        tenant_terms = [
            clause["term"]["tenant_id"]
            for clause in outer_filter
            if "term" in clause and "tenant_id" in clause["term"]
        ]
        assert tenant_terms == ["tenant-1"]
