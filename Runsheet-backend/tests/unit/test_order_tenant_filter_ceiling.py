"""
CI grep guard — every ES query targeting ``fuel_orders_current``,
``fuel_order_events``, ``drivers_current``, or ``intake_channels``
MUST include a tenant filter term.

Mirrors the ``test_http_exception_ceiling.py`` pattern: scans all
tracked Python files for ES query bodies that reference the protected
indices and asserts each one contains a tenant_id filter. Fails the
build on any new leak.

This guard prevents accidental cross-tenant data exposure by catching
queries that omit the mandatory ``{"term": {"tenant_id": ...}}`` or
equivalent ``inject_tenant_filter`` call.

Validates: Requirement 9.1.1
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: ES indices that MUST always be queried with a tenant_id filter.
PROTECTED_INDICES: Set[str] = {
    "fuel_orders_current",
    "fuel_order_events",
    "drivers_current",
    "intake_channels",
}

#: Patterns that indicate a tenant filter is present in the surrounding
#: context. If ANY of these appear within the same function/method body
#: as the index reference, the query is considered safe.
TENANT_FILTER_INDICATORS: List[str] = [
    "tenant_id",
    "inject_tenant_filter",
    "tenant_filter",
    "tenant.tenant_id",
    "context.tenant_id",
    "channel.tenant_id",
]

#: Files that are explicitly allowed to reference protected indices
#: WITHOUT a tenant filter (e.g. mapping setup, migration scripts,
#: test fixtures, this file itself).
ALLOWLISTED_PATH_PATTERNS: List[str] = [
    "order_es_mappings.py",
    "migrations/",
    "tests/",
    "conftest.py",
    "scripts/",
    "bootstrap/",
    "test_order_tenant_filter_ceiling.py",
]

#: Directories to skip entirely during scanning.
SKIP_DIRS: Set[str] = {
    "venv",
    "coverage_html",
    ".hypothesis",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".git",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the ``Runsheet-backend`` root (two levels up from this file)."""
    return Path(__file__).resolve().parent.parent.parent


def _is_allowlisted(rel_path: str) -> bool:
    """Check if a file path matches any allowlisted pattern."""
    for pattern in ALLOWLISTED_PATH_PATTERNS:
        if pattern in rel_path:
            return True
    return False


def _extract_function_bodies(text: str) -> List[Tuple[int, int, str]]:
    """Extract approximate function/method body ranges from Python source.

    Returns a list of (start_line, end_line, body_text) tuples.
    This is a heuristic — it finds ``def `` or ``async def `` lines and
    captures everything until the next top-level def or class or EOF.
    """
    lines = text.split("\n")
    bodies: List[Tuple[int, int, str]] = []
    func_start: int | None = None
    func_indent: int = 0

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("def ", "async def ", "class ")):
            # Close previous function body
            if func_start is not None:
                bodies.append((func_start, i - 1, "\n".join(lines[func_start:i])))
            func_start = i
            func_indent = len(line) - len(stripped)

    # Close the last function body
    if func_start is not None:
        bodies.append((func_start, len(lines) - 1, "\n".join(lines[func_start:])))

    return bodies


def _find_unguarded_index_references() -> Dict[str, List[str]]:
    """Scan the repo for ES queries targeting protected indices without
    a tenant filter.

    Returns a dict mapping file paths (relative to repo root) to a list
    of violation descriptions.
    """
    root = _repo_root()
    violations: Dict[str, List[str]] = {}

    # Build regex patterns for index references
    # Match string literals containing the index name (common patterns):
    #   "fuel_orders_current"
    #   'fuel_orders_current'
    #   index="fuel_orders_current"
    #   _index = "fuel_orders_current"
    index_pattern = re.compile(
        r"""(?:['"])({indices})(?:['"])""".format(
            indices="|".join(re.escape(idx) for idx in PROTECTED_INDICES)
        )
    )

    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in parts):
            continue

        rel = path.relative_to(root).as_posix()

        # Skip allowlisted files
        if _is_allowlisted(rel):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Find all index references in the file
        index_matches = list(index_pattern.finditer(text))
        if not index_matches:
            continue

        # For each function body containing an index reference,
        # check that it also contains a tenant filter indicator
        bodies = _extract_function_bodies(text)

        for match in index_matches:
            match_pos = match.start()
            match_line = text[:match_pos].count("\n")
            index_name = match.group(1)

            # Find the enclosing function body
            enclosing_body: str | None = None
            for start_line, end_line, body in bodies:
                if start_line <= match_line <= end_line:
                    enclosing_body = body
                    break

            # If no enclosing function found, use a context window
            if enclosing_body is None:
                lines = text.split("\n")
                context_start = max(0, match_line - 10)
                context_end = min(len(lines), match_line + 20)
                enclosing_body = "\n".join(lines[context_start:context_end])

            # Check for tenant filter indicators
            has_tenant_filter = any(
                indicator in enclosing_body
                for indicator in TENANT_FILTER_INDICATORS
            )

            if not has_tenant_filter:
                violations.setdefault(rel, []).append(
                    f"Line {match_line + 1}: reference to '{index_name}' "
                    f"without a tenant_id filter in the enclosing scope"
                )

    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_unguarded_es_queries_on_protected_indices() -> None:
    """Every ES query targeting a protected index MUST include a tenant filter.

    This CI guard scans all non-allowlisted Python files for references to
    ``fuel_orders_current``, ``fuel_order_events``, ``drivers_current``, or
    ``intake_channels`` and asserts that the enclosing function/method also
    references a tenant_id filter mechanism.

    Failing this test means a new query was introduced that could leak data
    across tenant boundaries. Fix by:
    1. Adding ``inject_tenant_filter`` or an explicit ``tenant_id`` term to
       the query.
    2. If the reference is legitimately tenant-agnostic (e.g. mapping setup),
       add the file to ``ALLOWLISTED_PATH_PATTERNS`` with a comment explaining
       why.

    Validates: Requirement 9.1.1
    """
    violations = _find_unguarded_index_references()

    if violations:
        report_lines = [
            "Tenant filter ceiling broken — the following ES queries reference "
            "protected indices without a tenant_id filter:\n"
        ]
        for filepath, issues in sorted(violations.items()):
            report_lines.append(f"\n  {filepath}:")
            for issue in issues:
                report_lines.append(f"    • {issue}")

        report_lines.append(
            "\n\nFix: add inject_tenant_filter() or an explicit "
            '{"term": {"tenant_id": ...}} to the query, OR add the file '
            "to ALLOWLISTED_PATH_PATTERNS if the reference is legitimately "
            "tenant-agnostic (with a comment explaining why)."
        )

        assert False, "\n".join(report_lines)
