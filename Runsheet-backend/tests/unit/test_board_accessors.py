"""Small public accessors the Dispatch Board needs (dispatch-board D8, K2.5, K8.4).

* ``driver.api.work_endpoints.get_work_service()`` returns the module's
  ``DriverWorkService`` (for ``invalidate``) or ``None``, and never raises.
* ``fuel.services.driver_daily_reset.get_tenant_timezone`` is the public name
  of the tenant time-zone rule: ``America/Chicago`` unless the settings carry a
  ``timezone``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from driver.api import work_endpoints
from driver.services.work_service import DriverWorkService
from fuel.services.driver_daily_reset import _get_tenant_timezone, get_tenant_timezone


@pytest.fixture
def restore_work_endpoints():
    saved = (
        work_endpoints._es_service,
        work_endpoints._order_repository,
        work_endpoints._job_service,
        work_endpoints._redis_client,
        work_endpoints._work_service,
    )
    yield
    (
        work_endpoints._es_service,
        work_endpoints._order_repository,
        work_endpoints._job_service,
        work_endpoints._redis_client,
        work_endpoints._work_service,
    ) = saved


def test_get_work_service_is_none_before_configure(restore_work_endpoints):
    work_endpoints.configure_work_endpoints()
    assert work_endpoints.get_work_service() is None


def test_get_work_service_returns_the_configured_service(restore_work_endpoints):
    work_endpoints.configure_work_endpoints(es_service=object())
    service = work_endpoints.get_work_service()
    assert isinstance(service, DriverWorkService)
    assert service is work_endpoints._work_service
    # Read at call time: a later configure is picked up.
    work_endpoints.configure_work_endpoints(es_service=object())
    assert work_endpoints.get_work_service() is not service


def test_tenant_timezone_defaults_to_chicago():
    assert get_tenant_timezone("tenant-1") == "America/Chicago"
    assert get_tenant_timezone("tenant-1", SimpleNamespace()) == "America/Chicago"


def test_tenant_timezone_honours_settings():
    assert get_tenant_timezone("tenant-1", SimpleNamespace(timezone="America/Denver")) == "America/Denver"


def test_public_alias_is_the_same_rule():
    assert get_tenant_timezone is _get_tenant_timezone
