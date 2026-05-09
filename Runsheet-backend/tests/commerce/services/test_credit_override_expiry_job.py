"""Unit tests for the credit override expiry scheduled job.

Tests cover:
- Scanning for expired overrides and calling expire_override for each
- No-op when no expired overrides exist
- Graceful handling of individual account failures
- Skipping records with missing account_id or tenant_id

Validates: Requirements 2.6, Task 4.4
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.credit_override_expiry_job import (
    CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS,
    run_credit_override_expiry_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_credit_service() -> AsyncMock:
    """Create a mocked CreditService."""
    cs = AsyncMock()
    cs.expire_override = AsyncMock(return_value=None)
    return cs


def _es_search_response(hits: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [{"_id": h.get("account_id", "unknown"), "_source": h} for h in hits],
            "total": {"value": len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreditOverrideExpiryJob:
    """Tests for run_credit_override_expiry_cycle."""

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_expires_overrides_past_expiry(self, mock_utcnow):
        """Calls expire_override for each account with expired override."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        expired_accounts = [
            {
                "account_id": "acct_001",
                "tenant_id": "tenant_a",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=5)
                ).isoformat(),
            },
            {
                "account_id": "acct_002",
                "tenant_id": "tenant_b",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(hours=1)
                ).isoformat(),
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(expired_accounts)
        )

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        assert result == 2
        assert credit_service.expire_override.call_count == 2

        # Verify correct tenant/account pairs were called
        calls = credit_service.expire_override.call_args_list
        assert calls[0].kwargs == {
            "tenant_id": "tenant_a",
            "account_id": "acct_001",
        }
        assert calls[1].kwargs == {
            "tenant_id": "tenant_b",
            "account_id": "acct_002",
        }

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_no_expired_overrides_returns_zero(self, mock_utcnow):
        """Returns 0 when no expired overrides are found."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        assert result == 0
        credit_service.expire_override.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_continues_on_individual_failure(self, mock_utcnow):
        """Continues processing remaining accounts when one fails."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        expired_accounts = [
            {
                "account_id": "acct_fail",
                "tenant_id": "tenant_a",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=5)
                ).isoformat(),
            },
            {
                "account_id": "acct_ok",
                "tenant_id": "tenant_a",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=10)
                ).isoformat(),
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(expired_accounts)
        )

        # First call fails, second succeeds
        credit_service.expire_override = AsyncMock(
            side_effect=[Exception("ES timeout"), None]
        )

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        # Only the second one succeeded
        assert result == 1
        assert credit_service.expire_override.call_count == 2

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_skips_records_with_missing_account_id(self, mock_utcnow):
        """Skips records that have no account_id."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        expired_accounts = [
            {
                "account_id": None,
                "tenant_id": "tenant_a",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=5)
                ).isoformat(),
            },
            {
                "account_id": "acct_valid",
                "tenant_id": "tenant_a",
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=5)
                ).isoformat(),
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(expired_accounts)
        )

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        assert result == 1
        credit_service.expire_override.assert_called_once_with(
            tenant_id="tenant_a",
            account_id="acct_valid",
        )

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_skips_records_with_missing_tenant_id(self, mock_utcnow):
        """Skips records that have no tenant_id."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        expired_accounts = [
            {
                "account_id": "acct_001",
                "tenant_id": None,
                "credit_override_expires_at": (
                    _FIXED_NOW - timedelta(minutes=5)
                ).isoformat(),
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(expired_accounts)
        )

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        assert result == 0
        credit_service.expire_override.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "commerce.services.credit_override_expiry_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_handles_es_search_failure_gracefully(self, mock_utcnow):
        """Returns 0 when the ES search itself fails."""
        es = _make_es_service()
        credit_service = _make_credit_service()

        es.search_documents = AsyncMock(side_effect=Exception("Connection refused"))

        result = await run_credit_override_expiry_cycle(
            es_service=es, credit_service=credit_service
        )

        assert result == 0
        credit_service.expire_override.assert_not_called()

    def test_interval_is_ten_minutes(self):
        """Verify the interval constant is 600 seconds (10 minutes)."""
        assert CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS == 600
