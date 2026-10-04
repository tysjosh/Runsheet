"""Compliance crons get the activity log after boot (staging F9).

``_BOOT_ORDER`` runs ``compliance`` before ``agents``, so the four compliance
cron agents were constructed with ``activity_log_service=None``. The first
cycle with detections then raised ``AttributeError: 'NoneType' object has no
attribute 'log_monitoring_cycle'`` (logged as "Monitor cycle error" every
day), monitoring cycles were never tenant-scoped, and ``/api/agent/health``
did not list the crons at all.

This reproduces the boot order with the real cron classes: build them exactly
as ``bootstrap/compliance.py`` does, let ``bootstrap.agents`` adopt them, then
run one real loop iteration with detections for tenant ``t1``.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bootstrap.compliance as compliance_boot
from Agents.autonomous.asset_cert_expiry_cron_agent import AssetCertExpiryCronAgent
from Agents.autonomous.driver_expiry_cron_agent import DriverExpiryCronAgent
from Agents.autonomous.dyed_diesel_cert_expiry_cron_agent import (
    DyedDieselCertExpiryCronAgent,
)
from Agents.autonomous.meter_calibration_cron_agent import MeterCalibrationCronAgent

CRON_IDS = {
    "driver_expiry_cron_agent",
    "asset_cert_expiry_cron_agent",
    "meter_calibration_cron_agent",
    "dyed_diesel_cert_expiry_cron_agent",
}


def _build_like_compliance_bootstrap(cls, **extra):
    # compliance runs before agents: none of these exist in the container yet.
    return cls(
        es_service=MagicMock(),
        activity_log_service=None,
        ws_manager=None,
        confirmation_protocol=None,
        **extra,
    )


@pytest.fixture
def crons(monkeypatch):
    built = {
        "_driver_expiry_cron_agent": _build_like_compliance_bootstrap(
            DriverExpiryCronAgent
        ),
        "_asset_cert_expiry_cron_agent": _build_like_compliance_bootstrap(
            AssetCertExpiryCronAgent
        ),
        "_meter_calibration_cron_agent": _build_like_compliance_bootstrap(
            MeterCalibrationCronAgent
        ),
        "_dyed_diesel_cert_expiry_cron_agent": _build_like_compliance_bootstrap(
            DyedDieselCertExpiryCronAgent, signal_bus=None
        ),
    }
    for name, agent in built.items():
        monkeypatch.setattr(compliance_boot, name, agent)
    return built


def _app():
    return SimpleNamespace(
        state=SimpleNamespace(
            autonomous_agents={}, overlay_agents={}, mvp_agents={}
        )
    )


def _spy_log():
    log = MagicMock()
    log.log_monitoring_cycle = AsyncMock(return_value="log-1")
    return log


def test_compliance_cron_agents_lists_the_built_crons(crons):
    assert {a.agent_id for a in compliance_boot.compliance_cron_agents()} == CRON_IDS


def test_compliance_cron_agents_skips_failed_wiring(crons, monkeypatch):
    monkeypatch.setattr(compliance_boot, "_meter_calibration_cron_agent", None)
    ids = {a.agent_id for a in compliance_boot.compliance_cron_agents()}
    assert ids == CRON_IDS - {"meter_calibration_cron_agent"}


def test_adoption_binds_services_and_registers_for_health(crons):
    from bootstrap.agents import adopt_compliance_cron_agents

    app = _app()
    log, ws, protocol = _spy_log(), MagicMock(), MagicMock()
    existing_ws = MagicMock()
    crons["_asset_cert_expiry_cron_agent"]._ws = existing_ws

    adopt_compliance_cron_agents(app, log, ws, protocol)

    assert set(app.state.autonomous_agents) == CRON_IDS
    for agent in crons.values():
        assert agent._activity_log is log
        assert agent._confirmation_protocol is protocol
        assert app.state.autonomous_agents[agent.agent_id] is agent
    # Only fills what is still None.
    assert crons["_asset_cert_expiry_cron_agent"]._ws is existing_ws
    assert crons["_driver_expiry_cron_agent"]._ws is ws


async def test_cron_cycle_after_adoption_logs_tenant_scoped_cycle(crons, caplog):
    from agent_endpoints import get_agent_health
    from bootstrap.agents import adopt_compliance_cron_agents

    app = _app()
    log = _spy_log()
    adopt_compliance_cron_agents(app, log, MagicMock(), MagicMock())

    agent = crons["_driver_expiry_cron_agent"]
    agent.poll_interval = 0  # one iteration, no daily sleep

    svc = MagicMock()
    svc.check_expiry_alerts = AsyncMock(return_value=[{"driver_id": "D1"}, {"driver_id": "D2"}])
    svc.auto_suspend_expired_drivers = AsyncMock(return_value=[])

    async def _overdue(tenant_id):
        agent._running = False  # stop after this cycle
        return []

    svc.check_drug_test_overdue = AsyncMock(side_effect=_overdue)

    agent._running = True
    with patch.object(agent, "_discover_tenants", AsyncMock(return_value=["t1"])), \
            patch(
                "Agents.autonomous.driver_expiry_cron_agent.DriverQualificationService",
                return_value=svc,
            ), \
            patch("persistence.leader_election.is_sweep_leader", return_value=True), \
            caplog.at_level(logging.WARNING, logger="agent.driver_expiry_cron_agent"):
        await agent._run_loop()

    log.log_monitoring_cycle.assert_awaited_once()
    call = log.log_monitoring_cycle.call_args
    assert call.args[0] == "driver_expiry_cron_agent"
    assert call.args[1] == 2 and call.args[2] == 0
    assert call.kwargs["tenant_id"] == "t1"
    assert not any("Monitor cycle error" in r.getMessage() for r in caplog.records)

    health = await get_agent_health(SimpleNamespace(app=app))
    assert CRON_IDS <= set(health["agents"])
    assert health["agents"]["driver_expiry_cron_agent"]["type"] == "autonomous"
