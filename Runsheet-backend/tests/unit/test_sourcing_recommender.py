"""
Unit tests for :mod:`fuel.services.sourcing_recommender`.

Covers Capability 8 / Requirements 8.5.1, 8.5.2, 8.5.3 of the fuel-ops
hardening spec:

* :class:`SourcingRecommender.recommend` returns a ranked
  :class:`SourcingRecommendation` — canonicalised product_code, origin
  round-tripped, and ``candidates`` ordered by score descending
  (Req 8.5.1).
* Scoring is a tenant-configurable weighted combination of negative
  price, negative wait, negative distance, plus a contract-match boost
  (Req 8.5.2). Weights loaded from Redis override defaults; malformed
  payloads fall back to defaults without raising.
* Disqualification by ``supported_products``, operating hours, and
  branded/unbranded preference runs before ranking (Req 8.5.3).
* Operational edge cases: missing wait data defaults to zero;
  contract prices override rack prices; empty candidate set yields an
  empty ``candidates`` list without raising.

The repository / rack-price provider / wait resolver dependencies are
stubbed in-memory so tests never touch ES, Redis, or httpx.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fuel.services.sourcing_recommender import (
    DEFAULT_WEIGHTS,
    DEFAULT_WAIT_WARNING_MINUTES,
    InvalidBrandedPreferenceError,
    SourcingRecommender,
    SourcingWeights,
    WAIT_WARNING_REDIS_KEY,
    WEIGHTS_REDIS_KEY,
)
from fuel.terminal_models import (
    OperatingHours,
    SourcingRecommendation,
    SupplierContract,
    SupplierContractRepository,
    Terminal,
    TerminalCandidate,
    TerminalRepository,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTerminalRepo:
    """Stand-in for :class:`TerminalRepository` — returns preloaded list."""

    def __init__(self, terminals: Sequence[Terminal]) -> None:
        self._terminals = list(terminals)
        self.calls: List[Dict[str, Any]] = []

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Terminal]:
        self.calls.append({"tenant_id": tenant_id, "status": status})
        return [t for t in self._terminals if t.tenant_id == tenant_id and
                (status is None or t.status == status)]


class _FakeContractRepo:
    """Stand-in for :class:`SupplierContractRepository`."""

    def __init__(self, contracts: Sequence[SupplierContract]) -> None:
        self._contracts = list(contracts)
        self.calls: List[Dict[str, Any]] = []

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        product_code: Optional[str] = None,
        **kwargs: Any,
    ) -> List[SupplierContract]:
        self.calls.append(
            {"tenant_id": tenant_id, "status": status, "product_code": product_code}
        )
        out: List[SupplierContract] = []
        for c in self._contracts:
            if c.tenant_id != tenant_id:
                continue
            if status is not None and c.status != status:
                continue
            if product_code is not None and c.product_code != product_code:
                continue
            out.append(c)
        return out


@dataclass
class _RackRow:
    terminal_id: str
    product_code: str
    price_per_gallon_usd: float
    effective_at: datetime


class _FakeRackPriceProvider:
    """Async provider returning preloaded rows regardless of input."""

    def __init__(
        self,
        rows: Sequence[_RackRow],
        *,
        raise_on_call: Optional[BaseException] = None,
    ) -> None:
        self._rows = list(rows)
        self._raise = raise_on_call
        self.calls: List[Dict[str, Any]] = []

    async def get_prices(
        self,
        terminal_ids,
        product_codes,
        as_of,
        *,
        tenant_id,
    ):
        self.calls.append(
            {
                "terminal_ids": list(terminal_ids),
                "product_codes": list(product_codes),
                "as_of": as_of,
                "tenant_id": tenant_id,
            }
        )
        if self._raise is not None:
            raise self._raise
        return [
            r
            for r in self._rows
            if r.terminal_id in terminal_ids and r.product_code in product_codes
        ]


class _FakeTenantConfig:
    """Minimal async Redis-handle stub used for weights + wait threshold."""

    def __init__(self, store: Optional[Dict[str, Any]] = None) -> None:
        self.store: Dict[str, Any] = dict(store or {})
        self.get_calls: List[str] = []

    async def get(self, key: str):
        self.get_calls.append(key)
        value = self.store.get(key)
        return value


def _wait_resolver(values: Dict[str, float]):
    async def _resolve(tenant_id: str, terminal_id: str):
        return values.get(terminal_id)

    return _resolve


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


AS_OF = datetime(2024, 11, 12, 14, 0, 0, tzinfo=timezone.utc)


def _operating_hours_24x7() -> List[Dict[str, str]]:
    return [
        {"day_of_week": day, "open": "00:00", "close": "23:59"}
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    ]


def _terminal(
    terminal_id: str = "term_A",
    *,
    tenant_id: str = "tenant-1",
    name: str = "Newark A",
    operator: str = "Buckeye",
    lat: float = 40.70,
    lon: float = -74.15,
    supported_products: Optional[List[str]] = None,
    branded: bool = False,
    supplier_brand: Optional[str] = None,
    operating_hours: Optional[List[Dict[str, str]]] = None,
    timezone_name: str = "UTC",
    status: str = "active",
) -> Terminal:
    return Terminal(
        terminal_id=terminal_id,
        tenant_id=tenant_id,
        name=name,
        operator=operator,
        location_lat=lat,
        location_lon=lon,
        address="123 Port Dr",
        timezone=timezone_name,
        operating_hours=operating_hours or _operating_hours_24x7(),
        supported_products=supported_products or ["DIESEL_2"],
        branded=branded,
        supplier_brand=supplier_brand,
        status=status,
    )


def _contract(
    *,
    contract_id: str = "sc_1",
    tenant_id: str = "tenant-1",
    supplier_name: str = "Buckeye Corp",
    product_code: str = "DIESEL_2",
    preferred_terminal_ids: Optional[List[str]] = None,
    contract_price: Optional[float] = None,
    branded_required: bool = False,
    effective_from: date = date(2024, 1, 1),
    effective_to: Optional[date] = None,
    status: str = "active",
) -> SupplierContract:
    return SupplierContract(
        contract_id=contract_id,
        tenant_id=tenant_id,
        supplier_name=supplier_name,
        product_code=product_code,
        preferred_terminal_ids=preferred_terminal_ids or [],
        contract_price_per_gallon_usd=contract_price,
        branded_required=branded_required,
        effective_from=effective_from,
        effective_to=effective_to,
        status=status,
    )


def _price_row(
    terminal_id: str,
    price: float,
    *,
    product_code: str = "DIESEL_2",
    effective_at: datetime = AS_OF,
) -> _RackRow:
    return _RackRow(
        terminal_id=terminal_id,
        product_code=product_code,
        price_per_gallon_usd=price,
        effective_at=effective_at,
    )


def _build_recommender(
    *,
    terminals: Sequence[Terminal],
    contracts: Sequence[SupplierContract] = (),
    prices: Sequence[_RackRow] = (),
    waits: Optional[Dict[str, float]] = None,
    tenant_config: Optional[_FakeTenantConfig] = None,
    wait_raises: Optional[BaseException] = None,
) -> Tuple[SourcingRecommender, _FakeTerminalRepo, _FakeContractRepo, _FakeRackPriceProvider]:
    t_repo = _FakeTerminalRepo(terminals)
    c_repo = _FakeContractRepo(contracts)
    rack = _FakeRackPriceProvider(prices)
    if wait_raises is not None:
        async def _resolver(tenant_id, terminal_id):
            raise wait_raises
        resolver = _resolver
    else:
        resolver = _wait_resolver(waits or {}) if waits is not None else None
    rec = SourcingRecommender(
        terminal_repo=t_repo,  # type: ignore[arg-type]
        contract_repo=c_repo,  # type: ignore[arg-type]
        rack_price_provider=rack,
        wait_time_resolver=resolver,
        tenant_config=tenant_config,
    )
    return rec, t_repo, c_repo, rack


# ---------------------------------------------------------------------------
# SourcingWeights model
# ---------------------------------------------------------------------------


class TestSourcingWeights:
    def test_defaults_match_design_doc(self) -> None:
        w = SourcingWeights()
        assert w.as_dict() == DEFAULT_WEIGHTS
        # Defaults already sum to 1.0 so normalisation is a no-op.
        n = w.normalised()
        assert pytest.approx(n.total(), abs=1e-9) == 1.0

    def test_rejects_negative_component(self) -> None:
        with pytest.raises(Exception):
            SourcingWeights(price=-0.1, wait=0.25, distance=0.2, contract=0.15)

    def test_rejects_all_zero_weights(self) -> None:
        with pytest.raises(ValueError):
            SourcingWeights(price=0, wait=0, distance=0, contract=0)

    def test_normalised_produces_unit_sum(self) -> None:
        w = SourcingWeights(price=2.0, wait=1.0, distance=1.0, contract=0.0)
        n = w.normalised()
        assert pytest.approx(n.total(), abs=1e-9) == 1.0
        # Proportions preserved (2:1:1:0 → 0.5, 0.25, 0.25, 0.0).
        assert pytest.approx(n.price, abs=1e-9) == 0.5
        assert pytest.approx(n.wait, abs=1e-9) == 0.25
        assert pytest.approx(n.distance, abs=1e-9) == 0.25
        assert pytest.approx(n.contract, abs=1e-9) == 0.0


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestRecommendArgValidation:
    @pytest.fixture
    def recommender(self) -> SourcingRecommender:
        rec, *_ = _build_recommender(terminals=[])
        return rec

    async def test_rejects_blank_tenant(self, recommender: SourcingRecommender) -> None:
        with pytest.raises(ValueError):
            await recommender.recommend(
                tenant_id="",
                product_code="DIESEL_2",
                volume_gallons=1000,
                origin_lat_lon=(40.0, -74.0),
                as_of=AS_OF,
            )

    async def test_rejects_blank_product(self, recommender: SourcingRecommender) -> None:
        with pytest.raises(ValueError):
            await recommender.recommend(
                tenant_id="tenant-1",
                product_code="  ",
                volume_gallons=1000,
                origin_lat_lon=(40.0, -74.0),
                as_of=AS_OF,
            )

    async def test_rejects_non_positive_volume(
        self, recommender: SourcingRecommender
    ) -> None:
        with pytest.raises(ValueError):
            await recommender.recommend(
                tenant_id="tenant-1",
                product_code="DIESEL_2",
                volume_gallons=0,
                origin_lat_lon=(40.0, -74.0),
                as_of=AS_OF,
            )

    async def test_rejects_bad_origin_shape(
        self, recommender: SourcingRecommender
    ) -> None:
        with pytest.raises(TypeError):
            await recommender.recommend(
                tenant_id="tenant-1",
                product_code="DIESEL_2",
                volume_gallons=1000,
                origin_lat_lon=(40.0,),  # type: ignore[arg-type]
                as_of=AS_OF,
            )

    async def test_rejects_out_of_range_lat(
        self, recommender: SourcingRecommender
    ) -> None:
        with pytest.raises(ValueError):
            await recommender.recommend(
                tenant_id="tenant-1",
                product_code="DIESEL_2",
                volume_gallons=1000,
                origin_lat_lon=(200.0, 0.0),
                as_of=AS_OF,
            )

    async def test_rejects_branded_non_bool(
        self, recommender: SourcingRecommender
    ) -> None:
        with pytest.raises(InvalidBrandedPreferenceError):
            await recommender.recommend(
                tenant_id="tenant-1",
                product_code="DIESEL_2",
                volume_gallons=1000,
                origin_lat_lon=(40.0, -74.0),
                as_of=AS_OF,
                branded="yes",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Disqualification — Req 8.5.3
# ---------------------------------------------------------------------------


class TestDisqualification:
    async def test_drops_terminals_missing_product(self) -> None:
        t_eligible = _terminal("term_ok", supported_products=["DIESEL_2"])
        t_missing = _terminal(
            "term_bad",
            supported_products=["GASOLINE_REG"],
            lat=40.75,
        )
        rec, *_, rack = _build_recommender(
            terminals=[t_eligible, t_missing],
            prices=[_price_row("term_ok", 3.45), _price_row("term_bad", 3.10)],
            waits={"term_ok": 10.0, "term_bad": 5.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_ok"]

    async def test_alias_input_matches_canonical_product_list(self) -> None:
        # Terminal supports DIESEL_2 (canonical). Caller passes "AGO" (alias).
        term = _terminal("term_ok", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_ok", 3.45)],
            waits={"term_ok": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="AGO",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.product_code == "DIESEL_2"
        assert len(result.candidates) == 1

    async def test_drops_closed_terminals(self) -> None:
        # A single Mon 06:00-09:00 window — as_of is 14:00 UTC Tuesday.
        tues_hours = [{"day_of_week": "mon", "open": "06:00", "close": "09:00"}]
        closed = _terminal(
            "term_closed",
            supported_products=["DIESEL_2"],
            operating_hours=tues_hours,
        )
        open_t = _terminal(
            "term_open",
            supported_products=["DIESEL_2"],
        )
        rec, *_ = _build_recommender(
            terminals=[closed, open_t],
            prices=[
                _price_row("term_closed", 3.00),
                _price_row("term_open", 3.45),
            ],
            waits={"term_closed": 0.0, "term_open": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_open"]

    async def test_enforces_branded_preference(self) -> None:
        branded = _terminal(
            "term_shell",
            supported_products=["DIESEL_2"],
            branded=True,
            supplier_brand="Shell",
        )
        unbranded = _terminal("term_generic", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[branded, unbranded],
            prices=[
                _price_row("term_shell", 3.50),
                _price_row("term_generic", 3.30),
            ],
            waits={"term_shell": 0.0, "term_generic": 0.0},
        )
        result_branded = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
            branded=True,
        )
        assert [c.terminal_id for c in result_branded.candidates] == ["term_shell"]

        result_unbranded = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
            branded=False,
        )
        assert [c.terminal_id for c in result_unbranded.candidates] == ["term_generic"]

    async def test_terminals_without_rack_price_are_dropped(self) -> None:
        has_price = _terminal("term_priced", supported_products=["DIESEL_2"])
        no_price = _terminal(
            "term_missing",
            supported_products=["DIESEL_2"],
            lat=40.75,
        )
        rec, *_ = _build_recommender(
            terminals=[has_price, no_price],
            prices=[_price_row("term_priced", 3.45)],
            waits={"term_priced": 0.0, "term_missing": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_priced"]

    async def test_empty_candidate_set_returns_empty_candidates(self) -> None:
        rec, *_ = _build_recommender(terminals=[])
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates == []
        assert result.product_code == "DIESEL_2"


# ---------------------------------------------------------------------------
# Ranking — Req 8.5.2
# ---------------------------------------------------------------------------


class TestRanking:
    async def test_cheaper_ranks_higher_with_default_weights(self) -> None:
        cheap = _terminal("term_cheap", supported_products=["DIESEL_2"])
        pricey = _terminal(
            "term_pricey",
            supported_products=["DIESEL_2"],
            lat=40.70,  # same location, isolate price signal
        )
        rec, *_ = _build_recommender(
            terminals=[cheap, pricey],
            prices=[
                _price_row("term_cheap", 3.10),
                _price_row("term_pricey", 3.50),
            ],
            waits={"term_cheap": 0.0, "term_pricey": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_cheap", "term_pricey"]
        assert result.candidates[0].score > result.candidates[1].score

    async def test_shorter_wait_ranks_higher_when_other_signals_equal(self) -> None:
        # Same price and location; differ only on wait.
        short_wait = _terminal("term_short", supported_products=["DIESEL_2"])
        long_wait = _terminal("term_long", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[short_wait, long_wait],
            prices=[
                _price_row("term_short", 3.40),
                _price_row("term_long", 3.40),
            ],
            waits={"term_short": 5.0, "term_long": 45.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_short", "term_long"]

    async def test_closer_ranks_higher_when_other_signals_equal(self) -> None:
        near = _terminal("term_near", supported_products=["DIESEL_2"], lat=40.70)
        # ~11 km south
        far = _terminal(
            "term_far", supported_products=["DIESEL_2"], lat=40.80, lon=-74.15
        )
        rec, *_ = _build_recommender(
            terminals=[near, far],
            prices=[
                _price_row("term_near", 3.40),
                _price_row("term_far", 3.40),
            ],
            waits={"term_near": 0.0, "term_far": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_near", "term_far"]

    async def test_contract_boost_breaks_ties_in_favor_of_contracted(self) -> None:
        # Same price, same location, same wait — the contract boost is the
        # only differentiator so the contracted terminal ranks first.
        contracted = _terminal("term_contracted", supported_products=["DIESEL_2"])
        unbound = _terminal("term_unbound", supported_products=["DIESEL_2"])
        contract = _contract(
            contract_id="sc_boost",
            preferred_terminal_ids=["term_contracted"],
            product_code="DIESEL_2",
        )
        rec, *_ = _build_recommender(
            terminals=[contracted, unbound],
            contracts=[contract],
            prices=[
                _price_row("term_contracted", 3.40),
                _price_row("term_unbound", 3.40),
            ],
            waits={"term_contracted": 0.0, "term_unbound": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].terminal_id == "term_contracted"
        assert result.candidates[0].contract_id == "sc_boost"
        # Higher score than the tie-break peer thanks to the boost.
        assert result.candidates[0].score > result.candidates[1].score

    async def test_contract_price_overrides_rack_price(self) -> None:
        contracted = _terminal("term_c", supported_products=["DIESEL_2"])
        contract = _contract(
            contract_id="sc_fixed",
            preferred_terminal_ids=["term_c"],
            contract_price=2.95,  # forced lower than the rack print
        )
        rec, *_ = _build_recommender(
            terminals=[contracted],
            contracts=[contract],
            prices=[_price_row("term_c", 3.80)],
            waits={"term_c": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].price_per_gallon_usd == pytest.approx(2.95)

    async def test_ties_break_deterministically_by_terminal_id(self) -> None:
        # Two identical terminals — same lat, same price, same wait. The
        # tie-break order defined in the module is:
        # (-score, price, wait, distance, terminal_id).
        a = _terminal("term_aaa", supported_products=["DIESEL_2"])
        b = _terminal("term_bbb", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[b, a],  # repo returns in reverse to prove sort runs
            prices=[
                _price_row("term_aaa", 3.40),
                _price_row("term_bbb", 3.40),
            ],
            waits={"term_aaa": 0.0, "term_bbb": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert [c.terminal_id for c in result.candidates] == ["term_aaa", "term_bbb"]
        assert result.candidates[0].score == result.candidates[1].score

    async def test_score_is_within_zero_and_one(self) -> None:
        terminals = [
            _terminal("term_a", supported_products=["DIESEL_2"], lat=40.70),
            _terminal("term_b", supported_products=["DIESEL_2"], lat=40.72),
            _terminal("term_c", supported_products=["DIESEL_2"], lat=40.74),
        ]
        rec, *_ = _build_recommender(
            terminals=terminals,
            prices=[
                _price_row("term_a", 3.10),
                _price_row("term_b", 3.30),
                _price_row("term_c", 3.50),
            ],
            waits={"term_a": 10.0, "term_b": 20.0, "term_c": 30.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        for candidate in result.candidates:
            assert 0.0 <= candidate.score <= 1.0

    async def test_wait_warning_annotated_above_threshold(self) -> None:
        terminals = [
            _terminal("term_q", supported_products=["DIESEL_2"]),
        ]
        rec, *_ = _build_recommender(
            terminals=terminals,
            prices=[_price_row("term_q", 3.40)],
            waits={"term_q": 90.0},  # above default 60m threshold
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].wait_warning is True
        assert "wait_warning" in result.candidates[0].reasons

    async def test_wait_warning_terminal_ids_populated_on_recommendation(
        self,
    ) -> None:
        """Task 7.11 — top-level summary field mirrors candidate flags."""

        terminals = [
            _terminal("term_ok", supported_products=["DIESEL_2"], lat=40.70),
            _terminal("term_slow", supported_products=["DIESEL_2"], lat=40.72),
        ]
        rec, *_ = _build_recommender(
            terminals=terminals,
            prices=[
                _price_row("term_ok", 3.10),
                _price_row("term_slow", 3.30),
            ],
            waits={"term_ok": 15.0, "term_slow": 90.0},  # slow trips the default
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.wait_warning_terminal_ids == ["term_slow"]
        # Dispatcher UI truthiness check works without nested traversal.
        assert bool(result.wait_warning_terminal_ids) is True

    async def test_wait_warning_terminal_ids_empty_when_all_below_threshold(
        self,
    ) -> None:
        terminals = [_terminal("term_quiet", supported_products=["DIESEL_2"])]
        rec, *_ = _build_recommender(
            terminals=terminals,
            prices=[_price_row("term_quiet", 3.10)],
            waits={"term_quiet": 5.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.wait_warning_terminal_ids == []


# ---------------------------------------------------------------------------
# Tenant-configurable weights — Req 8.5.2
# ---------------------------------------------------------------------------


class TestTenantConfigurableWeights:
    async def test_weights_override_flips_ranking(self) -> None:
        # Same setup as "cheaper wins", but weights bias toward contract.
        contracted = _terminal("term_contract", supported_products=["DIESEL_2"])
        cheap = _terminal("term_cheap", supported_products=["DIESEL_2"])
        contract = _contract(
            contract_id="sc_override",
            preferred_terminal_ids=["term_contract"],
        )
        cfg = _FakeTenantConfig(
            {
                WEIGHTS_REDIS_KEY.format(tenant_id="tenant-1"): json.dumps(
                    {"price": 0.1, "wait": 0.05, "distance": 0.05, "contract": 0.8}
                )
            }
        )
        rec, *_ = _build_recommender(
            terminals=[contracted, cheap],
            contracts=[contract],
            prices=[
                _price_row("term_contract", 4.00),  # expensive
                _price_row("term_cheap", 3.00),  # much cheaper
            ],
            waits={"term_contract": 0.0, "term_cheap": 0.0},
            tenant_config=cfg,
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].terminal_id == "term_contract"

    async def test_malformed_weights_fall_back_to_defaults(self) -> None:
        cfg = _FakeTenantConfig(
            {WEIGHTS_REDIS_KEY.format(tenant_id="tenant-1"): "not-json"}
        )
        term = _terminal("term_ok", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_ok", 3.40)],
            waits={"term_ok": 0.0},
            tenant_config=cfg,
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        # Did not raise and produced a candidate using defaults.
        assert len(result.candidates) == 1

    async def test_negative_weight_override_falls_back_to_defaults(self) -> None:
        cfg = _FakeTenantConfig(
            {
                WEIGHTS_REDIS_KEY.format(tenant_id="tenant-1"): json.dumps(
                    {"price": -1.0, "wait": 0.5, "distance": 0.5, "contract": 0.0}
                )
            }
        )
        term = _terminal("term_ok", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_ok", 3.40)],
            waits={"term_ok": 0.0},
            tenant_config=cfg,
        )
        # No raise — service silently falls back.
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert len(result.candidates) == 1

    async def test_wait_warning_threshold_is_tenant_configurable(self) -> None:
        # Lower the threshold so a 30-minute wait qualifies as warning.
        cfg = _FakeTenantConfig(
            {WAIT_WARNING_REDIS_KEY.format(tenant_id="tenant-1"): "20"}
        )
        term = _terminal("term_q", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_q", 3.40)],
            waits={"term_q": 30.0},
            tenant_config=cfg,
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].wait_warning is True


# ---------------------------------------------------------------------------
# Output record shape — Req 8.5.1
# ---------------------------------------------------------------------------


class TestRecommendationRecord:
    async def test_produces_persistable_sourcing_recommendation(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_a", 3.40)],
            waits={"term_a": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=1250,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
            truck_id="truck-9",
            run_id="run-42",
        )
        assert isinstance(result, SourcingRecommendation)
        assert result.tenant_id == "tenant-1"
        assert result.truck_id == "truck-9"
        assert result.run_id == "run-42"
        assert result.product_code == "DIESEL_2"
        assert result.origin_lat == pytest.approx(40.70)
        assert result.origin_lon == pytest.approx(-74.15)
        assert result.volume_gallons == pytest.approx(1250)
        assert result.recommendation_id.startswith("srec_")
        assert result.request_id
        # model_dump should match the ES mapping directly (no extra keys).
        dumped = result.model_dump(mode="json")
        assert "candidates" in dumped and isinstance(dumped["candidates"], list)

    async def test_passes_canonical_product_to_contract_query(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        _, _, c_repo, _ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_a", 3.40)],
            waits={"term_a": 0.0},
        )
        rec = SourcingRecommender(
            terminal_repo=_FakeTerminalRepo([term]),  # type: ignore[arg-type]
            contract_repo=c_repo,  # type: ignore[arg-type]
            rack_price_provider=_FakeRackPriceProvider(
                [_price_row("term_a", 3.40)]
            ),
            wait_time_resolver=_wait_resolver({"term_a": 0.0}),
        )
        await rec.recommend(
            tenant_id="tenant-1",
            product_code="ago",  # alias + lowercase
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        # Contract repo should have been asked for the canonical product.
        assert any(
            call["product_code"] == "DIESEL_2" for call in c_repo.calls
        ), c_repo.calls


# ---------------------------------------------------------------------------
# Operational resilience
# ---------------------------------------------------------------------------


class TestOperationalResilience:
    async def test_missing_wait_resolver_defaults_to_zero(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        t_repo = _FakeTerminalRepo([term])
        c_repo = _FakeContractRepo([])
        rack = _FakeRackPriceProvider([_price_row("term_a", 3.40)])
        rec = SourcingRecommender(
            terminal_repo=t_repo,  # type: ignore[arg-type]
            contract_repo=c_repo,  # type: ignore[arg-type]
            rack_price_provider=rack,
            wait_time_resolver=None,  # no resolver at all
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].avg_wait_minutes == pytest.approx(0.0)

    async def test_rack_price_provider_exception_drops_terminal(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        t_repo = _FakeTerminalRepo([term])
        c_repo = _FakeContractRepo([])
        rack = _FakeRackPriceProvider([], raise_on_call=RuntimeError("boom"))
        rec = SourcingRecommender(
            terminal_repo=t_repo,  # type: ignore[arg-type]
            contract_repo=c_repo,  # type: ignore[arg-type]
            rack_price_provider=rack,
            wait_time_resolver=_wait_resolver({"term_a": 0.0}),
        )
        # Exception inside the provider should degrade gracefully — the
        # terminal is dropped because no price could be resolved (no
        # contract price, no rack price).
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates == []

    async def test_wait_resolver_exception_defaults_to_zero(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[_price_row("term_a", 3.40)],
            wait_raises=RuntimeError("redis down"),
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].avg_wait_minutes == pytest.approx(0.0)

    async def test_uses_latest_effective_at_when_provider_returns_duplicates(
        self,
    ) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        # Two prints at different effective_at — the newer one should win.
        stale = _price_row(
            "term_a",
            4.00,
            effective_at=AS_OF - timedelta(minutes=30),
        )
        fresh = _price_row("term_a", 3.00, effective_at=AS_OF)
        rec, *_ = _build_recommender(
            terminals=[term],
            prices=[stale, fresh],
            waits={"term_a": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates[0].price_per_gallon_usd == pytest.approx(3.00)


# ---------------------------------------------------------------------------
# Reasons breadcrumbs
# ---------------------------------------------------------------------------


class TestReasons:
    async def test_reasons_include_signal_summary_and_contract_boost(self) -> None:
        contracted = _terminal(
            "term_c",
            supported_products=["DIESEL_2"],
            branded=True,
            supplier_brand="Shell",
        )
        contract = _contract(
            contract_id="sc_seen",
            preferred_terminal_ids=["term_c"],
        )
        rec, *_ = _build_recommender(
            terminals=[contracted],
            contracts=[contract],
            prices=[_price_row("term_c", 3.45)],
            waits={"term_c": 0.0},
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        reasons = result.candidates[0].reasons
        assert any(r.startswith("price=") for r in reasons)
        assert any(r.startswith("wait=") for r in reasons)
        assert any(r.startswith("distance=") for r in reasons)
        assert any(r.startswith("contract_priority_boost:sc_seen") for r in reasons)
        assert any(r.startswith("branded:Shell") for r in reasons)


# ---------------------------------------------------------------------------
# Rack-price sync integration (Task 7.4 / Req 8.2.5)
# ---------------------------------------------------------------------------


class _FakeRackPriceSyncResult:
    """Minimal shape used by :class:`_FakeRackPriceSyncService`.

    We only need the two fields :meth:`SourcingRecommender._fetch_rack_prices`
    reads (``prices`` and ``rack_price_fallback``), so a simple attribute
    holder is enough — no Pydantic required.
    """

    def __init__(self, prices, rack_price_fallback):
        self.prices = list(prices)
        self.rack_price_fallback = bool(rack_price_fallback)


class _FakeRackPriceSyncService:
    """Stand-in for :class:`integrations.rack_price_sync.RackPriceSyncService`.

    Records every invocation so tests can assert the sync service was
    wired up correctly, and returns a preloaded ``(prices, fallback)``
    payload regardless of input.
    """

    def __init__(self, rows, *, fallback: bool):
        self._rows = list(rows)
        self._fallback = bool(fallback)
        self.calls = []

    async def sync(
        self,
        provider,
        *,
        tenant_id,
        terminal_ids,
        product_codes,
        as_of,
    ):
        self.calls.append(
            {
                "provider": provider,
                "tenant_id": tenant_id,
                "terminal_ids": list(terminal_ids),
                "product_codes": list(product_codes),
                "as_of": as_of,
            }
        )
        return _FakeRackPriceSyncResult(self._rows, self._fallback)


class TestRackPriceSyncIntegration:
    async def test_sync_result_flags_rack_price_fallback_on_recommendation(
        self,
    ) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        rack = _FakeRackPriceProvider([_price_row("term_a", 3.40)])
        sync = _FakeRackPriceSyncService(
            [_price_row("term_a", 3.25)],
            fallback=True,
        )
        rec = SourcingRecommender(
            terminal_repo=_FakeTerminalRepo([term]),  # type: ignore[arg-type]
            contract_repo=_FakeContractRepo([]),  # type: ignore[arg-type]
            rack_price_provider=rack,
            wait_time_resolver=_wait_resolver({"term_a": 0.0}),
            rack_price_sync=sync,  # type: ignore[arg-type]
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        # The sync service was consulted (not the provider directly).
        assert len(sync.calls) == 1
        assert sync.calls[0]["tenant_id"] == "tenant-1"
        assert sync.calls[0]["terminal_ids"] == ["term_a"]
        assert sync.calls[0]["product_codes"] == ["DIESEL_2"]
        # Fallback flag is forwarded onto the persisted recommendation.
        assert result.rack_price_fallback is True
        # Price came from the sync result (3.25), not the raw provider (3.40).
        assert result.candidates[0].price_per_gallon_usd == pytest.approx(3.25)

    async def test_sync_result_without_fallback_leaves_flag_false(self) -> None:
        term = _terminal("term_a", supported_products=["DIESEL_2"])
        rack = _FakeRackPriceProvider([_price_row("term_a", 3.40)])
        sync = _FakeRackPriceSyncService(
            [_price_row("term_a", 3.25)],
            fallback=False,
        )
        rec = SourcingRecommender(
            terminal_repo=_FakeTerminalRepo([term]),  # type: ignore[arg-type]
            contract_repo=_FakeContractRepo([]),  # type: ignore[arg-type]
            rack_price_provider=rack,
            wait_time_resolver=_wait_resolver({"term_a": 0.0}),
            rack_price_sync=sync,  # type: ignore[arg-type]
        )
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.rack_price_fallback is False

    async def test_sync_exception_degrades_to_no_candidates(self) -> None:
        class _ExplodingSync:
            async def sync(self, *args, **kwargs):
                raise RuntimeError("es down")

        term = _terminal("term_a", supported_products=["DIESEL_2"])
        rack = _FakeRackPriceProvider([_price_row("term_a", 3.40)])
        rec = SourcingRecommender(
            terminal_repo=_FakeTerminalRepo([term]),  # type: ignore[arg-type]
            contract_repo=_FakeContractRepo([]),  # type: ignore[arg-type]
            rack_price_provider=rack,
            wait_time_resolver=_wait_resolver({"term_a": 0.0}),
            rack_price_sync=_ExplodingSync(),  # type: ignore[arg-type]
        )
        # No exception propagates — the terminal just loses its price and
        # is dropped from the candidate set.
        result = await rec.recommend(
            tenant_id="tenant-1",
            product_code="DIESEL_2",
            volume_gallons=500,
            origin_lat_lon=(40.70, -74.15),
            as_of=AS_OF,
        )
        assert result.candidates == []
        assert result.rack_price_fallback is False
