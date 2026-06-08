"""
Seed deterministic K-Factor calibration demo data for a tenant.

Creates one propane customer tank, a run of daily ``weather_observations``
(constant HDD so predictions are easy to reason about), and four
``delivered`` fuel orders spaced ~3 weeks apart. The K-Factor variance
pipeline (with the EsHddProvider fallback) can then compute predicted-vs-
actual variance for the three scoreable deliveries.

With K=2.5, HDD=20/day and ~21-day intervals: accumulated HDD ≈ 420 and
predicted ≈ 1050 gal. Actual gallons are chosen to land one delivery on
target, one well over (flagged), and one modestly under (within ±15%).

Idempotent: re-running overwrites the same fixed document ids.

Usage:
    ENVIRONMENT=development ./venv/bin/python -m scripts.seed_kfactor_demo
    ENVIRONMENT=development ./venv/bin/python -m scripts.seed_kfactor_demo --tenant demo-tenant
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time, timedelta, timezone

TANK_ID = "TANK-PROPANE-DEMO"
CUSTOMER_ID = "CUST-PROPANE-DEMO"
ZIP = "60601"
K_FACTOR = 2.5
HDD_PER_DAY = 20.0

# (order_id, delivery_date, gallons_requested)
DELIVERIES = [
    ("ORD-PROPANE-DEMO-1", date(2026, 3, 15), 1000.0),  # first → unscored (no prior)
    ("ORD-PROPANE-DEMO-2", date(2026, 4, 5), 1050.0),   # ~0% variance
    ("ORD-PROPANE-DEMO-3", date(2026, 4, 26), 1300.0),  # ~+24% → flagged
    ("ORD-PROPANE-DEMO-4", date(2026, 5, 17), 945.0),   # ~-10% within threshold
]

WEATHER_FROM = date(2026, 3, 1)
WEATHER_TO = date(2026, 5, 31)


def _iso(d: date) -> str:
    return datetime.combine(d, time(12, 0), tzinfo=timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="demo-tenant")
    args = parser.parse_args()
    tenant_id = args.tenant

    from services.elasticsearch_service import elasticsearch_service as es
    from commerce.services.commerce_es_mappings import CUSTOMERS_CURRENT_INDEX
    from fuel.services.fuel_ops_es_mappings import (
        CUSTOMER_TANKS_INDEX,
        WEATHER_OBSERVATIONS_INDEX,
    )
    from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

    now = _now()

    # 0. Owning customer record so the tank's "Customer" link resolves in
    #    Commerce (GET /api/commerce/customers/{id}). Without this the
    #    K-Factor → customer drill-in 404s.
    customer = {
        "customer_id": CUSTOMER_ID,
        "tenant_id": tenant_id,
        "display_name": "Propane Demo Customer",
        "legal_name": "Propane Demo Customer LLC",
        "primary_email": "ops@propane-demo.example",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    await es.index_document(CUSTOMERS_CURRENT_INDEX, CUSTOMER_ID, customer)
    # Mirror to the Postgres source-of-truth so the customer resolves even when
    # commerce reads are cut over to Postgres (best-effort / idempotent; a no-op
    # when the persistence layer is disabled).
    try:
        from commerce.services.commerce_persistence_bridge import (
            mirror_customer_create,
        )

        await mirror_customer_create(customer)
    except Exception as exc:  # pragma: no cover - best-effort seed mirror
        print(f"  (warn) customer Postgres mirror skipped: {exc}")
    print(f"seeded customer {CUSTOMER_ID}")

    # 1. Propane customer tank.
    tank = {
        "customer_tank_id": TANK_ID,
        "tenant_id": tenant_id,
        "customer_id": CUSTOMER_ID,
        "customer_type": "keep_full",
        "fuel_type": "propane",
        "fuel_product_code": "PROPANE",
        "capacity_gallons": 2000.0,
        "current_level_gallons": 600.0,
        "location_lat": 41.8853,
        "location_lon": -87.6219,
        "zip_code": ZIP,
        "k_factor": K_FACTOR,
        "use_case": "residential_heat",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    await es.index_document(CUSTOMER_TANKS_INDEX, TANK_ID, tank)
    print(f"seeded customer_tank {TANK_ID} (k_factor={K_FACTOR}, zip={ZIP})")

    # 2. Daily weather observations (constant HDD).
    n_days = (WEATHER_TO - WEATHER_FROM).days + 1
    for i in range(n_days):
        d = WEATHER_FROM + timedelta(days=i)
        doc_id = f"wx_{ZIP}_{d.isoformat()}"
        await es.index_document(
            WEATHER_OBSERVATIONS_INDEX,
            doc_id,
            {
                "tenant_id": tenant_id,
                "zip_code": ZIP,
                "date": d.isoformat(),
                "avg_temp_f": 65.0 - HDD_PER_DAY,
                "hdd": HDD_PER_DAY,
                "provider": "seed",
                "retrieved_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
    print(f"seeded {n_days} weather_observations for zip {ZIP} (hdd={HDD_PER_DAY}/day)")

    # 3. Delivered fuel orders. Write via the raw ES client so the historical
    #    delivery dates survive — es.index_document() force-stamps updated_at
    #    to "now", which would make every delivery look simultaneous and
    #    starve the prior-delivery lookup the variance window needs.
    for order_id, d, gallons in DELIVERIES:
        es.client.index(
            index=FUEL_ORDERS_CURRENT_INDEX,
            id=order_id,
            document={
                "order_id": order_id,
                "tenant_id": tenant_id,
                "customer_id": CUSTOMER_ID,
                "customer_tank_id": TANK_ID,
                "product_code": "PROPANE",
                "gallons_requested": gallons,
                "status": "delivered",
                "created_at": _iso(d),
                "updated_at": _iso(d),
            },
        )
    print(f"seeded {len(DELIVERIES)} delivered orders for {TANK_ID}")

    # Make everything searchable immediately.
    for index in (
        CUSTOMERS_CURRENT_INDEX,
        CUSTOMER_TANKS_INDEX,
        WEATHER_OBSERVATIONS_INDEX,
        FUEL_ORDERS_CURRENT_INDEX,
    ):
        try:
            es.client.indices.refresh(index=index)
        except Exception:
            pass

    print(
        "\nDone. Open K-Factor (Fuel Ops → K-Factor), click 'Consumption' on "
        f"tank {TANK_ID} to see the predicted-vs-actual history."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
