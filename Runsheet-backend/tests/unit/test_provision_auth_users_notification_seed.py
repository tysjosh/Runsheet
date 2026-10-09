"""B2: onboarding a tenant via the provisioning script seeds notification defaults.

There's no runtime tenant-create API; tenants are onboarded by provisioning
their ``auth_users`` rows with ``scripts/provision_auth_users.py``. That run
now seeds the default notification rules/templates once per tenant with a
successfully provisioned row.
"""

from __future__ import annotations

import pytest

import scripts.provision_auth_users as script
from auth.provisioner import AuthUserRow, ProvisionReport, ProvisionResult, ProvisionStatus

# pytest.ini sets asyncio_mode = auto, so the async tests need no mark.

ROWS = [
    AuthUserRow(id="1", email="a1@x.test", tenant_id="tenant-a", roles=("admin",)),
    AuthUserRow(id="2", email="a2@x.test", tenant_id="tenant-a", roles=("dispatcher",)),
    AuthUserRow(id="3", email="b1@x.test", tenant_id="tenant-b", roles=("admin",)),
    AuthUserRow(id="4", email="c1@x.test", tenant_id="tenant-c", roles=("admin",)),
]

REPORT = ProvisionReport(
    results=[
        ProvisionResult(email="a1@x.test", status=ProvisionStatus.CREATED),
        ProvisionResult(email="a2@x.test", status=ProvisionStatus.UPDATED),
        ProvisionResult(email="b1@x.test", status=ProvisionStatus.CREATED),
        ProvisionResult(email="c1@x.test", status=ProvisionStatus.FAILED, error="boom"),
    ]
)


@pytest.fixture(autouse=True)
def _fake_provision_all(monkeypatch):
    async def _provision_all(rows, *, admin=None, store=None):
        return REPORT

    monkeypatch.setattr(script, "provision_all", _provision_all)


async def _rows():
    return ROWS


class _Recorder:
    def __init__(self, fail_for=()):
        self.calls: list[str] = []
        self.fail_for = set(fail_for)

    async def __call__(self, tenant_id: str) -> None:
        self.calls.append(tenant_id)
        if tenant_id in self.fail_for:
            raise RuntimeError("store down")


async def _run(**kwargs):
    return await script.provision_auth_users(
        initialize_sdk=False,
        skip_role_creation=True,
        rows_reader=_rows,
        **kwargs,
    )


async def test_seeds_once_per_tenant_with_a_provisioned_row():
    recorder = _Recorder()

    report = await _run(notification_seeder=recorder)

    assert report is REPORT
    # tenant-a once despite two rows; tenant-c only had a failed row.
    assert recorder.calls == ["tenant-a", "tenant-b"]


async def test_seed_can_be_skipped():
    recorder = _Recorder()

    await _run(notification_seeder=recorder, seed_notifications=False)

    assert recorder.calls == []


async def test_a_seed_failure_does_not_stop_other_tenants_or_provisioning():
    recorder = _Recorder(fail_for={"tenant-a"})

    report = await _run(notification_seeder=recorder)

    assert report is REPORT
    assert recorder.calls == ["tenant-a", "tenant-b"]


async def test_roles_only_run_does_not_seed():
    recorder = _Recorder()

    report = await script.provision_auth_users(
        initialize_sdk=False,
        roles_only=True,
        role_creator=lambda role: _async_true(),
        notification_seeder=recorder,
    )

    assert report is None
    assert recorder.calls == []


async def _async_true() -> bool:
    return True


def test_cli_flag_disables_the_seed(monkeypatch):
    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return ProvisionReport()

    monkeypatch.setattr(script, "provision_auth_users", _fake)

    assert script.main(["--skip-notification-seed"]) == 0
    assert captured["seed_notifications"] is False
