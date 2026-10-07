"""N-new-4: staging_aws.sh must not ship plaintext origins to an HTTPS ALB.

api:18 was registered with CORS_ORIGINS / SUPERTOKENS_*_DOMAIN set to
``http://<alb dns>`` because ``deploy`` ran without DOMAIN while the staging ALB
already served HTTPS. These tests run the real script against PATH-stubbed
``aws`` / ``docker`` / ``git`` binaries only. Nothing here reaches AWS.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "staging_aws.sh"

AWS_STUB = r"""#!/usr/bin/env bash
echo "$0 $*" >> "$STUB_LOG"
args="$*"
case "$1 $2" in
  "sts get-caller-identity") echo 224535575204 ;;
  "elbv2 describe-load-balancers")
    case "$args" in
      *DNSName*) echo runsheet-staging-alb-1.us-east-2.elb.amazonaws.com ;;
      *) echo arn:aws:elasticloadbalancing:us-east-2:224535575204:loadbalancer/app/runsheet-staging-alb/abc ;;
    esac ;;
  "elbv2 describe-listeners")
    case "$args" in
      *--listener-arns*) echo arn:aws:acm:us-east-2:224535575204:certificate/x ;;
      *)
        if [ "${STUB_HTTPS:-0}" = "1" ]; then
          echo arn:aws:elasticloadbalancing:us-east-2:224535575204:listener/app/runsheet-staging-alb/abc/443
        else
          echo None
        fi ;;
    esac ;;
  "acm describe-certificate") echo api.staging.runsheetops.com ;;
  # OI-09: a live UI task definition that carries a HubSpot form GUID.
  "ecs describe-services")
    if [ "${STUB_LIVE_HUBSPOT:-0}" = "1" ] && [[ "$args" == *"services[0].taskDefinition"* ]]; then
      echo arn:aws:ecs:us-east-2:224535575204:task-definition/runsheet-staging-ui:7
    else
      echo None
    fi ;;
  "ecs describe-task-definition")
    if [ "${STUB_LIVE_HUBSPOT:-0}" = "1" ]; then echo live-form-guid; else echo None; fi ;;
  # No image in ECR yet, so the deploy reaches the build step (the sentinel).
  "ecr describe-images") exit 1 ;;
  *) echo None ;;
esac
exit 0
"""

DOCKER_STUB = r"""#!/usr/bin/env bash
echo "$0 $*" >> "$STUB_LOG"
[ "$1" = "build" ] && exit 97
exit 0
"""

GIT_STUB = r"""#!/usr/bin/env bash
echo "$0 $*" >> "$STUB_LOG"
# OI-09: `git -C <dir> rev-parse --path-format=absolute --git-common-dir` points at
# a fake main checkout's .git when the test sets STUB_GIT_COMMON_DIR.
case "$*" in
  *--git-common-dir*) [ -n "${STUB_GIT_COMMON_DIR:-}" ] && echo "$STUB_GIT_COMMON_DIR"; exit 0 ;;
esac
[ "$1" = "rev-parse" ] && echo abc1234
exit 0
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def harness(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    script = tree / "scripts" / "staging_aws.sh"
    shutil.copy(SCRIPT, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "aws", AWS_STUB)
    _write_exe(bin_dir / "docker", DOCKER_STUB)
    _write_exe(bin_dir / "git", GIT_STUB)
    log = tmp_path / "stub.log"
    log.write_text("")

    def run(*args, https=False, domain=None, env_file=None, extra_env=None, stdin=None):
        if env_file is not None:
            (tree / ".env.staging").write_text(env_file)
        env = {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "STUB_LOG": str(log),
            "STUB_HTTPS": "1" if https else "0",
            "AWS_REGION": "us-east-2",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            # The guard runs before either build path; the local path's stubbed
            # `docker build` (exit 97) is the "got past the guard" sentinel.
            "BUILD_MODE": "local",
        }
        if domain is not None:
            env["DOMAIN"] = domain
        env.update(extra_env or {})
        # The script must only ever see the stubbed aws CLI.
        assert shutil.which("aws", path=env["PATH"]) == str(bin_dir / "aws")
        log.write_text("")
        proc = subprocess.run(
            ["bash", str(script), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tree),
        )
        return proc, log.read_text()

    return run


@pytest.fixture
def fake_main(tmp_path):
    """A main checkout whose .git is the worktree's git common dir (OI-09)."""
    root = tmp_path / "main"
    (root / ".git").mkdir(parents=True)
    (root / "Runsheet-backend").mkdir()
    (root / "runsheet").mkdir()
    return root


def test_syntax():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_dispatcher_wrapped_in_main():
    """OI-46: the dispatcher runs from a fully parsed function, and nothing after it."""
    lines = [ln for ln in SCRIPT.read_text().splitlines() if ln.strip()]
    assert lines[-1] == 'main "$@"; exit $?'
    assert "main() {" in lines
    # No top-level dispatcher left behind: every case on $1 lives inside main().
    body = SCRIPT.read_text()
    assert body.count('case "${1:-plan}" in') == 1
    assert body.index("main() {") < body.index('case "${1:-plan}" in')


def test_a_deploy_refused_with_https_and_no_domain(harness):
    proc, log = harness("deploy", https=True)
    assert proc.returncode != 0
    assert proc.returncode != 97, "reached docker build"
    assert "DOMAIN=staging.runsheetops.com" in proc.stderr
    assert "docker build" not in log
    assert "register-task-definition" not in log


def test_b_deploy_ui_refused_with_https_and_no_domain(harness):
    proc, log = harness("deploy-ui", https=True)
    assert proc.returncode != 0
    assert "DOMAIN=staging.runsheetops.com" in proc.stderr
    assert "docker build" not in log
    assert "register-task-definition" not in log


def test_c_deploy_without_https_or_domain_proceeds(harness):
    proc, log = harness("deploy", https=False)
    assert proc.returncode == 97, proc.stderr
    assert "docker build" in log


def test_d_deploy_with_https_and_domain_proceeds(harness):
    proc, log = harness("deploy", https=True, domain="staging.runsheetops.com")
    assert proc.returncode == 97, proc.stderr
    assert "docker build" in log


def test_e_staging_domain_from_env_file(harness):
    env_file = "STAGING_DOMAIN=staging.runsheetops.com\n"
    proc, log = harness("deploy", https=True, env_file=env_file)
    assert proc.returncode == 97, proc.stderr
    assert "docker build" in log

    plan, _ = harness("plan", https=True)
    assert plan.returncode == 0, plan.stderr
    assert "api.staging.runsheetops.com" in plan.stdout


@pytest.mark.parametrize("command", ["plan", "status"])
def test_f_read_only_commands_work_without_domain(harness, command):
    proc, _ = harness(command, https=True)
    assert proc.returncode == 0, proc.stderr


def test_g_staging_domain_from_main_checkout(harness, fake_main):
    """OI-09: a worktree with no .env.staging uses the main checkout's copy."""
    (fake_main / "Runsheet-backend" / ".env.staging").write_text(
        "STAGING_DOMAIN=staging.runsheetops.com\n"
    )
    proc, log = harness(
        "deploy", https=True, extra_env={"STUB_GIT_COMMON_DIR": str(fake_main / ".git")}
    )
    assert proc.returncode == 97, proc.stderr
    assert "docker build" in log
    assert str(fake_main / "Runsheet-backend" / ".env.staging") in proc.stderr


def test_h_deploy_ui_refuses_to_drop_live_hubspot_guid(harness, fake_main):
    """OI-09: the live UI has a form GUID and none is resolvable, so no build."""
    proc, log = harness(
        "deploy-ui",
        https=True,
        domain="staging.runsheetops.com",
        extra_env={"STUB_LIVE_HUBSPOT": "1", "STUB_GIT_COMMON_DIR": str(fake_main / ".git")},
    )
    assert proc.returncode not in (0, 97), proc.stderr
    assert "HUBSPOT_FORM_GUID" in proc.stderr
    assert "runsheet/.env.local" in proc.stderr
    assert "docker build" not in log
    assert "register-task-definition" not in log


def test_i_deploy_ui_uses_main_checkout_hubspot_guid(harness, fake_main):
    (fake_main / "runsheet" / ".env.local").write_text("HUBSPOT_FORM_GUID=main-form-guid\n")
    proc, log = harness(
        "deploy-ui",
        https=True,
        domain="staging.runsheetops.com",
        extra_env={"STUB_LIVE_HUBSPOT": "1", "STUB_GIT_COMMON_DIR": str(fake_main / ".git")},
    )
    assert proc.returncode == 97, proc.stderr
    assert "ecs describe-task-definition" in log, "the live-GUID guard did not run"
    assert "docker build" in log
    assert "hubspot lead capture" in proc.stderr
