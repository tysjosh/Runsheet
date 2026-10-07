"""A create path may not upsert on an id it didn't mint (L1 regression guard).

Document ids are global: the store key is ``(index_name, doc_id)`` with no
tenant. A create that calls ``index_document`` on a client-supplied id replaces
whatever another tenant stored under it, which is how customer tanks, terminals,
supplier contracts, intake channels and integrations were taken over (L1). The
fix is ``create_document`` (``INSERT … ON CONFLICT DO NOTHING``) plus a 409.

This test walks the backend with ``ast``. Every function whose name says it
creates something (``*create*``, ``register*``, ``insert*``, ``add*``) and that
still calls ``.index_document(`` or ``.bulk_index_documents(`` must be listed
in :data:`ALLOWED_INDEX_ON_CREATE` with the reason its id can't collide. A new
create path fails here until someone reviews where its id comes from.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

BACKEND = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {"venv", ".venv", "tests", "scripts", "alembic", "node_modules"}
_WRITE_METHODS = {"index_document", "bulk_index_documents"}

_MINTED = "id is server-minted (uuid) and never read from the request"

#: (path relative to Runsheet-backend, function name) -> why the id is safe.
ALLOWED_INDEX_ON_CREATE: Dict[Tuple[str, str], str] = {
    ("Agents/approval_queue_service.py", "create"): (
        "action_id is uuid4, or a uuid5 over tenant|plan|truck (tenant in the key)"
    ),
    ("Agents/support/plan_execution_service.py", "create_execution"): _MINTED,
    ("commerce/api/price_protection_endpoints.py", "create_price_protection_contract"): (
        "contract_id is minted by the model; the body forbids extra fields"
    ),
    ("commerce/api/pricing_endpoints.py", "create_pricing_rule"): (
        "rule_id is minted by the model; the body forbids extra fields"
    ),
    ("commerce/services/account_service.py", "create"): _MINTED,
    ("commerce/services/customer_service.py", "create"): _MINTED,
    ("commerce/services/price_book_service.py", "create"): _MINTED,
    ("compliance/api/tax_endpoints.py", "create_exemption"): (
        "exemption_id is minted by the model; the body forbids extra fields"
    ),
    ("compliance/api/tax_endpoints.py", "create_tax_jurisdiction"): (
        "jurisdiction_id is minted by the model; the body forbids extra fields"
    ),
    ("compliance/services/asset_certification_service.py", "create"): _MINTED,
    ("compliance/services/driver_qualification_service.py", "create"): (
        "driver_id is minted by the Driver model; the caller can't pass one"
    ),
    ("compliance/services/meter_audit_service.py", "register_meter"): _MINTED,
    ("driver/services/device_registry.py", "register"): (
        "doc id is tenant:driver:device, so it can't collide across tenants"
    ),
    ("fuel/api/fuel_ops_endpoints.py", "insert_route_emergency_stop"): (
        "event id is the server-minted Replan_Diff diff_id"
    ),
    ("fuel/driver_report_repository.py", "create"): (
        "the only caller (voice driver report) mints report_id as uuid4"
    ),
    ("fuel/order_repository.py", "create"): (
        "no production caller; orders are written by intake with minted ids, "
        "and the store refuses a cross-tenant re-home regardless"
    ),
    ("inventory/driver_exception_handler.py", "_create_restock_request"): _MINTED,
    ("inventory/service.py", "create_item"): _MINTED,
}


def _is_create_name(name: str) -> bool:
    bare = name.lstrip("_").lower()
    return "create" in bare or bare.startswith(("register", "insert", "add"))


def _create_paths_that_index() -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_create_name(node.name):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in _WRITE_METHODS
                ):
                    found.add((rel.as_posix(), node.name))
                    break
    return found


def test_every_create_path_that_upserts_is_reviewed():
    unreviewed = sorted(_create_paths_that_index() - set(ALLOWED_INDEX_ON_CREATE))
    assert not unreviewed, (
        "These create paths write with index_document/bulk_index_documents, "
        "which replaces a document another tenant owns under the same id. Use "
        "create_document and raise errors.exceptions.already_exists on False, or, "
        "if the id is server-minted or contains the tenant, add the function to "
        f"ALLOWED_INDEX_ON_CREATE with the reason: {unreviewed}"
    )


def test_allowlist_has_reasons_and_no_stale_entries():
    present = _create_paths_that_index()
    stale = sorted(set(ALLOWED_INDEX_ON_CREATE) - present)
    assert not stale, f"allowlisted but no longer upserting, remove them: {stale}"
    assert all(reason.strip() for reason in ALLOWED_INDEX_ON_CREATE.values())


def test_the_fixed_l1_creates_stay_create_if_absent():
    """The L1 sites (terminal_models._create covers terminals and contracts)."""
    fixed = {
        ("fuel/customer_tank_models.py", "create"),
        ("fuel/terminal_models.py", "_create"),
        ("fuel/intake_channel_repository.py", "create"),
        ("integrations/connector_base.py", "create"),
    }
    assert not fixed & _create_paths_that_index()
    for rel, func in fixed:
        tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
        bodies = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
        ]
        calls = {
            c.func.attr
            for body in bodies
            for c in ast.walk(body)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        }
        assert "create_document" in calls, f"{rel}:{func} no longer uses create_document"
