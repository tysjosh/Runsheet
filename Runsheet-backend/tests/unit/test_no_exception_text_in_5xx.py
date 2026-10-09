"""5xx bodies never carry exception text (OI-36).

``internal_error(message=str(e))`` and ``details={"error": str(e)}`` put the
raw exception into the response body, and that text can hold store DSNs,
SQL fragments or file paths. The detail belongs in the server log. This test
walks the backend source with ``ast`` and fails on any ``internal_error(...)``
whose message, or any value in a literal ``details`` dict, is built from an
exception (``str(<name>)``/``repr(<name>)``) or is an f-string.

It also checks one real route: a forced store failure on
``GET /api/fuel/mvp/plans`` returns a 500 whose body doesn't contain the
exception text.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {"venv", ".venv", "tests", "node_modules"}


def _leaks(node: ast.AST) -> bool:
    """True when *node* builds text from a runtime value we can't vouch for."""
    if isinstance(node, ast.JoinedStr):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("str", "repr")
        and node.args
        and isinstance(node.args[0], ast.Name)
    ):
        return True
    if isinstance(node, ast.BinOp):
        return _leaks(node.left) or _leaks(node.right)
    return False


def _checked_values(call: ast.Call) -> List[ast.AST]:
    values: List[ast.AST] = []
    message = call.args[0] if call.args else None
    details = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "message":
            message = kw.value
        elif kw.arg == "details":
            details = kw.value
    if message is not None:
        values.append(message)
    if isinstance(details, ast.Dict):
        values.extend(details.values)
    return values


def _violations() -> List[str]:
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "internal_error":
                continue
            for value in _checked_values(node):
                if _leaks(value):
                    found.append(f"{rel.as_posix()}:{node.lineno} {ast.unparse(value)}")
    return found


def test_guard_detects_the_leak_shapes():
    tree = ast.parse(
        "internal_error(message=str(e))\n"
        "internal_error(message='x', details={'error': str(exc)})\n"
        "internal_error(f'failed: {e}')\n"
        "internal_error(message='ok', details={'id': plan_id})\n"
    )
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "internal_error"]
    flagged = [any(_leaks(v) for v in _checked_values(c)) for c in calls]
    assert flagged == [True, True, True, False]


def test_no_internal_error_carries_exception_text():
    violations = _violations()
    assert violations == [], (
        "internal_error() must use a fixed message and keep exception text in "
        "the log (OI-36):\n" + "\n".join(violations)
    )


def test_forced_500_body_has_no_exception_text(monkeypatch):
    from Agents.support import mvp_endpoints
    from errors.handlers import register_exception_handlers
    from tests.support.auth_seam import auth_headers, install_test_auth

    secret = "postgresql://svc:hunter2@db.internal:5432/runsheet"
    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=RuntimeError(f"connect failed {secret}"))
    monkeypatch.setattr(mvp_endpoints, "_es_service", es)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(mvp_endpoints.router)
    install_test_auth(app)
    client = TestClient(app, headers=auth_headers("tenant-a"), raise_server_exceptions=False)

    resp = client.get("/api/fuel/mvp/plans")

    assert resp.status_code == 500, resp.text
    assert "Plans could not be loaded" in resp.text
    assert "hunter2" not in resp.text
    assert "connect failed" not in resp.text
