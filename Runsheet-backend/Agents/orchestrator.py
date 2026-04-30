"""
Agent Orchestrator for multi-agent request routing and synthesis.

Top-level agent that receives user requests, classifies intent via
keyword matching against a routing table, delegates to specialist
agents, and synthesizes results into a unified response. Complex
multi-domain requests are routed through the ExecutionPlanner for
structured plan-based execution.

Key behaviours:
  - ``_classify_intent`` matches message keywords against the routing
    table to identify target specialist domains.
  - ``_is_complex_request`` detects multi-step or cross-domain requests
    that benefit from structured planning.
  - ``route`` orchestrates the full flow: classify → delegate → synthesize.
  - No-match requests fall back to the reporting agent.
  - Complex requests are delegated to the ExecutionPlanner.

Requirements: 7.6, 7.7, 7.8
"""

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Routes requests to specialist agents and synthesizes results.

    Maintains a keyword-based routing table that maps domain names to
    trigger keywords. Incoming messages are classified against this
    table to determine which specialist(s) should handle the request.

    Attributes:
        ROUTING_TABLE: Mapping of domain names to keyword lists used
            for intent classification.
    """

    ROUTING_TABLE: Dict[str, List[str]] = {
        "fleet": [
            "truck", "vehicle", "vessel", "equipment", "container",
            "asset", "location", "fleet",
        ],
        "scheduling": [
            "job", "schedule", "dispatch", "assign", "cancel",
            "delay", "cargo",
        ],
        "fuel": [
            "fuel", "refill", "station", "diesel", "petrol",
            "consumption",
        ],
        "ops": [
            "shipment", "rider", "sla", "delivery", "ops", "breach",
        ],
        "reporting": [
            "report", "analysis", "summary", "overview", "performance",
            "productivity",
        ],
    }

    # Indicators that a request involves multiple steps or domains
    _COMPLEX_INDICATORS = [
        " and ", " then ", " also ", " after that ",
        " followed by ", " next ", " additionally ",
        " as well as ", " plus ",
    ]

    def __init__(
        self,
        specialists: Dict[str, object],
        execution_planner: object,
        activity_log_service: object,
    ):
        """Initialise the orchestrator with its dependencies.

        Args:
            specialists: Dict mapping domain names to specialist agent
                instances (e.g. ``{"fleet": FleetAgent, ...}``). Each
                specialist must implement an ``async handle(task, context)``
                method.
            execution_planner: An ``ExecutionPlanner`` instance for
                handling complex multi-step requests.
            activity_log_service: An ``ActivityLogService`` instance for
                logging orchestration decisions and outcomes.
        """
        self._specialists = specialists
        self._planner = execution_planner
        self._activity_log = activity_log_service

    async def route(
        self,
        user_message: str,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Classify intent and delegate to appropriate specialist(s).

        Flow:
          1. Classify the message to identify target domains.
          2. Fall back to ``"reporting"`` if no domains matched.
          3. If the request is complex (multi-step), delegate to the
             ExecutionPlanner for structured plan execution.
          4. Otherwise, invoke each matched specialist sequentially and
             synthesize their results.

        Args:
            user_message: The user's natural language request.
            tenant_id: Tenant scope for the request.
            session_id: Optional session identifier for context.

        Returns:
            A synthesized response string combining specialist outputs.
        """
        start_time = time.monotonic()

        targets = self._classify_intent(user_message)

        # Fallback to reporting when no domain matches
        if len(targets) == 0:
            targets = ["reporting"]

        # Log the routing decision
        await self._activity_log.log({
            "agent_id": "orchestrator",
            "action_type": "routing",
            "tool_name": None,
            "parameters": {"message": user_message},
            "risk_level": None,
            "outcome": "success",
            "duration_ms": 0,
            "tenant_id": tenant_id,
            "user_id": None,
            "session_id": session_id,
            "details": {
                "event": "intent_classified",
                "targets": targets,
                "is_complex": self._is_complex_request(user_message),
            },
        })

        # Complex requests go through the execution planner
        if self._is_complex_request(user_message):
            result = await self._execute_complex_request(
                user_message, targets, tenant_id, session_id,
            )
        else:
            result = await self._execute_simple_request(
                user_message, targets, tenant_id, session_id,
            )

        duration_ms = (time.monotonic() - start_time) * 1000

        # Log the completed routing
        await self._activity_log.log({
            "agent_id": "orchestrator",
            "action_type": "routing",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "success",
            "duration_ms": duration_ms,
            "tenant_id": tenant_id,
            "user_id": None,
            "session_id": session_id,
            "details": {
                "event": "routing_completed",
                "targets": targets,
                "response_length": len(result),
            },
        })

        return result

    # ------------------------------------------------------------------
    # Intent classification
    # ------------------------------------------------------------------

    def _classify_intent(self, message: str) -> List[str]:
        """Keyword-based intent classification against routing table.

        Scans the lowercased message for keywords defined in each
        domain's entry in ``ROUTING_TABLE``. Returns a list of all
        domains that had at least one keyword match.

        Args:
            message: The user's natural language message.

        Returns:
            List of matched domain names (may be empty).
        """
        message_lower = message.lower()
        matched: List[str] = []
        for domain, keywords in self.ROUTING_TABLE.items():
            if any(kw in message_lower for kw in keywords):
                matched.append(domain)
        return matched

    def _is_complex_request(self, message: str) -> bool:
        """Detect whether a request involves multiple steps or domains.

        A request is considered complex if it contains conjunction or
        sequencing indicators (e.g. "and", "then", "also") **and**
        matches more than one specialist domain.

        Args:
            message: The user's natural language message.

        Returns:
            True if the request is complex, False otherwise.
        """
        message_lower = message.lower()
        has_conjunction = any(
            indicator in message_lower
            for indicator in self._COMPLEX_INDICATORS
        )
        targets = self._classify_intent(message)
        return has_conjunction and len(targets) > 1

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _execute_simple_request(
        self,
        user_message: str,
        targets: List[str],
        tenant_id: str,
        session_id: Optional[str],
    ) -> str:
        """Execute a simple (non-complex) request by delegating to specialists.

        Invokes each matched specialist sequentially and synthesizes
        their results.

        Args:
            user_message: The user's request.
            targets: List of specialist domain names to invoke.
            tenant_id: Tenant scope.
            session_id: Optional session identifier.

        Returns:
            Synthesized response string.
        """
        context = {"tenant_id": tenant_id}
        if session_id:
            context["session_id"] = session_id

        results: List[str] = []
        for target in targets:
            agent = self._specialists.get(target)
            if agent:
                try:
                    result = await agent.handle(user_message, context)
                    results.append(result)
                except Exception as e:
                    logger.error(
                        f"Specialist '{target}' failed for message: {e}"
                    )
                    results.append(
                        f"[{target}] Error processing request: {e}"
                    )

        return self._synthesize(results)

    async def _execute_complex_request(
        self,
        user_message: str,
        targets: List[str],
        tenant_id: str,
        session_id: Optional[str],
    ) -> str:
        """Execute a complex request through the ExecutionPlanner.

        Creates a plan from the request and target domains, then
        executes it. The plan results are formatted into a summary.

        Args:
            user_message: The user's request.
            targets: List of specialist domain names.
            tenant_id: Tenant scope.
            session_id: Optional session identifier.

        Returns:
            Formatted plan execution summary.
        """
        try:
            plan = await self._planner.create_plan(user_message, targets)
            executed_plan = await self._planner.execute_plan(plan, tenant_id)
            return self._format_plan_result(executed_plan)
        except Exception as e:
            logger.error(f"Complex request execution failed: {e}")
            # Fall back to simple sequential execution
            return await self._execute_simple_request(
                user_message, targets, tenant_id, session_id,
            )

    # ------------------------------------------------------------------
    # Result synthesis
    # ------------------------------------------------------------------

    def _synthesize(self, results: List[str]) -> str:
        """Combine multiple specialist results into a single response.

        If only one result is present, returns it directly. Multiple
        results are joined with double newlines.

        Args:
            results: List of specialist response strings.

        Returns:
            Combined response string, or a fallback message if empty.
        """
        if not results:
            return "I wasn't able to find relevant information for your request."

        if len(results) == 1:
            return results[0]

        return "\n\n".join(results)

    def _format_plan_result(self, plan) -> str:
        """Format an executed plan into a human-readable summary.

        Args:
            plan: An ``ExecutionPlan`` instance with executed steps.

        Returns:
            Formatted summary string.
        """
        parts = [f"**Plan: {plan.goal}** (Status: {plan.status})"]

        for step in plan.steps:
            status_icon = {
                "completed": "✅",
                "failed": "❌",
                "skipped": "⏭️",
                "pending": "⏳",
                "running": "🔄",
                "rolled_back": "↩️",
            }.get(step.status.value if hasattr(step.status, "value") else step.status, "❓")

            parts.append(
                f"{status_icon} Step {step.step_id}: {step.description}"
            )
            if step.result:
                parts.append(f"   Result: {step.result}")

        return "\n".join(parts)
