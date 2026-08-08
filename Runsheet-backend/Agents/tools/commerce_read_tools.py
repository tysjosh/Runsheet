"""Delivery eligibility — credit, hold and AR age in one read-only answer.

A dispatcher's question before sending a truck is not "what is this account's
balance?", it is **"can I deliver to this customer today?"**. The platform
already decides that deterministically in ``CreditService.check`` behind the
``COMMERCE_CREDIT_HOLDS_ENABLED`` gate, and it decides it with a row lock. No
specialist could read the decision, so the agent answered "I cannot identify
accounts over their credit limit. My tools do not have access to credit limit
information" while ``accounts_current`` held the limits.

**Read-only, permanently.** Not a phase-one compromise. These marketers run
their books on an established ERP and their controller has veto power in a deal;
an agent that can move a credit limit or void an invoice is a deal blocker. The
tool answers the dispatch question without touching the ledger, and no commerce
mutation tool is wired to any specialist.

Four properties, carried over from the run-out tool because they are what make a
number safe to repeat in an audited industry:

* **One authoritative verdict.** The tool does not re-implement the credit rule.
  It calls the same ``CreditService.check`` the order intake hook calls, so the
  answer cannot disagree with what actually happens when the order is placed.
  Re-deriving "over limit" here is how two surfaces end up giving two answers to
  one question.
* **The open balance is recomputed, not read off the projection.**
  ``accounts_current.open_balance_cents`` is a cached number maintained by
  payment application; the credit rule sums ``remaining_cents`` over open
  invoices each time. Where the two disagree the tool reports the recomputed
  figure and flags the drift rather than quietly preferring one.
* **Enforcement state is part of the answer.** "On credit hold" means nothing to
  a dispatcher if ``commerce_credit_holds_enabled`` is off — the order will sail
  through. The reply says which it is.
* **Total and page stay separate.** In list mode ``total_blocked`` is the match
  count and ``shown`` is the page length.

One shape the data forced: a customer can hold more than one account. Picking
the first silently would answer for an account the dispatcher did not name, so
an ambiguous customer id returns every account with its own verdict.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from strands import tool

from commerce.services.commerce_es_mappings import ACCOUNTS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter

from ._tenant_context import get_current_tenant
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

#: Wired at startup by ``configure_commerce_read_tools``. Left unset the tool
#: reports that it is unconfigured instead of guessing at a credit verdict.
_credit_service = None
_ar_aging_service = None
_es_service = None


def configure_commerce_read_tools(
    *,
    credit_service=None,
    ar_aging_service=None,
    es_service=None,
) -> None:
    """Wire the commerce read services into this module at startup.

    Args:
        credit_service: ``CreditService`` — owns the authoritative verdict.
        ar_aging_service: ``ARAgingService`` — owns the aging buckets.
        es_service: ``ElasticsearchService`` used to resolve ids to accounts.
    """
    global _credit_service, _ar_aging_service, _es_service
    _credit_service = credit_service
    _ar_aging_service = ar_aging_service
    _es_service = es_service
    logger.info(
        "Commerce read tools configured (credit=%s, aging=%s, es=%s)",
        credit_service is not None,
        ar_aging_service is not None,
        es_service is not None,
    )


def _log_tool_invocation(tool_name, input_params, start_time, success, error=None) -> None:
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )


def _cents(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dollars(cents: int) -> str:
    """Money is stored in integer cents (constraint C1). Render, never compute."""
    return f"{cents / 100:,.2f}"


def _holds_enforced() -> bool:
    """Whether a credit hold actually stops an order right now."""
    from config.settings import get_settings

    return bool(getattr(get_settings(), "commerce_credit_holds_enabled", False))


def _total_hits(response: dict) -> int:
    total = response.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        return total.get("value", 0)
    return total


def _sources(response: dict) -> List[Dict[str, Any]]:
    return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def _account_by_id(tenant_id: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Read one account, preferring Postgres once commerce reads are cut over.

    Mirrors ``CreditService._get_account`` deliberately: reading a different
    store than the credit rule reads is how a tool ends up contradicting the
    decision the order path will make.
    """
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_account_get_or_none,
    )

    pg = await read_account_get_or_none(tenant_id, account_id)
    if pg is not _NOT_CUT_OVER:
        return pg

    query = inject_tenant_filter(
        {"query": {"bool": {"must": [{"term": {"account_id": account_id}}]}}},
        tenant_id,
    )
    query["size"] = 1
    response = await _es_service.search_documents(ACCOUNTS_CURRENT_INDEX, query, size=1)
    sources = _sources(response)
    return sources[0] if sources else None


async def _accounts_for_customer(
    tenant_id: str, customer_id: str
) -> List[Dict[str, Any]]:
    """Every account belonging to a customer.

    Returns all of them: one customer can hold several accounts, and answering
    for whichever happened to sort first would answer a question nobody asked.
    """
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_account_list,
    )

    pg = await read_account_list(tenant_id, customer_id=customer_id, limit=25)
    if pg is not _NOT_CUT_OVER:
        return list((pg or {}).get("items") or [])

    query = inject_tenant_filter(
        {"query": {"bool": {"must": [{"term": {"customer_id": customer_id}}]}}},
        tenant_id,
    )
    query["size"] = 25
    response = await _es_service.search_documents(
        ACCOUNTS_CURRENT_INDEX, query, size=25
    )
    return _sources(response)


async def _account_count(tenant_id: str) -> int:
    """How many accounts the tenant has at all."""
    query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
    query["size"] = 0
    response = await _es_service.search_documents(
        ACCOUNTS_CURRENT_INDEX, query, size=0
    )
    return _total_hits(response)


async def _candidate_blocked_accounts(
    tenant_id: str, limit: int
) -> tuple[List[Dict[str, Any]], int]:
    """Accounts that look blocked, for the list mode.

    Two independent signals, either of which is enough to be a candidate:

    * ``credit_state == "hold"`` — the flag the credit service maintains.
    * ``available_credit_cents <= 0`` — catches an account already over its
      limit whose state has not been flipped yet, and an account on
      cash-on-delivery terms (``credit_limit_cents == 0``), which the credit
      rule treats as never approvable.

    Every candidate is then re-verified against the authoritative rule, so a
    stale projection widens the candidate set but cannot produce a wrong verdict.
    """
    query = inject_tenant_filter(
        {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"credit_state": "hold"}},
                        {"range": {"available_credit_cents": {"lte": 0}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        },
        tenant_id,
    )
    query["size"] = max(1, min(limit, 100))
    query["sort"] = [{"available_credit_cents": {"order": "asc"}}]
    response = await _es_service.search_documents(
        ACCOUNTS_CURRENT_INDEX, query, size=query["size"]
    )
    return _sources(response), _total_hits(response)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


async def _evaluate(
    tenant_id: str, account: Dict[str, Any], order_total_cents: int
) -> Dict[str, Any]:
    """Build one account's eligibility answer.

    The verdict comes from ``CreditService.check`` — the same call the order
    intake hook makes — so this cannot drift from what happens at order time.
    """
    account_id = account.get("account_id")

    decision = await _credit_service.check(
        tenant_id=tenant_id,
        account_id=account_id,
        order_total_cents=order_total_cents,
    )

    credit_limit_cents = _cents(account.get("credit_limit_cents"))
    projected_open_cents = _cents(account.get("open_balance_cents"))

    aging: Dict[str, Any] = {}
    open_balance_cents = projected_open_cents
    if _ar_aging_service is not None:
        aging = await _ar_aging_service.compute_account_aging(tenant_id, account_id)
        open_balance_cents = _cents(aging.get("total_open_cents"))

    entry: Dict[str, Any] = {
        "account_id": account_id,
        "customer_id": account.get("customer_id"),
        "display_name": account.get("display_name"),
        "account_status": account.get("status"),
        # The verdict, not a re-derivation of it.
        "can_deliver_on_credit": bool(decision.approved),
        "reason": decision.reason,
        "hold_required": bool(decision.hold_required),
        "credit_state": account.get("credit_state"),
        "credit_override_active": bool(decision.override_active),
        "credit_override_expires_at": account.get("credit_override_expires_at"),
        "credit_limit_cents": credit_limit_cents,
        "credit_limit_dollars": _dollars(credit_limit_cents),
        "open_balance_cents": open_balance_cents,
        "open_balance_dollars": _dollars(open_balance_cents),
        "available_credit_cents": credit_limit_cents - open_balance_cents,
        "net_terms_days": account.get("net_terms_days"),
    }

    if credit_limit_cents == 0:
        # Distinct from "over limit", and the difference changes what the
        # dispatcher does: this account can still take the delivery, on cash.
        entry["terms_note"] = (
            "Credit limit is zero — this account is cash or prepay only, not "
            "over its limit. A delivery can still go out against payment."
        )

    if aging:
        entry["ar_aging"] = {
            "bucket_0_30_cents": _cents(aging.get("bucket_0_30_cents")),
            "bucket_31_60_cents": _cents(aging.get("bucket_31_60_cents")),
            "bucket_61_90_cents": _cents(aging.get("bucket_61_90_cents")),
            "bucket_90_plus_cents": _cents(aging.get("bucket_90_plus_cents")),
            "total_open_cents": _cents(aging.get("total_open_cents")),
            "oldest_bucket": _oldest_bucket(aging),
        }
        if open_balance_cents != projected_open_cents:
            # Surfaced rather than silently preferred: a controller reading a
            # figure that disagrees with their ERP needs to know which one this
            # is and that the other exists.
            entry["balance_drift"] = {
                "recomputed_open_cents": open_balance_cents,
                "projection_open_cents": projected_open_cents,
                "note": (
                    "The figure above is recomputed from open invoices, which is "
                    "what the credit rule uses. The account projection carries a "
                    "different cached balance."
                ),
            }

    return entry


def _oldest_bucket(aging: Dict[str, Any]) -> Optional[str]:
    """The oldest bucket carrying money — the part a controller reacts to."""
    for key, label in (
        ("bucket_90_plus_cents", "90+"),
        ("bucket_61_90_cents", "61-90"),
        ("bucket_31_60_cents", "31-60"),
        ("bucket_0_30_cents", "0-30"),
    ):
        if _cents(aging.get(key)) > 0:
            return label
    return None


def _unconfigured() -> Optional[str]:
    if _credit_service is None or _es_service is None:
        return json.dumps(
            {
                "tool": "get_customer_delivery_eligibility",
                "error": (
                    "Commerce read tools are not configured on this deployment, "
                    "so credit eligibility cannot be checked."
                ),
            }
        )
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@tool
async def get_customer_delivery_eligibility(
    customer_id: Optional[str] = None,
    account_id: Optional[str] = None,
    order_total_cents: int = 0,
    limit: int = 15,
) -> str:
    """Check whether a customer can take a delivery on credit today.

    Answers "can I deliver to this customer?" and "which accounts are over their
    credit limit?" from the same authoritative credit rule the order intake path
    uses, so the answer cannot disagree with what happens when the order is
    placed. Read-only: it never changes a limit, a hold or an invoice.

    Call it with an id for one customer, or with no id to list the accounts that
    are currently blocked.

    Args:
        customer_id: The customer to check. If the customer holds several
            accounts, every one is returned with its own verdict.
        account_id: A specific account, when known. Takes precedence over
            ``customer_id``.
        order_total_cents: Size of the delivery being considered, in integer
            cents. Left at 0 the answer is "can this account take any credit
            order at all"; supply a figure to ask whether this particular
            delivery fits under the remaining limit.
        limit: In list mode, how many blocked accounts to return. Default 15.

    Returns:
        JSON carrying the verdict per account (``can_deliver_on_credit``,
        ``reason``, ``credit_state``, limit and balance in cents and dollars,
        and ``ar_aging`` with the oldest bucket holding money), plus
        ``credit_holds_enforced`` so the reply can say whether a hold actually
        stops an order on this deployment. In list mode, ``total_blocked`` and
        ``shown`` are separate numbers.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()
    params = {
        "customer_id": customer_id,
        "account_id": account_id,
        "order_total_cents": order_total_cents,
        "limit": limit,
    }

    try:
        logger.info(
            "AI tool invocation: tool=get_customer_delivery_eligibility "
            "tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        unconfigured = _unconfigured()
        if unconfigured is not None:
            success = True
            return unconfigured

        enforced = _holds_enforced()
        order_total_cents = _cents(order_total_cents)

        # --- Single account or single customer ---------------------------
        if account_id or customer_id:
            if account_id:
                account = await _account_by_id(tenant_id, account_id)
                accounts = [account] if account else []
                missing = f"Account '{account_id}' was not found for this tenant."
            else:
                accounts = await _accounts_for_customer(tenant_id, customer_id)
                missing = (
                    f"Customer '{customer_id}' has no billing account, so there "
                    "is no credit limit to check. An account has to exist before "
                    "credit terms apply."
                )

            if not accounts:
                success = True
                return json.dumps(
                    {
                        "tool": "get_customer_delivery_eligibility",
                        "mode": "account",
                        "accounts": [],
                        "credit_holds_enforced": enforced,
                        "no_data_reason": missing,
                    },
                    default=str,
                )

            evaluated = [
                await _evaluate(tenant_id, account, order_total_cents)
                for account in accounts
            ]

            result: Dict[str, Any] = {
                "tool": "get_customer_delivery_eligibility",
                "mode": "account",
                "order_total_cents": order_total_cents,
                "credit_holds_enforced": enforced,
                "accounts": evaluated,
            }
            if len(evaluated) > 1:
                result["ambiguous"] = (
                    f"This customer holds {len(evaluated)} accounts. Each verdict "
                    "below applies only to its own account; say which account you "
                    "mean before acting."
                )
            if not enforced:
                result["enforcement_note"] = (
                    "Credit holds are disabled on this deployment "
                    "(commerce_credit_holds_enabled is off), so an order will not "
                    "actually be stopped by the verdict above. Treat it as advice, "
                    "not as a block."
                )

            success = True
            return json.dumps(result, default=str)

        # --- List mode: who is blocked ----------------------------------
        candidates, candidate_total = await _candidate_blocked_accounts(
            tenant_id, limit
        )

        blocked: List[Dict[str, Any]] = []
        cleared = 0
        for account in candidates:
            entry = await _evaluate(tenant_id, account, order_total_cents)
            if entry["can_deliver_on_credit"]:
                # The projection said blocked, the authoritative rule disagreed.
                cleared += 1
                continue
            blocked.append(entry)

        blocked.sort(key=lambda e: e.get("available_credit_cents", 0))

        result = {
            "tool": "get_customer_delivery_eligibility",
            "mode": "blocked_list",
            "credit_holds_enforced": enforced,
            # Candidates come from the projection and are then re-verified, so
            # the total is an upper bound rather than a measured count. Saying
            # so beats publishing a number the rule has not confirmed.
            "total_blocked_candidates": candidate_total,
            "shown": len(blocked),
            "accounts": blocked,
        }
        if not blocked:
            # "Nobody is blocked" and "commerce has no accounts" are different
            # answers and only one of them is good news. Costs one count query,
            # and only on the empty path.
            total_accounts = await _account_count(tenant_id)
            result["no_data_reason"] = (
                "No billing accounts exist for this tenant, so there are no "
                "credit limits to be over. Commerce may not be enabled here."
                if total_accounts == 0
                else f"All {total_accounts} accounts are within their credit limits."
            )

        if cleared:
            result["projection_drift"] = (
                f"{cleared} account(s) looked blocked in the projection but the "
                "authoritative credit check cleared them. They are excluded above."
            )
        if not enforced:
            result["enforcement_note"] = (
                "Credit holds are disabled on this deployment "
                "(commerce_credit_holds_enabled is off), so these accounts are "
                "over their limits but orders are not being stopped."
            )

        success = True
        logger.info(
            "get_customer_delivery_eligibility: %d blocked of %d candidates "
            "(enforced=%s)",
            len(blocked),
            candidate_total,
            enforced,
        )
        return json.dumps(result, default=str)

    except Exception as exc:  # noqa: BLE001 — a tool must return, not raise
        error_msg = str(exc)
        logger.error("get_customer_delivery_eligibility failed: %s", exc)
        return json.dumps(
            {"tool": "get_customer_delivery_eligibility", "error": str(exc)}
        )
    finally:
        _log_tool_invocation(
            "get_customer_delivery_eligibility", params, start_time, success, error_msg
        )


__all__ = [
    "get_customer_delivery_eligibility",
    "configure_commerce_read_tools",
    "ACCOUNTS_CURRENT_INDEX",
]
