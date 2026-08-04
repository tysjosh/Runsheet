"""VoiceReviewHoldHook — promotes voice orders flagged for review to on_hold.

Conforms to the IntakeHook protocol consumed by
``OrderIntakePipeline.register_hook`` (see
``fuel/services/order_intake_pipeline.py``):

    async def before_accept(self, order_draft: dict) -> dict
    async def after_accept(self, order: dict) -> None

The ``VoiceIntakeAdapter`` stamps ``intake_channel="voice"`` and sets a
non-empty ``hold_reason`` (``"voice_review_required"``) when the Dinee
payload has ``reviewRequired`` true. The pipeline's ``_complete_order_doc``
always stamps ``status="placed"``, so adapters cannot express the review
disposition directly.

This hook runs after ``_complete_order_doc`` (which set ``status="placed"``)
and before ``FuelOrder.model_validate``. When a voice order carries a
non-empty ``hold_reason`` and is still ``placed``, it promotes the status to
``on_hold``. Because ``hold_reason`` is already non-empty, the ``FuelOrder``
``_validate_hold`` invariant (``on_hold`` ⇒ non-empty ``hold_reason``) holds.
``on_hold`` orders are excluded from dispatch by the existing state machine
(only ``on_hold → {placed, cancelled}`` transitions are allowed), so no
separate draft store is required.

Validates: Requirements 8.1, 8.2, 8.4
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class VoiceReviewHoldHook:
    """Intake hook that moves review-flagged voice orders to ``on_hold``.

    Conforms to the IntakeHook protocol. Only voice-channel orders that
    were marked for review (non-empty ``hold_reason``) and are still in the
    ``placed`` status are promoted; every other order passes through
    unchanged.

    Validates: Requirements 8.1, 8.2, 8.4
    """

    async def before_accept(self, order_draft: Dict[str, Any]) -> Dict[str, Any]:
        """Promote a review-flagged voice order from ``placed`` to ``on_hold``.

        A voice order is promoted when all of the following hold:
        - ``intake_channel == "voice"``,
        - a non-empty ``hold_reason`` is present (set by the adapter when
          the payload's ``reviewRequired`` is true), and
        - the current ``status`` is ``"placed"`` (stamped by the pipeline).

        The ``hold_reason`` is left intact so the ``FuelOrder`` hold
        invariant continues to hold after the promotion.

        Args:
            order_draft: Mutable dict representing the order before persist.

        Returns:
            The order draft, with ``status`` promoted to ``on_hold`` when the
            review-hold conditions are met; otherwise unchanged.
        """
        if (
            order_draft.get("intake_channel") == "voice"
            and order_draft.get("hold_reason")
            and order_draft.get("status") == "placed"
        ):
            order_draft["status"] = "on_hold"
            logger.info(
                "VoiceReviewHoldHook: promoted voice order to on_hold "
                "(hold_reason=%s)",
                order_draft.get("hold_reason"),
            )

        return order_draft

    async def after_accept(self, order: Dict[str, Any]) -> None:
        """No-op after acceptance — the review-hold disposition is pre-accept.

        Args:
            order: The persisted order document.
        """
        return None
