"""Portal tanks (design §7; TNK-1, AC15).

[real-auth] ``main.app`` with the :class:`OrderHarness` store.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from tests.portal.conftest import CUSTOMER_A, CUSTOMER_B, T1, T2, CUSTOMER_C, call, iso


@pytest.fixture
def h(portal_on, portal_orders, cA, monkeypatch):
    from portal.services import projection

    monkeypatch.setattr(projection, "now_utc", lambda: portal_orders.now)
    return portal_orders


def _delivery(h, order_id, *, days_ago, tank="QA-TANK-1", gallons=120.0, ticket="T-1"):
    created = h.now - timedelta(days=days_ago)
    h.add_order(
        T1, CUSTOMER_A, order_id, customer_tank_id=tank, status="delivered",
        created_at=created,
        delivery_result={
            "pod_id": f"pod-{order_id}", "actual_gallons": gallons,
            "actual_gallons_source": "meter", "delivered_at": iso(created + timedelta(hours=5)),
            "recipient_name": "Pat Recipient", "driver_id": "QA-DRV-9",
            "geotag": {"lat": 41.0, "lon": -88.0}, "ticket_number": ticket,
        },
    )


def test_list_active_only_with_forecast_and_stale_rules(client, h, cA):
    now = h.now
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-1", capacity=500, level=125, external_tank_id="Shop",
               last_reading_at=now - timedelta(days=7) + timedelta(seconds=1))
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-2", last_reading_at=now - timedelta(days=7, seconds=1))
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-3", last_reading_at=None)
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-OFF", status="inactive")
    h.add_tank(T1, CUSTOMER_B, "QA-TANK-B")
    h.add_forecast(T1, CUSTOMER_A, "QA-TANK-1", timestamp=now - timedelta(hours=10), hours=200)
    h.add_forecast(T1, CUSTOMER_A, "QA-TANK-1", timestamp=now - timedelta(hours=2), hours=50)

    resp = call(client, "GET", "/api/portal/tanks", cA)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "next_cursor", "limit", "request_id"}
    tanks = {t["customer_tank_id"]: t for t in body["data"]}
    assert set(tanks) == {"QA-TANK-1", "QA-TANK-2", "QA-TANK-3"}

    t1 = tanks["QA-TANK-1"]
    assert t1["label"] == "Shop"
    assert t1["percent_full"] == 25.0
    assert t1["reading_stale"] is False  # 7 d - 1 s
    assert t1["forecast"]["days_to_runout"] == 2  # newest forecast wins (50 h)
    assert t1["next_delivery"] is None
    assert tanks["QA-TANK-2"]["reading_stale"] is True  # 7 d + 1 s
    assert tanks["QA-TANK-3"]["reading_stale"] is True  # never read
    assert tanks["QA-TANK-2"]["forecast"] is None
    assert tanks["QA-TANK-2"]["label"] == "Tank …TANK-2"
    for tank in tanks.values():
        for key in ("location_lat", "location_lon", "k_factor", "source_system", "zip_code"):
            assert key not in tank


def test_next_delivery_earliest_open_window(client, h, cA):
    now = h.now
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-1")
    h.add_order(T1, CUSTOMER_A, "QA-ORD-LATE", customer_tank_id="QA-TANK-1", status="scheduled",
                window_start=now + timedelta(days=5))
    h.add_order(T1, CUSTOMER_A, "QA-ORD-SOON", customer_tank_id="QA-TANK-1", status="dispatched",
                window_start=now + timedelta(days=2))
    # Earlier windows that are not open deliveries.
    h.add_order(T1, CUSTOMER_A, "QA-ORD-HELD", customer_tank_id="QA-TANK-1", status="on_hold",
                hold_reason=PORTAL_REVIEW_HOLD_REASON, channel="web_portal",
                window_start=now + timedelta(hours=3))
    h.add_order(T1, CUSTOMER_A, "QA-ORD-DONE", customer_tank_id="QA-TANK-1", status="cancelled",
                window_start=now + timedelta(hours=1))

    for path in ("/api/portal/tanks", "/api/portal/tanks/QA-TANK-1"):
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        tank = data[0] if isinstance(data, list) else data
        nd = tank["next_delivery"]
        assert nd["order_id"] == "QA-ORD-SOON", path
        assert nd["status_label"] == "Out for delivery"


def test_history_24_months_paginated(client, h, cA):
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-1")
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-2")
    for i, days in enumerate((1, 30, 200, 729)):
        _delivery(h, f"QA-ORD-D{i}", days_ago=days, ticket=f"T-{i}")
    _delivery(h, "QA-ORD-TOO-OLD", days_ago=731)
    _delivery(h, "QA-ORD-OTHER-TANK", days_ago=2, tank="QA-TANK-2")
    h.add_order(T1, CUSTOMER_A, "QA-ORD-OPEN", customer_tank_id="QA-TANK-1", status="scheduled")

    seen, cursor = [], None
    for _ in range(5):
        path = "/api/portal/tanks/QA-TANK-1/deliveries?limit=2"
        if cursor:
            path += f"&cursor={cursor}"
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["limit"] == 2
        assert len(body["data"]) <= 2
        seen += body["data"]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert [d["order_id"] for d in seen] == ["QA-ORD-D0", "QA-ORD-D1", "QA-ORD-D2", "QA-ORD-D3"]
    first = seen[0]
    assert set(first) == {"order_id", "delivered_at", "delivered_gallons", "product_code", "ticket_number"}
    assert first["delivered_gallons"] == 120.0 and first["ticket_number"] == "T-0"

    bad = call(client, "GET", "/api/portal/tanks/QA-TANK-1/deliveries?cursor=%%%", cA)
    assert bad.status_code == 422
    too_big = call(client, "GET", "/api/portal/tanks/QA-TANK-1/deliveries?limit=51", cA)
    assert too_big.status_code == 422


@pytest.mark.parametrize("suffix", ["", "/deliveries"])
def test_foreign_or_inactive_tank_404(client, h, cA, suffix):
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-OFF", status="inactive")
    h.add_tank(T1, CUSTOMER_B, "QA-TANK-B")
    h.add_tank(T2, CUSTOMER_C, "QA-TANK-C")
    for tank_id in ("QA-TANK-OFF", "QA-TANK-B", "QA-TANK-C", "QA-TANK-NONE", "bad%20id"):
        resp = call(client, "GET", f"/api/portal/tanks/{tank_id}{suffix}", cA)
        assert resp.status_code == 404, tank_id
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"
