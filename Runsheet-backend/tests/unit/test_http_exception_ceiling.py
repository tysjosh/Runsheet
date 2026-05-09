"""
Ceiling guard — no new ``raise HTTPException(...)`` call sites may be
added to the codebase.

Handlers should raise through ``errors/exceptions.py`` (``forbidden``,
``internal_error``, ``resource_not_found``, ``validation_error``, ...)
so every response goes through the structured ``ErrorResponse`` envelope
the frontend parses. The existing 185 raw-``HTTPException`` call sites
are tolerated as tech debt but cannot grow.

This test freezes a per-file counter. A migration that removes call
sites will cause the freeze to drift below the counter and the test
will fail with a clear message ("counter is stale — update
EXPECTED_HTTPEXCEPTION_COUNTS"). A regression that adds a new call
site fails with the same message. Either way, every change to the
counter surface is a visible, reviewable step.

Validates code-review finding F21 (three live error envelopes).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Frozen baseline
# ---------------------------------------------------------------------------
#
# Every entry is the count of lines containing ``raise HTTPException(``
# in the corresponding file, as of 2026-05-08 after the security sprint.
# Callers migrating a file to the structured envelope should DECREASE
# the entry (or delete it entirely when the count hits zero). Adding a
# raw ``HTTPException`` anywhere else must be preceded by a deliberate
# entry here.
#
# Paths are relative to the backend repo root.
EXPECTED_HTTPEXCEPTION_COUNTS: dict[str, int] = {
    "middleware/auth_policy.py": 4,
    "inline_endpoints.py": 4,
    "ops/api/endpoints.py": 10,
    "integrations/api/integrations_endpoints.py": 7,
    "fuel/api/fuel_ops_endpoints.py": 115,
    "integrations/api/stripe_endpoints.py": 8,
    "import_endpoints.py": 12,
    "Agents/support/mvp_endpoints.py": 25,
}

#: Total ceiling — sum of per-file counts. A handy second gate that
#: catches a regression where someone adds a new file without touching
#: the per-file dict.
EXPECTED_TOTAL_HTTPEXCEPTION_CALLS: int = sum(EXPECTED_HTTPEXCEPTION_COUNTS.values())


def _repo_root() -> Path:
    """Return the ``Runsheet-backend`` root (two levels up from this file)."""
    return Path(__file__).resolve().parent.parent.parent


def _count_http_exceptions() -> dict[str, int]:
    """Scan the repo for ``raise HTTPException(`` occurrences per file."""
    pattern = re.compile(r"raise\s+HTTPException\s*\(")
    counts: dict[str, int] = {}

    # Walk only tracked Python files. We skip venv, coverage html, and
    # .hypothesis which can contain transient strings that match. We
    # also skip this file itself — it deliberately mentions the pattern
    # in docstrings and error messages, which would otherwise inflate
    # the counter.
    skip_dirs = {"venv", "coverage_html", ".hypothesis", ".pytest_cache", "__pycache__"}
    self_path = Path(__file__).resolve()

    root = _repo_root()
    for path in root.rglob("*.py"):
        if path.resolve() == self_path:
            continue
        parts = path.relative_to(root).parts
        if any(part in skip_dirs for part in parts):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        count = len(pattern.findall(text))
        if count:
            rel = path.relative_to(root).as_posix()
            counts[rel] = count
    return counts


def test_http_exception_counter_matches_freeze() -> None:
    """Per-file ``raise HTTPException`` counts must match the frozen baseline.

    Any deviation (up OR down) is a deliberate change: either you removed
    a call site (good — decrement / delete the entry) or you added one
    (bad — migrate to the structured envelope instead, OR bump the entry
    and document why).
    """
    actual = _count_http_exceptions()

    actual_files = set(actual.keys())
    expected_files = set(EXPECTED_HTTPEXCEPTION_COUNTS.keys())

    unexpected_new_files = actual_files - expected_files
    files_removed = expected_files - actual_files

    diffs: list[str] = []
    if unexpected_new_files:
        diffs.append(
            "New files raising HTTPException (migrate to errors/exceptions.py "
            "or add to EXPECTED_HTTPEXCEPTION_COUNTS with rationale):\n  - "
            + "\n  - ".join(
                f"{path}: {actual[path]} sites" for path in sorted(unexpected_new_files)
            )
        )
    if files_removed:
        diffs.append(
            "Files no longer raising HTTPException — remove from "
            "EXPECTED_HTTPEXCEPTION_COUNTS:\n  - "
            + "\n  - ".join(sorted(files_removed))
        )

    for path in sorted(actual_files & expected_files):
        if actual[path] != EXPECTED_HTTPEXCEPTION_COUNTS[path]:
            diffs.append(
                f"{path}: count drifted from {EXPECTED_HTTPEXCEPTION_COUNTS[path]} "
                f"to {actual[path]}. Update EXPECTED_HTTPEXCEPTION_COUNTS to match."
            )

    assert not diffs, (
        "raise HTTPException ceiling broken — the allowlist must match the "
        "on-disk state exactly. Reduce counts as call sites migrate to "
        "errors.exceptions.AppException. Details:\n\n" + "\n\n".join(diffs)
    )


def test_http_exception_total_ceiling() -> None:
    """Belt-and-suspenders — sum must match the frozen total."""
    actual_total = sum(_count_http_exceptions().values())
    assert actual_total == EXPECTED_TOTAL_HTTPEXCEPTION_CALLS, (
        f"Total ``raise HTTPException(`` count is {actual_total}, "
        f"expected {EXPECTED_TOTAL_HTTPEXCEPTION_CALLS}. Update "
        f"EXPECTED_TOTAL_HTTPEXCEPTION_CALLS (and the per-file dict) when "
        f"migrating or intentionally adding sites."
    )
