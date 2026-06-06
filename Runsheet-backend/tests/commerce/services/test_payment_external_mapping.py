"""Unit tests for PaymentService external→canonical mapping (Req 12.3).

Covers the Stripe-mapping seam used by the Admin Stripe view:

- ``find_by_external_id`` resolves a single Stripe charge id to its canonical
  Payment document, tenant-scoped.
- ``map_external`` batch-maps a page of external ids to canonical payment
  summaries (one tenant-scoped ``terms`` query), omitting unmapped ids.

Validates: Requirement 12.3, C3
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from commerce.models.payment import PaymentSource
from commerce.services.payment_service import PaymentService

_TENANT = "tenant_map_1"


def _es_with_hits(hits: List[Dict[str, Any]]) -> AsyncMock:
    es = AsyncMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [{"_source": h} for h in hits]}}
    )
    return es


def _payment_doc(
    *,
    payment_id: str,
    external_id: str,
    invoice_id: str = "inv_1",
    account_id: str = "acct_1",
    source: str = PaymentSource.STRIPE.value,
) -> Dict[str, Any]:
    return {
        "payment_id": payment_id,
        "tenant_id": _TENANT,
        "invoice_id": invoice_id,
        "account_id": account_id,
        "amount_cents": 5000,
        "source": source,
        "external_id": external_id,
        "status": "applied",
    }


class TestFindByExternalId:
    @pytest.mark.asyncio
    async def test_resolves_canonical_payment(self):
        es = _es_with_hits([_payment_doc(payment_id="pay_1", external_id="pi_1")])
        svc = PaymentService(es)

        found = await svc.find_by_external_id(
            tenant_id=_TENANT, source="stripe", external_id="pi_1"
        )

        assert found is not None
        assert found["payment_id"] == "pay_1"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        es = _es_with_hits([])
        svc = PaymentService(es)

        found = await svc.find_by_external_id(
            tenant_id=_TENANT, source="stripe", external_id="pi_missing"
        )

        assert found is None

    @pytest.mark.asyncio
    async def test_empty_external_id_returns_none_without_query(self):
        es = _es_with_hits([])
        svc = PaymentService(es)

        found = await svc.find_by_external_id(
            tenant_id=_TENANT, source="stripe", external_id=""
        )

        assert found is None
        es.search_documents.assert_not_called()


class TestMapExternal:
    @pytest.mark.asyncio
    async def test_maps_external_ids_to_summaries(self):
        es = _es_with_hits(
            [
                _payment_doc(payment_id="pay_1", external_id="pi_1"),
                _payment_doc(
                    payment_id="pay_2",
                    external_id="pi_2",
                    invoice_id="inv_2",
                    account_id="acct_2",
                ),
            ]
        )
        svc = PaymentService(es)

        mapping = await svc.map_external(
            tenant_id=_TENANT, external_ids=["pi_1", "pi_2", "pi_unmapped"]
        )

        assert set(mapping) == {"pi_1", "pi_2"}
        assert mapping["pi_1"]["payment_id"] == "pay_1"
        assert mapping["pi_1"]["invoice_id"] == "inv_1"
        assert mapping["pi_1"]["account_id"] == "acct_1"
        assert mapping["pi_2"]["payment_id"] == "pay_2"
        # "pi_unmapped" has no canonical record → absent from the result.
        assert "pi_unmapped" not in mapping

    @pytest.mark.asyncio
    async def test_empty_input_skips_query(self):
        es = _es_with_hits([])
        svc = PaymentService(es)

        mapping = await svc.map_external(tenant_id=_TENANT, external_ids=[])

        assert mapping == {}
        es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_is_tenant_scoped_and_source_filtered(self):
        es = _es_with_hits([_payment_doc(payment_id="pay_1", external_id="pi_1")])
        svc = PaymentService(es)

        await svc.map_external(tenant_id=_TENANT, external_ids=["pi_1"])

        # The single batched query filters by source + external_id terms and is
        # tenant-scoped via inject_tenant_filter.
        args, kwargs = es.search_documents.call_args
        query = args[1]
        query_str = str(query)
        assert _TENANT in query_str
        assert "stripe" in query_str
        assert "pi_1" in query_str
