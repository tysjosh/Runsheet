"""Dispatch Board telemetry and audit hooks (design K16, K17; R22, R24).

Metrics go through ``telemetry/service.py`` ``record_metric`` with an
allow-listed tag set, so no customer name, address or free-text reason can
reach a metric (R24.3): tag values must look like ids or enum codes, and any
other value is replaced with ``"other"``. Publish audit entries go to
``ActivityLogService.log`` and ``TelemetryService.log_audit_event`` with ids
and the actor's display name only.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: K16 metric names.
SNAPSHOT_MS = "board.snapshot.ms"
VALIDATE_MS = "board.validate.ms"
COMMAND_MS = "board.command.ms"
COMMAND_COUNT = "board.command.count"
CONFLICT_COUNT = "board.conflict.count"
OVERRIDE_COUNT = "board.override.count"
PUBLISH_LANE_MS = "board.publish.lane.ms"
PUBLISH_LANE_COUNT = "board.publish.lane.count"
REDISPATCH_COUNT = "board.redispatch.count"
SUGGESTION_COUNT = "board.suggestion.count"
UNDO_COUNT = "board.undo.count"
#: Rollback met an order state the guards exclude (K8.4 Recovery).
RECOVERY_INCONSISTENT = "dispatch_board.recovery_inconsistent"

METRIC_NAMES = frozenset({
    SNAPSHOT_MS, VALIDATE_MS, COMMAND_MS, COMMAND_COUNT, CONFLICT_COUNT, OVERRIDE_COUNT,
    PUBLISH_LANE_MS, PUBLISH_LANE_COUNT, REDISPATCH_COUNT, SUGGESTION_COUNT, UNDO_COUNT,
    RECOVERY_INCONSISTENT,
})

#: The only tag keys a board metric may carry.
ALLOWED_TAGS = frozenset({
    "tenant_id", "endpoint", "outcome", "type", "result", "input_modality",
    "kind", "reason_code", "stage", "action",
})
_TAG_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _default_telemetry() -> Any:
    try:
        from telemetry.service import get_telemetry_service

        return get_telemetry_service()
    except Exception:
        return None


def safe_tags(tags: Dict[str, Any]) -> Dict[str, str]:
    """Allow-listed keys; values reduced to id/enum form (else ``"other"``)."""
    out: Dict[str, str] = {}
    for key, value in tags.items():
        if key not in ALLOWED_TAGS or value is None:
            continue
        text = str(value)
        out[key] = text if _TAG_VALUE.match(text) else "other"
    return out


class BoardTelemetry:
    """Records board metrics and publish audit entries; never raises."""

    def __init__(
        self,
        *,
        telemetry: Any = None,
        activity_log: Any = None,
        telemetry_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._telemetry = telemetry
        self._getter = telemetry_getter or _default_telemetry
        self._activity_log = activity_log

    def _service(self) -> Any:
        return self._telemetry if self._telemetry is not None else self._getter()

    def metric(self, name: str, value: float = 1.0, **tags: Any) -> None:
        if name not in METRIC_NAMES:
            raise ValueError(f"unknown board metric {name!r}")
        service = self._service()
        if service is None:
            return
        try:
            service.record_metric(name, float(value), safe_tags(tags))
        except Exception as exc:
            logger.debug("board metric %s not recorded: %s", name, type(exc).__name__)

    async def audit_publish(
        self,
        *,
        tenant_id: str,
        user_id: str,
        actor_name: str,
        service_date: str,
        lanes: Iterable[Dict[str, Any]],
    ) -> None:
        """R22.2: one activity-log entry per publish, one audit event per lane.

        ``lanes`` entries carry ``truck_id``, ``plans`` (ids), ``orders`` (ids),
        ``driver_id`` and ``result``; nothing else is copied.
        """
        lane_entries: List[Dict[str, Any]] = [
            {
                "truck_id": lane.get("truck_id"),
                "plans": list(lane.get("plans") or []),
                "orders": list(lane.get("orders") or []),
                "driver_id": lane.get("driver_id"),
                "result": lane.get("result"),
            }
            for lane in lanes
        ]
        if self._activity_log is not None:
            try:
                await self._activity_log.log(
                    {
                        "agent_id": "dispatch_board",
                        "action_type": "dispatch_board_publish",
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "actor_name": actor_name,
                        "service_date": service_date,
                        "lanes": lane_entries,
                        "outcome": "success" if all(l["result"] == "published" for l in lane_entries) else "partial",
                    }
                )
            except Exception as exc:
                logger.warning("board publish activity log failed: %s", type(exc).__name__)
        service = self._service()
        if service is None:
            return
        for lane in lane_entries:
            try:
                service.log_audit_event(
                    "dispatch_board",
                    user_id,
                    "board_lane",
                    lane["truck_id"],
                    "publish",
                    {"tenant_id": tenant_id, "service_date": service_date, "actor_name": actor_name, **lane},
                )
            except Exception as exc:
                logger.warning("board publish audit event failed: %s", type(exc).__name__)
