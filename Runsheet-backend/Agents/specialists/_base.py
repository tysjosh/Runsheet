"""
Shared base for the specialist agents.

Each specialist used to build one Strands ``Agent`` in ``__init__`` and reuse
it for every ``handle()`` call. Bootstrap constructs the specialists once per
process, and Strands appends every user, assistant and tool message to
``agent.messages``, so that single list carried conversation history across
tenants, users and sessions, and concurrent requests mutated it together
(staging finding F1: tenant B's specialist answered with tenant A's data).

``SpecialistAgent.handle`` builds a fresh ``Agent`` per call instead. Only the
model (stateless config), system prompt and tool list are shared, so no two
requests can ever share a ``messages`` list.

Validates:
- Requirement 7.9: Each specialist builds its own Strands Agent, with its
  domain-specific system prompt and tool set, for every request
- Requirements 9.2, 9.4: Tenant scoping on every ES read via ContextVar
"""

import logging

from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools._tenant_context import require_tenant_id, set_current_tenant

logger = logging.getLogger(__name__)


class SpecialistAgent:
    """Base class for domain specialists.

    Subclasses declare ``TOOLS`` and ``SYSTEM_PROMPT`` only. The
    one-Agent-per-call invariant lives here so a new specialist cannot
    reintroduce a long-lived, shared ``Agent`` (F1).
    """

    TOOLS: list = []

    SYSTEM_PROMPT: str = ""

    def __init__(self, model: LiteLLMModel):
        """Store the shared model. No Strands ``Agent`` is built here.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self._model = model
        logger.info(
            "✅ %s initialized with %d tools", type(self).__name__, len(self.TOOLS)
        )

    def _new_agent(self) -> Agent:
        """Build a Strands ``Agent`` with empty history for one request.

        Never cache the result: its ``messages`` list must not outlive the
        call that created it (F1).

        ``callback_handler=None`` installs Strands' null handler. The default
        ``PrintingCallbackHandler`` writes every streamed answer and tool call
        to stdout, which on staging put tenant data in CloudWatch (F13).
        """
        return Agent(
            model=self._model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=list(self.TOOLS),
            callback_handler=None,
        )

    async def handle(self, task: str, context: dict = None) -> str:
        """Process one subtask on a fresh Strands ``Agent``.

        Binds the tenant id from ``context`` to the tool ContextVar before
        dispatching the Strands agent so every ES-reading tool runs
        tenant-scoped. The agent is local to this call, so its history
        cannot reach another request, tenant or session (F1).

        Args:
            task: The natural language task to process.
            context: Optional context dict (e.g. tenant_id, session_id).

        Returns:
            The agent's response as a string.
        """
        prompt = task
        tenant_id = require_tenant_id((context or {}).get("tenant_id"))
        if context:
            ctx_parts = []
            if tenant_id:
                ctx_parts.append(f"Tenant: {tenant_id}")
            if ctx_parts:
                prompt = f"[Context: {', '.join(ctx_parts)}]\n{task}"

        agent = self._new_agent()
        with set_current_tenant(tenant_id):
            result = await agent.invoke_async(prompt)
        return str(result)
