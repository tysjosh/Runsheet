"""Mailtrap off switch in staging_aws.sh (mailtrap-review issue 1, 2026-10-09).

A secret scheduled for deletion still answers ``describe-secret`` (with
``DeletedDate`` set) but ECS can't resolve it at task start, so the deploy must
treat it as absent. ``STAGING_EMAIL_OFF=1`` turns email off without touching
the secret. The functions are cut out of the real script and run against a
PATH-stubbed ``aws``; nothing reaches AWS.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "staging_aws.sh"

#: STUB_SECRET: missing | live | deleted
AWS_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "${STUB_SECRET}" in
  missing) echo "ResourceNotFoundException" >&2; exit 254 ;;
  live) echo None ;;
  deleted) echo 2026-10-09T10:00:00+00:00 ;;
esac
"""


def _function(name: str) -> str:
    m = re.search(rf"^{name}\(\) {{\n.*?^}}\n", SCRIPT.read_text(), re.S | re.M)
    assert m, f"{name}() not found in staging_aws.sh"
    return m.group(0)


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(AWS_STUB)
    aws.chmod(aws.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "aws.log"
    body = _function("secret_live") + _function("mailtrap_on")

    def go(secret: str, off: str | None = None) -> tuple[int, str]:
        log.write_text("")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_SECRET": secret,
            "SECRET_MAILTRAP": "runsheet-staging/mailtrap-api-token",
        }
        if off is not None:
            env["STAGING_EMAIL_OFF"] = off
        proc = subprocess.run(
            ["bash", "-c", body + "mailtrap_on"], env=env, capture_output=True, text=True
        )
        return proc.returncode, log.read_text()

    return go


def test_live_secret_turns_email_on(run):
    code, log = run("live")
    assert code == 0
    assert "describe-secret --secret-id runsheet-staging/mailtrap-api-token" in log


def test_missing_secret_is_email_off(run):
    assert run("missing")[0] != 0


def test_secret_pending_deletion_is_email_off(run):
    assert run("deleted")[0] != 0


def test_off_switch_wins_and_never_calls_aws(run):
    code, log = run("live", off="1")
    assert code != 0
    assert log == ""


def test_off_switch_other_values_leave_email_on(run):
    assert run("live", off="0")[0] == 0


def test_deploy_uses_the_switch_not_bare_existence():
    text = SCRIPT.read_text()
    assert 'secret_exists "${SECRET_MAILTRAP}"' not in text
    assert text.count("if mailtrap_on; then") == 2


def test_margin_feed_enabled_in_api_env():
    """Owner decision 2026-10-09: the margin feed is on for staging."""
    assert '{"name": "COMMERCE_MARGIN_FEED_ENABLED", "value": "true"}' in SCRIPT.read_text()
