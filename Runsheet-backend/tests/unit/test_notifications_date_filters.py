"""B5: notification date filters are typed, so an invalid date is a 422.

Before, ``start_date``/``end_date`` were ``Optional[str]`` passed straight into
the range query, so ``start_date=not-a-date`` returned 200 with a filter that
silently matched nothing (or everything).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import notifications.api.endpoints as endpoints
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


class _FakeNotificationService:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.summary_calls: list[dict] = []

    async def list_notifications(self, *, tenant_id, filters, page, size):
        self.list_calls.append(dict(filters))
        return {"items": [], "total": 0, "page": page, "size": size}

    async def get_summary(self, *, tenant_id, start_date=None, end_date=None):
        self.summary_calls.append({"start_date": start_date, "end_date": end_date})
        return {"by_type": {}, "by_channel": {}, "by_status": {}}


@pytest.fixture
def service(monkeypatch):
    fake = _FakeNotificationService()
    monkeypatch.setattr(endpoints, "_notification_service", fake)
    return fake


@pytest.fixture
def client(service):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=["dispatcher"],
    )
    return TestClient(app)


ROUTES = ["/api/notifications", "/api/notifications/summary"]


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("param", ["start_date", "end_date"])
def test_invalid_date_is_422(client, service, path, param):
    response = client.get(path, params={param: "not-a-date"})

    assert response.status_code == 422
    assert service.list_calls == [] and service.summary_calls == []


def test_list_passes_iso_strings_downstream(client, service):
    response = client.get(
        "/api/notifications",
        params={"start_date": "2026-10-01T00:00:00Z", "end_date": "2026-10-02T12:30:00Z"},
    )

    assert response.status_code == 200
    assert service.list_calls == [
        {"start_date": "2026-10-01T00:00:00+00:00", "end_date": "2026-10-02T12:30:00+00:00"}
    ]


def test_summary_passes_iso_strings_downstream(client, service):
    response = client.get(
        "/api/notifications/summary", params={"start_date": "2026-10-01T00:00:00Z"}
    )

    assert response.status_code == 200
    assert service.summary_calls == [
        {"start_date": "2026-10-01T00:00:00+00:00", "end_date": None}
    ]


@pytest.mark.parametrize("path", ROUTES)
def test_date_only_value_is_still_accepted(client, service, path):
    # The UI's <input type="date"> sends YYYY-MM-DD.
    response = client.get(path, params={"start_date": "2026-10-01"})

    assert response.status_code == 200
    calls = service.list_calls or service.summary_calls
    assert calls[0]["start_date"] == "2026-10-01T00:00:00"
