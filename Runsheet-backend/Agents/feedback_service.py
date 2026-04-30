"""
Feedback Service for capturing and querying human feedback signals.

Stores rejection, override, and correction feedback in the
``agent_feedback`` Elasticsearch index, enabling agents to learn from
human corrections and adjust future autonomous decisions.

Index mapping fields:
    feedback_id, agent_id, action_type, original_proposal, user_action,
    feedback_type, tenant_id, user_id, timestamp, context

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7
"""
import math
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class FeedbackService:
    """Captures and queries human feedback signals for agent learning.

    Stores structured feedback entries in the ``agent_feedback``
    Elasticsearch index. Feedback signals include rejections (from the
    approval queue), overrides (manual user actions contradicting agent
    suggestions), and corrections. The service also computes a
    confidence score based on historical rejection counts using
    exponential decay.

    Attributes:
        INDEX: The Elasticsearch index name for feedback entries.
        DECAY_RATE: Exponential decay rate for confidence computation.
    """

    INDEX = "agent_feedback"
    DECAY_RATE = 0.3

    def __init__(self, es_service):
        """Initialise the service with its dependencies.

        Args:
            es_service: ElasticsearchService for persistence.
        """
        self._es = es_service

    # ------------------------------------------------------------------
    # Record feedback
    # ------------------------------------------------------------------

    async def record_rejection(
        self,
        agent_id: str,
        action_type: str,
        original_proposal: dict,
        rejection_reason: str,
        user_action: dict,
        tenant_id: str,
        user_id: str,
    ) -> str:
        """Record a rejection feedback signal.

        Called when a user rejects an agent-proposed action in the
        Approval Queue. Stores the original proposal, rejection reason,
        and the action the user took instead (if any).

        Args:
            agent_id: The agent whose proposal was rejected.
            action_type: Type of action that was rejected.
            original_proposal: Dict describing the original agent proposal.
            rejection_reason: Reason the user gave for rejecting.
            user_action: Dict describing what the user did instead, or
                empty dict if nothing.
            tenant_id: Tenant scope.
            user_id: The user who rejected the proposal.

        Returns:
            The generated feedback_id (UUID string).
        """
        return await self._store(
            agent_id=agent_id,
            action_type=action_type,
            original_proposal=original_proposal,
            user_action=user_action,
            feedback_type="rejection",
            tenant_id=tenant_id,
            user_id=user_id,
            context={"rejection_reason": rejection_reason},
        )

    async def record_override(
        self,
        agent_id: str,
        action_type: str,
        original_suggestion: dict,
        user_action: dict,
        tenant_id: str,
        user_id: str,
    ) -> str:
        """Record a manual override feedback signal.

        Called when a user manually performs an action that contradicts
        a recent agent suggestion.

        Args:
            agent_id: The agent whose suggestion was overridden.
            action_type: Type of action that was overridden.
            original_suggestion: Dict describing the original suggestion.
            user_action: Dict describing the action the user took.
            tenant_id: Tenant scope.
            user_id: The user who performed the override.

        Returns:
            The generated feedback_id (UUID string).
        """
        return await self._store(
            agent_id=agent_id,
            action_type=action_type,
            original_proposal=original_suggestion,
            user_action=user_action,
            feedback_type="override",
            tenant_id=tenant_id,
            user_id=user_id,
            context={},
        )

    async def _store(
        self,
        agent_id: str,
        action_type: str,
        original_proposal: dict,
        user_action: dict,
        feedback_type: str,
        tenant_id: str,
        user_id: str,
        context: dict,
    ) -> str:
        """Internal helper to persist a feedback entry.

        Args:
            agent_id: The agent involved.
            action_type: Type of action.
            original_proposal: The original agent proposal.
            user_action: The user's alternative action.
            feedback_type: ``"rejection"``, ``"override"``, or
                ``"correction"``.
            tenant_id: Tenant scope.
            user_id: The user who provided feedback.
            context: Additional context dict.

        Returns:
            The generated feedback_id.
        """
        feedback_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        doc = {
            "feedback_id": feedback_id,
            "agent_id": agent_id,
            "action_type": action_type,
            "original_proposal": original_proposal,
            "user_action": user_action,
            "feedback_type": feedback_type,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "timestamp": now,
            "context": context,
        }

        await self._es.index_document(self.INDEX, feedback_id, doc)
        return feedback_id

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    async def query_similar(
        self,
        action_type: str,
        context: dict,
        tenant_id: str,
        limit: int = 10,
    ) -> list:
        """Query recent feedback for similar situations.

        Retrieves feedback signals matching the given action type and
        tenant, ordered by most recent first. This allows agents to
        check whether similar proposals were previously rejected or
        overridden.

        Args:
            action_type: The action type to search for.
            context: Context dict (currently used for future semantic
                matching; the query filters by action_type and tenant).
            tenant_id: Tenant scope.
            limit: Maximum number of feedback entries to return.

        Returns:
            List of feedback entry dicts, ordered by timestamp desc.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"action_type": action_type}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": limit,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_stats(self, tenant_id: str) -> dict:
        """Return aggregated feedback statistics for a tenant.

        Uses Elasticsearch aggregations to compute:
            - ``total_feedback``: total number of feedback entries
            - ``rejection_count``: number of rejection signals
            - ``override_count``: number of override signals
            - ``rejection_rate``: percentage of rejections
            - ``rejections_per_agent``: rejection count grouped by agent
            - ``common_action_types``: most common action types in
              feedback

        Args:
            tenant_id: The tenant to compute stats for.

        Returns:
            Dict with aggregated feedback statistics.
        """
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "size": 0,
            "aggs": {
                "feedback_types": {
                    "terms": {"field": "feedback_type", "size": 10}
                },
                "rejections_per_agent": {
                    "filter": {"term": {"feedback_type": "rejection"}},
                    "aggs": {
                        "agents": {
                            "terms": {"field": "agent_id", "size": 50}
                        }
                    },
                },
                "common_action_types": {
                    "terms": {"field": "action_type", "size": 20}
                },
            },
        }

        result = await self._es.search_documents(self.INDEX, query)

        # Parse total
        total_value = result.get("hits", {}).get("total", {})
        if isinstance(total_value, dict):
            total_feedback = total_value.get("value", 0)
        else:
            total_feedback = total_value

        # Parse aggregations
        aggs = result.get("aggregations", {})

        # Feedback type counts
        type_buckets = aggs.get("feedback_types", {}).get("buckets", [])
        type_counts = {
            bucket["key"]: bucket["doc_count"] for bucket in type_buckets
        }
        rejection_count = type_counts.get("rejection", 0)
        override_count = type_counts.get("override", 0)

        rejection_rate = (
            (rejection_count / total_feedback * 100)
            if total_feedback > 0
            else 0.0
        )

        # Rejections per agent
        agent_buckets = (
            aggs.get("rejections_per_agent", {})
            .get("agents", {})
            .get("buckets", [])
        )
        rejections_per_agent = {
            bucket["key"]: bucket["doc_count"] for bucket in agent_buckets
        }

        # Common action types
        action_buckets = aggs.get("common_action_types", {}).get("buckets", [])
        common_action_types = {
            bucket["key"]: bucket["doc_count"] for bucket in action_buckets
        }

        return {
            "tenant_id": tenant_id,
            "total_feedback": total_feedback,
            "rejection_count": rejection_count,
            "override_count": override_count,
            "rejection_rate": round(rejection_rate, 2),
            "rejections_per_agent": rejections_per_agent,
            "common_action_types": common_action_types,
        }

    # ------------------------------------------------------------------
    # Confidence computation
    # ------------------------------------------------------------------

    def compute_confidence(
        self,
        base: float,
        rejection_count: int,
    ) -> float:
        """Compute confidence score based on historical feedback.

        Uses exponential decay: ``base * e^(-0.3 * rejection_count)``.
        Actions similar to previously rejected proposals receive a lower
        confidence score and are more likely to require approval
        regardless of autonomy level.

        Args:
            base: Base confidence score (0.0–1.0).
            rejection_count: Number of similar rejections found.

        Returns:
            Adjusted confidence score (0.0–1.0).
        """
        if rejection_count <= 0:
            return base
        return base * math.exp(-self.DECAY_RATE * rejection_count)

    # ------------------------------------------------------------------
    # List (for REST endpoint)
    # ------------------------------------------------------------------

    async def list_feedback(
        self,
        tenant_id: str,
        agent_id: str = None,
        action_type: str = None,
        time_range: dict = None,
        page: int = 1,
        size: int = 20,
    ) -> dict:
        """List feedback signals for a tenant with optional filtering.

        Args:
            tenant_id: Tenant scope.
            agent_id: Optional filter by agent_id.
            action_type: Optional filter by action_type.
            time_range: Optional dict with ``gte`` and/or ``lte`` ISO
                strings for timestamp filtering.
            page: Page number (1-based).
            size: Number of entries per page.

        Returns:
            Dict with ``items``, ``total``, ``page``, and ``size``.
        """
        must_clauses = [{"term": {"tenant_id": tenant_id}}]

        if agent_id:
            must_clauses.append({"term": {"agent_id": agent_id}})

        if action_type:
            must_clauses.append({"term": {"action_type": action_type}})

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
            "query": {"bool": {"must": must_clauses}},
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
