"""Terminal attribution for an order (design "Terminal attribution (D11)").

``MarginAttribution(es_service).terminal_for_order(tenant_id, order)`` returns
``(terminal_id, reason)``:

1. no ``assigned_run_id`` -> ``(None, "no_plan")``;
2. the ``mvp_load_plans`` query is the one ``PodService._resolve_loading_plan``
   (``driver/services/pod_service.py``) issues: tenant term, ``should`` on
   ``plan_id`` / ``run_id`` equal to ``assigned_run_id``, size 5. The first
   plan with an ``assignments[].station_id`` equal to the order's
   ``customer_tank_id`` or ``order_id`` is the order's plan; none ->
   ``(None, "no_plan")``;
3. ``plan.terminal_id`` set -> ``(terminal_id, "plan_terminal")``;
4. otherwise the plan's BOLs (``terminal_bols`` by ``load_plan_id``,
   tenant-scoped, size 20): exactly one distinct non-null ``terminal_id`` ->
   ``(terminal_id, "bol_terminal")``;
5. otherwise ``(None, "unattributed")``.

The query is duplicated from POD on purpose rather than importing POD's
private method (three lines, pinned by a test).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from compliance.services.compliance_es_mappings import TERMINAL_BOLS_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

MVP_LOAD_PLANS_INDEX = "mvp_load_plans"
PLAN_QUERY_SIZE = 5
BOL_QUERY_SIZE = 20

REASON_NO_PLAN = "no_plan"
REASON_PLAN_TERMINAL = "plan_terminal"
REASON_BOL_TERMINAL = "bol_terminal"
REASON_UNATTRIBUTED = "unattributed"


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sources(response: Mapping[str, Any]) -> list[Dict[str, Any]]:
    hits = ((response or {}).get("hits") or {}).get("hits") or []
    return [hit.get("_source") or {} for hit in hits]


class MarginAttribution:
    """Which terminal an order's fuel was lifted at, for the cost basis."""

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    async def terminal_for_order(
        self, tenant_id: str, order: Mapping[str, Any]
    ) -> Tuple[Optional[str], str]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        plan_ref = _clean(order.get("assigned_run_id"))
        if plan_ref is None:
            return None, REASON_NO_PLAN

        plan = await self._find_plan(tenant_id, plan_ref, order)
        if plan is None:
            return None, REASON_NO_PLAN

        plan_terminal = _clean(plan.get("terminal_id"))
        if plan_terminal is not None:
            return plan_terminal, REASON_PLAN_TERMINAL

        plan_id = _clean(plan.get("plan_id")) or plan_ref
        response = await self._es.search_documents(
            TERMINAL_BOLS_INDEX,
            inject_tenant_filter(
                {"query": {"term": {"load_plan_id": plan_id}}, "size": BOL_QUERY_SIZE},
                tenant_id,
            ),
            BOL_QUERY_SIZE,
        )
        terminals = {
            terminal
            for source in _sources(response)
            if source.get("tenant_id") == tenant_id
            for terminal in [_clean(source.get("terminal_id"))]
            if terminal is not None
        }
        if len(terminals) == 1:
            return next(iter(terminals)), REASON_BOL_TERMINAL
        return None, REASON_UNATTRIBUTED

    async def _find_plan(
        self, tenant_id: str, plan_ref: str, order: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        # Same query shape as PodService._resolve_loading_plan (duplicated, not imported).
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"tenant_id": tenant_id}}],
                    "should": [
                        {"term": {"plan_id": plan_ref}},
                        {"term": {"run_id": plan_ref}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": PLAN_QUERY_SIZE,
        }
        response = await self._es.search_documents(MVP_LOAD_PLANS_INDEX, query, PLAN_QUERY_SIZE)
        station_keys = {
            str(value)
            for value in (order.get("customer_tank_id"), order.get("order_id"))
            if value
        }
        for source in _sources(response):
            if source.get("tenant_id") not in (None, tenant_id):
                logger.error(
                    "margin attribution: dropping load plan %s from another tenant "
                    "(owner=%s, requester=%s)",
                    source.get("plan_id"),
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            assignments = source.get("assignments") or []
            if any(str(a.get("station_id")) in station_keys for a in assignments if isinstance(a, Mapping)):
                return source
        return None


__all__ = [
    "MarginAttribution",
    "REASON_BOL_TERMINAL",
    "REASON_NO_PLAN",
    "REASON_PLAN_TERMINAL",
    "REASON_UNATTRIBUTED",
]
