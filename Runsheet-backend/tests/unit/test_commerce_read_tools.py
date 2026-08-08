"""Unit tests for the delivery-eligibility read tool.

The agent answered "I cannot identify accounts over their credit limit. My tools
do not have access to credit limit information" while ``accounts_current`` held
the limits. This tool closes that, and these tests pin the properties that stop
it from closing the gap with an answer the order path would contradict:

1. **The verdict is delegated, never re-derived.** It comes from the same
   ``CreditService.check`` the intake hook calls. A second implementation of the
   credit rule is a second answer to one question.
2. **Enforcement state travels with the verdict.** "On credit hold" is
   meaningless to a dispatcher when ``commerce_credit_holds_enabled`` is off.
3. **The open balance is recomputed, and drift is reported.** The projection
   caches a balance that the credit rule does not use.
4. **An ambiguous customer is not silently resolved.** One customer can hold
   several accounts.
5. **Read-only.** No code path here writes to the ledger.
6. **Total and page stay separate** in list mode, and a candidate the
   authoritative check clears is excluded rather than reported as blocked.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools._tenant_context import (  # noqa: E402
    current_tenant_id_var,
    set_current_tenant,
)
from Agents.tools.commerce_read_tools import (  # noqa: E402
    configure_commerce_read_tools,
    get_customer_delivery_eligibility,
)

TENANT = "tenant-a"
_tool = (
    getattr(get_customer_delivery_eligibility, "_tool_func", None)
    or get_customer_delivery_eligibility
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _account(
    account_id: str = "acct-1",
    *,
    customer_id: str = "CUST-001",
    credit_limit_cents: int = 500_000,
    open_balance_cents: int = 100_000,
    credit_state: str = "ok",
) -> Dict[str, Any]:
    """An ``accounts_current`` projection document."""
    return {
        "account_id": account_id,
        "tenant_id": TENANT,
        "customer_id": customer_id,
        "display_name": "Northline Propane",
        "status": "active",
        "credit_limit_cents": credit_limit_cents,
        "open_balance_cents": open_balance_cents,
        "available_credit_cents": credit_limit_cents - open_balance_cents,
        "credit_state": credit_state,
        "credit_override_expires_at": None,
        "net_terms_days": 30,
    }


class _Decision:
    """Stands in for ``CreditDecision``, which is a frozen dataclass."""

    def __init__(self, approved: bool, reason: str, hold_required: bool = False,
                 override_active: bool = False):
        self.approved = approved
        self.reason = reason
        self.hold_required = hold_required
        self.override_active = override_active


def _credit_service(decision: _Decision | None = None, per_account: Dict[str, _Decision] | None = None):
    svc = MagicMock()
    calls: List[Dict[str, Any]] = []

    async def _check(*, tenant_id, account_id, order_total_cents):
        calls.append(
            {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "order_total_cents": order_total_cents,
            }
        )
        if per_account is not None:
            return per_account[account_id]
        return decision or _Decision(True, "within_credit_limit")

    svc.check = _check
    svc.check_calls = calls
    return svc


def _aging_service(total_open_cents: int = 100_000, **buckets):
    svc = MagicMock()
    payload = {
        "bucket_0_30_cents": buckets.get("b0", total_open_cents),
        "bucket_31_60_cents": buckets.get("b31", 0),
        "bucket_61_90_cents": buckets.get("b61", 0),
        "bucket_90_plus_cents": buckets.get("b90", 0),
        "total_open_cents": total_open_cents,
    }
    svc.compute_account_aging = AsyncMock(return_value=payload)
    return svc


def _es_returning(*responses: Dict[str, Any]):
    """ES double whose ``search_documents`` returns the given responses in order."""
    es = MagicMock()
    queue = list(responses)
    captured: List[tuple] = []

    def _search(index, body, size=None):
        captured.append((index, body))
        return queue.pop(0) if queue else {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = AsyncMock(side_effect=_search)
    es.captured = captured
    return es


def _hits(docs: List[Dict[str, Any]], total: int | None = None) -> Dict[str, Any]:
    return {
        "hits": {
            "hits": [{"_source": d} for d in docs],
            "total": {"value": total if total is not None else len(docs)},
        }
    }


@pytest.fixture(autouse=True)
def _reset_module_state():
    yield
    configure_commerce_read_tools(
        credit_service=None, ar_aging_service=None, es_service=None
    )


def _patch_bridge(account=None, account_list=None):
    """Force the ES path by reporting commerce reads are not cut over to PG.

    ``_NOT_CUT_OVER`` is a sentinel object, so the double has to return that
    exact object for the tool to fall through to Elasticsearch.
    """
    from commerce.services import commerce_persistence_bridge as bridge

    return patch.multiple(
        bridge,
        read_account_get_or_none=AsyncMock(
            return_value=account if account is not None else bridge._NOT_CUT_OVER
        ),
        read_account_list=AsyncMock(
            return_value=account_list
            if account_list is not None
            else bridge._NOT_CUT_OVER
        ),
    )


async def _call(**kwargs) -> Dict[str, Any]:
    with set_current_tenant(TENANT):
        return json.loads(await _tool(**kwargs))


def _holds(enabled: bool):
    settings = MagicMock()
    settings.commerce_credit_holds_enabled = enabled
    return patch("config.settings.get_settings", return_value=settings)


# ---------------------------------------------------------------------------
# The verdict is delegated
# ---------------------------------------------------------------------------


class TestVerdictComesFromTheCreditService:
    @pytest.mark.asyncio
    async def test_approved_account_can_take_a_delivery(self):
        credit = _credit_service(_Decision(True, "within_credit_limit"))
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(100_000),
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        entry = payload["accounts"][0]
        assert entry["can_deliver_on_credit"] is True
        assert entry["reason"] == "within_credit_limit"
        assert entry["credit_limit_cents"] == 500_000
        assert entry["credit_limit_dollars"] == "5,000.00"

    @pytest.mark.asyncio
    async def test_over_limit_account_is_refused_with_the_services_reason(self):
        credit = _credit_service(
            _Decision(False, "credit_limit_exceeded", hold_required=True)
        )
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(900_000),
            es_service=_es_returning(
                _hits([_account(credit_limit_cents=500_000, credit_state="hold")])
            ),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        entry = payload["accounts"][0]
        assert entry["can_deliver_on_credit"] is False
        assert entry["reason"] == "credit_limit_exceeded"
        assert entry["hold_required"] is True
        assert entry["credit_state"] == "hold"

    @pytest.mark.asyncio
    async def test_the_order_size_is_passed_to_the_credit_rule(self):
        """Whether a delivery fits depends on its size; dropping it would answer
        a different question from the one the dispatcher asked."""
        credit = _credit_service(_Decision(True, "within_credit_limit"))
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            await _call(account_id="acct-1", order_total_cents=250_000)

        assert credit.check_calls[0]["order_total_cents"] == 250_000
        assert credit.check_calls[0]["tenant_id"] == TENANT

    @pytest.mark.asyncio
    async def test_active_override_is_surfaced(self):
        credit = _credit_service(
            _Decision(True, "credit_override_active", override_active=True)
        )
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account(credit_state="override")])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        entry = payload["accounts"][0]
        assert entry["credit_override_active"] is True
        assert entry["can_deliver_on_credit"] is True


# ---------------------------------------------------------------------------
# Enforcement state
# ---------------------------------------------------------------------------


class TestEnforcementStateTravelsWithTheVerdict:
    @pytest.mark.asyncio
    async def test_flag_off_is_reported_and_explained(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(
                _Decision(False, "credit_limit_exceeded", hold_required=True)
            ),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account(credit_state="hold")])),
        )
        with _patch_bridge(), _holds(False):
            payload = await _call(account_id="acct-1")

        assert payload["credit_holds_enforced"] is False
        assert "enforcement_note" in payload, (
            "a hold that does not stop an order must say so, or the dispatcher "
            "acts on a block that is not there"
        )

    @pytest.mark.asyncio
    async def test_flag_on_needs_no_caveat(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        assert payload["credit_holds_enforced"] is True
        assert "enforcement_note" not in payload


# ---------------------------------------------------------------------------
# Balances and aging
# ---------------------------------------------------------------------------


class TestBalanceAndAging:
    @pytest.mark.asyncio
    async def test_recomputed_balance_wins_and_drift_is_flagged(self):
        """The projection caches a balance the credit rule does not use."""
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            # Recomputed 300,000 vs the projection's 100,000.
            ar_aging_service=_aging_service(300_000),
            es_service=_es_returning(_hits([_account(open_balance_cents=100_000)])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        entry = payload["accounts"][0]
        assert entry["open_balance_cents"] == 300_000
        assert entry["available_credit_cents"] == 200_000
        drift = entry["balance_drift"]
        assert drift["recomputed_open_cents"] == 300_000
        assert drift["projection_open_cents"] == 100_000

    @pytest.mark.asyncio
    async def test_agreeing_numbers_produce_no_drift_block(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(100_000),
            es_service=_es_returning(_hits([_account(open_balance_cents=100_000)])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        assert "balance_drift" not in payload["accounts"][0]

    @pytest.mark.asyncio
    async def test_oldest_bucket_holding_money_is_named(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(50_000, b0=20_000, b31=0, b61=0, b90=30_000),
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        assert payload["accounts"][0]["ar_aging"]["oldest_bucket"] == "90+"

    @pytest.mark.asyncio
    async def test_no_open_money_has_no_oldest_bucket(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(0, b0=0),
            es_service=_es_returning(_hits([_account(open_balance_cents=0)])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        assert payload["accounts"][0]["ar_aging"]["oldest_bucket"] is None

    @pytest.mark.asyncio
    async def test_zero_limit_is_cash_terms_not_over_limit(self):
        """The credit rule treats a zero limit as never approvable. Reporting
        that as 'over their limit' would send a dispatcher chasing a receivable
        that does not exist."""
        configure_commerce_read_tools(
            credit_service=_credit_service(
                _Decision(False, "credit_limit_exceeded", hold_required=True)
            ),
            ar_aging_service=_aging_service(0, b0=0),
            es_service=_es_returning(
                _hits([_account(credit_limit_cents=0, open_balance_cents=0)])
            ),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        note = payload["accounts"][0]["terms_note"]
        assert "cash or prepay" in note
        assert "not" in note and "over its limit" in note


# ---------------------------------------------------------------------------
# Customer resolution
# ---------------------------------------------------------------------------


class TestCustomerResolution:
    @pytest.mark.asyncio
    async def test_multiple_accounts_are_all_returned_and_flagged_ambiguous(self):
        accounts = [_account("acct-1"), _account("acct-2")]
        credit = _credit_service(
            per_account={
                "acct-1": _Decision(True, "within_credit_limit"),
                "acct-2": _Decision(False, "credit_limit_exceeded", hold_required=True),
            }
        )
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits(accounts)),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(customer_id="CUST-001")

        assert len(payload["accounts"]) == 2
        assert "ambiguous" in payload, (
            "answering for whichever account sorted first answers a question "
            "nobody asked"
        )
        verdicts = {a["account_id"]: a["can_deliver_on_credit"] for a in payload["accounts"]}
        assert verdicts == {"acct-1": True, "acct-2": False}

    @pytest.mark.asyncio
    async def test_single_account_is_not_flagged_ambiguous(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(customer_id="CUST-001")

        assert "ambiguous" not in payload

    @pytest.mark.asyncio
    async def test_customer_without_an_account_explains_itself(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(customer_id="CUST-404")

        assert payload["accounts"] == []
        assert "no billing account" in payload["no_data_reason"], payload
        assert "credit limit" in payload["no_data_reason"]

    @pytest.mark.asyncio
    async def test_missing_account_is_not_reported_as_eligible(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-missing")

        assert payload["accounts"] == []
        assert "not found" in payload["no_data_reason"]

    @pytest.mark.asyncio
    async def test_postgres_is_preferred_once_commerce_reads_are_cut_over(self):
        """Reading a different store than the credit rule reads is how a tool
        ends up contradicting the decision the order path will make."""
        es = _es_returning(_hits([_account("acct-from-es")]))
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=es,
        )
        with _patch_bridge(account=_account("acct-from-pg")), _holds(True):
            payload = await _call(account_id="acct-from-pg")

        assert payload["accounts"][0]["account_id"] == "acct-from-pg"
        assert not es.search_documents.called, (
            "the Postgres read was available and ES was queried anyway"
        )


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------


class TestBlockedListMode:
    @pytest.mark.asyncio
    async def test_candidates_are_reverified_and_cleared_ones_excluded(self):
        candidates = [_account("acct-1", credit_state="hold"), _account("acct-2")]
        credit = _credit_service(
            per_account={
                "acct-1": _Decision(False, "credit_limit_exceeded", hold_required=True),
                # Projection looked blocked; the authoritative rule disagrees.
                "acct-2": _Decision(True, "within_credit_limit"),
            }
        )
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits(candidates, total=7)),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call()

        assert payload["mode"] == "blocked_list"
        assert [a["account_id"] for a in payload["accounts"]] == ["acct-1"]
        assert payload["shown"] == 1
        assert payload["total_blocked_candidates"] == 7
        assert "projection_drift" in payload, (
            "a candidate the credit rule cleared must be accounted for, not "
            "silently dropped"
        )

    @pytest.mark.asyncio
    async def test_nobody_blocked_is_distinguished_from_no_accounts(self):
        """Good news and missing data are different answers."""
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            # First response: no candidates. Second: the account count.
            es_service=_es_returning(_hits([]), _hits([], total=12)),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call()

        assert "All 12 accounts are within their credit limits" in payload["no_data_reason"]

    @pytest.mark.asyncio
    async def test_no_accounts_at_all_says_so(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([]), _hits([], total=0)),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call()

        assert "No billing accounts exist" in payload["no_data_reason"]

    @pytest.mark.asyncio
    async def test_candidate_query_catches_holds_and_exhausted_credit(self):
        es = _es_returning(_hits([]))
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=es,
        )
        with _patch_bridge(), _holds(True):
            await _call()

        _index, body = es.captured[0]
        inner = body["query"]["bool"]["must"][0]["bool"]
        should = inner["should"]
        assert {"term": {"credit_state": "hold"}} in should, should
        assert {"range": {"available_credit_cents": {"lte": 0}}} in should, should
        assert inner["minimum_should_match"] == 1

    @pytest.mark.asyncio
    async def test_every_query_is_tenant_scoped(self):
        es = _es_returning(_hits([]))
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=es,
        )
        with _patch_bridge(), _holds(True):
            await _call()

        for _index, body in es.captured:
            terms = body["query"]["bool"]["filter"]
            assert any(
                f.get("term", {}).get("tenant_id") == TENANT for f in terms
            ), body

    @pytest.mark.asyncio
    async def test_page_size_is_capped(self):
        es = _es_returning(_hits([]))
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=es,
        )
        with _patch_bridge(), _holds(True):
            await _call(limit=10_000)

        assert es.captured[0][1]["size"] <= 100

    @pytest.mark.asyncio
    async def test_most_exposed_account_first(self):
        candidates = [
            _account("acct-small", credit_limit_cents=100_000, open_balance_cents=110_000),
            _account("acct-big", credit_limit_cents=100_000, open_balance_cents=900_000),
        ]
        credit = _credit_service(
            _Decision(False, "credit_limit_exceeded", hold_required=True)
        )
        aging = MagicMock()

        async def _per_account(tenant_id, account_id):
            total = 110_000 if account_id == "acct-small" else 900_000
            return {
                "bucket_0_30_cents": total,
                "bucket_31_60_cents": 0,
                "bucket_61_90_cents": 0,
                "bucket_90_plus_cents": 0,
                "total_open_cents": total,
            }

        aging.compute_account_aging = _per_account
        configure_commerce_read_tools(
            credit_service=credit,
            ar_aging_service=aging,
            es_service=_es_returning(_hits(candidates)),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call()

        assert [a["account_id"] for a in payload["accounts"]] == [
            "acct-big",
            "acct-small",
        ]


# ---------------------------------------------------------------------------
# Read-only, tenant scope, failure handling
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_the_module_never_writes(self):
        """A grep-level guard. Nothing here may index, update or delete."""
        import pathlib

        import Agents.tools.commerce_read_tools as module

        source = pathlib.Path(module.__file__).read_text()
        for forbidden in (
            "index_document",
            "update_document",
            "delete_document",
            "apply_override",
            "expire_override",
            "bulk(",
        ):
            assert forbidden not in source, (
                f"{forbidden!r} appears in a tool documented as read-only. The "
                "ERP is the book of record and credit enforcement stays in the "
                "row-locked intake hook."
            )


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_unconfigured_declines_instead_of_guessing(self):
        configure_commerce_read_tools(
            credit_service=None, ar_aging_service=None, es_service=None
        )
        with set_current_tenant(TENANT):
            payload = json.loads(await _tool(account_id="acct-1"))

        assert "not configured" in payload["error"]

    @pytest.mark.asyncio
    async def test_aging_service_absent_still_yields_a_verdict(self):
        """Credit is the gate; aging is context. Losing the context must not
        turn a clear verdict into an error."""
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=None,
            es_service=_es_returning(_hits([_account()])),
        )
        with _patch_bridge(), _holds(True):
            payload = await _call(account_id="acct-1")

        entry = payload["accounts"][0]
        assert entry["can_deliver_on_credit"] is True
        assert "ar_aging" not in entry

    @pytest.mark.asyncio
    async def test_backend_error_is_returned_not_raised(self):
        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=Exception("es down"))
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=es,
        )
        with _patch_bridge(), _holds(True), set_current_tenant(TENANT):
            payload = json.loads(await _tool(account_id="acct-1"))

        assert payload["error"] == "es down"

    @pytest.mark.asyncio
    async def test_missing_tenant_scope_raises(self):
        configure_commerce_read_tools(
            credit_service=_credit_service(),
            ar_aging_service=_aging_service(),
            es_service=_es_returning(_hits([_account()])),
        )
        token = current_tenant_id_var.set(None)
        try:
            with pytest.raises(RuntimeError):
                await _tool(account_id="acct-1")
        finally:
            current_tenant_id_var.reset(token)
