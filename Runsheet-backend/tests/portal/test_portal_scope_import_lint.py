"""Portal handlers reach data only through ``portal.services`` (design §2.1 E8;
ISO-C-5 / T-SCOPE-IMPORT).

An AST scan of every module in ``portal/api/``: none may import a data store
or service directly.
"""
from __future__ import annotations

import ast
from pathlib import Path

import portal.api

FORBIDDEN_NAMES = {
    "InvoiceService",
    "FuelOrderRepository",
    "CustomerTankRepository",
    "ElasticsearchService",
    "elasticsearch_service",
}
FORBIDDEN_MODULES = {
    "commerce.services.invoice_service",
    "fuel.order_repository",
    "fuel.customer_tank_models",
    "services.elasticsearch_service",
}


def _violations(source: str, filename: str = "<portal.api>"):
    tree = ast.parse(source, filename=filename)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES or alias.name.split(".")[-1] in FORBIDDEN_NAMES:
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES:
                out.append(module)
            out += [f"{module}.{a.name}" for a in node.names if a.name in FORBIDDEN_NAMES]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            out.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            out.append(node.attr)
    return out


def test_portal_api_imports_no_store():
    api_dir = Path(portal.api.__file__).parent
    modules = sorted(api_dir.glob("*.py"))
    assert any(m.name == "order_endpoints.py" for m in modules)
    found = {m.name: v for m in modules if (v := _violations(m.read_text(), str(m)))}
    assert found == {}


def test_lint_catches_a_direct_import():
    assert _violations("from fuel.order_repository import FuelOrderRepository\n")
    assert _violations("import services.elasticsearch_service\n")
    assert _violations("x = svc.ElasticsearchService\n")
    assert not _violations("from portal.services.scoped_readers import get_portal_readers\n")
