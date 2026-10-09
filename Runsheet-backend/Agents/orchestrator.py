"""
Agent Orchestrator for multi-agent request routing and synthesis.

Top-level agent that receives user requests, classifies intent via
keyword matching against a routing table, delegates to specialist
agents, and synthesizes results into a unified response. Complex
multi-domain requests are routed through the ExecutionPlanner for
structured plan-based execution.

Key behaviours:
  - ``_classify_intent`` matches message keywords (word-start, so "stops"
    is not "ops") against the routing table, then narrows each clause to
    the one domain whose *entity* keyword matched when the other domains
    matched only via qualifiers ("scheduled fuel delivery jobs" is a
    scheduling question, not three) (N3).
  - ``_is_complex_request`` detects multi-step or cross-domain requests
    that benefit from structured planning.
  - With several simple targets, answers are buffered; a non-answer
    ("There are no ...") is dropped when another target answered, and the
    kept answers are labelled by specialist (N3).
  - ``route_stream`` orchestrates the full flow (classify → delegate →
    stream normalized ``ChatEvent``s); ``route`` collects it into a string.
  - LLM failures are retried per ``Agents.llm_errors`` and surface as
    typed error events with a safe message, never as answer text (F3): one
    terminal event when nothing could answer, one ``partial`` event per
    failed specialist when the rest of the answer stands (N4).
  - No-match requests fall back to the reporting agent.
  - Complex requests are delegated to the ExecutionPlanner.

Requirements: 7.6, 7.7, 7.8
"""

import asyncio
import functools
import inspect
import logging
import random
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Pattern, Tuple

from Agents.llm_errors import (
    AI_RATE_LIMITED,
    AI_SERVICE_UNAVAILABLE,
    DOMAIN_LABELS,
    MAX_ATTEMPTS,
    AgentServiceError,
    ChatEvent,
    call_with_llm_retry,
    classify_llm_exception,
    done_event,
    error_event,
    partial_error_event,
    retry_delay,
    safe_message_for,
    status_event,
    text_event,
)

logger = logging.getLogger(__name__)

NO_RESULTS_MESSAGE = "I wasn't able to find relevant information for your request."


@dataclass
class _PlanRun:
    """Per-request state the planner's read-step callback reports into."""

    request_id: Optional[str]
    failures: List[AgentServiceError] = field(default_factory=list)


#: Set by ``route_stream`` around plan execution. The planner calls
#: ``_execute_read_step`` with a fixed signature, so this is how a read step's
#: ``AgentServiceError`` reaches the request that ran the plan.
_plan_run: ContextVar[Optional[_PlanRun]] = ContextVar("orchestrator_plan_run", default=None)


def _failed_note(domain: str) -> str:
    return f"_The {domain} assistant couldn't answer this part right now._"


#: Routing keywords that describe *what kind* of thing is asked about rather
#: than the thing itself: "scheduled fuel delivery jobs" is about jobs. A
#: clause whose one entity keyword belongs to a single domain goes only to
#: that domain, even when other domains matched through these (N3).
_QUALIFIER_KEYWORDS = frozenset({
    "location", "schedule", "delay", "fuel", "diesel", "petrol", "delivery", "ops",
})


@functools.lru_cache(maxsize=None)
def _keyword_pattern(keyword: str) -> Pattern[str]:
    """Word-start, prefix match: "trucks" and "scheduled" match, "stops"
    (``ops``) and "translate" (``sla``) do not."""
    return re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)


#: A short reply that says the specialist has nothing to report. Only the
#: first 300 characters are checked, and only replies of at most 500.
_NON_ANSWER = re.compile(
    r"\b(i (?:cannot|can't|can not|am unable|am not able|don't have|do not have)"
    r"|unable to|there (?:are|is) no|no [^.]{0,60}(?:found|available|at this time|exist)"
    r"|not (?:available|found)|outside (?:of )?my)\b",
    re.IGNORECASE,
)


def _is_non_answer(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) <= 500 and bool(_NON_ANSWER.search(stripped[:300]))


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
            "consumption", "refuel",
        ],
        "ops": [
            "shipment", "rider", "sla", "delivery", "ops", "breach",
            "driver", "order", "customer",
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

    # Clause boundaries for per-clause target narrowing.
    _CLAUSE_SPLIT = re.compile(
        "|".join(re.escape(i) for i in _COMPLEX_INDICATORS) + r"|[,;]",
        re.IGNORECASE,
    )

    def __init__(
        self,
        specialists: Dict[str, object],
        execution_planner: object,
        activity_log_service: object,
        *,
        sleep=None,
        rand=None,
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
            sleep: Optional ``async (seconds)`` used between LLM retries
                (tests inject a no-op).
            rand: Optional ``(lo, hi) -> float`` jitter source.
        """
        self._specialists = specialists
        self._planner = execution_planner
        self._activity_log = activity_log_service
        self._sleep = sleep or asyncio.sleep
        self._rand = rand or random.uniform

        # Let the planner answer read-only steps through the specialists.
        # Without this a plan's read steps went through the mutation path,
        # where the risk registry classifies their invented tool names
        # (``fleet_query`` and friends) as HIGH — so every step of every
        # complex request became a dispatcher approval for a read.
        setter = getattr(execution_planner, "set_read_step_executor", None)
        if callable(setter):
            setter(self._execute_read_step)

    async def _execute_read_step(
        self,
        step,
        resolved_params: Dict[str, Any],
        tenant_id: str,
    ) -> str:
        """Answer one read-only plan step with its specialist.

        Raises when the step names a domain this orchestrator has no
        specialist for, so ``execute_plan`` records a step failure instead of
        reporting a success that never happened.

        The specialist call gets the same bounded LLM retry as a chat turn.
        A final failure raises ``AgentServiceError`` (``recoverable=False``,
        so the planner does not re-run it) and is recorded for the request
        that is running the plan.
        """
        agent = self._specialists.get(step.agent)
        if agent is None:
            raise RuntimeError(
                f"No specialist registered for domain '{step.agent}'"
            )
        request = resolved_params.get("request") or step.description
        run = _plan_run.get()
        try:
            return await call_with_llm_retry(
                lambda: agent.handle(request, {"tenant_id": tenant_id}),
                describe=f"Plan step {step.step_id} ({step.agent})",
                domain=step.agent,
                request_id=run.request_id if run else None,
                sleep=self._sleep,
                rand=self._rand,
            )
        except AgentServiceError as err:
            if run is not None:
                run.failures.append(err)
            raise

    async def route(
        self,
        user_message: str,
        tenant_id: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Collect :meth:`route_stream` into one string.

        Returns the joined text; a partial error event becomes a short note
        naming the failed specialist. Raises ``AgentServiceError`` (safe
        message only) when the stream ended in a non-partial error event. The
        stream is drained first so its ``routing_completed`` entry is always
        written.
        """
        parts: List[str] = []
        error: Optional[ChatEvent] = None
        async for event in self.route_stream(
            user_message,
            tenant_id,
            session_id=session_id,
            request_id=request_id,
            user_id=user_id,
        ):
            if event["type"] == "text":
                parts.append(event["content"])
            elif event["type"] == "error" and event.get("partial"):
                note = _failed_note(event.get("specialist") or "AI")
                parts.append(("\n\n" if parts else "") + note)
            elif event["type"] == "error":
                error = event
        if error is not None:
            raise AgentServiceError(
                error["code"],
                error["message"],
                retry_after_seconds=error.get("retry_after_seconds"),
            )
        return "".join(parts)

    async def route_stream(
        self,
        user_message: str,
        tenant_id: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[ChatEvent]:
        """Classify intent, delegate, and yield normalized chat events.

        Event order: ``status(routing)``, then per specialist
        ``status(specialist_start)`` and its ``text`` (with ``tool`` /
        ``tool_result`` when the specialist streams), then ``done``. With
        several targets the text is held back until every target finished
        (status and tool events still stream) and then sent as labelled
        sections, without non-answers when another target answered. Complex
        requests emit ``status(planning)`` and one ``text`` block.

        Provider failures are retried per :mod:`Agents.llm_errors`. When every
        target fails the stream carries one ``error`` event (code, safe
        message, ``request_id``) and no text; when some fail, the answered
        text plus one ``error`` event with ``partial=True`` and
        ``specialist`` per failed domain (N4). Exception text never reaches
        an event (F3). ``routing_completed`` records ``outcome``
        ``success`` / ``partial`` / ``failure`` (F4); partial events do not
        make it a failure.

        ``user_id`` is the verified caller and is recorded on both activity
        entries (OI-60).

        ``history`` is the caller's text-only transcript for this (tenant,
        user, session), loaded by ``LogisticsAgent`` (OI-17). Only the simple
        path uses it: it reaches each specialist as ``context["history"]``.
        The complex (planner) path ignores it, including its fallback to
        simple execution.
        """
        start_time = time.monotonic()

        targets = await self._classify_intent(user_message)

        # Fallback to reporting when no domain matches
        if len(targets) == 0:
            targets = ["reporting"]

        is_complex = self._is_complex_request(user_message)

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
            "user_id": user_id,
            "session_id": session_id,
            "details": {
                "event": "intent_classified",
                "targets": targets,
                "is_complex": is_complex,
            },
        })

        yield status_event("routing", targets=targets)

        failures: Dict[str, Optional[AgentServiceError]] = {}
        dropped: List[str] = []
        response_length = 0
        errored = False

        if is_complex:
            events = self._stream_complex_request(
                user_message, targets, tenant_id, session_id, request_id, failures,
                dropped, user_id=user_id,
            )
        else:
            events = self._stream_simple_request(
                user_message, targets, tenant_id, session_id, request_id, failures,
                dropped, history=history,
            )
        async for event in events:
            if event["type"] == "text":
                response_length += len(event["content"])
            elif event["type"] == "error" and not event.get("partial"):
                errored = True
            yield event

        if errored:
            outcome = "failure"
        elif failures:
            outcome = "partial"
        else:
            outcome = "success"

        duration_ms = (time.monotonic() - start_time) * 1000

        # Log the completed routing
        await self._activity_log.log({
            "agent_id": "orchestrator",
            "action_type": "routing",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "details": {
                "event": "routing_completed",
                "targets": targets,
                "response_length": response_length,
                "failed_targets": list(failures),
                "error_codes": sorted({e.code for e in failures.values() if e is not None}),
                "dropped_targets": dropped,
            },
        })

        yield done_event()

    # ------------------------------------------------------------------
    # Intent classification
    # ------------------------------------------------------------------

    def _classify_intent_keywords(self, message: str) -> List[str]:
        """Synchronous keyword-based intent classification.

        Scans the message for keywords from the routing table and returns
        the list of matched domain names.  This is the fast path used by
        ``_is_complex_request`` and can be called without ``await``.

        Args:
            message: The user's natural language message.

        Returns:
            List of matched domain names (may be empty), un-narrowed: every
            domain any keyword matched. Routing uses :meth:`_narrow_targets`.
        """
        return list(self._domain_hits(message))

    def _domain_hits(self, text: str) -> Dict[str, bool]:
        """Matched domains in routing-table order, each mapped to whether an
        *entity* keyword (not only a ``_QUALIFIER_KEYWORDS`` one) matched."""
        hits: Dict[str, bool] = {}
        for domain, keywords in self.ROUTING_TABLE.items():
            for kw in keywords:
                if _keyword_pattern(kw).search(text):
                    is_entity = kw not in _QUALIFIER_KEYWORDS
                    hits[domain] = hits.get(domain, False) or is_entity
        return hits

    def _narrow_targets(self, message: str) -> List[str]:
        """Keyword targets, narrowed per clause (N3).

        The message is split on ``_COMPLEX_INDICATORS`` and ``,``/``;``. In a
        clause where exactly one domain matched through an entity keyword,
        the other domains matched only through qualifiers, so the clause goes
        to that one domain. Otherwise it goes to every domain it matched.
        Returns the ordered union over the clauses.
        """
        targets: List[str] = []
        for clause in self._CLAUSE_SPLIT.split(message):
            hits = self._domain_hits(clause)
            entities = [d for d, is_entity in hits.items() if is_entity]
            for domain in (entities if len(entities) == 1 else list(hits)):
                if domain not in targets:
                    targets.append(domain)
        return targets or self._classify_intent_keywords(message)

    async def _classify_intent(self, message: str) -> List[str]:
        """Hybrid intent classification: keywords first, LLM fallback.

        1. Matches routing-table keywords and narrows them per clause
           (:meth:`_narrow_targets`).
        2. If no keywords match, asks Gemini to classify the intent.
        3. Falls back to empty list if both fail.

        Args:
            message: The user's natural language message.

        Returns:
            List of matched domain names (may be empty).
        """
        # Step 1: Fast keyword matching
        matched = self._narrow_targets(message)

        if matched:
            return matched

        # Step 2: LLM-based classification when keywords fail
        try:
            from litellm import acompletion

            from Agents.model_provider import try_resolve_agent_model_spec

            # Optional path: this classification already has a keyword-matching
            # fallback, so a missing credential should degrade routing rather
            # than raise into a user request. It previously passed an empty
            # api_key, which made every classification a silent 401 that the
            # except-block below logged as "LLM classification failed".
            model_id, model_kwargs = try_resolve_agent_model_spec()
            if model_id is None:
                return matched

            domains = list(self.ROUTING_TABLE.keys())
            classification_prompt = (
                f"Classify this user message into one or more of these domains: {domains}\n\n"
                f"User message: \"{message}\"\n\n"
                f"Reply with ONLY a comma-separated list of matching domain names. "
                f"If the message is a greeting or general question, reply with: reporting"
            )

            response = await acompletion(
                model=model_id,
                messages=[{"role": "user", "content": classification_prompt}],
                max_tokens=50,
                temperature=0.0,
                **model_kwargs,
            )

            result_text = response.choices[0].message.content.strip().lower()
            llm_domains = [d.strip() for d in result_text.split(",") if d.strip() in domains]

            if llm_domains:
                logger.info(f"LLM classified '{message}' → {llm_domains}")
                return llm_domains

        except Exception as e:
            logger.warning(f"LLM classification failed, using fallback: {e}")

        return matched

    def _is_complex_request(self, message: str) -> bool:
        """Detect whether a request involves multiple steps or domains.

        A request is considered complex if it contains conjunction or
        sequencing indicators (e.g. "and", "then", "also") **and**
        targets more than one specialist domain after narrowing.

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
        targets = self._narrow_targets(message)
        return has_conjunction and len(targets) > 1

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _specialist_events(
        self, agent, task: str, context: dict
    ) -> AsyncIterator[ChatEvent]:
        """One specialist attempt as chat events.

        Specialists that stream (``SpecialistAgent.stream``) forward text
        deltas and tool progress as they happen (F6). Anything else, e.g. a
        mock with only ``handle``, yields its whole answer as one text event.
        The check is on the class so a ``MagicMock``'s auto-attributes do not
        count as a stream.
        """
        if inspect.isasyncgenfunction(getattr(type(agent), "stream", None)):
            async for event in agent.stream(task, context):
                yield event
            return
        yield text_event(await agent.handle(task, context))

    async def _run_specialist(
        self,
        target: str,
        agent,
        task: str,
        context: dict,
        request_id: Optional[str],
        text_reaches_client: bool = True,
    ) -> AsyncIterator[ChatEvent]:
        """Run one specialist with the bounded LLM retry policy.

        When the caller forwards text live (``text_reaches_client``), retries
        only while the current attempt has emitted no text: a retry would
        repeat text the client already has. When the caller buffers text, a
        retry is allowed after text too, and the caller discards that
        specialist's buffer on its ``status(retrying)`` (N4). Each wait is
        announced as ``status(retrying)``. Full exception detail is logged
        here, server side; the caller only ever sees ``AgentServiceError``'s
        safe message.
        """
        attempt = 0
        while True:
            attempt += 1
            emitted_text = False
            try:
                async for event in self._specialist_events(agent, task, context):
                    if event["type"] == "text":
                        emitted_text = True
                    yield event
                return
            except AgentServiceError:
                raise
            except Exception as exc:
                failure = classify_llm_exception(exc)
                logger.warning(
                    "Specialist '%s' failed (attempt %d/%d, code=%s, request_id=%s)",
                    target, attempt, MAX_ATTEMPTS, failure.code, request_id,
                    exc_info=exc,
                )
                delay = (
                    None
                    if emitted_text and text_reaches_client
                    else retry_delay(failure, attempt, self._rand)
                )
                if delay is None:
                    raise AgentServiceError(
                        failure.code,
                        retry_after_seconds=failure.retry_after_seconds,
                        domain=target,
                    ) from exc
                yield status_event("retrying", specialist=target, attempt=attempt + 1)
                await self._sleep(delay)

    @staticmethod
    def _combined_error(
        errors: List[AgentServiceError], request_id: Optional[str]
    ) -> ChatEvent:
        """One error event for a request whose every target failed."""
        code = (
            AI_RATE_LIMITED
            if any(e.code == AI_RATE_LIMITED for e in errors)
            else AI_SERVICE_UNAVAILABLE
        )
        known = [e.retry_after_seconds for e in errors if e.retry_after_seconds is not None]
        retry_after = max(known) if known else None
        return error_event(code, safe_message_for(code, retry_after), request_id, retry_after)

    async def _stream_simple_request(
        self,
        user_message: str,
        targets: List[str],
        tenant_id: str,
        session_id: Optional[str],
        request_id: Optional[str],
        failures: Dict[str, Optional[AgentServiceError]],
        dropped: Optional[List[str]] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[ChatEvent]:
        """Run each matched specialist in turn and stream its events.

        Failed targets are recorded in ``failures``. One target streams its
        text live. Several targets have their text buffered (status and tool
        events still stream live) and flushed by :meth:`_answer_sections`;
        targets whose non-answer was dropped are appended to ``dropped``.

        Unless every target failed before any text (one terminal error), each
        failed target gets a :func:`partial_error_event` right after its text
        (live) or at its slot in the flush (buffered). A buffered target may
        be retried after partial text; its discarded attempt never reaches
        the client (N4).
        """
        context = {"tenant_id": tenant_id}
        if session_id:
            context["session_id"] = session_id
        if history:
            # The specialist copies this into a fresh Agent per call (OI-17).
            context["history"] = list(history)

        buffered = len(targets) > 1
        answers: List[Tuple[str, str]] = []
        attempted: List[str] = []
        any_text = False
        for target in targets:
            agent = self._specialists.get(target)
            if not agent:
                continue
            attempted.append(target)
            yield status_event("specialist_start", specialist=target)
            parts: List[str] = []
            try:
                async for event in self._run_specialist(
                    target, agent, user_message, context, request_id,
                    text_reaches_client=not buffered,
                ):
                    if buffered and event["type"] == "text":
                        parts.append(event["content"])
                        continue
                    if event["type"] == "text":
                        any_text = True
                    elif (
                        event.get("stage") == "retrying"
                        and event.get("specialist") == target
                    ):
                        parts.clear()  # the failed attempt's text is discarded
                    yield event
            except AgentServiceError as err:
                failures[target] = err
            answer = "".join(parts)
            # A buffered specialist that finally failed sends no text: its
            # answer is incomplete and the partial error stands in for it.
            if answer and target not in failures:
                answers.append((target, answer))

        if attempted and len(failures) == len(attempted) and not any_text:
            yield self._combined_error(
                [e for e in failures.values() if e is not None], request_id
            )
            return

        sections = dict(self._answer_sections(answers, dropped)) if answers else {}
        for target in attempted:
            if target in sections:
                yield sections[target]
                any_text = True
            if target in failures:
                yield partial_error_event(failures[target], request_id, specialist=target)

        if not any_text:
            yield text_event(NO_RESULTS_MESSAGE)

    @staticmethod
    def _answer_sections(
        answers: List[Tuple[str, str]], dropped: Optional[List[str]]
    ) -> List[Tuple[str, ChatEvent]]:
        """Text events for buffered answers from several specialists (N3).

        Non-answers are dropped when at least one answer is substantive, so a
        "There are no scheduled jobs" from one specialist no longer sits next
        to another's list of those jobs. When more than one answer is kept
        each is headed by its specialist's label. When every answer is a
        non-answer, all are kept. Returns ``(target, text event)`` pairs in
        answer order.
        """
        substantive = [(t, a) for t, a in answers if not _is_non_answer(a)]
        kept = substantive or answers
        if dropped is not None:
            kept_targets = {t for t, _ in kept}
            dropped.extend(t for t, _ in answers if t not in kept_targets)
        if len(kept) == 1:
            return [(kept[0][0], text_event(kept[0][1]))]
        return [
            (
                target,
                text_event(
                    ("\n\n" if i else "")
                    + f"**{DOMAIN_LABELS.get(target, target.title())}**\n\n{answer}"
                ),
            )
            for i, (target, answer) in enumerate(kept)
        ]

    async def _stream_complex_request(
        self,
        user_message: str,
        targets: List[str],
        tenant_id: str,
        session_id: Optional[str],
        request_id: Optional[str],
        failures: Dict[str, Optional[AgentServiceError]],
        dropped: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[ChatEvent]:
        """Run a complex request through the ExecutionPlanner.

        The plan runs to completion and is reported as one text block,
        followed by one partial error event per domain whose read step failed
        with an AI-service error. When every read step failed and at least one
        failure was an AI-service error, the request reports that error
        instead of a plan of failures. A planner exception falls back to
        simple sequential execution.
        """
        yield status_event("planning")

        run = _PlanRun(request_id=request_id)
        # No ``yield`` between set and reset: the token must be reset in the
        # context that created it.
        token = _plan_run.set(run)
        try:
            try:
                # Tenant-scoped so the plan_created activity entry (which
                # carries the prompt as its goal) reaches only this tenant (L2).
                plan = await self._planner.create_plan(
                    user_message,
                    targets,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                )
                executed_plan = await self._planner.execute_plan(plan, tenant_id)
            except Exception:
                logger.exception(
                    "Complex request execution failed (request_id=%s)", request_id
                )
                executed_plan = None
        finally:
            _plan_run.reset(token)

        if executed_plan is None:
            # Fall back to simple sequential execution
            async for event in self._stream_simple_request(
                user_message, targets, tenant_id, session_id, request_id, failures,
                dropped,
            ):
                yield event
            return

        by_domain = {e.domain: e for e in run.failures}
        for step in executed_plan.steps:
            status = getattr(step.status, "value", step.status)
            if status == "failed":
                failures[step.agent] = by_domain.get(step.agent)

        read_steps = [s for s in executed_plan.steps if getattr(s, "read_only", False)]
        all_reads_failed = bool(read_steps) and all(
            getattr(s.status, "value", s.status) == "failed" for s in read_steps
        )
        if all_reads_failed and run.failures:
            yield self._combined_error(run.failures, request_id)
            return

        yield text_event(self._format_plan_result(executed_plan))

        # One partial event per domain whose read step failed on the AI
        # service (N4). Steps that failed for other reasons are already shown
        # as failed in the plan text.
        reported = set()
        for step in read_steps:
            err = by_domain.get(step.agent)
            if (
                getattr(step.status, "value", step.status) == "failed"
                and err is not None
                and step.agent not in reported
            ):
                reported.add(step.agent)
                yield partial_error_event(err, request_id, specialist=step.agent)

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
            return NO_RESULTS_MESSAGE

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
