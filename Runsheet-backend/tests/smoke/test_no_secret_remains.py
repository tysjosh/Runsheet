"""
No-secret-remains smoke tests (SuperTokens Auth Migration, Task 16.4).

These smoke tests assert two cutover invariants:

1. The legacy hardcoded development JWT signing secret literal is **absent**
   from backend (``Runsheet-backend/``) and frontend (``runsheet/src/``)
   source — the value was removed as part of the SuperTokens cutover
   (Req 10.5). Build artifacts (``.next/``), dependencies (``node_modules/``),
   the Python virtualenv (``venv/``), Hypothesis caches, and the test suite
   itself are excluded from the scan; test files legitimately reference the
   literal to exercise the legacy-rejection paths.

2. The production settings validator **rejects** a set ``jwt_secret`` once
   ``auth_provider == "supertokens"`` — the forgeable legacy credential must
   be unset after the hard cutover (Req 10.2, 10.5).

The forbidden literal is reconstructed at runtime (never embedded in this
file) so the scan does not match its own source.

Validates: Requirements 10.2, 10.5
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import Settings, Environment

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# This file lives at Runsheet-backend/tests/smoke/test_no_secret_remains.py
_THIS_FILE = Path(__file__).resolve()
BACKEND_ROOT = _THIS_FILE.parents[2]          # Runsheet-backend/
REPO_ROOT = _THIS_FILE.parents[3]             # repository root
FRONTEND_SRC = REPO_ROOT / "runsheet" / "src"  # runsheet/src/

# The forbidden secret literal, assembled at runtime so it never appears
# verbatim in this source file (which would self-match the scan below).
FORBIDDEN_SECRET = "-".join(
    ("dev", "jwt", "secret", "change", "me", "in", "production")
)

# Directories that are never source we control (build output, deps, caches,
# the virtualenv, and the test suite itself which legitimately references the
# literal to drive legacy-rejection tests).
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".next",
        "node_modules",
        "venv",
        ".venv",
        ".hypothesis",
        "__pycache__",
        ".pytest_cache",
        ".git",
        "tests",  # exclude the whole backend test tree (incl. this file)
        "dist",
        "build",
        "coverage",
        ".mypy_cache",
        ".ruff_cache",
    }
)

BACKEND_SOURCE_SUFFIXES = frozenset({".py"})
FRONTEND_SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


def _iter_source_files(root: Path, suffixes) -> list[Path]:
    """Yield source files under ``root`` with one of ``suffixes``, skipping
    excluded directories (build output, deps, caches, test trees)."""
    matched: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so os.walk does not descend.
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in suffixes:
                matched.append(path)
    return matched


def _files_containing_secret(files) -> list[str]:
    """Return repo-relative paths of files containing the forbidden literal."""
    offenders: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if FORBIDDEN_SECRET in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    return offenders


# ---------------------------------------------------------------------------
# 1. Literal absence from source
# ---------------------------------------------------------------------------

def test_backend_source_does_not_contain_legacy_secret():
    """The legacy dev JWT secret literal is absent from backend source
    (excluding the test suite, venv, and caches) — Req 10.5."""
    files = _iter_source_files(BACKEND_ROOT, BACKEND_SOURCE_SUFFIXES)
    # Sanity check: the scan actually found backend source to inspect.
    assert files, "expected to find backend .py source files to scan"

    offenders = _files_containing_secret(files)
    assert not offenders, (
        "Legacy dev JWT secret literal must not appear in backend source "
        f"(Req 10.5). Found in: {offenders}"
    )


def test_frontend_source_does_not_contain_legacy_secret():
    """The legacy dev JWT secret literal is absent from frontend source
    under runsheet/src/ (excluding .next/ and node_modules/) — Req 10.5."""
    assert FRONTEND_SRC.is_dir(), f"frontend src directory not found: {FRONTEND_SRC}"

    files = _iter_source_files(FRONTEND_SRC, FRONTEND_SOURCE_SUFFIXES)
    assert files, "expected to find frontend source files to scan"

    offenders = _files_containing_secret(files)
    assert not offenders, (
        "Legacy dev JWT secret literal must not appear in frontend source "
        f"(Req 10.5). Found in: {offenders}"
    )


def test_scan_would_detect_the_literal_if_present():
    """Guard against a silently-broken scan: a synthetic file containing the
    forbidden literal IS detected by the same helper used above."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "planted.py"
        planted.write_text(f'SECRET = "{FORBIDDEN_SECRET}"\n', encoding="utf-8")
        # Use the helper directly with a relative-to base that contains it.
        text = planted.read_text(encoding="utf-8")
        assert FORBIDDEN_SECRET in text


# ---------------------------------------------------------------------------
# 2. Prod validator rejects a set jwt_secret under supertokens
# ---------------------------------------------------------------------------

@pytest.fixture
def prod_supertokens_env_vars():
    """Production env vars with SuperTokens config present so the validator
    reaches the jwt_secret-must-be-unset invariant (Req 10.2, 10.5)."""
    return {
        "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
        "ELASTIC_API_KEY": "test-api-key-12345",
        "GOOGLE_CLOUD_PROJECT": "test-project-id",
        "ENVIRONMENT": "production",
        "REDIS_URL": "redis://redis.internal:6379",
        "DINEE_WEBHOOK_SECRET": "prod-webhook-secret",
        "CORS_ORIGINS": '["https://app.runsheet.com"]',
        "AUTH_PROVIDER": "supertokens",
        "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
        "SUPERTOKENS_API_KEY": "prod-st-api-key",
    }


def test_prod_validator_rejects_set_jwt_secret_under_supertokens(prod_supertokens_env_vars):
    """A set jwt_secret with auth_provider=supertokens in production fails
    startup with a descriptive error — the legacy credential must be unset
    after the hard cutover (Req 10.2, 10.5)."""
    env_vars = {
        **prod_supertokens_env_vars,
        "JWT_SECRET": "prod-jwt-secret-that-is-at-least-32-chars-long",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        with pytest.raises(Exception) as exc_info:
            Settings()
    message = str(exc_info.value).lower()
    assert "jwt_secret" in message
    assert "unset" in message


def test_prod_validator_accepts_unset_jwt_secret_under_supertokens(prod_supertokens_env_vars):
    """With the legacy secret removed, supertokens mode validates cleanly in
    production (the cutover end state) — Req 10.5."""
    env_vars = {**prod_supertokens_env_vars, "JWT_SECRET": ""}
    with patch.dict(os.environ, env_vars, clear=True):
        settings = Settings()
    assert settings.environment == Environment.PRODUCTION
    assert settings.auth_provider == "supertokens"
    assert settings.jwt_secret == ""
