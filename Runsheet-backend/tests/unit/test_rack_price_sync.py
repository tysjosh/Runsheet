"""
Unit + property-based tests for :mod:`integrations.rack_price_sync`.

Covers Capability 8 / Task 7.4 / Requirements 8.2.3 and 8.2.5:

* Every fetched :class:`RackPrice` row is persisted to ``rack_prices`` ES
  with the full tuple (tenant_id, terminal_id, product_code, price,
  branded_flag, effective_at, retrieved_at, provider, supplier_brand).
* On provider failure or 10-second timeout, the service falls back to
  the most-recent cached price within 24 hours and annotates the
  recommendation with ``rack_price_fallback: true``.
* Cross-tenant rows never leak through either the sync or fallback path.
* The ``annotate_rack_price_fallback`` helper is idempotent.
* Property-based tests (:mod:`hypothesis`) verify:
    - Sync persistence is complete (every provider row → one ES doc).
    - Fallback always returns at most one row per (terminal, product)
      pair and never exceeds the 24-hour freshness window.

Validates: Requirements 8.2.3, 8.2.5.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings as hypo_settings, strategies as st

from fuel.terminal_models import SourcingRecommendation, TerminalCandidate
from integrations.rack_price_provider_base import (
    RackPrice,
    RackPriceProvider,
)
from integrations.rack_price_sync import (
    DEFAULT_FALLBACK_WINDOW_HOURS,
    RACK_PRICE_FALLBACK_FIELD,
    RackPriceSyncResult,
    RackPriceSyncService,
    _select_latest_per_pair,
    annotate_rack_price_fallback,
)
from services.external_call_tracing import default_circuit_breaker


# ---------------------------------------------------------------------------
# Shared circuit-breaker isolation
#
# Rack-price providers share the process-wide :data:`default_circuit_breaker`
# with the weather/traffic provider tests (key shape is
# ``(tenant_id, provider)``). Resetting between tests keeps the property
# test's 60 Hypothesis iterations from inheriting an OPEN breaker from a
# prior module's failure-path case.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_default_circuit_breaker():
    """Reset the shared breaker between tests to avoid cross-test leaks."""
    await default_circuit_breaker.reset()
    yield
    await default_circuit_breaker.reset()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeES:
    """Minimal in-memory ES stub supporting index + search by tenant/terminal/product."""

    docs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    index_calls: List[Dict[str, Any]] = field(default_factory=list)
    search_calls: List[Dict[str, Any]] = field(default_factory=list)
    raise_on_index: Optional[Exception] = None
    raise_on_search: Optional[Exception] = None

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]) -> None:
        self.index_calls.append({"index": index, "id": doc_id, "document": dict(document)})
        if self.raise_on_index is not None:
            raise self.raise_on_index
        self.docs[doc_id] = dict(document)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 100
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        if self.raise_on_search is not None:
            raise self.raise_on_search

        must = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id: Optional[str] = None
        terminal_filter: Optional[List[str]] = None
        product_filter: Optional[List[str]] = None
        cutoff: Optional[datetime] = None
        for clause in must:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_id = clause["term"]["tenant_id"]
            if "terms" in clause and "terminal_id" in clause["terms"]:
                terminal_filter = list(clause["terms"]["terminal_id"])
            if "terms" in clause and "product_code" in clause["terms"]:
                product_filter = list(clause["terms"]["product_code"])
            if "range" in clause and "retrieved_at" in clause["range"]:
                gte = clause["range"]["retrieved_at"].get("gte")
                if isinstance(gte, str):
                    cutoff = datetime.fromisoformat(gte.replace("Z", "+00:00"))

        hits: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if terminal_filter is not None and doc.get("terminal_id") not in terminal_filter:
                continue
            if product_filter is not None and doc.get("product_code") not in product_filter:
                continue
            if cutoff is not None:
                retrieved = doc.get("retrieved_at")
                if isinstance(retrieved, str):
                    try:
                        retrieved_dt = datetime.fromisoformat(
                            retrieved.replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                    if retrieved_dt < cutoff:
                        continue
            hits.append({"_source": dict(doc)})

        # Sort newest-first on effective_at, tiebreak retrieved_at desc
        def _key(hit: Dict[str, Any]) -> Tuple[str, str]:
            src = hit["_source"]
            return (src.get("effective_at", ""), src.get("retrieved_at", ""))

        hits.sort(key=_key, reverse=True)
        return {"hits": {"hits": hits[:size], "total": {"value": len(hits)}}}


class _StaticProvider(RackPriceProvider):
    """Provider stub returning a configured list (or raising a configured error)."""

    name = "stub"  # type: ignore[assignment]

    def __init__(
        self,
        rows: Optional[List[RackPrice]] = None,
        *,
        raise_exc: Optional[BaseException] = None,
        delay_seconds: float = 0.0,
    ) -> None:
        super().__init__(redis_client=None, http_client=None)
        self._rows = list(rows or [])
        self._raise = raise_exc
        self._delay = delay_seconds
        self.calls: List[Dict[str, Any]] = []

    async def _fetch_raw(
        self,
        *,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        self.calls.append(
            {
                "terminal_ids": list(terminal_ids),
                "product_codes": list(product_codes),
                "as_of": as_of,
                "tenant_id": tenant_id,
            }
        )
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        # Filter configured rows to the requested cross-product so the stub
        # behaves like a real provider under sparse caller inputs.
        out: List[RackPrice] = []
        t_set = set(terminal_ids)
        p_set = set(product_codes)
        for row in self._rows:
            if row.terminal_id in t_set and row.product_code in p_set:
                out.append(row.model_copy(update={"tenant_id": tenant_id}))
        return out


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------


def _row(
    *,
    terminal_id: str = "term_01",
    product_code: str = "DIESEL_2",
    price: float = 3.45,
    tenant: str = "t-1",
    provider: str = "stub",
    effective_at: Optional[datetime] = None,
    retrieved_at: Optional[datetime] = None,
    branded: bool = False,
    supplier_brand: Optional[str] = None,
    rack_price_id: Optional[str] = None,
) -> RackPrice:
    eff = effective_at or datetime(2024, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    return RackPrice(
        rack_price_id=rack_price_id or f"rp_{uuid4()}",
        tenant_id=tenant,
        terminal_id=terminal_id,
        product_code=product_code,
        price_per_gallon_usd=price,
        branded_flag=branded,
        supplier_brand=supplier_brand,
        provider=provider,
        effective_at=eff,
        retrieved_at=retrieved_at or eff + timedelta(minutes=5),
    )


def _as_of(hour: int = 12, minute: int = 30) -> datetime:
    return datetime(2024, 10, 15, hour, minute, 0, tzinfo=timezone.utc)


def _recommendation(
    *,
    tenant: str = "t-1",
    fallback: bool = False,
) -> SourcingRecommendation:
    return SourcingRecommendation(
        recommendation_id=f"srec_{uuid4()}",
        request_id=f"req_{uuid4()}",
        tenant_id=tenant,
        product_code="DIESEL_2",
        volume_gallons=8500.0,
        origin_lat=40.0,
        origin_lon=-75.0,
        candidates=[
            TerminalCandidate(
                terminal_id="term_01",
                price_per_gallon_usd=3.25,
                branded_flag=False,
                avg_wait_minutes=15.0,
                distance_km_from_start=12.5,
                score=0.8,
                reasons=["best_price"],
            )
        ],
        rack_price_fallback=fallback,
        generated_at=_as_of(),
    )


# ---------------------------------------------------------------------------
# Construction / argument validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError, match="es_service"):
            RackPriceSyncService(None)  # type: ignore[arg-type]

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError, match="fallback_window_hours"):
            RackPriceSyncService(_FakeES(), fallback_window_hours=0)

    def test_rejects_non_positive_timeout(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            RackPriceSyncService(_FakeES(), timeout_seconds=0.0)


class TestSyncArgValidation:
    @pytest.mark.asyncio
    async def test_rejects_non_provider(self):
        svc = RackPriceSyncService(_FakeES())
        with pytest.raises(TypeError, match="RackPriceProvider"):
            await svc.sync(
                provider="not_a_provider",  # type: ignore[arg-type]
                tenant_id="t-1",
                terminal_ids=["term_01"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
            )

    @pytest.mark.asyncio
    async def test_rejects_blank_tenant(self):
        svc = RackPriceSyncService(_FakeES())
        provider = _StaticProvider(rows=[])
        with pytest.raises(ValueError, match="tenant_id"):
            await svc.sync(
                provider=provider,
                tenant_id="   ",
                terminal_ids=["term_01"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
            )

    @pytest.mark.asyncio
    async def test_rejects_string_as_terminals_sequence(self):
        svc = RackPriceSyncService(_FakeES())
        provider = _StaticProvider(rows=[])
        with pytest.raises(TypeError, match="terminal_ids must be a sequence"):
            await svc.sync(
                provider=provider,
                tenant_id="t-1",
                terminal_ids="term_01",  # type: ignore[arg-type]
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
            )


# ---------------------------------------------------------------------------
# Happy path — every fetched row is persisted
# ---------------------------------------------------------------------------


class TestSyncPersistence:
    """Requirement 8.2.3: persist every fetched price to rack_prices."""

    @pytest.mark.asyncio
    async def test_every_fetched_row_is_indexed(self):
        es = _FakeES()
        rows = [
            _row(terminal_id="term_a", product_code="DIESEL_2", price=3.10),
            _row(terminal_id="term_b", product_code="GASOLINE_REG", price=3.80),
            _row(terminal_id="term_c", product_code="PROPANE", price=2.20, branded=True, supplier_brand="Shell"),
        ]
        provider = _StaticProvider(rows=rows)
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a", "term_b", "term_c"],
            product_codes=["DIESEL_2", "GASOLINE_REG", "PROPANE"],
            as_of=_as_of(),
        )

        # One index_document call per row.
        assert len(es.index_calls) == 3
        assert result.persisted_count == 3
        assert result.rack_price_fallback is False
        assert result.fallback_count == 0

        # Every mandatory field from Requirement 8.2.3 is present.
        for call in es.index_calls:
            doc = call["document"]
            for required in (
                "tenant_id",
                "terminal_id",
                "product_code",
                "price_per_gallon_usd",
                "branded_flag",
                "effective_at",
                "retrieved_at",
                "provider",
            ):
                assert required in doc, f"missing {required} in persisted doc"
            assert doc["tenant_id"] == "t-1"

        # The branded row preserved its supplier_brand.
        branded_doc = next(c for c in es.index_calls if c["document"]["terminal_id"] == "term_c")
        assert branded_doc["document"]["supplier_brand"] == "Shell"
        assert branded_doc["document"]["branded_flag"] is True

        # Result rows are sorted by (terminal_id, product_code, effective_at).
        assert [(r.terminal_id, r.product_code) for r in result.prices] == [
            ("term_a", "DIESEL_2"),
            ("term_b", "GASOLINE_REG"),
            ("term_c", "PROPANE"),
        ]

    @pytest.mark.asyncio
    async def test_dedupes_to_latest_per_pair_when_provider_returns_duplicates(self):
        es = _FakeES()
        base = _as_of(hour=10)
        duplicates = [
            _row(
                terminal_id="term_a",
                product_code="DIESEL_2",
                price=3.10,
                effective_at=base,
            ),
            _row(
                terminal_id="term_a",
                product_code="DIESEL_2",
                price=3.25,
                effective_at=base + timedelta(minutes=30),
            ),
            _row(
                terminal_id="term_a",
                product_code="DIESEL_2",
                price=3.30,
                effective_at=base - timedelta(hours=1),
            ),
        ]
        provider = _StaticProvider(rows=duplicates)
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(hour=12),
        )

        # All three rows are persisted (source of truth), but the
        # result collapses to the latest per pair.
        assert result.persisted_count == 3
        assert len(result.prices) == 1
        assert result.prices[0].price_per_gallon_usd == 3.25

    @pytest.mark.asyncio
    async def test_skips_cross_tenant_rows_from_buggy_provider(self):
        es = _FakeES()
        rows = [
            _row(terminal_id="term_a", tenant="t-1", price=3.10),
            _row(terminal_id="term_b", tenant="t-OTHER", price=3.80),
        ]
        provider = _StaticProvider(rows=[])
        # Inject rows via a custom _fetch_raw that bypasses the stub's tenant re-stamp.
        async def _fetch_raw(*, terminal_ids, product_codes, as_of, tenant_id):
            return rows  # deliberately wrong tenant on one row
        provider._fetch_raw = _fetch_raw  # type: ignore[method-assign]

        svc = RackPriceSyncService(es)
        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a", "term_b"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
        )

        # Only the correctly-tenanted row was persisted.
        assert result.persisted_count == 1
        assert result.rack_price_fallback is False
        assert [c["document"]["tenant_id"] for c in es.index_calls] == ["t-1"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_terminals_or_products(self):
        es = _FakeES()
        provider = _StaticProvider(rows=[])
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=[],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
        )
        assert result.prices == []
        assert result.rack_price_fallback is False
        # Provider never invoked.
        assert provider.calls == []


# ---------------------------------------------------------------------------
# Fallback path — Requirement 8.2.5
# ---------------------------------------------------------------------------


class TestFallback:
    """Requirement 8.2.5: on provider failure, fall back to cached prices within 24h."""

    async def _seed_fallback_row(
        self,
        es: _FakeES,
        *,
        tenant: str,
        terminal_id: str,
        product_code: str,
        price: float,
        retrieved_offset: timedelta,
        as_of: datetime,
        effective_offset: Optional[timedelta] = None,
    ) -> None:
        effective = as_of + (effective_offset if effective_offset else retrieved_offset)
        retrieved = as_of + retrieved_offset
        row = _row(
            terminal_id=terminal_id,
            product_code=product_code,
            price=price,
            tenant=tenant,
            effective_at=effective,
            retrieved_at=retrieved,
        )
        doc = row.model_dump(mode="json")
        es.docs[row.rack_price_id] = doc

    @pytest.mark.asyncio
    async def test_provider_failure_triggers_fallback_annotation(self):
        es = _FakeES()
        as_of = _as_of()
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.15,
            retrieved_offset=-timedelta(hours=2),
            as_of=as_of,
        )
        provider = _StaticProvider(raise_exc=RuntimeError("provider exploded"))
        svc = RackPriceSyncService(es)

        # The provider base class catches RuntimeError via `_fetch_with_timeout`
        # so it degrades to [] and triggers the sync-level fallback.
        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
        )

        assert result.rack_price_fallback is True
        assert result.persisted_count == 0
        assert result.fallback_count == 1
        assert result.prices[0].terminal_id == "term_a"
        assert result.prices[0].price_per_gallon_usd == 3.15

    @pytest.mark.asyncio
    async def test_provider_timeout_triggers_fallback(self):
        es = _FakeES()
        as_of = _as_of()
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.10,
            retrieved_offset=-timedelta(hours=1),
            as_of=as_of,
        )
        # Provider sleeps longer than the sync timeout.
        provider = _StaticProvider(delay_seconds=2.0)
        svc = RackPriceSyncService(es, timeout_seconds=0.05)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
        )

        assert result.rack_price_fallback is True
        assert result.persisted_count == 0
        assert len(result.prices) == 1

    @pytest.mark.asyncio
    async def test_fallback_honors_24_hour_window(self):
        es = _FakeES()
        as_of = _as_of()
        # Row A: within the window (retrieved 5h ago) — should be returned.
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.10,
            retrieved_offset=-timedelta(hours=5),
            as_of=as_of,
        )
        # Row B: outside the window (retrieved 30h ago) — should be filtered.
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_b",
            product_code="DIESEL_2",
            price=3.30,
            retrieved_offset=-timedelta(hours=30),
            as_of=as_of,
        )
        provider = _StaticProvider(raise_exc=RuntimeError("down"))
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a", "term_b"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
        )

        assert result.rack_price_fallback is True
        terminals = {r.terminal_id for r in result.prices}
        assert terminals == {"term_a"}

    @pytest.mark.asyncio
    async def test_fallback_prefers_latest_effective_per_pair(self):
        es = _FakeES()
        as_of = _as_of()
        # Same terminal/product pair; the older effective_at should lose.
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.00,
            retrieved_offset=-timedelta(hours=6),
            as_of=as_of,
            effective_offset=-timedelta(hours=6),
        )
        await self._seed_fallback_row(
            es,
            tenant="t-1",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.50,
            retrieved_offset=-timedelta(hours=1),
            as_of=as_of,
            effective_offset=-timedelta(hours=1),
        )
        provider = _StaticProvider(raise_exc=RuntimeError("down"))
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
        )

        assert len(result.prices) == 1
        assert result.prices[0].price_per_gallon_usd == 3.50

    @pytest.mark.asyncio
    async def test_fallback_isolated_per_tenant(self):
        es = _FakeES()
        as_of = _as_of()
        # A row exists for a *different* tenant only.
        await self._seed_fallback_row(
            es,
            tenant="t-OTHER",
            terminal_id="term_a",
            product_code="DIESEL_2",
            price=3.10,
            retrieved_offset=-timedelta(hours=2),
            as_of=as_of,
        )
        provider = _StaticProvider(raise_exc=RuntimeError("down"))
        svc = RackPriceSyncService(es)

        result = await svc.sync(
            provider=provider,
            tenant_id="t-1",
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
        )

        assert result.rack_price_fallback is True
        # The flag stays set even though no row was available — the
        # service's intent to use historical data is preserved.
        assert result.fallback_count == 0
        assert result.prices == []


# ---------------------------------------------------------------------------
# Annotation helper
# ---------------------------------------------------------------------------


class TestAnnotateRackPriceFallback:
    def test_sets_flag_true(self):
        rec = _recommendation(fallback=False)
        annotated = annotate_rack_price_fallback(rec, True)
        assert annotated.rack_price_fallback is True
        # Original untouched.
        assert rec.rack_price_fallback is False

    def test_noop_when_flag_already_matches(self):
        rec = _recommendation(fallback=True)
        annotated = annotate_rack_price_fallback(rec, True)
        # Same instance when flag already matches (zero-allocation path).
        assert annotated is rec

    def test_idempotent(self):
        rec = _recommendation(fallback=False)
        once = annotate_rack_price_fallback(rec, True)
        twice = annotate_rack_price_fallback(once, True)
        assert once == twice
        assert twice.rack_price_fallback is True

    def test_rejects_non_recommendation(self):
        with pytest.raises(TypeError, match="SourcingRecommendation"):
            annotate_rack_price_fallback({"not": "a recommendation"}, True)  # type: ignore[arg-type]

    def test_rejects_non_bool(self):
        rec = _recommendation()
        with pytest.raises(TypeError, match="bool"):
            annotate_rack_price_fallback(rec, "true")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helper: _select_latest_per_pair
# ---------------------------------------------------------------------------


class TestSelectLatestPerPair:
    def test_single_row_returns_itself(self):
        row = _row()
        assert _select_latest_per_pair([row]) == [row]

    def test_latest_effective_wins(self):
        base = _as_of(hour=10)
        early = _row(effective_at=base)
        late = _row(effective_at=base + timedelta(hours=1))
        out = _select_latest_per_pair([early, late])
        assert out == [late]

    def test_ties_break_on_retrieved_at(self):
        base = _as_of(hour=10)
        older = _row(effective_at=base, retrieved_at=base + timedelta(minutes=1))
        newer = _row(effective_at=base, retrieved_at=base + timedelta(minutes=10))
        out = _select_latest_per_pair([older, newer])
        assert out == [newer]


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------


_TERMINAL_IDS = ["term_a", "term_b", "term_c"]
_PRODUCT_CODES = ["DIESEL_2", "GASOLINE_REG", "PROPANE"]


def _row_strategy() -> st.SearchStrategy[RackPrice]:
    """Build random RackPrice rows over a fixed terminal/product space."""

    # Keep the effective_at/retrieved_at window bounded so the fallback
    # property can reason about 24-hour inclusion.
    as_of = _as_of()
    min_ts = as_of - timedelta(hours=48)

    def _build(
        terminal_id: str,
        product_code: str,
        price: float,
        eff_offset_minutes: int,
        retr_offset_minutes: int,
        branded: bool,
        brand: str,
    ) -> RackPrice:
        effective = min_ts + timedelta(minutes=eff_offset_minutes)
        # retrieved_at must be >= effective_at in practice, but the model
        # does not enforce it so hypothesis is free to flip them. Clamp
        # retrieved_at to be no earlier than effective_at to keep the
        # generated corpus realistic.
        retrieved = max(effective, min_ts + timedelta(minutes=retr_offset_minutes))
        return _row(
            terminal_id=terminal_id,
            product_code=product_code,
            price=round(price, 4),
            tenant="t-1",
            branded=branded,
            supplier_brand=brand if branded else None,
            effective_at=effective,
            retrieved_at=retrieved,
        )

    return st.builds(
        _build,
        terminal_id=st.sampled_from(_TERMINAL_IDS),
        product_code=st.sampled_from(_PRODUCT_CODES),
        price=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        eff_offset_minutes=st.integers(min_value=0, max_value=60 * 48),
        retr_offset_minutes=st.integers(min_value=0, max_value=60 * 48),
        branded=st.booleans(),
        brand=st.sampled_from(["Shell", "BP", "Exxon", "Mobil"]),
    )


@hypo_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rows=st.lists(_row_strategy(), min_size=1, max_size=10))
@pytest.mark.asyncio
async def test_property_every_fetched_row_is_persisted(rows: List[RackPrice]):
    """**Validates: Requirements 8.2.3**

    For any non-empty list of provider-returned rack prices, the sync
    service persists exactly ``len(rows)`` documents (keyed by
    ``rack_price_id``, which the provider uuid-generates uniquely),
    each carrying the full tuple required by Requirement 8.2.3.
    """

    es = _FakeES()
    provider = _StaticProvider(rows=rows)
    svc = RackPriceSyncService(es)

    # Accept every terminal/product pair the generated rows use.
    terminals = sorted({r.terminal_id for r in rows})
    products = sorted({r.product_code for r in rows})

    result = await svc.sync(
        provider=provider,
        tenant_id="t-1",
        terminal_ids=terminals,
        product_codes=products,
        as_of=_as_of(),
    )

    assert result.persisted_count == len(rows)
    assert result.rack_price_fallback is False
    assert len(es.index_calls) == len(rows)

    required_fields = {
        "tenant_id",
        "terminal_id",
        "product_code",
        "price_per_gallon_usd",
        "branded_flag",
        "effective_at",
        "retrieved_at",
        "provider",
    }
    for call in es.index_calls:
        doc = call["document"]
        assert required_fields.issubset(doc.keys()), f"missing fields in {doc}"
        assert doc["tenant_id"] == "t-1"


@hypo_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rows=st.lists(_row_strategy(), min_size=1, max_size=10))
@pytest.mark.asyncio
async def test_property_fallback_returns_one_row_per_pair_within_24h(
    rows: List[RackPrice],
):
    """**Validates: Requirements 8.2.5**

    When the provider fails, the fallback path:
        1. Returns at most one row per (terminal_id, product_code).
        2. Never returns a row whose ``retrieved_at`` is older than
           24 hours before ``as_of``.
        3. Always marks the result with ``rack_price_fallback=True``.
    """

    as_of = _as_of()

    # Seed ES directly so the rows are "cached" before the failed
    # provider call happens.
    es = _FakeES()
    for row in rows:
        doc = row.model_dump(mode="json")
        es.docs[row.rack_price_id] = doc

    provider = _StaticProvider(raise_exc=RuntimeError("down"))
    svc = RackPriceSyncService(es)

    terminals = sorted({r.terminal_id for r in rows})
    products = sorted({r.product_code for r in rows})
    result = await svc.sync(
        provider=provider,
        tenant_id="t-1",
        terminal_ids=terminals,
        product_codes=products,
        as_of=as_of,
    )

    assert result.rack_price_fallback is True

    # Property 1: one row per pair.
    pairs = [(r.terminal_id, r.product_code) for r in result.prices]
    assert len(pairs) == len(set(pairs))

    # Property 2: retrieved_at strictly within the 24-hour window.
    cutoff = as_of - timedelta(hours=DEFAULT_FALLBACK_WINDOW_HOURS)
    for row in result.prices:
        retrieved = row.retrieved_at
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        assert retrieved >= cutoff, f"row retrieved at {retrieved} is older than cutoff {cutoff}"


# ---------------------------------------------------------------------------
# Result model sanity
# ---------------------------------------------------------------------------


class TestResultModel:
    def test_defaults_are_sensible(self):
        result = RackPriceSyncResult(as_of=_as_of())
        assert result.prices == []
        assert result.rack_price_fallback is False
        assert result.persisted_count == 0
        assert result.fallback_count == 0

    def test_rejects_extra_fields(self):
        with pytest.raises(Exception):
            RackPriceSyncResult(as_of=_as_of(), not_a_field=True)  # type: ignore[arg-type]
