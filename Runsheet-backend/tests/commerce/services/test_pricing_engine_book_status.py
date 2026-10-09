"""OI-42: rules in a draft (or archived) price book don't resolve.

``PriceBookService.create`` writes a book's rules to ``pricing_rules_current``
whatever its status, and the engine used to resolve every rule for the
tenant and product. These tests drive the real ``PriceBookService`` and
``PricingEngine`` over one in-memory store that evaluates queries with the
real DSL matcher.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from commerce.models.account import Account, AccountTier
from commerce.services.price_book_service import PriceBookService
from commerce.services.pricing_engine import PricingEngine, PricingError

pytestmark = pytest.mark.asyncio

TENANT = "tenant-pricing"


class _Store:
    """Index-keyed documents; queries go through ``persistence.document_matcher``."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def index_document(self, index, doc_id, document):
        self.docs.setdefault(index, {})[doc_id] = dict(document)

    async def update_document(self, index, doc_id, partial):
        self.docs[index][doc_id].update(partial)

    async def delete_document(self, index, doc_id):
        self.docs.get(index, {}).pop(doc_id, None)

    async def search_documents(self, index, query, size=10, **kwargs):
        from persistence.document_matcher import matches

        hits = [
            {"_id": doc_id, "_source": dict(doc)}
            for doc_id, doc in self.docs.get(index, {}).items()
            if matches(doc, query.get("query"), doc_id=doc_id)
        ]
        return {"hits": {"hits": hits[:size], "total": {"value": len(hits)}}}


def _account() -> Account:
    return Account(
        account_id="acct_qa",
        tenant_id=TENANT,
        customer_id="cust_qa",
        display_name="QA Account",
        tier=AccountTier.SILVER,
        credit_limit_cents=1_000_000,
        net_terms_days=30,
    )


_RULE = {
    "product_code": "DIESEL_2",
    "scope_type": "default",
    "unit_price_cents": 321,
    "effective_from": "2020-01-01T00:00:00+00:00",
}


async def _resolve(engine: PricingEngine):
    return await engine.resolve(
        tenant_id=TENANT,
        account=_account(),
        product_code="DIESEL_2",
        quantity_gallons=100,
    )


async def test_draft_book_rule_does_not_resolve_until_activated():
    store = _Store()
    books = PriceBookService(store)
    engine = PricingEngine(store)
    book = await books.create(TENANT, name="QA draft", status="draft", rules=[_RULE])

    with pytest.raises(PricingError) as exc_info:
        await _resolve(engine)
    assert exc_info.value.code == PricingError.no_rule_matched(TENANT, "x", "y").code

    await books.activate(TENANT, book["price_book_id"])

    result = await _resolve(engine)
    assert result.unit_price_cents == 321


async def test_archived_book_rule_stops_resolving():
    store = _Store()
    books = PriceBookService(store)
    engine = PricingEngine(store)
    book = await books.create(TENANT, name="QA active", status="active", rules=[_RULE])
    assert (await _resolve(engine)).unit_price_cents == 321

    await books.update(TENANT, book["price_book_id"], status="archived")

    with pytest.raises(PricingError):
        await _resolve(engine)


async def test_active_book_wins_over_a_draft_with_a_different_price():
    store = _Store()
    books = PriceBookService(store)
    engine = PricingEngine(store)
    await books.create(TENANT, name="QA active", status="active", rules=[_RULE])
    await books.create(
        TENANT, name="QA draft", status="draft", rules=[{**_RULE, "unit_price_cents": 1}]
    )

    assert (await _resolve(engine)).unit_price_cents == 321


async def test_legacy_rule_without_a_book_still_resolves():
    store = _Store()
    engine = PricingEngine(store)
    await store.index_document(
        "pricing_rules_current",
        "rule_legacy",
        {
            "rule_id": "rule_legacy",
            "tenant_id": TENANT,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2020-01-01T00:00:00+00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 300,
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert (await _resolve(engine)).unit_price_cents == 300
