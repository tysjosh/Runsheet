"""
Property-based tests for Surface B customer lookup and product validation.

# Feature: dinee-voice-integration, Property 15: Customer lookup by phone or
# account
# Feature: dinee-voice-integration, Property 16: Product validation

These properties exercise the ``GET /customers/lookup`` and
``GET /products/validate`` handlers implemented in
``fuel/voice/voice_read_driver_router.py`` (task 8.2).

Property 15 (**Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5**):

    * results are always restricted to the credential-bound tenant — a
      customer belonging to another tenant is never returned even when it
      shares the queried phone/accountId (Req 13.1, 13.2, 11.4);
    * each returned customer carries ``id`` and ``name`` and includes
      ``phone``/``accountId`` only when the source doc has them (Req 13.3);
    * a query that matches nothing returns ``{"customers": []}`` with HTTP 200
      (Req 13.4);
    * supplying neither ``phone`` nor ``accountId`` is a client error, HTTP 400
      (Req 13.5).

Property 16 (**Validates: Requirements 15.1, 15.2, 15.3**):

    * a code that resolves through the fuel-product catalog returns
      ``{"valid": true}`` (Req 15.1);
    * any code that does not resolve returns ``{"valid": false}`` (Req 15.2);
    * a missing ``code`` query parameter is rejected with HTTP 422 by the
      framework's required-parameter validation (Req 15.3).

The handlers are driven directly (task 8.2 wires the repositories/services via
``configure_voice_read_driver_router``); a recording in-memory fake customer
service enforces tenant scoping the same way the real ES-backed service does,
and the real ``fuel_product_catalog`` module is used for product validation so
no live Elasticsearch is required. The missing-parameter (422) case is driven
through a FastAPI ``TestClient`` with the auth dependency overridden, since that
rejection is produced by the framework's request validation.
"""

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import from_regex

from errors.exceptions import AppException
from fuel.services import fuel_product_catalog
from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG
from fuel.voice import voice_read_driver_router as vrouter
from fuel.voice.voice_auth import VoiceTenantContext, get_voice_tenant
from fuel.voice.voice_read_driver_router import (
    configure_voice_read_driver_router,
    customers_lookup,
    products_validate,
    router,
)


# ---------------------------------------------------------------------------
# Recording in-memory fake — CustomerService.lookup_by_phone_or_account
# ---------------------------------------------------------------------------
class FakeCustomerService:
    """Tenant-scoped fake mirroring ``CustomerService.lookup_by_phone_or_account``.

    Holds a per-tenant list of ``customers_current`` source docs and returns
    only the docs owned by the ``tenant_id`` it is called with, matching on the
    projected ``phone`` / ``account_id`` fields (should/minimum_should_match=1
    semantics). This makes cross-tenant leakage observable: a matching customer
    in another tenant is simply absent from the returned rows.
    """

    def __init__(self, rows_by_tenant: dict[str, list[dict]]) -> None:
        self.rows_by_tenant = rows_by_tenant
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def lookup_by_phone_or_account(
        self, tenant_id, *, phone=None, account_id=None
    ):
        self.calls.append((tenant_id, phone, account_id))
        phone_val = str(phone).strip() if phone and str(phone).strip() else None
        acct_val = (
            str(account_id).strip() if account_id and str(account_id).strip() else None
        )
        matched: list[dict] = []
        for row in self.rows_by_tenant.get(tenant_id, []):
            row_phone = row.get("phone")
            row_acct = row.get("account_id")
            if phone_val is not None and row_phone and str(row_phone).strip() == phone_val:
                matched.append(row)
            elif acct_val is not None and row_acct and str(row_acct).strip() == acct_val:
                matched.append(row)
        return matched


def _run(coro):
    return asyncio.run(coro)


def _ctx(tenant_id: str) -> VoiceTenantContext:
    return VoiceTenantContext(tenant_id=tenant_id, channel_id=f"chan-{tenant_id}")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_tenant_ids = from_regex(r"tenant-[a-z0-9]{6,12}", fullmatch=True)
_customer_ids = from_regex(r"cust-[a-z0-9]{6,12}", fullmatch=True)
_names = from_regex(r"[A-Za-z][A-Za-z ]{1,18}[A-Za-z]", fullmatch=True)
_phones = from_regex(r"\+1[0-9]{10}", fullmatch=True)
_account_ids = from_regex(r"ACCT-[A-Z0-9]{4,10}", fullmatch=True)


@st.composite
def _customer_records(draw):
    """A list of distinct ``customers_current`` source docs.

    Each doc always carries ``customer_id`` / ``display_name``; ``phone`` and
    ``account_id`` are present-or-absent so the projection's "include when
    present" behaviour (Req 13.3) is exercised. Ids are de-duplicated so the
    returned-id set can be compared exactly.
    """
    raw = draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "customer_id": _customer_ids,
                    "display_name": _names,
                    "phone": st.one_of(st.none(), _phones),
                    "account_id": st.one_of(st.none(), _account_ids),
                }
            ),
            min_size=1,
            max_size=6,
        )
    )
    seen: set[str] = set()
    records: list[dict] = []
    for rec in raw:
        cid = rec["customer_id"]
        if cid in seen:
            continue
        seen.add(cid)
        source = {"customer_id": cid, "display_name": rec["display_name"]}
        if rec["phone"] is not None:
            source["phone"] = rec["phone"]
        if rec["account_id"] is not None:
            source["account_id"] = rec["account_id"]
        records.append(source)
    return records


# Known product codes/aliases straight from the shipped catalog.
_KNOWN_CANONICAL = [p.product_code for p in FUEL_PRODUCT_CATALOG]
_KNOWN_ALIASES = ["ago", "pms", "atk", "lpg", "AGO", " PMS ", "DiEsEl_2"]
_known_codes = st.sampled_from(_KNOWN_CANONICAL + _KNOWN_ALIASES)
_arbitrary_codes = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ",
    min_size=1,
    max_size=24,
)


# ---------------------------------------------------------------------------
# TestClient helper (only needed for the framework-level 422 case)
# ---------------------------------------------------------------------------
def _build_client(bound_tenant: str) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})

    app.dependency_overrides[get_voice_tenant] = lambda: _ctx(bound_tenant)
    return TestClient(app)


# ===========================================================================
# Property 15 — Customer lookup by phone or account
# ===========================================================================
class TestCustomerLookup:
    """# Feature: dinee-voice-integration, Property 15: Customer lookup by
    phone or account

    **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5**
    """

    @given(
        bound_tenant=_tenant_ids,
        other_tenant=_tenant_ids,
        records=_customer_records(),
        query_phone=_phones,
    )
    @settings(max_examples=100)
    def test_lookup_by_phone_is_tenant_scoped_and_well_shaped(
        self, bound_tenant, other_tenant, records, query_phone
    ):
        assume(other_tenant != bound_tenant)

        async def scenario():
            # A customer in another tenant that shares the queried phone must
            # never surface (Req 13.1/13.2/11.4).
            poison = {
                "customer_id": "cust-otherxyz",
                "display_name": "Other Tenant Co",
                "phone": query_phone,
                "account_id": "ACCT-OTHER1",
            }
            service = FakeCustomerService(
                {bound_tenant: records, other_tenant: [poison]}
            )
            configure_voice_read_driver_router(customer_service=service)

            resp = await customers_lookup(
                phone=query_phone, accountId=None, voice=_ctx(bound_tenant)
            )
            body = resp.model_dump()
            returned_ids = {c["id"] for c in body["customers"]}

            # Exactly the bound-tenant customers whose phone matches (Req 13.1).
            expected = {
                r["customer_id"]
                for r in records
                if r.get("phone") == query_phone
            }
            assert returned_ids == expected
            # The cross-tenant customer is never leaked (Req 13.2/11.4).
            assert "cust-otherxyz" not in returned_ids

            # Shape: id + name always; phone/accountId only when present (13.3).
            by_id = {r["customer_id"]: r for r in records}
            for cust in body["customers"]:
                src = by_id[cust["id"]]
                assert cust["id"] == src["customer_id"]
                assert cust["name"] == src["display_name"]
                assert cust["phone"] == (src.get("phone") or None)
                assert cust["accountId"] == (src.get("account_id") or None)

        _run(scenario())

    @given(
        bound_tenant=_tenant_ids,
        records=_customer_records(),
        missing_phone=_phones,
    )
    @settings(max_examples=100)
    def test_no_match_returns_empty_list(self, bound_tenant, records, missing_phone):
        # Only exercise phones that genuinely match nothing (Req 13.4).
        assume(all(r.get("phone") != missing_phone for r in records))

        async def scenario():
            service = FakeCustomerService({bound_tenant: records})
            configure_voice_read_driver_router(customer_service=service)
            resp = await customers_lookup(
                phone=missing_phone, accountId=None, voice=_ctx(bound_tenant)
            )
            assert resp.model_dump() == {"customers": []}

        _run(scenario())

    @given(
        bound_tenant=_tenant_ids,
        blank_phone=st.sampled_from([None, "", "   "]),
        blank_account=st.sampled_from([None, "", "   "]),
    )
    @settings(max_examples=100)
    def test_neither_param_is_client_error_400(
        self, bound_tenant, blank_phone, blank_account
    ):
        async def scenario():
            service = FakeCustomerService({bound_tenant: []})
            configure_voice_read_driver_router(customer_service=service)
            with pytest.raises(AppException) as exc_info:
                await customers_lookup(
                    phone=blank_phone,
                    accountId=blank_account,
                    voice=_ctx(bound_tenant),
                )
            assert exc_info.value.status_code == 400
            # The service is never consulted when no selector is supplied.
            assert service.calls == []

        _run(scenario())


# ===========================================================================
# Property 16 — Product validation
# ===========================================================================
class TestProductValidation:
    """# Feature: dinee-voice-integration, Property 16: Product validation

    **Validates: Requirements 15.1, 15.2, 15.3**
    """

    @given(bound_tenant=_tenant_ids, code=_known_codes)
    @settings(max_examples=100)
    def test_known_product_is_valid(self, bound_tenant, code):
        async def scenario():
            configure_voice_read_driver_router(product_catalog=fuel_product_catalog)
            resp = await products_validate(code=code, voice=_ctx(bound_tenant))
            assert resp.model_dump() == {"valid": True}

        _run(scenario())

    @given(bound_tenant=_tenant_ids, code=_arbitrary_codes)
    @settings(max_examples=100)
    def test_unknown_product_is_invalid(self, bound_tenant, code):
        # Skip any generated string that happens to be a real catalog entry.
        assume(not fuel_product_catalog.is_known_product(code))

        async def scenario():
            configure_voice_read_driver_router(product_catalog=fuel_product_catalog)
            resp = await products_validate(code=code, voice=_ctx(bound_tenant))
            assert resp.model_dump() == {"valid": False}

        _run(scenario())

    @given(bound_tenant=_tenant_ids)
    @settings(max_examples=100)
    def test_missing_code_param_is_422(self, bound_tenant):
        # The required ``code`` query parameter is enforced by the framework, so
        # this rejection is only observable through the HTTP layer (Req 15.3).
        client = _build_client(bound_tenant)
        resp = client.get("/products/validate")
        assert resp.status_code == 422
