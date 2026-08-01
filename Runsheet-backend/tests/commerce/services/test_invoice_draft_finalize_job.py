"""
Draft invoices must reach ``open`` on their own, closing the delivery→ERP loop.

Requirement 5.2: an Invoice "SHALL be ``draft`` for ``draft_grace_seconds``
(tenant-configurable, default 300 seconds) to allow finalize-time adjustments,
then auto-transition to ``open``".

Nothing performed that transition. ``finalize_draft`` had one caller — the
``POST /api/commerce/invoices/{invoice_id}/finalize`` endpoint — so an invoice
generated from a delivered order waited for a human. Since finalization is what
records ``qbo_push_state=pending`` and fires ``on_invoice_finalized``, the ERP
push never started either, and the design's own
``CommerceInvoiceStuckInDraft`` alert (15 minutes, "5x the default grace
window") would have fired for every single invoice.

Validates: Requirement 5.2
"""

from datetime import timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from commerce.models.invoice import InvoiceStatus
from commerce.services.invoice_draft_finalize_job import (
    INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS,
    resolve_draft_grace_seconds,
    run_invoice_draft_finalize_cycle,
)
from commerce.services.invoice_service import _DEFAULT_DRAFT_GRACE_SECONDS
from services.time_utils import utcnow

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _es_returning(sources: List[Dict[str, Any]]) -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [{"_source": s} for s in sources]}}
    )
    return es


def _invoice(
    invoice_id: str = "inv_1",
    tenant_id: str = TENANT_A,
    age_seconds: int = _DEFAULT_DRAFT_GRACE_SECONDS + 60,
) -> Dict[str, Any]:
    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant_id,
        "status": InvoiceStatus.DRAFT.value,
        "created_at": (utcnow() - timedelta(seconds=age_seconds)).isoformat(),
    }


def _invoice_service() -> MagicMock:
    service = MagicMock()
    service.finalize_draft = AsyncMock(return_value={"status": "open"})
    return service


def _redis_with(value: Any) -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=value)
    return redis


# ---------------------------------------------------------------------------
# The transition itself
# ---------------------------------------------------------------------------


class TestExpiredDraftsAreFinalized:
    @pytest.mark.asyncio
    async def test_a_draft_past_its_grace_window_is_finalized(self):
        es = _es_returning([_invoice()])
        service = _invoice_service()

        count = await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=service
        )

        assert count == 1
        service.finalize_draft.assert_awaited_once_with(
            tenant_id=TENANT_A, invoice_id="inv_1", actor="draft_grace_job"
        )

    @pytest.mark.asyncio
    async def test_the_scan_targets_only_draft_invoices(self):
        """Finalizing an already-open invoice would be a status violation."""
        es = _es_returning([])

        await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=_invoice_service()
        )

        query = es.search_documents.await_args.args[1]
        clauses = query["query"]["bool"]["must"]
        assert {"term": {"status": InvoiceStatus.DRAFT.value}} in clauses

    @pytest.mark.asyncio
    async def test_the_scan_is_bounded_and_oldest_first(self):
        """A backlog must not monopolise the loop, and age order must be fair."""
        es = _es_returning([])

        await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=_invoice_service()
        )

        query = es.search_documents.await_args.args[1]
        assert query["size"] == 500
        assert query["sort"] == [{"created_at": {"order": "asc"}}]

    @pytest.mark.asyncio
    async def test_multiple_tenants_are_each_finalized(self):
        es = _es_returning(
            [_invoice("inv_a", TENANT_A), _invoice("inv_b", TENANT_B)]
        )
        service = _invoice_service()

        count = await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=service
        )

        assert count == 2
        finalized = {
            call.kwargs["invoice_id"]
            for call in service.finalize_draft.await_args_list
        }
        assert finalized == {"inv_a", "inv_b"}

    @pytest.mark.asyncio
    async def test_the_sweep_interval_stays_inside_the_stuck_draft_alert(self):
        """Grace + interval must land well under the 15-minute alert.

        ``CommerceInvoiceStuckInDraft`` fires at 900s. An invoice's worst case
        is its grace window plus one full sweep interval.
        """
        worst_case = (
            _DEFAULT_DRAFT_GRACE_SECONDS
            + INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS
        )
        assert worst_case < 900


# ---------------------------------------------------------------------------
# Not-yet-expired drafts
# ---------------------------------------------------------------------------


class TestUnexpiredDraftsAreLeftAlone:
    @pytest.mark.asyncio
    async def test_a_longer_tenant_grace_defers_finalization(self):
        """The grace window exists for finalize-time adjustments; respect it.

        The coarse scan filters on the platform default, so a tenant with a
        longer window needs the per-tenant re-check to protect it.
        """
        es = _es_returning([_invoice(age_seconds=400)])
        service = _invoice_service()

        count = await run_invoice_draft_finalize_cycle(
            es_service=es,
            invoice_service=service,
            redis_client=_redis_with("3600"),
        )

        assert count == 0
        service.finalize_draft.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_shorter_tenant_grace_finalizes(self):
        es = _es_returning([_invoice(age_seconds=400)])
        service = _invoice_service()

        count = await run_invoice_draft_finalize_cycle(
            es_service=es,
            invoice_service=service,
            redis_client=_redis_with("60"),
        )

        assert count == 1

    @pytest.mark.asyncio
    async def test_an_unparseable_created_at_is_left_in_draft(self):
        """Finalization is the idempotency cutoff — never act on a guess."""
        invoice = _invoice()
        invoice["created_at"] = "not-a-timestamp"
        service = _invoice_service()

        count = await run_invoice_draft_finalize_cycle(
            es_service=_es_returning([invoice]), invoice_service=service
        )

        assert count == 0
        service.finalize_draft.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tenant-configurable grace (Req 5.2)
# ---------------------------------------------------------------------------


class TestGraceResolution:
    @pytest.mark.asyncio
    async def test_no_redis_yields_the_platform_default(self):
        assert await resolve_draft_grace_seconds(TENANT_A) == (
            _DEFAULT_DRAFT_GRACE_SECONDS
        )

    @pytest.mark.asyncio
    async def test_an_unset_key_yields_the_platform_default(self):
        assert await resolve_draft_grace_seconds(
            TENANT_A, redis_client=_redis_with(None)
        ) == _DEFAULT_DRAFT_GRACE_SECONDS

    @pytest.mark.asyncio
    async def test_a_tenant_override_is_honoured(self):
        assert await resolve_draft_grace_seconds(
            TENANT_A, redis_client=_redis_with("120")
        ) == 120

    @pytest.mark.parametrize("bad", ["abc", "", "-5", None])
    @pytest.mark.asyncio
    async def test_a_bad_value_falls_back_rather_than_raising(self, bad):
        """A bad config value must not strand a tenant's invoices forever."""
        assert await resolve_draft_grace_seconds(
            TENANT_A, redis_client=_redis_with(bad)
        ) == _DEFAULT_DRAFT_GRACE_SECONDS

    @pytest.mark.asyncio
    async def test_a_redis_outage_falls_back_rather_than_raising(self):
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

        assert await resolve_draft_grace_seconds(
            TENANT_A, redis_client=redis
        ) == _DEFAULT_DRAFT_GRACE_SECONDS


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailuresAreIsolated:
    @pytest.mark.asyncio
    async def test_one_failing_invoice_does_not_stop_the_batch(self):
        es = _es_returning(
            [_invoice("inv_bad", TENANT_A), _invoice("inv_ok", TENANT_A)]
        )
        service = _invoice_service()
        service.finalize_draft = AsyncMock(
            side_effect=[RuntimeError("boom"), {"status": "open"}]
        )

        count = await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=service
        )

        assert count == 1
        assert service.finalize_draft.await_count == 2

    @pytest.mark.asyncio
    async def test_a_failed_scan_returns_zero_rather_than_raising(self):
        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=RuntimeError("es down"))

        assert await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=_invoice_service()
        ) == 0

    @pytest.mark.asyncio
    async def test_invoices_missing_identifiers_are_skipped(self):
        service = _invoice_service()
        es = _es_returning(
            [
                {"tenant_id": TENANT_A, "created_at": utcnow().isoformat()},
                {"invoice_id": "inv_no_tenant"},
            ]
        )

        assert await run_invoice_draft_finalize_cycle(
            es_service=es, invoice_service=service
        ) == 0
        service.finalize_draft.assert_not_awaited()
