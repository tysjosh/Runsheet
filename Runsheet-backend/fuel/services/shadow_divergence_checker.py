"""
Shadow-mode divergence comparison for the Order Intake Pipeline.

Per sampled event, runs both the legacy and new adapter outputs through a
field-by-field diff (skipping ``_id``, ``updated_at``, timestamps) and emits
``orders_shadow_divergence_total{tenant_id, intake_channel, field}`` for
every mismatch.

Sample rate is controlled by
``settings.orders_intake_pipeline_shadow_divergence_sample_rate``.

Validates: Requirement 9.3.2.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Dict, Optional, Set

from fuel.services.order_metrics import orders_shadow_divergence_total

logger = logging.getLogger(__name__)

#: Fields to skip during divergence comparison. These are either
#: platform-assigned (timestamps, IDs) or internal ES metadata.
SKIP_FIELDS: Set[str] = frozenset({
    "_id",
    "updated_at",
    "created_at",
    "last_event_timestamp",
    "event_timestamp",
    "ingested_at",
    "order_id",
    "event_id",
    "trace_id",
})


class ShadowDivergenceChecker:
    """Compares new adapter output against legacy adapter output.

    When the overlay is in ``shadow`` mode, both the new and legacy
    adapters process the same inbound event. This checker diffs the
    two outputs field-by-field and emits a Prometheus counter for
    every divergent field.

    The comparison is sampled: only a fraction of events (controlled
    by ``settings.orders_intake_pipeline_shadow_divergence_sample_rate``)
    are compared. A rate of ``1.0`` means every event is compared;
    ``0.0`` disables comparison entirely.
    """

    def __init__(
        self,
        sample_rate: Optional[float] = None,
        legacy_adapter: Optional[Any] = None,
    ) -> None:
        """
        Args:
            sample_rate: Override for the sample rate. If None, reads
                from settings at comparison time.
            legacy_adapter: Optional legacy adapter instance for
                producing the legacy output. If None, the checker
                attempts to import and use the LegacyDineeShipmentAdapter.
        """
        self._sample_rate = sample_rate
        self._legacy_adapter = legacy_adapter

    def _get_sample_rate(self) -> float:
        """Resolve the sample rate from settings or the override."""
        if self._sample_rate is not None:
            return self._sample_rate
        try:
            from config.settings import get_settings
            settings = get_settings()
            return settings.orders_intake_pipeline_shadow_divergence_sample_rate
        except Exception:
            return 1.0

    def _should_sample(self) -> bool:
        """Determine whether this event should be sampled for comparison."""
        rate = self._get_sample_rate()
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return random.random() < rate

    async def compare(
        self,
        new_output: Dict[str, Any],
        original_payload: Dict[str, Any],
        channel: Any,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Compare the new adapter output against the legacy adapter output.

        Args:
            new_output: The order document produced by the new adapter.
            original_payload: The original inbound payload.
            channel: The intake channel object.
            tenant_id: The tenant ID for metric labelling.

        Returns:
            A dict of divergent fields: ``{field_name: {"new": ..., "legacy": ...}}``.
            Empty dict if no divergences or if the event was not sampled.
        """
        if not self._should_sample():
            return {}

        # Produce the legacy output for comparison
        legacy_output = await self._produce_legacy_output(
            original_payload, channel, tenant_id
        )
        if legacy_output is None:
            # Cannot produce legacy output — skip comparison
            return {}

        # Perform field-by-field diff
        divergences = self._diff_outputs(new_output, legacy_output)

        # Emit metrics for each divergent field
        intake_channel = getattr(channel, "channel_type", "unknown")
        for field_name in divergences:
            orders_shadow_divergence_total.labels(
                tenant_id=tenant_id,
                intake_channel=intake_channel,
                field=field_name,
            ).inc()

        if divergences:
            logger.info(
                "Shadow divergence detected: tenant=%s, channel=%s, "
                "divergent_fields=%s",
                tenant_id,
                intake_channel,
                list(divergences.keys()),
            )

        return divergences

    async def _produce_legacy_output(
        self,
        original_payload: Dict[str, Any],
        channel: Any,
        tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Produce the legacy adapter output for comparison.

        Attempts to run the legacy adapter on the same payload. Returns
        None if the legacy adapter is not available or fails.
        """
        try:
            if self._legacy_adapter is not None:
                adapter = self._legacy_adapter
            else:
                from fuel.intake.legacy_dinee_adapter import (
                    LegacyDineeShipmentAdapter,
                )
                adapter = LegacyDineeShipmentAdapter()

            from fuel.intake.adapter_base import IntakeContext

            context = IntakeContext(
                tenant_id=tenant_id,
                channel=channel,
                trace_id="shadow-compare",
                request_id="shadow-compare",
            )
            result = adapter.transform(original_payload, context)
            return result.order_doc
        except Exception as exc:
            logger.debug(
                "Shadow divergence: legacy adapter failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return None

    @staticmethod
    def _diff_outputs(
        new_output: Dict[str, Any],
        legacy_output: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Perform a field-by-field diff between new and legacy outputs.

        Skips fields in :data:`SKIP_FIELDS` (timestamps, IDs, metadata).

        Returns:
            A dict of divergent fields with their new and legacy values.
        """
        divergences: Dict[str, Any] = {}

        # Collect all keys from both outputs
        all_keys = set(new_output.keys()) | set(legacy_output.keys())

        for key in all_keys:
            if key in SKIP_FIELDS:
                continue

            new_val = new_output.get(key)
            legacy_val = legacy_output.get(key)

            if new_val != legacy_val:
                divergences[key] = {
                    "new": new_val,
                    "legacy": legacy_val,
                }

        return divergences


__all__ = [
    "ShadowDivergenceChecker",
    "SKIP_FIELDS",
]
