"""The browser can send ``Idempotency-Key`` cross-origin (design §14; PAY-13)."""
from __future__ import annotations

from config.cors import get_cors_origins


def test_idempotency_key_preflight(client):
    origins = [o for o in get_cors_origins() if o != "*"]
    assert origins, "no CORS origin configured for the test environment"
    resp = client.options(
        "/api/portal/invoices/x/payments",
        headers={
            "Origin": origins[0],
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,content-type",
        },
    )
    assert resp.status_code == 200
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in [h.strip() for h in allowed.split(",")]
