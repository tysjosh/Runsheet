"""AI tools run for the request's bound tenant, never a model-supplied one.

Twenty tools expose a ``tenant_id`` argument, so the model fills it in. They
resolved it as ``tenant_id or get_current_tenant()`` (fuel, scheduling,
mutation tools), or used the argument outright (ops metrics and report
tools). A model-chosen value, from a prompt injection or a guess, then read,
or queued mutations in, another tenant. The specialist prompt also carried
``[Context: Tenant: <id>]``, which is why answers quoted the internal tenant
id to users.
"""
import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from Agents.specialists._base import SpecialistAgent
from Agents.tools._tenant_context import resolve_tool_tenant, set_current_tenant
from Agents.tools.fuel_tools import search_fuel_stations
from Agents.tools.ops_report_tools import generate_sla_report
from Agents.tools.ops_search_tools import get_ops_metrics

BOUND = "tenant-bound"
OTHER = "tenant-other"
TOOLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "Agents" / "tools"


def test_bound_tenant_wins_over_the_argument(caplog):
    with set_current_tenant(BOUND):
        assert resolve_tool_tenant(OTHER) == BOUND
        assert resolve_tool_tenant(None) == BOUND
        assert resolve_tool_tenant(BOUND) == BOUND
    assert any("different tenant" in r.getMessage() for r in caplog.records)


def test_argument_is_used_only_outside_a_scope():
    assert resolve_tool_tenant(OTHER) == OTHER
    with pytest.raises(RuntimeError):
        resolve_tool_tenant(None)


@pytest.mark.asyncio
async def test_fuel_search_ignores_a_model_supplied_tenant():
    with patch("Agents.tools.fuel_tools.elasticsearch_service") as es, set_current_tenant(BOUND):
        es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
        await search_fuel_stations(query="critical stations", tenant_id=OTHER)
    body = str(es.search_documents.call_args[0][1])
    assert BOUND in body
    assert OTHER not in body


@pytest.mark.asyncio
async def test_ops_metrics_ignores_a_model_supplied_tenant():
    guard = AsyncMock(return_value="disabled")
    with patch("Agents.tools.ops_search_tools.check_ops_feature_flag", guard), set_current_tenant(BOUND):
        await get_ops_metrics(tenant_id=OTHER)
    guard.assert_awaited_once_with(BOUND)


@pytest.mark.asyncio
async def test_ops_report_ignores_a_model_supplied_tenant():
    guard = AsyncMock(return_value="disabled")
    with patch("Agents.tools.ops_report_tools.check_ops_feature_flag", guard), set_current_tenant(BOUND):
        await generate_sla_report("2026-10-01", "2026-10-02", tenant_id=OTHER)
    guard.assert_awaited_once_with(BOUND)


@pytest.mark.asyncio
async def test_ops_tools_no_longer_require_a_tenant_argument():
    guard = AsyncMock(return_value="disabled")
    with patch("Agents.tools.ops_search_tools.check_ops_feature_flag", guard), set_current_tenant(BOUND):
        await get_ops_metrics()
    guard.assert_awaited_once_with(BOUND)


def test_every_tool_with_a_tenant_argument_resolves_it_through_the_bound_scope():
    """A new tool taking ``tenant_id`` must call ``resolve_tool_tenant`` (directly
    or via a module ``_resolve_tenant_id`` that does)."""
    offenders = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolvers = {"resolve_tool_tenant"}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and "resolve_tool_tenant" in ast.unparse(node):
                resolvers.add(node.name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not any("tool" in ast.unparse(d) for d in node.decorator_list):
                continue
            if "tenant_id" not in [a.arg for a in node.args.args + node.args.kwonlyargs]:
                continue
            called = {
                c.func.id for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            if not called & resolvers:
                offenders.append(f"{path.name}:{node.name}")
    assert offenders == []


def test_specialist_prompt_does_not_carry_the_tenant_id():
    prompt, tenant = SpecialistAgent._prompt("Which stations are critical?", {"tenant_id": BOUND})
    assert tenant == BOUND
    assert BOUND not in prompt
    assert prompt == "Which stations are critical?"
