"""
Activity Log Service for agent observability and audit trail.

Centralized logging for all agent actions to a dedicated Elasticsearch
index (`agent_activity_log`). Every agent action — tool invocations,
mutations, monitoring cycles, and plan executions — is recorded with
structured metadata and broadcast via WebSocket for real-time
observability.

Index mapping fields:
    log_id, agent_id, action_type, tool_name, parameters, risk_level,
    outcome, duration_ms, tenant_id, user_id, session_id, timestamp,
    details

Requirements: 1.8, 8.1, 8.2, 8.3, 8.6, 8.7
"""
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ActivityLogService:
    """Centralized logging for all agent actions.

    Stores structured log entries in the ``agent_activity_log``
    Elasticsearch index and optionally broadcasts each entry via
    WebSocket for real-time frontend consumption.

    Attributes:
        INDEX: The Elasticsearch index name for activity log entries.
    """

    INDEX = "agent_activity_log"

    def __init__(self, es_service, ws_manager=None):
        """Initialise the service with its dependencies.

        Args:
            es_service: ElasticsearchService for persistence.
            ws_manager: Optional WebSocket manager for broadcasting
                activity events in real time.
        """
        self._es = es_service
        self._ws = ws_manager

    # ------------------------------------------------------------------
    # Core logging
    # ------------------------------------------------------------------

    async def log(self, entry: dict) -> str:
        """Write an activity log entry and broadcast via WebSocket.

        Generates a UUID ``log_id`` and adds a UTC ``timestamp`` before
        persisting the entry to Elasticsearch. If a WebSocket manager is
        configured, the entry is broadcast to connected clients.

        Args:
            entry: Dict containing activity log fields. At minimum should
                include ``agent_id``, ``action_type``, and ``outcome``.

        Returns:
            The generated log_id (UUID string).
        """
        log_id = str(uuid.uuid4())
        entry["log_id"] = log_id
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()

        await self._es.index_document(self.INDEX, log_id, entry)

        if self._ws:
            try:
                await self._ws.broadcast_activity(entry)
            except Exception as e:
                logger.warning(f"Failed to broadcast activity event: {e}")

        return log_id

    # ------------------------------------------------------------------
    # Convenience loggers
    # ------------------------------------------------------------------

    async def log_mutation(self, request, risk_level, confirmation_method, result) -> str:
        """Log a mutation processed through the Confirmation Protocol.

        Creates a structured log entry from a MutationRequest with the
        risk classification, confirmation method, and execution result.

        Args:
            request: A MutationRequest instance.
            risk_level: The classified RiskLevel (enum or string).
            confirmation_method: How the mutation was handled
                (``"immediate"``, ``"approval_queue"``, ``"rejected"``).
            result: The execution result string, or None if queued.

        Returns:
            The generated log_id.
        """
        risk_value = risk_level.value if hasattr(risk_level, "value") else str(risk_level)
        outcome = "success" if result else "pending_approval"
        if confirmation_method == "rejected":
            outcome = "rejected"

        entry = {
            "agent_id": request.agent_id,
            "action_type": "mutation",
            "tool_name": request.tool_name,
            "parameters": request.parameters,
            "risk_level": risk_value,
            "outcome": outcome,
            "duration_ms": 0,
            "tenant_id": request.tenant_id,
            "user_id": getattr(request, "user_id", None),
            "session_id": getattr(request, "session_id", None),
            "details": {
                "confirmation_method": confirmation_method,
                "result": result,
            },
        }
        return await self.log(entry)

    async def log_monitoring_cycle(
        self, agent_id: str, detection_count: int, action_count: int, duration_ms: float
    ) -> str:
        """Log an autonomous agent monitoring cycle.

        Args:
            agent_id: The autonomous agent identifier.
            detection_count: Number of conditions detected this cycle.
            action_count: Number of actions taken this cycle.
            duration_ms: Cycle duration in milliseconds.

        Returns:
            The generated log_id.
        """
        entry = {
            "agent_id": agent_id,
            "action_type": "monitoring_cycle",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "success",
            "duration_ms": duration_ms,
            "tenant_id": None,
            "user_id": None,
            "session_id": None,
            "details": {
                "detection_count": detection_count,
                "action_count": action_count,
            },
        }
        return await self.log(entry)

    async def log_tool_invocation(
        self, agent_id: str, tool_name: str, params: dict, outcome: str, duration_ms: float
    ) -> str:
        """Log a tool invocation by an agent.

        Args:
            agent_id: The agent that invoked the tool.
            tool_name: Name of the tool invoked.
            params: Parameters passed to the tool.
            outcome: Result outcome (``"success"``, ``"failure"``).
            duration_ms: Invocation duration in milliseconds.

        Returns:
            The generated log_id.
        """
        entry = {
            "agent_id": agent_id,
            "action_type": "tool_invocation",
            "tool_name": tool_name,
            "parameters": params,
            "risk_level": None,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "tenant_id": None,
            "user_id": None,
            "session_id": None,
            "details": None,
        }
        return await self.log(entry)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    async def query(self, filters: dict, page: int = 1, size: int = 50) -> dict:
        """Query activity log with filters.

        Builds an Elasticsearch query from the provided filters dict and
        returns paginated results sorted by timestamp descending.

        Supported filter keys:
            - ``agent_id``: exact match
            - ``action_type``: exact match
            - ``tenant_id``: exact match
            - ``outcome``: exact match
            - ``time_range``: dict with ``gte`` and/or ``lte`` ISO strings

        Args:
            filters: Dict of filter criteria.
            page: Page number (1-based).
            size: Number of entries per page.

        Returns:
            Dict with ``items`` (list of entries), ``total``, ``page``,
            and ``size``.
        """
        must_clauses = []

        # Exact-match keyword filters
        for field in ("agent_id", "action_type", "tenant_id", "outcome"):
            value = filters.get(field)
            if value is not None:
                must_clauses.append({"term": {field: value}})

        # Time range filter
        time_range = filters.get("time_range")
        if time_range and isinstance(time_range, dict):
            range_clause = {}
            if "gte" in time_range:
                range_clause["gte"] = time_range["gte"]
            if "lte" in time_range:
                range_clause["lte"] = time_range["lte"]
            if range_clause:
                must_clauses.append({"range": {"timestamp": range_clause}})

        from_offset = (page - 1) * size
        query = {
            "query": {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}},
            "sort": [{"timestamp": {"order": "desc"}}],
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
    # Aggregated statistics
    # ------------------------------------------------------------------

    async def get_stats(self, tenant_id: str) -> dict:
        """Return aggregated statistics for a tenant.

        Uses Elasticsearch aggregations to compute:
            - ``actions_per_agent``: count of actions grouped by agent_id
            - ``success_rate``: percentage of successful outcomes
            - ``failure_rate``: percentage of failed outcomes
            - ``avg_duration_ms``: average action duration
            - ``total_actions``: total number of log entries

        Args:
            tenant_id: The tenant to compute stats for.

        Returns:
            Dict with aggregated statistics.
        """
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "size": 0,
            "aggs": {
                "actions_per_agent": {
                    "terms": {"field": "agent_id", "size": 50}
                },
                "outcomes": {
                    "terms": {"field": "outcome", "size": 10}
                },
                "avg_duration": {
                    "avg": {"field": "duration_ms"}
                },
            },
        }

        result = await self._es.search_documents(self.INDEX, query)

        # Parse total
        total_value = result.get("hits", {}).get("total", {})
        if isinstance(total_value, dict):
            total_actions = total_value.get("value", 0)
        else:
            total_actions = total_value

        # Parse aggregations
        aggs = result.get("aggregations", {})

        # Actions per agent
        agent_buckets = aggs.get("actions_per_agent", {}).get("buckets", [])
        actions_per_agent = {
            bucket["key"]: bucket["doc_count"] for bucket in agent_buckets
        }

        # Outcome rates
        outcome_buckets = aggs.get("outcomes", {}).get("buckets", [])
        outcome_counts = {
            bucket["key"]: bucket["doc_count"] for bucket in outcome_buckets
        }
        success_count = outcome_counts.get("success", 0)
        failure_count = outcome_counts.get("failure", 0)

        success_rate = (success_count / total_actions * 100) if total_actions > 0 else 0.0
        failure_rate = (failure_count / total_actions * 100) if total_actions > 0 else 0.0

        # Average duration
        avg_duration = aggs.get("avg_duration", {}).get("value", 0.0)

        return {
            "tenant_id": tenant_id,
            "total_actions": total_actions,
            "actions_per_agent": actions_per_agent,
            "success_rate": round(success_rate, 2),
            "failure_rate": round(failure_rate, 2),
            "avg_duration_ms": round(avg_duration, 2) if avg_duration else 0.0,
            "outcome_counts": outcome_counts,
        }
