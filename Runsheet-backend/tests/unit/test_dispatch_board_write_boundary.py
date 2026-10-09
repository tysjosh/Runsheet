"""T-B10 (design I4, plan task 15): only ``dispatch_board_publish.py`` writes orders.

Every other ``fuel/services/dispatch_board_*.py`` module reads orders through
the validation context and never imports or calls an order write API, so order
status and links change only at Publish and re-publish.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[2] / "fuel" / "services"
ALLOWED = "dispatch_board_publish.py"

#: Modules whose import gives a board module order write access.
WRITE_MODULES = {
    "fuel.order_repository",
    "fuel.services.order_service",
    "fuel.services.plan_dispatch_service",
    "fuel.services.loading_plan_executor",
    "Agents.support.plan_execution_service",
}
#: Order and plan write methods (repository, order service, dispatch, executor).
WRITE_CALLS = {
    "relink_dispatched_assignment",
    "claim_assignment",
    "release_assignment",
    "upsert_with_last_event_timestamp",
    "apply_status_transition",
    "append_event",
    "record_checkin",
    "create_execution",
    "merge_plan_fields",
}
#: Collaborators that write orders when called (``.execute`` / ``.dispatch`` on them).
WRITE_COLLABORATORS = {"executor", "_executor", "dispatch_service", "_dispatch", "plan_dispatch_service", "loading_plan_executor"}


def _board_modules():
    return sorted(p for p in SERVICES.glob("dispatch_board_*.py") if p.name != ALLOWED)


def test_the_boundary_covers_the_board_modules():
    names = {p.name for p in _board_modules()}
    assert {"dispatch_board_service.py", "dispatch_board_engine.py", "dispatch_board_models.py"} <= names
    assert (SERVICES / ALLOWED).exists()


@pytest.mark.parametrize("path", _board_modules(), ids=lambda p: p.name)
def test_no_board_module_but_publish_imports_or_calls_order_writes(path):
    tree = ast.parse(path.read_text())
    imported = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert imported & WRITE_MODULES == set()
    assert calls & WRITE_CALLS == set()
    assert attributes & WRITE_COLLABORATORS == set()


def test_publish_module_is_where_the_writes_are():
    tree = ast.parse((SERVICES / ALLOWED).read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {"relink_dispatched_assignment", "dispatch", "execute", "append_event"} <= calls
