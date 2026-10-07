"""Order write actions shared by the staff order API and the customer portal.

* :func:`transition_order_if` — the compare-and-set status write (customer
  portal FREEZE F3) plus the Postgres mirror.
* :func:`cancel_order` — cancel, ``order_cancelled`` event and the driver
  counter decrement, extracted from the staff ``POST /api/orders/{id}/cancel``
  handler so the portal cancel runs the same code (design §4.4).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fuel.order_repository import ANY_HOLD_REASON
from fuel.services.order_id_generator import mint_event_id
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = ["ANY_HOLD_REASON", "cancel_order", "transition_order_if"]


async def _mirror(order_id: str, doc: Dict[str, Any]) -> None:
    """Best-effort Postgres mirror, like ``_apply_order_update``."""
    try:
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )

        await mirror_current_state_upsert("fuel_order", doc)
    except Exception as exc:  # noqa: BLE001 — best-effort during the soak
        logger.warning(
            "order_actions: Postgres mirror failed for order=%s: %s", order_id, exc
        )


async def _legacy_write(
    repo: Any,
    tenant_id: str,
    order_id: str,
    *,
    expected_status: str,
    expected_hold_reason: Any,
    update_fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Read-check-write for repositories without ``transition_if``.

    Only the narrow in-memory repository fakes in older endpoint tests lack
    ``transition_if``; the production :class:`FuelOrderRepository` always has
    it. Same refusal rules, without the row lock.
    """
    order = await repo.get(tenant_id, order_id)
    if order is None or order.status != expected_status:
        return None
    if expected_hold_reason is not ANY_HOLD_REASON and order.hold_reason != expected_hold_reason:
        return None
    await repo._es.update_document(repo._orders_index, order_id, update_fields)
    merged = order.model_dump(mode="json")
    merged.update(update_fields)
    merged.setdefault("tenant_id", tenant_id)
    return merged


async def transition_order_if(
    repo: Any,
    tenant_id: str,
    order_id: str,
    *,
    expected_status: str,
    expected_hold_reason: Any = ANY_HOLD_REASON,
    update_fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """CAS status write (``FuelOrderRepository.transition_if``) plus mirror.

    Returns the new order document, or ``None`` when the stored order no
    longer has ``expected_status`` (and ``expected_hold_reason`` when given).
    """
    transition_if = getattr(repo, "transition_if", None)
    if transition_if is None:
        doc = await _legacy_write(
            repo,
            tenant_id,
            order_id,
            expected_status=expected_status,
            expected_hold_reason=expected_hold_reason,
            update_fields=update_fields,
        )
    else:
        doc = await transition_if(
            tenant_id,
            order_id,
            expected_status=expected_status,
            expected_hold_reason=expected_hold_reason,
            update_fields=update_fields,
        )
    if doc is not None:
        await _mirror(order_id, doc)
    return doc


async def cancel_order(
    repo: Any,
    tenant_id: str,
    order_id: str,
    *,
    actor_user_id: Optional[str],
    reason: str,
    notes: Optional[str],
    expected_status: str,
    expected_hold_reason: Any = ANY_HOLD_REASON,
    counter_service: Any = None,
    clear_hold_reason: bool = False,
) -> Optional[Dict[str, Any]]:
    """Cancel an order if it is still in ``expected_status``.

    Writes ``status="cancelled"`` through :func:`transition_order_if`, then
    appends one ``order_cancelled`` event ``{old_status, reason, notes,
    actor_user_id}`` and, for an order cancelled from ``dispatched``,
    decrements the assigned driver's active-order counter (best effort).
    ``clear_hold_reason`` also sets ``hold_reason`` to ``None`` (the portal
    cancel; the staff cancel keeps it, as before).

    Returns the cancelled order document, or ``None`` (nothing written) when
    the order has changed since the caller read it. The caller validates the
    transition with the state machine first.
    """
    now = utcnow()
    update_fields: Dict[str, Any] = {
        "status": "cancelled",
        "updated_at": now.isoformat(),
        "last_event_timestamp": now.isoformat(),
    }
    if clear_hold_reason:
        update_fields["hold_reason"] = None
    doc = await transition_order_if(
        repo,
        tenant_id,
        order_id,
        expected_status=expected_status,
        expected_hold_reason=expected_hold_reason,
        update_fields=update_fields,
    )
    if doc is None:
        return None

    event_doc = {
        "event_id": mint_event_id(),
        "order_id": order_id,
        "tenant_id": tenant_id,
        "event_type": "order_cancelled",
        "event_payload": {
            "old_status": expected_status,
            "reason": reason,
            "notes": notes,
            "actor_user_id": actor_user_id,
        },
        "event_timestamp": now.isoformat(),
        "ingested_at": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": str(uuid.uuid4()),
    }
    await repo.append_event(tenant_id, event_doc)

    # Decrement the driver's active_order_count on cancel from dispatched.
    driver_id = doc.get("assigned_driver_id")
    if counter_service is not None and driver_id and expected_status == "dispatched":
        try:
            await counter_service.increment_counters(
                driver_id=driver_id,
                tenant_id=tenant_id,
                delta_active=-1,
            )
        except Exception as exc:
            logger.warning(
                "order_endpoints.cancel: counter decrement failed for "
                "driver=%s, order=%s: %s",
                driver_id,
                order_id,
                exc,
            )
    return doc
