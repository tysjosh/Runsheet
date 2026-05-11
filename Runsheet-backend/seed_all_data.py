#!/usr/bin/env python3
"""
Standalone seed script for ALL Elasticsearch indices needed by the Runsheet frontend.

Usage:
    SEED_TENANT_ID=tenant-a python seed_all_data.py
    SEED_TENANT_ID=tenant-a python seed_all_data.py --force

Uses the existing elasticsearch_service singleton and the sync client.index() / client.bulk()
methods directly.
"""

import sys
import os
import uuid
import random
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import the shared ES singleton
# ---------------------------------------------------------------------------
from services.elasticsearch_service import elasticsearch_service

ES = elasticsearch_service.client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TENANT = os.environ.get("SEED_TENANT_ID", "").strip()
SCHEMA_VERSION = "1.0"

US_CITIES = {
    "Houston":   {"lat": 29.7604, "lon": -95.3698},
    "Dallas":    {"lat": 32.7767, "lon": -96.7970},
    "Chicago":   {"lat": 41.8781, "lon": -87.6298},
    "Denver":    {"lat": 39.7392, "lon": -104.9903},
    "Atlanta":   {"lat": 33.7490, "lon": -84.3880},
    "Phoenix":   {"lat": 33.4484, "lon": -112.0740},
    "Detroit":   {"lat": 42.3314, "lon": -83.0458},
    "Charlotte": {"lat": 35.2271, "lon": -80.8431},
}

CITY_NAMES = list(US_CITIES.keys())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(days: float = 0, hours: float = 0, minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours, minutes=minutes)).isoformat()


def _future(days: float = 0, hours: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _geo(city: str) -> dict:
    c = US_CITIES[city]
    return {"lat": c["lat"] + random.uniform(-0.02, 0.02),
            "lon": c["lon"] + random.uniform(-0.02, 0.02)}


def _index_count(index: str) -> int:
    """Return document count for an index, 0 if it doesn't exist."""
    try:
        if not ES.indices.exists(index=index):
            return 0
        resp = ES.count(index=index)
        return resp.get("count", 0)
    except Exception:
        return 0


def _bulk(actions: list):
    """Execute a bulk request."""
    if not actions:
        return
    resp = ES.bulk(body=actions, refresh=True)
    if resp.get("errors"):
        for item in resp["items"]:
            for op, detail in item.items():
                if detail.get("error"):
                    logger.error(f"  Bulk error: {detail['error']}")


def _single(index: str, doc_id: str, body: dict):
    ES.index(index=index, id=doc_id, body=body, refresh=True)


# ---------------------------------------------------------------------------
# 0. trucks (assets)
# ---------------------------------------------------------------------------
def seed_trucks(force: bool = False):
    index = "trucks"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    trucks = [
        ("TRK-001", "TRK-001", "vehicle", "truck",    "Peterbilt 579",     "DRV-001", "Mike Johnson",     "Houston",   "on_time"),
        ("TRK-002", "TRK-002", "vehicle", "truck",    "Kenworth T680",     "DRV-002", "Sarah Williams",   "Dallas",    "on_time"),
        ("TRK-003", "TRK-003", "vehicle", "truck",    "Freightliner Cascadia", "DRV-003", "James Rodriguez", "Chicago",   "delayed"),
        ("TRK-004", "TRK-004", "vehicle", "truck",    "Mack Anthem",       "DRV-004", "Emily Chen",       "Denver",    "on_time"),
        ("TRK-005", "TRK-005", "vehicle", "truck",    "Volvo VNL 860",     "DRV-005", "David Thompson",   "Atlanta",   "on_time"),
        ("TRK-006", "TRK-006", "vehicle", "truck",    "International LT",  "DRV-006", "Maria Garcia",     "Phoenix",   "delayed"),
        ("TRK-007", "TRK-007", "vehicle", "van",      "Ford F-750",        "DRV-007", "Robert Kim",       "Detroit",   "on_time"),
        ("TRK-008", "TRK-008", "vehicle", "van",      "RAM 5500",          "DRV-008", "Jennifer Davis",   "Charlotte", "on_time"),
        ("TNK-001", "TNK-001", "vehicle", "tanker",   "Peterbilt 567 Tanker", "DRV-009", "Mike Johnson",  "Houston",   "on_time"),
        ("TNK-002", "TNK-002", "vehicle", "tanker",   "Kenworth T880 Tanker", "DRV-010", "Emily Chen",    "Denver",    "on_time"),
    ]

    actions = []
    for tid, plate, atype, subtype, model, drv_id, drv_name, city, status in trucks:
        dest_city = random.choice([c for c in CITY_NAMES if c != city])
        doc = {
            "truck_id": tid,
            "plate_number": plate,
            "asset_type": atype,
            "asset_subtype": subtype,
            "asset_name": f"{model} ({plate})",
            "equipment_model": model,
            "driver_id": drv_id,
            "driver_name": drv_name,
            "status": status,
            "current_location": {
                "id": f"LOC-{city[:3].upper()}",
                "name": city,
                "type": "city",
                "coordinates": _geo(city),
                "address": f"{random.randint(1,200)} Industrial Blvd, {city}",
            },
            "destination": {
                "id": f"LOC-{dest_city[:3].upper()}",
                "name": dest_city,
                "type": "city",
                "coordinates": _geo(dest_city),
                "address": f"{random.randint(1,200)} Terminal Dr, {dest_city}",
            },
            "route": {
                "id": f"RT-{city[:3]}-{dest_city[:3]}".upper(),
                "distance": round(random.uniform(100, 900), 1),
                "estimated_duration": random.randint(120, 720),
                "actual_duration": random.randint(130, 800),
            },
            "estimated_arrival": _future(hours=random.uniform(2, 48)),
            "last_update": _ago(minutes=random.randint(5, 120)),
            "cargo": {
                "type": random.choice(["fuel", "general", "perishable", "equipment"]),
                "weight": round(random.uniform(5000, 30000), 1),
                "volume": round(random.uniform(20, 80), 1),
                "priority": random.choice(["normal", "high", "urgent"]),
            },
            "created_at": _ago(days=random.randint(30, 365)),
            "updated_at": _now(),
            "tenant_id": TENANT,
        }
        actions.append({"index": {"_index": index, "_id": tid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(trucks)} docs → {index}")


# ---------------------------------------------------------------------------
# 1. riders_current
# ---------------------------------------------------------------------------
def seed_riders(force: bool = False):
    index = "riders_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    rider_names = [
        ("RDR-001", "Mike Johnson",     "active",  "Houston",   3, 7),
        ("RDR-002", "Sarah Williams",   "active",  "Dallas",    2, 5),
        ("RDR-003", "James Rodriguez",  "active",  "Chicago",   1, 4),
        ("RDR-004", "Emily Chen",       "active",  "Denver",    2, 6),
        ("RDR-005", "David Thompson",   "idle",    "Atlanta",   0, 3),
        ("RDR-006", "Maria Garcia",     "idle",    "Phoenix",   0, 2),
        ("RDR-007", "Robert Kim",       "offline", "Detroit",   0, 0),
        ("RDR-008", "Jennifer Davis",   "offline", "Charlotte", 0, 1),
    ]

    actions = []
    for rid, name, status, city, active, completed in rider_names:
        avail = "available" if status == "active" else ("break" if status == "idle" else "offline")
        doc = {
            "rider_id": rid,
            "rider_name": name,
            "status": status,
            "tenant_id": TENANT,
            "availability": avail,
            "last_seen": _ago(minutes=random.randint(1, 120)) if status != "offline" else _ago(hours=random.randint(6, 48)),
            "current_location": _geo(city),
            "active_shipment_count": active,
            "completed_today": completed,
            "last_event_timestamp": _ago(minutes=random.randint(5, 300)),
            "source_schema_version": SCHEMA_VERSION,
            "trace_id": _uid(),
            "ingested_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": rid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(rider_names)} docs → {index}")



# ---------------------------------------------------------------------------
# 2. shipments_current
# ---------------------------------------------------------------------------
def seed_shipments(force: bool = False):
    index = "shipments_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    rider_ids = [f"RDR-{i:03d}" for i in range(1, 9)]

    shipments = []
    # 5 delivered
    for i in range(1, 6):
        shipments.append(("delivered", None, random.choice(rider_ids)))
    # 4 in_transit
    for i in range(6, 10):
        shipments.append(("in_transit", None, random.choice(rider_ids[:4])))
    # 3 failed
    failure_reasons = ["customer_unavailable", "address_not_found", "vehicle_breakdown"]
    for i, reason in zip(range(10, 13), failure_reasons):
        shipments.append(("failed", reason, random.choice(rider_ids)))
    # 2 pending
    for i in range(13, 15):
        shipments.append(("pending", None, None))
    # 1 returned
    shipments.append(("returned", "customer_refused", random.choice(rider_ids)))

    actions = []
    for idx, (status, failure, rider) in enumerate(shipments, start=1):
        sid = f"SHP-{idx:03d}"
        origin_city = random.choice(CITY_NAMES)
        dest_city = random.choice([c for c in CITY_NAMES if c != origin_city])
        created = _ago(days=random.randint(1, 14))
        doc = {
            "shipment_id": sid,
            "status": status,
            "tenant_id": TENANT,
            "rider_id": rider,
            "origin": origin_city,
            "destination": dest_city,
            "created_at": created,
            "updated_at": _ago(hours=random.randint(0, 48)),
            "estimated_delivery": _future(days=random.randint(0, 3)),
            "current_location": _geo(dest_city if status == "delivered" else origin_city),
            "failure_reason": failure,
            "last_event_timestamp": _ago(hours=random.randint(0, 72)),
            "source_schema_version": SCHEMA_VERSION,
            "trace_id": _uid(),
            "ingested_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": sid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(shipments)} docs → {index}")


# ---------------------------------------------------------------------------
# 3. shipment_events
# ---------------------------------------------------------------------------
def seed_shipment_events(force: bool = False):
    index = "shipment_events"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    event_flows = {
        "delivered": ["shipment_created", "shipment_assigned", "shipment_picked_up",
                      "shipment_in_transit", "shipment_delivered"],
        "in_transit": ["shipment_created", "shipment_assigned", "shipment_picked_up",
                       "shipment_in_transit"],
        "failed": ["shipment_created", "shipment_assigned", "shipment_picked_up",
                    "shipment_failed"],
        "pending": ["shipment_created"],
        "returned": ["shipment_created", "shipment_assigned", "shipment_picked_up",
                     "shipment_in_transit", "shipment_failed"],
    }

    # Build shipment statuses list matching seed_shipments order
    statuses = (["delivered"] * 5 + ["in_transit"] * 4 + ["failed"] * 3
                + ["pending"] * 2 + ["returned"] * 1)

    actions = []
    for s_idx, status in enumerate(statuses, start=1):
        sid = f"SHP-{s_idx:03d}"
        flow = event_flows[status]
        # Use 3-4 events per shipment (trim flow if needed, but keep at least 3)
        events_to_use = flow[:random.randint(3, len(flow))] if len(flow) >= 3 else flow
        base_time = datetime.now(timezone.utc) - timedelta(days=random.randint(2, 10))

        for e_idx, etype in enumerate(events_to_use):
            eid = f"EVT-{sid}-{e_idx:02d}"
            ts = (base_time + timedelta(hours=e_idx * random.randint(1, 6))).isoformat()
            city = random.choice(CITY_NAMES)
            doc = {
                "event_id": eid,
                "shipment_id": sid,
                "event_type": etype,
                "tenant_id": TENANT,
                "event_timestamp": ts,
                "event_payload": [],
                "location": _geo(city),
                "source_schema_version": SCHEMA_VERSION,
                "trace_id": _uid(),
                "ingested_at": _now(),
            }
            actions.append({"index": {"_index": index, "_id": eid}})
            actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(actions) // 2} events → {index}")


# ---------------------------------------------------------------------------
# 4. jobs_current
# ---------------------------------------------------------------------------
def seed_jobs(force: bool = False):
    index = "jobs_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    job_types = ["delivery", "pickup", "cargo_transport", "fuel_delivery"]
    priorities = ["low", "medium", "high", "critical"]
    creators = ["dispatcher-01", "dispatcher-02", "auto-scheduler"]

    # status distribution: 3 scheduled, 3 assigned, 3 in_progress, 2 completed, 1 failed
    status_list = (["scheduled"] * 3 + ["assigned"] * 3 + ["in_progress"] * 3
                   + ["completed"] * 2 + ["failed"] * 1)

    # Mark jobs 2, 5, 9 as delayed (indices 1, 4, 8 in 0-based)
    delayed_indices = {1, 4, 8}

    actions = []
    for idx, status in enumerate(status_list):
        jid = f"JOB-{idx + 1:03d}"
        jtype = job_types[idx % len(job_types)]
        origin_city = random.choice(CITY_NAMES)
        dest_city = random.choice([c for c in CITY_NAMES if c != origin_city])
        is_delayed = idx in delayed_indices
        created = _ago(days=random.randint(1, 20))
        scheduled = _ago(days=random.randint(0, 5), hours=random.randint(0, 12))

        started = None
        completed_at = None
        failure_reason = None

        if status in ("in_progress", "completed", "failed"):
            started = _ago(days=random.randint(0, 3), hours=random.randint(0, 8))
        if status == "completed":
            completed_at = _ago(hours=random.randint(1, 48))
        if status == "failed":
            failure_reason = random.choice(["mechanical_failure", "road_closure", "permit_expired"])

        # Cargo manifest: 2-3 items per job
        cargo_count = random.randint(2, 3)
        cargo_manifest = []
        for ci in range(cargo_count):
            cargo_manifest.append({
                "item_id": f"CARGO-{jid}-{ci + 1:02d}",
                "description": random.choice([
                    "Diesel #2 drums", "Regular gasoline barrels", "Industrial lubricants",
                    "Heating oil containers", "Propane cylinders", "DEF totes",
                    "Off-road diesel drums", "Kerosene barrels",
                ]),
                "weight_kg": round(random.uniform(500, 5000), 1),
                "container_number": f"CONT-{_uid()[:6].upper()}",
                "seal_number": f"SEAL-{_uid()[:8].upper()}",
                "item_status": random.choice(["loaded", "in_transit", "delivered", "pending"]),
            })

        doc = {
            "job_id": jid,
            "job_type": jtype,
            "status": status,
            "tenant_id": TENANT,
            "asset_assigned": f"TRK-{random.randint(100, 999)}" if status != "scheduled" else None,
            "origin": origin_city,
            "destination": dest_city,
            "origin_location": _geo(origin_city),
            "destination_location": _geo(dest_city),
            "scheduled_time": scheduled,
            "estimated_arrival": _future(hours=random.randint(2, 48)),
            "started_at": started,
            "completed_at": completed_at,
            "created_at": created,
            "updated_at": _ago(hours=random.randint(0, 24)),
            "created_by": random.choice(creators),
            "priority": random.choice(priorities),
            "delayed": is_delayed,
            "delay_duration_minutes": random.randint(30, 180) if is_delayed else 0,
            "failure_reason": failure_reason,
            "notes": f"Seed job {jid} — {jtype} from {origin_city} to {dest_city}",
            "cargo_manifest": cargo_manifest,
        }
        actions.append({"index": {"_index": index, "_id": jid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(status_list)} docs → {index}")



# ---------------------------------------------------------------------------
# 5. fuel_stations
# ---------------------------------------------------------------------------
def seed_fuel_stations(force: bool = False):
    index = "fuel_stations"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # 14 stations across 8 US cities with diverse fuel grades and stock levels
    # Tuple: (id, name, fuel_type, city, capacity, current_stock, daily_consumption, status)
    stations = [
        # --- Houston (major hub, 2 stations) ---
        ("FS-001", "Houston Ship Channel Terminal",     "DIESEL_2",      "Houston",   80000, 62000, 3200, "normal"),
        ("FS-002", "Houston Pasadena Gasoline Rack",    "GASOLINE_REG",  "Houston",   60000, 45000, 2800, "normal"),
        # --- Dallas (2 stations) ---
        ("FS-003", "Dallas Fort Worth Fuel Depot",      "GASOLINE_REG",  "Dallas",    45000, 5400,  2200, "critical"),
        ("FS-004", "Dallas Love Field Kerosene Depot",  "KEROSENE",      "Dallas",    35000, 28000, 1200, "normal"),
        # --- Chicago (1 station) ---
        ("FS-005", "Chicago Lemont Diesel Terminal",    "DIESEL_2",      "Chicago",   40000, 3600,  1800, "critical"),
        # --- Denver (2 stations) ---
        ("FS-006", "Denver Commerce City Terminal",     "DIESEL_2",      "Denver",    70000, 58000, 3500, "normal"),
        ("FS-007", "Denver Propane Distribution Hub",   "PROPANE",       "Denver",    30000, 18000, 1000, "normal"),
        # --- Atlanta (1 station) ---
        ("FS-008", "Atlanta Doraville Gasoline Hub",    "GASOLINE_PREM", "Atlanta",   35000, 4200,  1900, "critical"),
        # --- Phoenix (1 station) ---
        ("FS-009", "Phoenix West Propane Depot",        "PROPANE",       "Phoenix",   25000, 6000,  800,  "low"),
        # --- Detroit (2 stations) ---
        ("FS-010", "Detroit River Rouge Terminal",      "DIESEL_2",      "Detroit",   55000, 42000, 2600, "normal"),
        ("FS-011", "Detroit Zug Island Gasoline Rack",  "GASOLINE_REG",  "Detroit",   30000, 7200,  1500, "low"),
        # --- Charlotte (1 station) ---
        ("FS-012", "Charlotte Airport Rd Depot",        "GASOLINE_REG",  "Charlotte", 40000, 32000, 2000, "normal"),
        # --- Houston (heating oil) ---
        ("FS-013", "Houston Baytown Heating Oil Depot", "HEATING_OIL",   "Houston",   50000, 11000, 2200, "low"),
        # --- Chicago (DEF) ---
        ("FS-014", "Chicago Joliet DEF Terminal",       "DEF",           "Chicago",   20000, 1800,  600,  "critical"),
    ]

    actions = []
    for sid, name, ftype, city, cap, stock, daily, status in stations:
        days_left = round(stock / daily, 1) if daily > 0 and stock > 0 else 0
        geo = _geo(city)
        doc = {
            "station_id": sid,
            "name": name,
            "fuel_type": ftype,
            "fuel_grade": ftype,  # alias for TankForecastingAgent compatibility
            "capacity_liters": float(cap),
            "current_stock_liters": float(stock),
            "daily_consumption_rate": float(daily),
            "days_until_empty": days_left,
            "stock_level_pct": round((stock / cap) * 100, 1) if cap > 0 else 0,
            "alert_threshold_pct": 15.0,
            "status": status,
            "location": geo,
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "location_name": city,
            "tenant_id": TENANT,
            "created_at": _ago(days=90),
            "last_updated": _now(),
        }
        actions.append({"index": {"_index": index, "_id": sid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(stations)} docs → {index}")


# ---------------------------------------------------------------------------
# 5b. truck_compartments (fuel tanker configuration)
# ---------------------------------------------------------------------------
def seed_truck_compartments(force: bool = False):
    index = "truck_compartments"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # 4 fuel tankers with varied compartment configurations
    # Tuple: (truck_id, comp_id, capacity, allowed_grades, position, depot_city)
    compartments = [
        # Truck FT-001: 4 compartments, 40,000L total, Diesel/Gasoline compatible, Houston depot
        ("FT-001", "C1", 12000, ["DIESEL_2", "GASOLINE_REG"], 1, "Houston"),
        ("FT-001", "C2", 12000, ["DIESEL_2", "GASOLINE_REG"], 2, "Houston"),
        ("FT-001", "C3", 10000, ["DIESEL_2", "GASOLINE_REG"], 3, "Houston"),
        ("FT-001", "C4", 6000,  ["DIESEL_2", "GASOLINE_REG"], 4, "Houston"),
        # Truck FT-002: 3 compartments, 25,000L total, Gasoline/Kerosene compatible, Dallas depot
        ("FT-002", "C1", 10000, ["GASOLINE_REG", "KEROSENE"], 1, "Dallas"),
        ("FT-002", "C2", 9000,  ["GASOLINE_REG", "KEROSENE"], 2, "Dallas"),
        ("FT-002", "C3", 6000,  ["GASOLINE_REG", "KEROSENE"], 3, "Dallas"),
        # Truck FT-003: 5 compartments, 50,000L total, all grades, Denver depot
        ("FT-003", "C1", 14000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 1, "Denver"),
        ("FT-003", "C2", 12000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 2, "Denver"),
        ("FT-003", "C3", 10000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 3, "Denver"),
        ("FT-003", "C4", 8000,  ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 4, "Denver"),
        ("FT-003", "C5", 6000,  ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 5, "Denver"),
        # Truck FT-004: 3 compartments, 20,000L total, Diesel only, Chicago depot
        ("FT-004", "C1", 8000,  ["DIESEL_2"], 1, "Chicago"),
        ("FT-004", "C2", 7000,  ["DIESEL_2"], 2, "Chicago"),
        ("FT-004", "C3", 5000,  ["DIESEL_2"], 3, "Chicago"),
    ]

    actions = []
    for truck_id, comp_id, capacity, grades, pos, depot in compartments:
        doc_id = f"{truck_id}_{comp_id}"
        geo = _geo(depot)
        doc = {
            "compartment_id": comp_id,
            "truck_id": truck_id,
            "capacity_liters": float(capacity),
            "allowed_grades": grades,
            "position_index": pos,
            "depot_city": depot,
            "depot_location": geo,
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "tenant_id": TENANT,
            "created_at": _ago(days=30),
            "updated_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": doc_id}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(compartments)} docs → {index}")


# ---------------------------------------------------------------------------
# 6. fuel_events
# ---------------------------------------------------------------------------
def seed_fuel_events(force: bool = False):
    index = "fuel_events"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    station_ids = [f"FS-{i:03d}" for i in range(1, 7)]
    fuel_types = {"FS-001": "DIESEL_2", "FS-002": "GASOLINE_REG", "FS-003": "GASOLINE_REG",
                  "FS-004": "KEROSENE", "FS-005": "DIESEL_2", "FS-006": "DIESEL_2"}

    actions = []

    # 15 consumption events spread over last 7 days
    for i in range(1, 16):
        sid = random.choice(station_ids)
        eid = f"FE-CON-{i:03d}"
        doc = {
            "event_id": eid,
            "station_id": sid,
            "event_type": "consumption",
            "fuel_type": fuel_types[sid],
            "quantity_liters": round(random.uniform(200, 2000), 1),
            "asset_id": f"TRK-{random.randint(100, 999)}",
            "operator_id": f"OP-{random.randint(1, 20):03d}",
            "odometer_reading": round(random.uniform(50000, 200000), 1),
            "tenant_id": TENANT,
            "event_timestamp": _ago(days=random.uniform(0, 7)),
            "ingested_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": eid}})
        actions.append(doc)

    # 5 refill events
    for i in range(1, 6):
        sid = random.choice(station_ids)
        eid = f"FE-REF-{i:03d}"
        doc = {
            "event_id": eid,
            "station_id": sid,
            "event_type": "refill",
            "fuel_type": fuel_types[sid],
            "quantity_liters": round(random.uniform(5000, 20000), 1),
            "asset_id": f"TNK-{random.randint(1, 10):03d}",
            "operator_id": f"OP-{random.randint(1, 20):03d}",
            "supplier": random.choice(["Marathon Petroleum", "Valero Energy", "Phillips 66", "ExxonMobil"]),
            "delivery_reference": f"DEL-{_uid()[:8].upper()}",
            "tenant_id": TENANT,
            "event_timestamp": _ago(days=random.uniform(0, 7)),
            "ingested_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": eid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded 20 docs → {index}")



# ---------------------------------------------------------------------------
# 7. agent_memory
# ---------------------------------------------------------------------------
def seed_agent_memory(force: bool = False):
    index = "agent_memory"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    memories = [
        ("MEM-001", "pattern",    "sla-guardian",   "Drivers in Houston zone tend to have higher delivery success rates during morning hours (6-10 AM).",
         0.92, ["delivery", "houston", "timing"]),
        ("MEM-002", "preference", "fuel-agent",     "Dispatcher prefers diesel refills to be scheduled before 8 AM to avoid peak traffic.",
         0.85, ["fuel", "scheduling", "preference"]),
        ("MEM-003", "pattern",    "ops-intel",      "Shipments to Chicago frequently experience delays on Fridays due to expressway congestion.",
         0.88, ["shipment", "chicago", "delay", "pattern"]),
        ("MEM-004", "preference", "sla-guardian",   "SLA breach threshold for priority customers is 30 minutes, not the default 60.",
         0.95, ["sla", "threshold", "priority"]),
        ("MEM-005", "pattern",    "fleet-agent",    "Trucks returning from Denver route need maintenance check after 3 consecutive trips.",
         0.78, ["fleet", "maintenance", "denver"]),
        ("MEM-006", "preference", "scheduling-agent", "Night shifts should not be assigned to drivers with less than 30 days experience.",
         0.90, ["scheduling", "night-shift", "experience"]),
        ("MEM-007", "pattern",    "ops-intel",      "Address-not-found failures cluster in newly developed areas of Charlotte.",
         0.82, ["failure", "address", "charlotte"]),
        ("MEM-008", "preference", "fuel-agent",     "Propane deliveries require hazmat-certified drivers only.",
         0.97, ["fuel", "propane", "safety", "certification"]),
    ]

    actions = []
    for mid, mtype, agent, content, score, tags in memories:
        doc = {
            "memory_id": mid,
            "memory_type": mtype,
            "agent_id": agent,
            "tenant_id": TENANT,
            "content": content,
            "confidence_score": score,
            "created_at": _ago(days=random.randint(5, 60)),
            "last_accessed": _ago(days=random.randint(0, 5)),
            "access_count": random.randint(1, 50),
            "tags": tags,
            "updated_at": _ago(days=random.randint(0, 3)),
        }
        actions.append({"index": {"_index": index, "_id": mid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(memories)} docs → {index}")


# ---------------------------------------------------------------------------
# 8. agent_approval_queue
# ---------------------------------------------------------------------------
def seed_approval_queue(force: bool = False):
    index = "agent_approval_queue"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    approvals = [
        {
            "action_id": "APR-001",
            "action_type": "reassign_shipment",
            "tool_name": "reassign_rider",
            "parameters": {"shipment_id": "SHP-010", "from_rider": "RDR-007", "to_rider": "RDR-002"},
            "risk_level": "medium",
            "proposed_by": "sla-guardian",
            "proposed_at": _ago(hours=2),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": _future(hours=4),
            "impact_summary": "Reassign failed shipment SHP-010 from offline driver to active driver in Dallas.",
            "execution_result": {},
            "tenant_id": TENANT,
            "created_at": _ago(hours=2),
            "updated_at": _ago(hours=2),
        },
        {
            "action_id": "APR-002",
            "action_type": "emergency_refuel",
            "tool_name": "schedule_refuel",
            "parameters": {"station_id": "FS-005", "quantity_liters": 15000, "supplier": "Marathon Petroleum"},
            "risk_level": "high",
            "proposed_by": "fuel-agent",
            "proposed_at": _ago(hours=5),
            "status": "approved",
            "reviewed_by": "admin-user-01",
            "reviewed_at": _ago(hours=4),
            "expiry_time": _future(hours=12),
            "impact_summary": "Emergency refuel for critically low Chicago station. Stock at 10% capacity.",
            "execution_result": {"status": "scheduled", "eta": _future(hours=6)},
            "tenant_id": TENANT,
            "created_at": _ago(hours=5),
            "updated_at": _ago(hours=4),
        },
        {
            "action_id": "APR-003",
            "action_type": "cancel_job",
            "tool_name": "cancel_scheduled_job",
            "parameters": {"job_id": "JOB-012", "reason": "permit_expired"},
            "risk_level": "low",
            "proposed_by": "scheduling-agent",
            "proposed_at": _ago(hours=8),
            "status": "rejected",
            "reviewed_by": "admin-user-02",
            "reviewed_at": _ago(hours=7),
            "expiry_time": _ago(hours=1),
            "impact_summary": "Cancel scheduled cargo transport due to expired transit permit.",
            "execution_result": {},
            "tenant_id": TENANT,
            "created_at": _ago(hours=8),
            "updated_at": _ago(hours=7),
        },
        {
            "action_id": "APR-004",
            "action_type": "reroute_shipment",
            "tool_name": "update_route",
            "parameters": {"shipment_id": "SHP-007", "new_route": "Houston-Dallas-Denver", "reason": "road_closure"},
            "risk_level": "medium",
            "proposed_by": "ops-intel",
            "proposed_at": _ago(minutes=45),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": _future(hours=2),
            "impact_summary": "Reroute in-transit shipment via Dallas due to reported road closure on direct Houston-Denver route.",
            "execution_result": {},
            "tenant_id": TENANT,
            "created_at": _ago(minutes=45),
            "updated_at": _ago(minutes=45),
        },
    ]

    actions = []
    for doc in approvals:
        actions.append({"index": {"_index": index, "_id": doc["action_id"]}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(approvals)} docs → {index}")


# ---------------------------------------------------------------------------
# 9. ops_poison_queue
# ---------------------------------------------------------------------------
def seed_poison_queue(force: bool = False):
    index = "ops_poison_queue"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    entries = [
        {
            "event_id": "PSN-001",
            "error_type": "schema_validation_error",
            "status": "pending",
            "tenant_id": TENANT,
            "original_payload": {"shipment_id": "SHP-UNKNOWN", "status": "invalid_status", "raw": "corrupted payload"},
            "error_reason": "Field 'status' contains invalid value 'invalid_status'. Expected one of: pending, in_transit, delivered, failed, returned.",
            "created_at": _ago(hours=6),
            "retry_count": 0,
            "max_retries": 3,
            "trace_id": _uid(),
        },
        {
            "event_id": "PSN-002",
            "error_type": "missing_required_field",
            "status": "retrying",
            "tenant_id": TENANT,
            "original_payload": {"event_type": "shipment_delivered", "timestamp": _ago(hours=12)},
            "error_reason": "Required field 'shipment_id' is missing from event payload.",
            "created_at": _ago(hours=12),
            "retry_count": 2,
            "max_retries": 3,
            "trace_id": _uid(),
        },
        {
            "event_id": "PSN-003",
            "error_type": "elasticsearch_index_error",
            "status": "permanently_failed",
            "tenant_id": TENANT,
            "original_payload": {"shipment_id": "SHP-999", "status": "delivered", "extra_field": "not_in_mapping"},
            "error_reason": "Strict mapping rejection: field [extra_field] not allowed in index [shipments_current].",
            "created_at": _ago(days=2),
            "retry_count": 3,
            "max_retries": 3,
            "trace_id": _uid(),
        },
    ]

    actions = []
    for doc in entries:
        actions.append({"index": {"_index": index, "_id": doc["event_id"]}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(entries)} docs → {index}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TENANT:
        raise SystemExit(
            "SEED_TENANT_ID is required; refusing to seed records with a "
            "hardcoded/default tenant."
        )

    force = "--force" in sys.argv

    print("=" * 60)
    print("  Runsheet — Elasticsearch Seed Script")
    print("=" * 60)

    if force:
        print("⚠️  --force flag detected: will re-seed ALL indices\n")
    else:
        print("ℹ️  Will only seed indices that are empty\n")

    try:
        if not ES.ping():
            print("❌ Cannot reach Elasticsearch. Check your .env / connection settings.")
            sys.exit(1)
        print("✅ Elasticsearch connection OK\n")
    except Exception as e:
        print(f"❌ Elasticsearch connection failed: {e}")
        sys.exit(1)

    seeders = [
        ("trucks",                seed_trucks),
        ("riders_current",        seed_riders),
        ("shipments_current",     seed_shipments),
        ("shipment_events",       seed_shipment_events),
        ("jobs_current",          seed_jobs),
        ("fuel_stations",         seed_fuel_stations),
        ("truck_compartments",    seed_truck_compartments),
        ("fuel_events",           seed_fuel_events),
        ("agent_memory",          seed_agent_memory),
        ("agent_approval_queue",  seed_approval_queue),
        ("ops_poison_queue",      seed_poison_queue),
    ]

    for name, fn in seeders:
        try:
            print(f"{'─' * 40}")
            print(f"  Seeding: {name}")
            fn(force=force)
        except Exception as e:
            logger.error(f"❌ Failed to seed {name}: {e}")
            print(f"  ❌ Error seeding {name}: {e}")

    print(f"\n{'=' * 60}")
    print("  Seeding complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
