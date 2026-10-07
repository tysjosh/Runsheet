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
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.llm_errors import ChatEvent, text_event, tool_event, tool_result_event
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

    def _new_agent(self, history: Optional[List[Dict[str, Any]]] = None) -> Agent:
        """Build a Strands ``Agent`` for one request.

        Never cache the result: its ``messages`` list must not outlive the
        call that created it (F1).

        ``history`` is the caller's own text-only transcript
        (``[{"role": "user"|"assistant", "content": str}, ...]``, OI-17). It
        is copied into a new ``messages`` list here, so two calls never share
        one, even when given the same transcript.

        ``callback_handler=None`` installs Strands' null handler. The default
        ``PrintingCallbackHandler`` writes every streamed answer and tool call
        to stdout, which on staging put tenant data in CloudWatch (F13).
        """
        messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in history or ()
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m["content"]
        ]
        return Agent(
            model=self._model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=list(self.TOOLS),
            messages=messages,
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
            context: Optional context dict (tenant_id, session_id, and the
                caller's ``history`` transcript on the orchestrator's simple
                path, OI-17).

        Returns:
            The agent's response as a string.
        """
        prompt, tenant_id = self._prompt(task, context)
        agent = self._new_agent((context or {}).get("history"))
        with set_current_tenant(tenant_id):
            result = await agent.invoke_async(prompt)
        return str(result)

    async def stream(
        self, task: str, context: dict = None
    ) -> AsyncIterator[ChatEvent]:
        """Process one subtask and yield normalized chat events as they occur.

        Same prompt, tenant binding and fresh-``Agent``-per-call invariant as
        :meth:`handle` (F1). Strands' raw stream is normalized here (F6):

        * ``{"data": str}`` text deltas -> ``text`` events;
        * the assistant ``message`` -> one ``tool`` event per new ``toolUse``
          block;
        * the user ``message`` carrying ``toolResult`` blocks -> one
          ``tool_result`` event each, with the tool's name and
          ``success``/``error`` status only. The tool output stays with the
          model: it can hold ``str(exc)`` from a failing tool (F3).

        Everything else is ignored: per-delta ``current_tool_use`` events, and
        any event with a ``"type"`` key (Strands 1.24 typed events). Exceptions
        propagate; the orchestrator classifies and retries them.
        """
        prompt, tenant_id = self._prompt(task, context)
        agent = self._new_agent((context or {}).get("history"))
        tool_names: Dict[str, str] = {}
        with set_current_tenant(tenant_id):
            async for event in agent.stream_async(prompt):
                if not isinstance(event, dict) or "type" in event:
                    continue
                text = event.get("data")
                if isinstance(text, str):
                    if text:
                        yield text_event(text)
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if message.get("role") == "assistant" and "toolUse" in block:
                        use = block["toolUse"] or {}
                        use_id = use.get("toolUseId")
                        if use_id in tool_names:
                            continue
                        tool_names[use_id] = use.get("name", "")
                        yield tool_event(use.get("name", ""), use.get("input"))
                    elif message.get("role") == "user" and "toolResult" in block:
                        result = block["toolResult"] or {}
                        yield tool_result_event(
                            tool_names.get(result.get("toolUseId"), ""),
                            result.get("status", "success"),
                        )

    @staticmethod
    def _prompt(task: str, context: Optional[dict]) -> Tuple[str, str]:
        """The prompt sent to the model and the tenant it is scoped to.

        The tenant id is NOT put in the prompt. Tools take it from the bound
        ContextVar (``resolve_tool_tenant``), so the model has no use for it,
        and putting it there made answers quote the internal tenant id to the
        user ("for 'demo-tenant'").
        """
        tenant_id = require_tenant_id((context or {}).get("tenant_id"))
        return task, tenant_id
