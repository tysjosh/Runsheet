"""
Strands agents must not print LLM answers or tool calls to stdout.

Staging finding F13: ``Agent()`` defaults to ``PrintingCallbackHandler``, which
writes every streamed text delta and every tool call to stdout. On ECS stdout is
CloudWatch, so tenant answers and tool inputs landed in the logs. Separately,
LiteLLM response objects trigger Pydantic serializer warnings that echo field
values to stderr.

The specialist tests drive a real Strands loop with ``RecordingModel`` in place
of the LLM, so what reaches stdout is exactly what Strands would print.

Validates: staging finding F13.
"""
from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path
from typing import Any, List

from strands.handlers.callback_handler import null_callback_handler
from strands.models import Model

from Agents.specialists import FleetAgent
from Agents.specialists._base import SpecialistAgent

AGENTS_DIR = Path(__file__).resolve().parents[2] / "Agents"


class RecordingModel(Model):
    """Fake Strands model: records each call's messages, answers "ok"."""

    def __init__(self) -> None:
        self.calls: List[list] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(messages)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


async def test_specialist_answer_is_not_printed(capsys):
    specialist = FleetAgent(model=RecordingModel())

    answer = await specialist.handle("x", {"tenant_id": "t"})

    assert "ok" in answer
    assert "ok" not in capsys.readouterr().out


def test_specialist_agent_uses_null_callback_handler():
    agent = SpecialistAgent(model=RecordingModel())._new_agent()
    assert agent.callback_handler is null_callback_handler


def test_logistics_agent_uses_null_callback_handler(monkeypatch):
    mainagent = importlib.import_module("Agents.mainagent")
    model_provider = importlib.import_module("Agents.model_provider")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setattr(
        model_provider, "build_agent_model", lambda settings: RecordingModel()
    )

    agent = mainagent.LogisticsAgent()

    assert agent.agent.callback_handler is null_callback_handler


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_every_strands_agent_construction_sets_callback_handler():
    """Drift guard: a new ``Agent(...)`` without ``callback_handler`` would
    bring the printing handler back."""
    offenders = []
    for path in sorted(AGENTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == "Agent":
                if not any(kw.arg == "callback_handler" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(AGENTS_DIR)}:{node.lineno}")
    assert not offenders, f"Agent() without callback_handler: {offenders}"


def test_pydantic_serializer_warnings_are_filtered():
    # pytest restores ``warnings.filters`` around every test, so an
    # import-time filter is only observable reliably in a fresh interpreter.
    probe = (
        "import re, warnings, Agents.model_provider\n"
        "print(any(a == 'ignore' and isinstance(m, re.Pattern)"
        " and m.pattern == 'Pydantic serializer warnings' and c is UserWarning"
        " for a, m, c, _mod, _ln in warnings.filters))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=AGENTS_DIR.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "True", "Pydantic serializer warning filter missing"
