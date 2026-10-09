"""
One answer per question: chat routing no longer fans a single-entity question
out to several specialists that contradict each other.

Staging finding N3: "List the scheduled fuel delivery jobs" matched scheduling
("job", "schedule"), fuel ("fuel") and ops ("delivery") by substring, and the
dispatcher got the scheduling agent's list of jobs followed by the fuel and
ops agents' "There are no scheduled fuel delivery jobs". Substring matching
also routed "stops" to ops.

The orchestrator is real; specialists are fakes. No LLM is called.

Validates: staging finding N3.
"""
from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock

from Agents.llm_errors import text_event, tool_event, tool_result_event
from Agents.orchestrator import AgentOrchestrator


class Answers:
    """``handle`` returns a fixed answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def handle(self, task: str, context: dict = None) -> str:
        return self.answer


class Streams:
    """A streaming specialist: ``stream`` is an async generator on the class."""

    def __init__(self, *events) -> None:
        self.events = list(events)

    async def stream(self, task: str, context: dict = None):
        for event in self.events:
            yield event

    async def handle(self, task: str, context: dict = None) -> str:  # pragma: no cover
        raise AssertionError("streaming specialists are not called via handle")


async def _no_sleep(_seconds: float) -> None:
    return None


def _log() -> MagicMock:
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id")
    return log


def _orch(specialists: dict, log: MagicMock = None) -> AgentOrchestrator:
    return AgentOrchestrator(
        specialists=specialists,
        execution_planner=MagicMock(),
        activity_log_service=log or _log(),
        sleep=_no_sleep,
        rand=lambda lo, hi: lo,
    )


async def _events(orch: AgentOrchestrator, message: str) -> List[dict]:
    return [e async for e in orch.route_stream(message, "tenant-1", request_id="req-1")]


def _text(events: List[dict]) -> str:
    return "".join(e["content"] for e in events if e["type"] == "text")


def _routing_completed(log: MagicMock) -> dict:
    entries = [c.args[0] for c in log.log.call_args_list]
    done = [e for e in entries if e["details"]["event"] == "routing_completed"]
    assert len(done) == 1
    return done[0]


# ---------------------------------------------------------------------------
# Narrowed routing
# ---------------------------------------------------------------------------


async def test_staging_prompt_goes_to_scheduling_only():
    orch = _orch({})
    assert await orch._classify_intent("List the scheduled fuel delivery jobs") == ["scheduling"]


async def test_delayed_trucks_go_to_fleet_only():
    orch = _orch({})
    assert await orch._classify_intent("Show me delayed trucks") == ["fleet"]


async def test_fuel_station_question_goes_to_fuel_only():
    # Pin: already fuel-only before N3.
    orch = _orch({})
    assert await orch._classify_intent(
        "Which fuel stations are critical or closest to running out?"
    ) == ["fuel"]


async def test_two_entity_keywords_keep_two_targets():
    # Pin: truck (fleet) and consumption (fuel) are both entities.
    orch = _orch({})
    assert await orch._classify_intent("Show truck fuel consumption") == ["fleet", "fuel"]


async def test_two_clauses_stay_complex_with_both_targets():
    # Pin: each clause names its own domain.
    orch = _orch({})
    message = "Check truck status and show fuel levels"
    assert orch._is_complex_request(message) is True
    assert await orch._classify_intent(message) == ["fleet", "fuel"]


def test_keywords_match_at_word_start_only():
    orch = _orch({})
    # "stops" used to contain "ops"; "translate" used to contain "sla".
    assert orch._classify_intent_keywords("Show stops on the route") == []
    assert "ops" not in orch._classify_intent_keywords("translate this")
    # Prefix matches still work.
    assert orch._classify_intent_keywords("refuel the scheduled trucks") == [
        "fleet", "scheduling", "fuel",
    ]


# ---------------------------------------------------------------------------
# Several targets: buffered, filtered, labelled
# ---------------------------------------------------------------------------

# job/schedule → scheduling (entity "job"), station/fuel → fuel (entity
# "station"): two entity domains, so two targets.
TWO_TARGETS = "Show scheduled jobs by fuel station"


async def test_non_answer_is_dropped_when_another_target_answered():
    log = _log()
    found = "Found 3 scheduled fuel delivery jobs: JOB_5, JOB_6, JOB_7"
    none = "There are no scheduled fuel delivery jobs at this time."
    orch = _orch({"scheduling": Answers(found), "fuel": Answers(none)}, log)

    events = await _events(orch, TWO_TARGETS)

    assert events[0]["targets"] == ["scheduling", "fuel"]
    assert _text(events) == found
    assert none not in _text(events)
    details = _routing_completed(log)["details"]
    assert details["dropped_targets"] == ["fuel"]
    assert _routing_completed(log)["outcome"] == "success"


async def test_two_substantive_answers_are_both_kept_and_labelled():
    log = _log()
    orch = _orch(
        {"fleet": Answers("12 trucks on the road"), "fuel": Answers("Average 31 L/100 km")},
        log,
    )

    events = await _events(orch, "Show truck fuel consumption")

    assert _text(events) == (
        "**Fleet**\n\n12 trucks on the road\n\n**Fuel**\n\nAverage 31 L/100 km"
    )
    assert _routing_completed(log)["details"]["dropped_targets"] == []


async def test_two_non_answers_are_both_kept_and_labelled():
    log = _log()
    orch = _orch(
        {
            "fleet": Answers("I don't have consumption data for trucks."),
            "fuel": Answers("No consumption records found."),
        },
        log,
    )

    events = await _events(orch, "Show truck fuel consumption")

    assert _text(events) == (
        "**Fleet**\n\nI don't have consumption data for trucks."
        "\n\n**Fuel**\n\nNo consumption records found."
    )
    assert _routing_completed(log)["details"]["dropped_targets"] == []


async def test_progress_events_stream_before_buffered_text():
    fleet = Streams(
        tool_event("search_fleet_data"),
        tool_result_event("search_fleet_data"),
        text_event("12 "),
        text_event("trucks"),
    )
    fuel = Streams(tool_event("get_fuel_summary"), text_event("31 L/100 km"))
    orch = _orch({"fleet": fleet, "fuel": fuel})

    events = await _events(orch, "Show truck fuel consumption")

    kinds = [e["type"] for e in events]
    first_text = kinds.index("text")
    progress = [
        i for i, e in enumerate(events)
        if e["type"] in ("tool", "tool_result") or e.get("stage") == "specialist_start"
    ]
    assert len(progress) == 5
    assert max(progress) < first_text
    assert _text(events) == "**Fleet**\n\n12 trucks\n\n**Fuel**\n\n31 L/100 km"


async def test_single_target_text_still_streams_incrementally():
    # Pin: one target is not buffered.
    orch = _orch({"fleet": Streams(text_event("12 "), text_event("trucks"))})

    events = await _events(orch, "Show trucks")

    assert [e["content"] for e in events if e["type"] == "text"] == ["12 ", "trucks"]
