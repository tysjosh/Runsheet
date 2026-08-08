"""A failed number allocation must not finalize an unnumbered invoice.

``allocate_invoice_number`` had two return paths that a caller could not tell
apart:

* ``None`` because numbering is switched off (persistence dormant / dual-write
  off) — a deliberate posture for an ES-only deployment.
* ``None`` because the allocation *failed* — the exception was caught, logged,
  and swallowed.

``finalize_draft`` treated both as "no number available", so a database blip
transitioned the invoice to OPEN with ``invoice_number`` unset and returned 200.
An open invoice with no number cannot be referenced by an accounting system, and
by the time anyone reads the projection the FINALIZED event has already been
written — so there is nothing to reconcile against.

The two cases are now distinct: a failure raises ``InvoiceNumberingUnavailable``,
and ``finalize_draft`` converts it to a 503 leaving the invoice in ``draft``. 503
rather than 409 because the caller did nothing wrong and the request is worth
retrying unchanged.

Requirements: commerce invoice lifecycle (finalize), Constraint C7 (event first).
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.services.commerce_persistence_bridge import (
    InvoiceNumberingUnavailable,
    allocate_invoice_number,
)
from errors.codes import ErrorCode


@contextlib.asynccontextmanager
async def _fake_session_scope():
    """Stand in for the real session scope so no database is needed.

    Without this the failure cases below would pass for the wrong reason:
    ``session_scope()`` itself raises "persistence layer is dormant" before
    ``allocate_number`` is ever called, so the test would be asserting that a
    missing DATABASE_URL raises rather than that a failed allocation does.
    """
    yield MagicMock()


@pytest.fixture(autouse=True)
def _session_scope():
    with patch(
        "persistence.database.session_scope", _fake_session_scope
    ):
        yield


class TestAllocateDistinguishesOffFromBroken:
    @pytest.mark.asyncio
    async def test_numbering_off_returns_none(self):
        """The legacy ES-only posture is preserved, not converted to an error."""
        with patch(
            "commerce.services.commerce_persistence_bridge._enabled",
            return_value=False,
        ):
            assert await allocate_invoice_number("tenant-1") is None

    @pytest.mark.asyncio
    async def test_a_failed_allocation_raises(self):
        with patch(
            "commerce.services.commerce_persistence_bridge._enabled",
            return_value=True,
        ), patch(
            "persistence.repositories.InvoiceRepository.allocate_number",
            new=AsyncMock(side_effect=RuntimeError("connection reset")),
        ):
            with pytest.raises(InvoiceNumberingUnavailable) as excinfo:
                await allocate_invoice_number("tenant-1")
        assert "tenant-1" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_counter_returning_nothing_also_raises(self):
        """The same defect by another route.

        The repository allocates or raises, so a ``None`` here would mean the
        counter silently produced no number — indistinguishable from success to
        the old caller.
        """
        with patch(
            "commerce.services.commerce_persistence_bridge._enabled",
            return_value=True,
        ), patch(
            "persistence.repositories.InvoiceRepository.allocate_number",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(InvoiceNumberingUnavailable):
                await allocate_invoice_number("tenant-1")

    @pytest.mark.asyncio
    async def test_a_successful_allocation_returns_the_number(self):
        """Counterweight: raising unconditionally would satisfy the above."""
        with patch(
            "commerce.services.commerce_persistence_bridge._enabled",
            return_value=True,
        ), patch(
            "persistence.repositories.InvoiceRepository.allocate_number",
            new=AsyncMock(return_value=41),
        ):
            assert await allocate_invoice_number("tenant-1") == 41


class TestTheErrorCodeIsRetryable:
    def test_numbering_unavailable_maps_to_503(self):
        """409 would blame the client for a dependency being unreachable."""
        from errors.codes import ERROR_CODE_STATUS_MAP

        assert (
            ERROR_CODE_STATUS_MAP[ErrorCode.COMMERCE_INVOICE_NUMBERING_UNAVAILABLE]
            == 503
        )
