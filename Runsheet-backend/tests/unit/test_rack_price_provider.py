"""
Unit tests for :mod:`integrations.rack_price_provider_base`.

Covers Capability 8 / Requirements 8.2.1, 8.2.2, 8.2.4 of the fuel-ops
hardening spec:

* :class:`RackPrice` Pydantic validation, alias canonicalization, and the
  canonical mapping shape.
* :class:`RackPriceProvider` base-class plumbing:
    - Redis cache lookup + populate keyed by
      ``rack_price:{provider}:{terminal_id}:{product_code}:{bucket_15min}``
      with 900s TTL (Req 8.2.4).
    - 10-second timeout enforced via ``asyncio.wait_for`` (mirrors the
      Sourcing_Recommender's budget, per Req 8.2.5 discussion in design).
    - Graceful degradation to ``[]`` on network / parse failures.
    - Cross-product expansion of terminals × products with sparse cache
      reuse.
* :class:`OPISRackPriceProvider` parses OPIS-shaped JSON via an injected
  ``httpx.AsyncClient`` backed by :class:`httpx.MockTransport` — no real
  network is touched (Req 8.2.2).
* :class:`CSVFallbackRackPriceProvider` consumes an async bytes loader
  standing in for the tenant-scoped S3 read, filters on terminal/product/
  as_of, canonicalizes legacy aliases, and keeps the latest effective row
  per pair.
* :func:`build_rack_price_provider` factory resolves by short name and
  rejects unknowns.

Validates: Requirements 8.2.1, 8.2.2, 8.2.4.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from integrations.rack_price_provider_base import (
    CACHE_BUCKET_MINUTES,
    CSV_REQUIRED_COLUMNS,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    CSVFallbackRackPriceProvider,
    OPISRackPriceProvider,
    RackPrice,
    RackPriceProvider,
    build_rack_price_provider,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory async Redis stub exposing ``get`` and ``setex`` only."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.get_calls: List[str] = []
        self.setex_calls: List[Dict[str, Any]] = []
        self.raise_on_get: Optional[Exception] = None
        self.raise_on_setex: Optional[Exception] = None

    async def get(self, key: str) -> Optional[str]:
        self.get_calls.append(key)
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.setex_calls.append({"key": key, "ttl": ttl, "value": value})
        if self.raise_on_setex is not None:
            raise self.raise_on_setex
        self.store[key] = value
        self.ttls[key] = ttl
        return True


class _StubProvider(RackPriceProvider):
    """Concrete provider whose ``_fetch_raw`` is configurable per test."""

    name = "stub"  # type: ignore[assignment]

    def __init__(self, rows_or_exc: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rows_or_exc = rows_or_exc
        self.fetch_raw_calls: List[Dict[str, Any]] = []

    async def _fetch_raw(
        self,
        *,
        terminal_ids,
        product_codes,
        as_of,
        tenant_id,
    ) -> List[RackPrice]:
        self.fetch_raw_calls.append(
            {
                "terminal_ids": list(terminal_ids),
                "product_codes": list(product_codes),
                "as_of": as_of,
                "tenant_id": tenant_id,
            }
        )
        if callable(self._rows_or_exc) and not isinstance(self._rows_or_exc, BaseException):
            return await self._rows_or_exc(
                terminal_ids=terminal_ids,
                product_codes=product_codes,
                as_of=as_of,
                tenant_id=tenant_id,
            )
        if isinstance(self._rows_or_exc, BaseException):
            raise self._rows_or_exc
        return list(self._rows_or_exc)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _row(
    *,
    terminal_id: str = "term_01",
    product_code: str = "DIESEL_2",
    price: float = 3.45,
    branded: bool = False,
    supplier_brand: Optional[str] = None,
    tenant: str = "t-1",
    provider: str = "stub",
    effective_at: datetime = datetime(2024, 10, 15, 12, 0, 0, tzinfo=timezone.utc),
    retrieved_at: datetime = datetime(2024, 10, 15, 12, 5, 0, tzinfo=timezone.utc),
) -> RackPrice:
    return RackPrice(
        rack_price_id=f"rp_{uuid4()}",
        tenant_id=tenant,
        terminal_id=terminal_id,
        product_code=product_code,
        price_per_gallon_usd=price,
        branded_flag=branded,
        supplier_brand=supplier_brand,
        provider=provider,
        effective_at=effective_at,
        retrieved_at=retrieved_at,
    )


def _as_of(hour: int = 12, minute: int = 7) -> datetime:
    """Return a stable timestamp inside the 12:00–12:14 cache bucket."""

    return datetime(2024, 10, 15, hour, minute, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestRackPriceModel:
    def test_valid_row_round_trips(self):
        row = _row()
        assert row.product_code == "DIESEL_2"
        dumped = row.model_dump(mode="json")
        assert dumped["product_code"] == "DIESEL_2"
        assert dumped["effective_at"].startswith("2024-10-15T12:00:00")
        # Round trip back through the model to prove schema stability.
        parsed = RackPrice(**dumped)
        assert parsed == row

    @pytest.mark.parametrize(
        "alias, canonical",
        [("AGO", "DIESEL_2"), ("PMS", "GASOLINE_REG"), ("ATK", "KEROSENE"), ("LPG", "PROPANE")],
    )
    def test_legacy_aliases_canonicalize(self, alias: str, canonical: str):
        row = _row(product_code=alias)
        assert row.product_code == canonical

    def test_rejects_negative_price(self):
        with pytest.raises(ValidationError):
            _row(price=-0.01)

    def test_rejects_blank_required_strings(self):
        base = dict(
            rack_price_id="rp_1",
            tenant_id="t-1",
            terminal_id="term_01",
            product_code="DIESEL_2",
            price_per_gallon_usd=3.0,
            branded_flag=False,
            provider="stub",
            effective_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        for field in ("rack_price_id", "tenant_id", "terminal_id", "provider"):
            payload = {**base, field: "   "}
            with pytest.raises(ValidationError):
                RackPrice(**payload)

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            RackPrice(
                rack_price_id="rp_1",
                tenant_id="t-1",
                terminal_id="term_01",
                product_code="DIESEL_2",
                price_per_gallon_usd=3.0,
                branded_flag=False,
                provider="stub",
                effective_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                not_a_field=True,  # type: ignore[arg-type]
            )

    def test_strips_supplier_brand_whitespace(self):
        row = _row(branded=True, supplier_brand="  Shell ")
        assert row.supplier_brand == "Shell"
        blank = _row(branded=False, supplier_brand="   ")
        assert blank.supplier_brand is None


# ---------------------------------------------------------------------------
# Base class — cache, fallback, timeout
# ---------------------------------------------------------------------------


class TestRackPriceProviderBase:
    async def test_fetch_returns_rows_and_caches_each_pair(self):
        redis = _FakeRedis()
        as_of = _as_of()
        rows = [
            _row(terminal_id="term_a", product_code="DIESEL_2", price=3.20),
            _row(terminal_id="term_b", product_code="GASOLINE_REG", price=3.80),
        ]
        provider = _StubProvider(rows_or_exc=rows, redis_client=redis)

        out = await provider.get_prices(
            terminal_ids=["term_a", "term_b"],
            product_codes=["DIESEL_2", "GASOLINE_REG"],
            as_of=as_of,
            tenant_id="t-1",
        )

        # Every requested pair missed, so the provider was called once.
        assert len(provider.fetch_raw_calls) == 1
        call = provider.fetch_raw_calls[0]
        assert sorted(call["terminal_ids"]) == ["term_a", "term_b"]
        assert sorted(call["product_codes"]) == ["DIESEL_2", "GASOLINE_REG"]

        # Returned rows include the two supplied rows, sorted deterministically.
        assert [(r.terminal_id, r.product_code) for r in out] == [
            ("term_a", "DIESEL_2"),
            ("term_b", "GASOLINE_REG"),
        ]

        # Each fetched row was cached under its canonical bucketed key.
        assert len(redis.setex_calls) == 2
        keys = {call["key"] for call in redis.setex_calls}
        assert keys == {
            "rack_price:stub:term_a:DIESEL_2:2024-10-15T12:00",
            "rack_price:stub:term_b:GASOLINE_REG:2024-10-15T12:00",
        }
        for call in redis.setex_calls:
            assert call["ttl"] == DEFAULT_CACHE_TTL_SECONDS
            payload = json.loads(call["value"])
            # Sanity-check the serialized shape used by cache lookups.
            assert set(payload.keys()) >= {
                "rack_price_id",
                "tenant_id",
                "terminal_id",
                "product_code",
                "price_per_gallon_usd",
                "provider",
                "effective_at",
                "retrieved_at",
            }

    async def test_cache_hit_skips_provider_call(self):
        redis = _FakeRedis()
        as_of = _as_of()
        row = _row(terminal_id="term_a", product_code="DIESEL_2")
        provider = _StubProvider(rows_or_exc=[row], redis_client=redis)

        # Populate the cache.
        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-1",
        )
        provider.fetch_raw_calls.clear()

        # Second call at the same bucket should be a pure cache hit.
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-1",
        )

        assert len(out) == 1
        assert provider.fetch_raw_calls == []

    async def test_partial_cache_hit_only_fetches_missing_pairs(self):
        redis = _FakeRedis()
        as_of = _as_of()
        seeded = _row(terminal_id="term_a", product_code="DIESEL_2", price=3.00)
        miss_row = _row(terminal_id="term_b", product_code="DIESEL_2", price=3.40)
        provider = _StubProvider(rows_or_exc=[seeded], redis_client=redis)

        # Seed one pair.
        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-1",
        )
        # Reconfigure the stub to return the miss row on the next call.
        provider._rows_or_exc = [miss_row]
        provider.fetch_raw_calls.clear()

        out = await provider.get_prices(
            terminal_ids=["term_a", "term_b"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-1",
        )

        assert len(provider.fetch_raw_calls) == 1
        call = provider.fetch_raw_calls[0]
        # Only the missing pair's terminal was fetched.
        assert call["terminal_ids"] == ["term_b"]
        assert call["product_codes"] == ["DIESEL_2"]
        # Result includes both the cached row and the freshly-fetched row.
        assert {(r.terminal_id, r.price_per_gallon_usd) for r in out} == {
            ("term_a", 3.00),
            ("term_b", 3.40),
        }

    async def test_cache_key_bucketing_collapses_within_15_minutes(self):
        redis = _FakeRedis()
        row = _row(terminal_id="term_a", product_code="DIESEL_2")
        provider = _StubProvider(rows_or_exc=[row], redis_client=redis)

        # First call at 12:02 populates the 12:00 bucket.
        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(minute=2),
            tenant_id="t-1",
        )
        provider.fetch_raw_calls.clear()

        # Second call 12 minutes later still falls in the same bucket.
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(minute=14),
            tenant_id="t-1",
        )
        assert provider.fetch_raw_calls == []
        assert len(out) == 1

        # A call past 12:15 falls into the next bucket and misses.
        provider._rows_or_exc = [row]
        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(minute=15),
            tenant_id="t-1",
        )
        assert len(provider.fetch_raw_calls) == 1

    async def test_cache_hit_restamps_tenant_id(self):
        redis = _FakeRedis()
        as_of = _as_of()
        row = _row(tenant="t-1", terminal_id="term_a", product_code="DIESEL_2")
        provider = _StubProvider(rows_or_exc=[row], redis_client=redis)

        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-1",
        )
        provider.fetch_raw_calls.clear()

        # Another tenant hitting the same cache bucket should still get rows
        # stamped with their own tenant_id.
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=as_of,
            tenant_id="t-2",
        )
        assert len(out) == 1
        assert out[0].tenant_id == "t-2"
        assert provider.fetch_raw_calls == []

    async def test_fetch_returns_empty_on_raw_exception(self):
        provider = _StubProvider(
            rows_or_exc=RuntimeError("boom"),
            redis_client=_FakeRedis(),
        )
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_fetch_returns_empty_on_http_error(self):
        provider = _StubProvider(
            rows_or_exc=httpx.ConnectError("connection refused"),
            redis_client=_FakeRedis(),
        )
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_fetch_enforces_timeout(self):
        async def slow(**_: Any) -> List[RackPrice]:
            await asyncio.sleep(0.5)
            return []

        provider = _StubProvider(rows_or_exc=slow, timeout_seconds=0.05)
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_rejects_invalid_args(self):
        provider = _StubProvider(rows_or_exc=[])
        with pytest.raises(ValueError):
            await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="",
            )
        with pytest.raises(TypeError):
            await provider.get_prices(
                terminal_ids="term_a",  # type: ignore[arg-type]
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        with pytest.raises(TypeError):
            await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes="DIESEL_2",  # type: ignore[arg-type]
                as_of=_as_of(),
                tenant_id="t-1",
            )
        with pytest.raises(TypeError):
            await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2"],
                as_of="2024-01-01",  # type: ignore[arg-type]
                tenant_id="t-1",
            )

    async def test_empty_inputs_short_circuit_without_fetch(self):
        redis = _FakeRedis()
        provider = _StubProvider(rows_or_exc=RuntimeError("should not run"), redis_client=redis)
        out = await provider.get_prices(
            terminal_ids=[],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []
        assert provider.fetch_raw_calls == []

        out2 = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=[],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out2 == []

    async def test_unknown_product_codes_are_dropped(self):
        provider = _StubProvider(rows_or_exc=[])
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["NOT_A_REAL_PRODUCT"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []
        # Canonicalization drops unknown codes before the fetch path runs.
        assert provider.fetch_raw_calls == []

    async def test_canonicalizes_aliases_before_fetch(self):
        row = _row(terminal_id="term_a", product_code="DIESEL_2", price=3.1)
        provider = _StubProvider(rows_or_exc=[row])
        await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["AGO"],  # legacy alias → DIESEL_2
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert provider.fetch_raw_calls[0]["product_codes"] == ["DIESEL_2"]

    async def test_cache_failures_do_not_mask_rows(self):
        redis = _FakeRedis()
        redis.raise_on_setex = RuntimeError("redis down")
        redis.raise_on_get = RuntimeError("redis down on get")
        provider = _StubProvider(
            rows_or_exc=[_row(terminal_id="term_a", product_code="DIESEL_2")],
            redis_client=redis,
        )
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert len(out) == 1

    async def test_drops_rows_with_foreign_tenant_id(self):
        row = _row(terminal_id="term_a", product_code="DIESEL_2", tenant="attacker")
        provider = _StubProvider(rows_or_exc=[row])
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="victim",
        )
        assert out == []

    async def test_invalid_init_args(self):
        with pytest.raises(ValueError):
            _StubProvider(rows_or_exc=[], timeout_seconds=0)
        with pytest.raises(ValueError):
            _StubProvider(rows_or_exc=[], cache_ttl_seconds=0)
        with pytest.raises(ValueError):
            _StubProvider(rows_or_exc=[], cache_bucket_minutes=0)


# ---------------------------------------------------------------------------
# OPIS adapter
# ---------------------------------------------------------------------------


def _opis_response(rows: List[Dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"prices": rows})


class TestOPISRackPriceProvider:
    async def test_happy_path_parses_and_stamps_provider(self):
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _opis_response(
                [
                    {
                        "terminal_id": "term_a",
                        "product_code": "DIESEL_2",
                        "price_per_gallon_usd": 3.245,
                        "branded_flag": False,
                        "effective_at": "2024-10-15T12:10:00Z",
                    },
                    {
                        "terminal_id": "term_a",
                        "product_code": "GASOLINE_REG",
                        "price_per_gallon_usd": 3.829,
                        "branded_flag": True,
                        "supplier_brand": "Shell",
                        "effective_at": "2024-10-15T12:10:00Z",
                    },
                ]
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OPISRackPriceProvider(
                api_key="unit-test-key",
                http_client=client,
                base_url="https://opis.test/v1",
            )
            out = await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2", "GASOLINE_REG"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert len(out) == 2
        assert {r.product_code for r in out} == {"DIESEL_2", "GASOLINE_REG"}
        for row in out:
            assert row.provider == "opis"
            assert row.tenant_id == "t-1"
            assert row.retrieved_at.tzinfo is not None

        # Verify the wire shape we send to OPIS is what the docstring promises.
        assert len(captured) == 1
        request = captured[0]
        assert request.url.path == "/v1/rack/prices"
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        assert "X-OPIS-Signature" not in request.headers  # no secret configured
        assert request.url.params["terminal_ids"] == "term_a"
        assert set(request.url.params["product_codes"].split(",")) == {
            "DIESEL_2",
            "GASOLINE_REG",
        }
        assert request.url.params["as_of"].startswith("2024-10-15T12:07:00")

    async def test_signs_requests_when_secret_configured(self):
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _opis_response([])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OPISRackPriceProvider(
                api_key="unit-test-key",
                api_secret="unit-test-secret",
                http_client=client,
                base_url="https://opis.test/v1",
            )
            await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert len(captured) == 1
        signature = captured[0].headers.get("X-OPIS-Signature")
        assert isinstance(signature, str) and signature
        # Signature is base64; base64 only contains these chars.
        import re
        assert re.fullmatch(r"[A-Za-z0-9+/=]+", signature)

    async def test_missing_api_key_returns_empty(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _opis_response([])))
        try:
            provider = OPISRackPriceProvider(http_client=client, base_url="https://opis.test/v1")
            # Make sure env does not leak a real key into the test.
            import os
            prev = os.environ.pop("OPIS_API_KEY", None)
            try:
                out = await provider.get_prices(
                    terminal_ids=["term_a"],
                    product_codes=["DIESEL_2"],
                    as_of=_as_of(),
                    tenant_id="t-1",
                )
            finally:
                if prev is not None:
                    os.environ["OPIS_API_KEY"] = prev
        finally:
            await client.aclose()
        assert out == []

    async def test_http_500_degrades_to_empty(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OPISRackPriceProvider(
                api_key="unit-test-key",
                http_client=client,
                base_url="https://opis.test/v1",
            )
            out = await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()
        assert out == []

    async def test_malformed_rows_are_skipped(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return _opis_response(
                [
                    # Valid row.
                    {
                        "terminal_id": "term_a",
                        "product_code": "DIESEL_2",
                        "price_per_gallon_usd": 3.00,
                        "effective_at": "2024-10-15T12:00:00Z",
                    },
                    # Missing required field.
                    {
                        "terminal_id": "term_b",
                        "product_code": "DIESEL_2",
                        "effective_at": "2024-10-15T12:00:00Z",
                    },
                    # Invalid price.
                    {
                        "terminal_id": "term_c",
                        "product_code": "DIESEL_2",
                        "price_per_gallon_usd": "not-a-float",
                        "effective_at": "2024-10-15T12:00:00Z",
                    },
                    # Invalid timestamp.
                    {
                        "terminal_id": "term_d",
                        "product_code": "DIESEL_2",
                        "price_per_gallon_usd": 3.0,
                        "effective_at": "not-a-date",
                    },
                ]
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OPISRackPriceProvider(
                api_key="unit-test-key",
                http_client=client,
                base_url="https://opis.test/v1",
            )
            out = await provider.get_prices(
                terminal_ids=["term_a", "term_b", "term_c", "term_d"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert [r.terminal_id for r in out] == ["term_a"]

    async def test_canonicalizes_legacy_aliases_in_response(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return _opis_response(
                [
                    {
                        "terminal_id": "term_a",
                        "product_code": "AGO",  # legacy → DIESEL_2
                        "price_per_gallon_usd": 3.10,
                        "effective_at": "2024-10-15T12:00:00Z",
                    }
                ]
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = OPISRackPriceProvider(
                api_key="unit-test-key",
                http_client=client,
                base_url="https://opis.test/v1",
            )
            out = await provider.get_prices(
                terminal_ids=["term_a"],
                product_codes=["DIESEL_2"],
                as_of=_as_of(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert len(out) == 1
        assert out[0].product_code == "DIESEL_2"


# ---------------------------------------------------------------------------
# CSV fallback adapter
# ---------------------------------------------------------------------------


def _make_csv_loader(payloads: Dict[str, bytes]) -> Callable[[str], Awaitable[bytes]]:
    """Build an async loader that serves tenant-scoped CSV bytes."""

    async def _loader(tenant_id: str) -> bytes:
        if tenant_id not in payloads:
            raise FileNotFoundError(tenant_id)
        return payloads[tenant_id]

    return _loader


class TestCSVFallbackRackPriceProvider:
    async def test_happy_path_filters_by_terminal_product_and_as_of(self):
        csv_body = (
            "terminal_id,product_code,price_per_gallon_usd,branded_flag,effective_at,supplier_brand\n"
            "term_a,DIESEL_2,3.205,false,2024-10-15T11:30:00Z,\n"
            # Newer effective_at for the same pair — should win.
            "term_a,DIESEL_2,3.210,false,2024-10-15T12:00:00Z,\n"
            # Future effective_at — dropped.
            "term_a,DIESEL_2,9.999,false,2024-10-15T15:00:00Z,\n"
            # Different terminal — not requested.
            "term_z,DIESEL_2,3.000,false,2024-10-15T12:00:00Z,\n"
            # Legacy alias canonicalizes to GASOLINE_REG.
            "term_a,PMS,3.899,true,2024-10-15T11:45:00Z,Shell\n"
        ).encode("utf-8")

        loader = _make_csv_loader({"t-1": csv_body})
        provider = CSVFallbackRackPriceProvider(csv_loader=loader)

        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2", "GASOLINE_REG"],
            as_of=_as_of(minute=14),
            tenant_id="t-1",
        )

        by_pair = {(r.terminal_id, r.product_code): r for r in out}
        assert set(by_pair.keys()) == {
            ("term_a", "DIESEL_2"),
            ("term_a", "GASOLINE_REG"),
        }
        # Latest effective row for DIESEL_2 wins.
        assert by_pair[("term_a", "DIESEL_2")].price_per_gallon_usd == pytest.approx(3.210)
        assert by_pair[("term_a", "DIESEL_2")].effective_at == datetime(
            2024, 10, 15, 12, 0, 0, tzinfo=timezone.utc
        )
        # Legacy alias canonicalized, branded flag parsed.
        gasoline = by_pair[("term_a", "GASOLINE_REG")]
        assert gasoline.branded_flag is True
        assert gasoline.supplier_brand == "Shell"
        assert gasoline.provider == "csv_fallback"

    async def test_missing_csv_degrades_to_empty(self):
        loader = _make_csv_loader({})
        provider = CSVFallbackRackPriceProvider(csv_loader=loader)
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_missing_required_columns_degrades_to_empty(self):
        # "price_per_gallon_usd" is missing from the header.
        csv_body = (
            "terminal_id,product_code,effective_at\n"
            "term_a,DIESEL_2,2024-10-15T12:00:00Z\n"
        ).encode("utf-8")
        loader = _make_csv_loader({"t-1": csv_body})
        provider = CSVFallbackRackPriceProvider(csv_loader=loader)
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_malformed_rows_are_skipped_not_fatal(self):
        csv_body = (
            "terminal_id,product_code,price_per_gallon_usd,branded_flag,effective_at\n"
            "term_a,DIESEL_2,3.10,false,2024-10-15T11:00:00Z\n"
            # Negative price: row skipped.
            "term_a,DIESEL_2,-1.0,false,2024-10-15T11:30:00Z\n"
            # Blank price: row skipped.
            "term_b,DIESEL_2,,false,2024-10-15T11:00:00Z\n"
            # Unparsable branded flag: row skipped.
            "term_b,DIESEL_2,3.20,maybe,2024-10-15T11:00:00Z\n"
            # Unknown product_code: row skipped.
            "term_b,NOT_REAL,3.20,false,2024-10-15T11:00:00Z\n"
        ).encode("utf-8")
        loader = _make_csv_loader({"t-1": csv_body})
        provider = CSVFallbackRackPriceProvider(csv_loader=loader)
        out = await provider.get_prices(
            terminal_ids=["term_a", "term_b"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        # Only the first good row survives.
        assert len(out) == 1
        assert out[0].terminal_id == "term_a"
        assert out[0].price_per_gallon_usd == pytest.approx(3.10)

    async def test_permission_error_degrades_gracefully(self):
        async def _loader(tenant_id: str) -> bytes:
            raise PermissionError("cross-tenant file_ref")

        provider = CSVFallbackRackPriceProvider(csv_loader=_loader)
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_non_utf8_blob_degrades_to_empty(self):
        # Bytes that are valid Latin-1 but not valid UTF-8.
        loader = _make_csv_loader({"t-1": b"\xff\xfebroken"})
        provider = CSVFallbackRackPriceProvider(csv_loader=loader)
        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert out == []

    async def test_results_are_cached_like_opis(self):
        csv_body = (
            "terminal_id,product_code,price_per_gallon_usd,branded_flag,effective_at\n"
            "term_a,DIESEL_2,3.10,false,2024-10-15T11:00:00Z\n"
        ).encode("utf-8")
        redis = _FakeRedis()
        loader = _make_csv_loader({"t-1": csv_body})
        provider = CSVFallbackRackPriceProvider(
            csv_loader=loader, redis_client=redis
        )

        out = await provider.get_prices(
            terminal_ids=["term_a"],
            product_codes=["DIESEL_2"],
            as_of=_as_of(),
            tenant_id="t-1",
        )
        assert len(out) == 1
        assert len(redis.setex_calls) == 1
        assert redis.setex_calls[0]["key"] == (
            "rack_price:csv_fallback:term_a:DIESEL_2:2024-10-15T12:00"
        )
        assert redis.setex_calls[0]["ttl"] == DEFAULT_CACHE_TTL_SECONDS

    def test_requires_async_callable(self):
        with pytest.raises(TypeError):
            CSVFallbackRackPriceProvider(csv_loader="not-callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestBuildRackPriceProvider:
    def test_builds_opis_provider_by_short_name(self):
        provider = build_rack_price_provider("opis", api_key="unit-test-key")
        assert isinstance(provider, OPISRackPriceProvider)

    def test_builds_csv_provider_by_short_name(self):
        async def _loader(tenant_id: str) -> bytes:
            return b""

        provider = build_rack_price_provider("csv_fallback", csv_loader=_loader)
        assert isinstance(provider, CSVFallbackRackPriceProvider)

    def test_matches_name_case_insensitively(self):
        provider = build_rack_price_provider("OPIS", api_key="unit-test-key")
        assert isinstance(provider, OPISRackPriceProvider)

    def test_rejects_unknown_name(self):
        with pytest.raises(ValueError):
            build_rack_price_provider("not-a-real-provider")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            build_rack_price_provider("   ")


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_cache_ttl_is_900_seconds(self):
        # Requirement 8.2.4 mandates a 900-second TTL.
        assert DEFAULT_CACHE_TTL_SECONDS == 900

    def test_default_http_timeout_is_10_seconds(self):
        # Requirement 8.2.5 mandates a 10-second budget.
        assert DEFAULT_HTTP_TIMEOUT_SECONDS == 10.0

    def test_cache_bucket_is_15_minutes(self):
        assert CACHE_BUCKET_MINUTES == 15

    def test_csv_required_columns_match_docstring(self):
        assert CSV_REQUIRED_COLUMNS == frozenset(
            {"terminal_id", "product_code", "price_per_gallon_usd", "effective_at"}
        )
