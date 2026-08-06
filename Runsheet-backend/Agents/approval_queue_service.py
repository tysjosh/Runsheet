"""
Approval Queue Service for managing pending agent actions.

Manages the lifecycle of agent-proposed actions requiring human approval.
Entries are stored in the `agent_approval_queue` Elasticsearch index with
status tracking (pending, approved, rejected, expired, executed).

State machine:
    pending → approved → executed
    pending → rejected
    pending → expired

Uses ES optimistic concurrency (seq_no / primary_term) to prevent
concurrent approve/reject races. Broadcasts approval events via
WebSocket on every state change.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Valid status transitions for the approval lifecycle
VALID_TRANSITIONS = {
    "pending": {"approved", "rejected", "expired"},
    "approved": {"executed"},
}


class ApprovalQueueService:
    """Manages the lifecycle of pending agent actions requiring human approval.

    Stores approval entries in the `agent_approval_queue` ES index and
    broadcasts state-change events via WebSocket.

    Attributes:
        INDEX: The Elasticsearch index name for approval entries.
    """

    def __init__(self, es_service, ws_manager, activity_log_service, confirmation_protocol=None):
        """Initialise the service with its dependencies.

        Args:
            es_service: ElasticsearchService for persistence.
            ws_manager: WebSocket manager for broadcasting events.
            activity_log_service: Activity log for audit entries.
            confirmation_protocol: Optional back-reference used when
                executing an approved mutation.
        """
        self._es = es_service
        self._ws = ws_manager
        self._activity_log = activity_log_service
        self._confirmation_protocol = confirmation_protocol
        self.INDEX = "agent_approval_queue"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self, request, risk_level, expiry_minutes: int = 60) -> str:
        """Create a pending approval entry.

        Args:
            request: A MutationRequest describing the proposed action.
            risk_level: The classified RiskLevel for the action.
            expiry_minutes: Minutes until the approval expires (default 60).

        Returns:
            The generated action_id (UUID string).
        """
        action_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        risk_value = risk_level.value if hasattr(risk_level, "value") else str(risk_level)

        doc = {
            "action_id": action_id,
            "action_type": "mutation",
            "tool_name": request.tool_name,
            "parameters": request.parameters,
            "risk_level": risk_value,
            "proposed_by": request.agent_id,
            "proposed_at": now.isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": (now + timedelta(minutes=expiry_minutes)).isoformat(),
            "impact_summary": self._generate_impact_summary(request),
            "tenant_id": request.tenant_id,
        }

        await self._es.index_document(self.INDEX, action_id, doc)

        # Broadcast creation event via WebSocket
        await self._broadcast("approval_created", doc)

        logger.info(
            f"Created approval entry {action_id} for {request.tool_name} "
            f"(risk={risk_value}, tenant={request.tenant_id})"
        )
        return action_id

    async def approve(self, action_id: str, reviewer_id: str) -> dict:
        """Approve a pending action and optionally execute it.

        Uses ES optimistic concurrency to prevent concurrent approve/reject
        races. If a confirmation_protocol is wired, the approved mutation
        is executed and the result stored.

        Args:
            action_id: The UUID of the approval entry.
            reviewer_id: The ID of the user approving the action.

        Returns:
            The updated approval entry dict.

        Raises:
            ValueError: If the entry is not in "pending" status.
        """
        entry = await self._get_entry(action_id)

        if entry["status"] != "pending":
            raise ValueError(
                f"Cannot approve action {action_id}: "
                f"current status is '{entry['status']}', expected 'pending'"
            )

        now = datetime.now(timezone.utc)
        update_fields = {
            "status": "approved",
            "reviewed_by": reviewer_id,
            "reviewed_at": now.isoformat(),
        }

        await self._update_with_concurrency(
            action_id, update_fields, expected_status="pending"
        )
        entry.update(update_fields)

        # Broadcast approval event
        await self._broadcast("approval_approved", entry)

        # Execute the mutation if confirmation protocol is wired
        execution_result = None
        if self._confirmation_protocol:
            try:
                execution_result = await self._execute_approved_action(entry)
                exec_update = {
                    "status": "executed",
                    "execution_result": {
                        "success": True,
                        "result": str(execution_result),
                    },
                }
                # No status guard: this records the result of the approval
                # this same call just made, so "did someone else change it" is
                # not the question — and re-reading only to assert a sequence
                # number taken microseconds earlier never protected anything.
                await self._update_with_concurrency(
                    action_id, exec_update, expected_status=None
                )
                entry.update(exec_update)
            except Exception as e:
                logger.exception("Failed to execute approved action %s", action_id)
                exec_update = {
                    "status": "executed",
                    "execution_result": {
                        "success": False,
                        "error": str(e),
                    },
                }
                await self._update_with_concurrency(
                    action_id, exec_update, expected_status=None
                )
                entry.update(exec_update)

        logger.info(
            f"Approved action {action_id} by {reviewer_id}"
        )
        return entry

    async def reject(self, action_id: str, reviewer_id: str, reason: str = "") -> dict:
        """Reject a pending action and store a feedback signal.

        Args:
            action_id: The UUID of the approval entry.
            reviewer_id: The ID of the user rejecting the action.
            reason: Optional rejection reason.

        Returns:
            The updated approval entry dict.

        Raises:
            ValueError: If the entry is not in "pending" status.
        """
        entry = await self._get_entry(action_id)

        if entry["status"] != "pending":
            raise ValueError(
                f"Cannot reject action {action_id}: "
                f"current status is '{entry['status']}', expected 'pending'"
            )

        now = datetime.now(timezone.utc)
        update_fields = {
            "status": "rejected",
            "reviewed_by": reviewer_id,
            "reviewed_at": now.isoformat(),
            "rejection_reason": reason,
        }

        await self._update_with_concurrency(
            action_id, update_fields, expected_status="pending"
        )
        entry.update(update_fields)

        # Broadcast rejection event
        await self._broadcast("approval_rejected", entry)

        # Log rejection to activity log
        if self._activity_log:
            await self._activity_log.log({
                "agent_id": entry.get("proposed_by", "unknown"),
                "action_type": "approval_rejected",
                "tool_name": entry.get("tool_name"),
                "parameters": entry.get("parameters"),
                "risk_level": entry.get("risk_level"),
                "outcome": "rejected",
                "duration_ms": 0,
                "tenant_id": entry.get("tenant_id"),
                "user_id": reviewer_id,
                "details": {"reason": reason, "action_id": action_id},
            })

        logger.info(
            f"Rejected action {action_id} by {reviewer_id}: {reason}"
        )
        return entry

    async def expire_stale(self) -> int:
        """Mark expired approvals whose expiry_time has passed.

        Queries for entries with status="pending" and expiry_time < now,
        updates each to "expired", and logs the expiry to the activity log.

        Returns:
            The number of entries that were expired.
        """
        now = datetime.now(timezone.utc)
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "pending"}},
                        {"range": {"expiry_time": {"lt": now.isoformat()}}},
                    ]
                }
            },
            "size": 500,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        expired_count = 0

        for hit in hits:
            entry = hit["_source"]
            action_id = entry["action_id"]
            try:
                update_fields = {
                    "status": "expired",
                    "reviewed_at": now.isoformat(),
                }
                await self._es.update_document(self.INDEX, action_id, update_fields)

                entry.update(update_fields)
                await self._broadcast("approval_expired", entry)

                # Log expiry to activity log
                if self._activity_log:
                    await self._activity_log.log({
                        "agent_id": entry.get("proposed_by", "unknown"),
                        "action_type": "approval_expired",
                        "tool_name": entry.get("tool_name"),
                        "parameters": entry.get("parameters"),
                        "risk_level": entry.get("risk_level"),
                        "outcome": "expired",
                        "duration_ms": 0,
                        "tenant_id": entry.get("tenant_id"),
                        "details": {"action_id": action_id},
                    })

                expired_count += 1
            except Exception:
                logger.exception("Failed to expire action %s", action_id)

        if expired_count > 0:
            logger.info(f"Expired {expired_count} stale approval(s)")

        return expired_count

    async def list_pending(self, tenant_id: str, page: int = 1, size: int = 20) -> dict:
        """List pending approvals for a tenant, sorted by proposed_at descending.

        Args:
            tenant_id: The tenant to filter by.
            page: Page number (1-based).
            size: Number of entries per page.

        Returns:
            Dict with "items" (list of entries), "total" count, "page", and "size".
        """
        from_offset = (page - 1) * size
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "pending"}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"proposed_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        total_value = result.get("hits", {}).get("total", {})
        if isinstance(total_value, dict):
            total = total_value.get("value", 0)
        else:
            total = total_value

        items = [hit["_source"] for hit in hits]

        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _generate_impact_summary(self, request) -> str:
        """Generate a human-readable summary of the proposed action.

        Args:
            request: The MutationRequest to summarise.

        Returns:
            A short description of the proposed action.
        """
        tool = request.tool_name
        params = request.parameters

        summaries = {
            "cancel_job": lambda p: (
                f"Cancel job {p.get('job_id', 'unknown')}"
                f"{': ' + p['reason'] if p.get('reason') else ''}"
            ),
            "reassign_rider": lambda p: (
                f"Reassign shipment {p.get('shipment_id', 'unknown')} "
                f"to rider {p.get('new_rider_id', 'unknown')}"
            ),
            "assign_asset_to_job": lambda p: (
                f"Assign asset {p.get('asset_id', 'unknown')} "
                f"to job {p.get('job_id', 'unknown')}"
            ),
            "update_job_status": lambda p: (
                f"Update job {p.get('job_id', 'unknown')} "
                f"status to {p.get('new_status', 'unknown')}"
            ),
            "create_job": lambda p: (
                f"Create {p.get('job_type', '')} job "
                f"from {p.get('origin', 'unknown')} to {p.get('destination', 'unknown')}"
            ),
            "escalate_shipment": lambda p: (
                f"Escalate shipment {p.get('shipment_id', 'unknown')} "
                f"to priority {p.get('priority', 'unknown')}"
            ),
            "request_fuel_refill": lambda p: (
                f"Request {p.get('quantity_liters', '?')}L fuel refill "
                f"for station {p.get('station_id', 'unknown')}"
            ),
            "update_fuel_threshold": lambda p: (
                f"Update fuel threshold for station {p.get('station_id', 'unknown')} "
                f"to {p.get('threshold_pct', '?')}%"
            ),
        }

        formatter = summaries.get(tool)
        if formatter:
            try:
                return formatter(params)
            except Exception:
                pass

        # Fallback for unknown tools
        return f"Execute {tool} with parameters: {params}"

    async def _get_entry(self, action_id: str) -> dict:
        """Fetch an approval entry.

        The ES ``_seq_no`` / ``_primary_term`` this used to return alongside the
        document are gone. They existed so the follow-up write could assert
        "nothing changed since I read", and the raw ``client.get`` needed to
        obtain them bypassed the document-store backend switch — after the cutover
        this read would have gone to Elasticsearch while the write went to
        Postgres, which is the worst possible split for a concurrency guard.

        The guard itself is preserved and is now stated in the terms it actually
        protects: see :meth:`_update_with_concurrency`.

        Raises:
            ValueError: If the entry is not found.
        """
        entry = await self._es.get_document(self.INDEX, action_id)
        if entry is None:
            raise ValueError(f"Approval entry {action_id} not found")
        return entry

    async def _update_with_concurrency(
        self, action_id: str, fields: dict, expected_status: Optional[str]
    ) -> None:
        """Update an approval entry, refusing if its status moved under us.

        What the ``if_seq_no`` assertion actually protected here was one thing:
        that no other reviewer had changed the entry between this caller's read
        and its write. Every caller reads, checks ``status``, then writes — so
        "the status is still what I read" IS the invariant, and asserting it
        directly is both backend-independent and easier to reason about than a
        sequence number.

        It is also *narrower in the right direction*: a sequence number changes on
        any write, including one that touched an unrelated field, so the old guard
        could reject a legitimate approval because something else had been
        appended to the entry.

        ``expected_status=None`` skips the check, for the follow-up writes that
        record an execution result immediately after the approval they belong to.

        Raises:
            RuntimeError: If another actor changed the status first.
        """
        def _apply(current: dict) -> Optional[dict]:
            if expected_status is not None and current.get("status") != expected_status:
                return None
            merged = dict(current)
            merged.update(fields)
            return merged

        _document, applied = await self._es.atomic_update(
            self.INDEX, action_id, _apply
        )
        if not applied:
            raise RuntimeError(
                f"Concurrent modification detected for action {action_id}. "
                f"Another user may have already approved or rejected this action."
            )

    async def _broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast an approval event via WebSocket.

        Silently catches errors to avoid breaking the approval flow.

        Args:
            event_type: The event type string (e.g. "approval_created").
            data: The event payload.
        """
        if self._ws:
            try:
                await self._ws.broadcast_approval_event(event_type, data)
            except Exception as e:
                logger.warning(f"Failed to broadcast {event_type}: {e}")

    async def _execute_approved_action(self, entry: dict) -> str:
        """Execute an approved mutation via the confirmation protocol.

        Args:
            entry: The approval entry dict.

        Returns:
            The execution result string.
        """
        from Agents.confirmation_protocol import MutationRequest

        request = MutationRequest(
            tool_name=entry["tool_name"],
            parameters=entry["parameters"],
            tenant_id=entry["tenant_id"],
            agent_id=entry["proposed_by"],
        )
        result = await self._confirmation_protocol._execute_mutation(request)
        return result
