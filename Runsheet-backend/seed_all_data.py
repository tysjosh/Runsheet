#!/usr/bin/env python3
"""
Single seed entry point for ALL Elasticsearch data needed by Runsheet.

This one script does everything:
  1. Creates index mappings (idempotent — only creates what's missing).
     With ``--recreate`` it first DROPS every managed index (destructive).
  2. Loads the static JSON fixtures from ``scripts/data/*.json``.
  3. Generates programmatic demo data (trucks, jobs, riders, commerce, etc.).

Usage:
    SEED_TENANT_ID=demo-tenant python seed_all_data.py
        Fill empty indices only (safe, idempotent).

    SEED_TENANT_ID=demo-tenant python seed_all_data.py --force
        Re-seed every index, overwriting existing seed data.

    SEED_TENANT_ID=demo-tenant python seed_all_data.py --recreate
        DROP and recreate all index mappings first, then seed. Destructive —
        requires typing YES to confirm (skip the prompt with --yes).

    Extra flags:
      --skip-json          Skip the static JSON fixtures (step 2).
      --skip-programmatic  Skip the generated demo data (step 3).
      --yes                Skip the --recreate confirmation prompt.

Uses the existing elasticsearch_service singleton and the sync client.index() /
client.bulk() methods directly.
"""

import sys
import os
import json
import glob
import uuid
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Load environment BEFORE importing the ES singleton so it connects to the
# right cluster when run standalone (mirrors main.py / the old loaders).
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

_ENV = os.environ.get("ENVIRONMENT", "development").lower()
_ENV_FILE = Path(__file__).parent / f".env.{_ENV}"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
else:
    load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import the shared ES singleton
# ---------------------------------------------------------------------------
from services.elasticsearch_service import elasticsearch_service

ES = elasticsearch_service.client

# Directory holding the static JSON fixtures (step 2).
DATA_DIR = Path(__file__).parent / "scripts" / "data"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TENANT = os.environ.get("SEED_TENANT_ID", "").strip()


def _retired_indices() -> set:
    """Indices retired to Postgres as their sole store.

    Still meaningful after Elasticsearch is gone: the seeder writes through
    ``ElasticsearchService``, which serves the document store, and a retired index
    is one whose aggregate has a relational table instead. Seeding it would
    resurrect a shape nothing reads. Sourced from the same ``RETIRED_ES_INDICES``
    setting the runtime gate uses.
    """
    try:
        from config.settings import get_settings
        return set(get_settings().retired_es_indices or [])
    except Exception:  # noqa: BLE001
        # Fall back to the raw env var so the seeder works even if settings
        # validation is unavailable in a standalone run.
        raw = os.environ.get("RETIRED_ES_INDICES", "")
        return {p.strip() for p in raw.replace("[", "").replace("]", "")
                .replace('"', "").split(",") if p.strip()}


_RETIRED_INDICES = _retired_indices()
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
    """Execute a bulk request.

    Action pairs targeting a Phase-6 retired index are dropped centrally so no
    programmatic seeder resurrects a dropped index (Postgres is its sole store).
    """
    if not actions:
        return
    if _RETIRED_INDICES:
        filtered: list = []
        skipped = False
        i = 0
        while i < len(actions):
            action = actions[i]
            # Bulk actions come in (meta, doc) pairs; meta has the target index.
            meta = action.get("index") or action.get("create") or {} if isinstance(action, dict) else {}
            target = meta.get("_index")
            has_doc = (i + 1) < len(actions) and not (
                isinstance(actions[i + 1], dict)
                and ({"index", "create", "update", "delete"} & set(actions[i + 1].keys()))
            )
            if target in _RETIRED_INDICES:
                skipped = True
                i += 2 if has_doc else 1
                continue
            filtered.append(action)
            if has_doc:
                filtered.append(actions[i + 1])
            i += 2 if has_doc else 1
        if skipped:
            logger.info("⏭️  Skipped bulk writes to retired index(es): %s",
                        ", ".join(sorted(_RETIRED_INDICES)))
        actions = filtered
    if not actions:
        return
    resp = ES.bulk(body=actions, refresh=True)
    if resp.get("errors"):
        for item in resp["items"]:
            for op, detail in item.items():
                if detail.get("error"):
                    logger.error("Bulk error: %s", detail["error"])


def _single(index: str, doc_id: str, body: dict):
    if index in _RETIRED_INDICES:
        logger.info("⏭️  Skipping write to retired index: %s", index)
        return
    ES.index(index=index, id=doc_id, body=body, refresh=True)


# ---------------------------------------------------------------------------
# Step 1: Index mappings (create / recreate)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 2: Static JSON fixtures (scripts/data/*.json)
# ---------------------------------------------------------------------------

# ID fields tried (in order) when assigning a document _id for JSON records.
_JSON_ID_FIELDS = [
    "id", "record_id", "event_id", "notification_id", "preference_id",
    "template_id", "rule_id", "jurisdiction_id", "exemption_id", "contract_id",
    "cert_id", "meter_id", "audit_id", "bol_id", "adjustment_id", "forecast_id",
    "priority_id", "priority_list_id", "plan_id", "group_id", "reconciliation_id",
    "depot_id", "terminal_id", "rack_price_id", "report_id", "alert_id",
    "override_id", "instance_id", "reading_id", "telemetry_id", "cleaning_event_id",
    "restriction_id", "run_id", "ocr_result_id", "recommendation_id", "request_id",
    "channel_id", "location_id", "message_id", "exception_id", "driver_id",
    "order_id", "customer_id", "account_id", "invoice_id", "payment_id",
    "price_book_id", "pricing_rule_id", "job_id", "route_id", "pod_id",
    "truck_id", "rider_id", "station_id", "tank_id", "customer_tank_id",
    "memory_id", "action_id",
]


#: Indices whose document id neither follows the ``<index>_id`` convention nor
#: appears in :data:`_JSON_ID_FIELDS`. Each entry mirrors the id the PRODUCTION
#: writer uses, so a seeded document and a live one are the same document rather
#: than two copies of the same fact.
#:
#: Both entries were found by the fixture-collapse property test:
#:
#: * ``atg_readings`` carries ``reading_id``, but ``instance_id`` precedes it in
#:   the generic list, so the 2-row fixture loaded as 1 document.
#:   ``TankImportService`` keys on ``reading_id``.
#: * ``weather_observations`` has no ``*_id`` field at all, so every row was
#:   skipped with a warning and the index stayed EMPTY — while
#:   ``EsHddProvider`` reads it for the compliance K-factor service's
#:   accumulated degree-days. ``WeatherProvider._persist_observations`` keys on
#:   ``wxobs:{tenant_id}:{provider}:{zip_code}:{date}``.
_INDEX_ID_OVERRIDES = {
    "atg_readings": lambda d: d.get("reading_id"),
    "weather_observations": lambda d: (
        "wxobs:{}:{}:{}:{}".format(
            d.get("tenant_id"), d.get("provider"), d.get("zip_code"), d.get("date")
        )
        if all(d.get(k) for k in ("tenant_id", "provider", "zip_code", "date"))
        else None
    ),
}


def _natural_id_fields(index_name: str) -> tuple:
    """The id field names that belong to ``index_name`` itself.

    ``rack_prices`` -> ``rack_prices_id``, ``rack_price_id``;
    ``fuel_orders_current`` -> ``fuel_orders_id``, ``fuel_order_id``.
    """
    base = index_name.replace("_current", "")
    singular = base[:-1] if base.endswith("s") else base
    return (f"{base}_id", f"{singular}_id")


def _resolve_json_doc_id(index_name: str, doc: dict):
    """Pick a document _id for a JSON seed record.

    The index's **own** key wins over :data:`_JSON_ID_FIELDS`. That ordering is
    the whole point: the generic list is ordered, and a record carrying both its
    own key and a foreign key used to get whichever appeared earlier in the
    list. Two seeded indices did:

    * ``rack_prices`` was keyed by ``terminal_id`` (earlier in the list than
      ``rack_price_id``), so the 5-row fixture loaded as **3 documents** —
      RACK-002 overwrote RACK-001 and RACK-004 overwrote RACK-003, both sharing
      a terminal. Rack prices are per (terminal, product) and the sourcing
      recommender scores candidates on them, so each terminal kept only its last
      product's price and the rest silently did not exist.
    * ``customer_tanks`` was keyed by ``customer_id``. Latent today because no
      fixture gives one customer two tanks, but the domain supports it — the
      production repository keys on ``customer_tank_id`` — so the next
      multi-tank customer would have lost every tank but one.

    Nothing was logged in either case: a bulk index with a duplicate id is an
    ordinary overwrite. The generic list stays as the fallback for indices whose
    key does not follow the naming convention (``fuel_orders_current`` ->
    ``order_id``).
    """
    override = _INDEX_ID_OVERRIDES.get(index_name)
    if override is not None:
        return override(doc)
    for candidate in _natural_id_fields(index_name):
        if candidate in doc:
            return doc[candidate]
    for field in _JSON_ID_FIELDS:
        if field in doc:
            return doc[field]
    return None


def _index_property_names(index_name: str):
    """Return the set of top-level mapped field names for a strict index.

    Returns ``None`` when the mapping can't be read or the index is not
    ``dynamic: strict`` — callers treat ``None`` as "permissive" (any field
    allowed). Cached per run to avoid repeated mapping fetches.
    """
    cache = _index_property_names._cache
    if index_name in cache:
        return cache[index_name]

    result = None
    try:
        if ES.indices.exists(index=index_name):
            mapping = ES.indices.get_mapping(index=index_name)
            mappings = mapping.get(index_name, {}).get("mappings", {})
            # Only constrain when the index is strict; dynamic indices accept
            # arbitrary fields so there's nothing to filter.
            if mappings.get("dynamic") == "strict":
                result = set((mappings.get("properties") or {}).keys())
    except Exception:  # noqa: BLE001 — be permissive on any lookup failure
        result = None

    cache[index_name] = result
    return result


_index_property_names._cache = {}


def _load_json_file(filepath: Path, force: bool) -> int:
    """Load one JSON fixture file into ES. Returns records loaded."""
    with open(filepath, "r") as f:
        data = json.load(f)

    total = 0
    for index_name, records in data.items():
        if not isinstance(records, list) or not records:
            continue
        if index_name in _RETIRED_INDICES:
            logger.info(
                "⏭️  %s retired (Postgres-only) — skipping ES fixture load",
                index_name,
            )
            continue
        if not force and _index_count(index_name) > 0:
            logger.info(f"⏭️  {index_name} already has data — skipping")
            continue

        # Strict indices reject unknown fields, so only auto-stamp the
        # convenience timestamps the index actually defines. ``allowed`` is
        # None when the mapping can't be read (treat as permissive).
        allowed = _index_property_names(index_name)

        def _allows(field: str) -> bool:
            return allowed is None or field in allowed

        actions = []
        now = _now()
        for record in records:
            doc = dict(record)
            doc.setdefault("tenant_id", TENANT)
            if _allows("created_at"):
                doc.setdefault("created_at", now)
            if _allows("updated_at"):
                doc.setdefault("updated_at", now)
            doc_id = _resolve_json_doc_id(index_name, doc)
            if not doc_id:
                logger.warning(
                    "No ID field for record in %s: %s",
                    index_name, list(doc.keys())[:5],
                )
                continue
            actions.append({"index": {"_index": index_name, "_id": doc_id}})
            actions.append(doc)

        if actions:
            _bulk(actions)
            count = len(actions) // 2
            total += count
            logger.info(f"✅ Loaded {count} records → {index_name}")
    return total


def load_json_fixtures(force: bool = False):
    """Load every ``scripts/data/*.json`` fixture (auto-discovered)."""
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        print(f"⚠️  No *.json seed files found in {DATA_DIR}")
        return

    print(f"Discovered {len(files)} JSON fixture(s): {', '.join(p.name for p in files)}")
    total = 0
    for filepath in files:
        try:
            print(f"{'─' * 40}")
            print(f"  Loading fixture: {filepath.name}")
            total += _load_json_file(filepath, force=force)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to load %s", filepath.name)
            print(f"  ❌ Error loading {filepath.name}: {e}")
    print(f"\n  JSON fixtures: loaded {total} records")


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
# 2. jobs_current
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
# 3. fuel_stations
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
# 3a. mvp_tank_forecasts (urgent delivery scenarios)
# ---------------------------------------------------------------------------
def seed_tank_forecasts(force: bool = False):
    index = "mvp_tank_forecasts"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create forecasts for the fuel stations with realistic hours_to_runout
    # that will trigger delivery priorities
    # FuelGrade enum: AGO (diesel), PMS (gasoline), ATK (kerosene/jet fuel), LPG (propane)
    forecasts = [
        # Critical stations (< 12 hours to runout)
        ("FS-003", "PMS",  8.5,  6.2,  0.85, 0.92, ["low_stock"]),  # GASOLINE_REG → PMS
        ("FS-005", "AGO",  10.2, 7.8,  0.78, 0.88, ["low_stock"]),  # DIESEL_2 → AGO
        ("FS-014", "AGO",  6.5,  4.2,  0.92, 0.95, ["low_stock", "critical"]),  # DEF → AGO
        
        # High priority stations (12-24 hours to runout)
        ("FS-008", "PMS",  18.5, 14.2, 0.68, 0.82, ["low_stock"]),  # GASOLINE_PREM → PMS
        ("FS-009", "LPG",  22.0, 16.5, 0.55, 0.78, ["low_stock"]),  # PROPANE → LPG
        ("FS-013", "AGO",  20.5, 15.8, 0.62, 0.80, ["low_stock"]),  # HEATING_OIL → AGO
        
        # Medium priority stations (24-48 hours to runout)
        ("FS-011", "PMS",  36.0, 28.5, 0.42, 0.75, []),  # GASOLINE_REG → PMS
        
        # Normal stations (> 48 hours to runout)
        ("FS-001", "AGO",  72.0, 58.0, 0.15, 0.88, []),  # DIESEL_2 → AGO
        ("FS-002", "PMS",  68.5, 52.0, 0.18, 0.85, []),  # GASOLINE_REG → PMS
        ("FS-004", "ATK",  96.0, 78.0, 0.08, 0.90, []),  # KEROSENE → ATK
        ("FS-006", "AGO",  84.0, 68.0, 0.12, 0.92, []),  # DIESEL_2 → AGO
        ("FS-007", "LPG",  120.0, 96.0, 0.05, 0.88, []),  # PROPANE → LPG
        ("FS-010", "AGO",  78.0, 62.0, 0.14, 0.90, []),  # DIESEL_2 → AGO
        ("FS-012", "PMS",  88.0, 72.0, 0.10, 0.87, []),  # GASOLINE_REG → PMS
    ]

    actions = []
    for station_id, fuel_grade, hours_p50, hours_p90, risk_24h, confidence, anomalies in forecasts:
        forecast_id = f"FC-{station_id}-{_uid()[:8]}"
        doc = {
            "forecast_id": forecast_id,
            "tenant_id": TENANT,
            "station_id": station_id,
            "fuel_grade": fuel_grade,
            "hours_to_runout_p50": hours_p50,
            "hours_to_runout_p90": hours_p90,
            "runout_risk_24h": risk_24h,
            "confidence": confidence,
            "anomaly_flags": anomalies,
            "timestamp": _now(),
            "model_name": "baseline_consumption",
            "baseline_source": "historical_7d",
            "weather_fallback": False,
            "customer_type": "retail",
            "customer_type_multiplier": 1.0,
            "scheduled_deliveries": [],
        }
        actions.append({"index": {"_index": index, "_id": forecast_id}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(forecasts)} tank forecasts → {index}")


# ---------------------------------------------------------------------------
# 4. truck_compartments (fuel tanker configuration)
# ---------------------------------------------------------------------------
def seed_truck_compartments(force: bool = False):
    index = "truck_compartments"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Configure compartments for the actual tanker trucks (TNK-001, TNK-002)
    # Tuple: (truck_id, comp_id, capacity, allowed_grades, position, depot_city)
    # allowed_grades use canonical US product codes (Capability 6 migration):
    # DIESEL_2 (was AGO), GASOLINE_REG (was PMS), KEROSENE (was ATK), PROPANE (was LPG).
    compartments = [
        # Truck TNK-001: 4 compartments, 40,000L total, all fuel types, Houston depot
        ("TNK-001", "C1", 12000, ["DIESEL_2", "GASOLINE_REG"], 1, "Houston"),
        ("TNK-001", "C2", 12000, ["DIESEL_2", "GASOLINE_REG"], 2, "Houston"),
        ("TNK-001", "C3", 10000, ["DIESEL_2", "GASOLINE_REG"], 3, "Houston"),
        ("TNK-001", "C4", 6000,  ["DIESEL_2", "GASOLINE_REG"], 4, "Houston"),
        # Truck TNK-002: 5 compartments, 50,000L total, all grades, Denver depot
        ("TNK-002", "C1", 14000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 1, "Denver"),
        ("TNK-002", "C2", 12000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 2, "Denver"),
        ("TNK-002", "C3", 10000, ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 3, "Denver"),
        ("TNK-002", "C4", 8000,  ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 4, "Denver"),
        ("TNK-002", "C5", 6000,  ["DIESEL_2", "GASOLINE_REG", "KEROSENE", "PROPANE"], 5, "Denver"),
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
            "depot_city": depot,  # String field for equipment check matching
            "depot_location": geo,  # Geo point as per schema
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
# 5. fuel_events
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

    # Consumption events. Fuel efficiency (GET /fuel/metrics/efficiency) is
    # derived per asset_id from the min→max odometer spread across that
    # asset's events, so a truck needs MULTIPLE events with increasing
    # odometer readings to yield a non-null liters_per_km. We therefore seed
    # a fixed fleet of trucks, each with several fill-ups whose odometer
    # climbs by a per-truck km/L profile (so the Fleet Efficiency view shows
    # a realistic spread across good/average/poor tiers).
    #
    # Tuple: (asset_id, start_odometer_km, km_per_fill, liters_per_fill, station)
    # efficiency km/L ≈ km_per_fill / liters_per_fill:
    #   TRK-100: 600/120  = 5.0 km/L  (good / green)
    #   TRK-153: 520/130  = 4.0 km/L  (good / green)
    #   TRK-176: 450/150  = 3.0 km/L  (average / yellow)
    #   TRK-293: 360/180  = 2.0 km/L  (average / yellow)
    #   TRK-412: 300/200  = 1.5 km/L  (poor / red)
    fleet = [
        ("TRK-100", 50_000.0, 600.0, 120.0, "FS-001"),
        ("TRK-153", 82_000.0, 520.0, 130.0, "FS-002"),
        ("TRK-176", 120_000.0, 450.0, 150.0, "FS-005"),
        ("TRK-293", 64_000.0, 360.0, 180.0, "FS-006"),
        ("TRK-412", 98_000.0, 300.0, 200.0, "FS-003"),
    ]
    fills_per_truck = 4  # 5 trucks × 4 fills = 20 consumption events
    con_seq = 0
    for asset_id, start_odo, km_per_fill, liters_per_fill, sid in fleet:
        for fill in range(fills_per_truck):
            con_seq += 1
            eid = f"FE-CON-{con_seq:03d}"
            # Odometer climbs each fill; spread events across the last 7 days
            # oldest-first so timestamps line up with the rising odometer.
            odometer = round(start_odo + km_per_fill * fill, 1)
            days_ago = 7.0 - (fill * (7.0 / fills_per_truck))
            doc = {
                "event_id": eid,
                "station_id": sid,
                "event_type": "consumption",
                "fuel_type": fuel_types[sid],
                "quantity_liters": round(
                    liters_per_fill * random.uniform(0.95, 1.05), 1
                ),
                "asset_id": asset_id,
                "operator_id": f"OP-{random.randint(1, 20):03d}",
                "odometer_reading": odometer,
                "tenant_id": TENANT,
                "event_timestamp": _ago(days=days_ago),
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
    logger.info(f"✅ Seeded 25 docs → {index}")


# ---------------------------------------------------------------------------
# 6. agent_memory
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
# 7. agent_approval_queue
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
# 8. ops_poison_queue
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
# 8a. Canonical driver roster — single source of truth shared by the
#     Utilization (drivers_current) and Qualifications (drivers) seeders so
#     the same driver looks consistent across both Drivers tabs.
# ---------------------------------------------------------------------------

# 2-letter CDL state per home city (drives both seeders).
_DRIVER_CITY_STATE = {
    "Houston": "TX", "Dallas": "TX", "Chicago": "IL", "Denver": "CO",
    "Atlanta": "GA", "Phoenix": "AZ", "Detroit": "MI", "Charlotte": "NC",
}

# Each entry is one physical driver, identified by the SAME driver_id +
# full_name across both indices.
#
# Fields:
#   id, name, city, cdl_class, hazmat (bool), tanker (bool),
#   util_status   — drivers_current status: active|on_break|off_duty|inactive
#   availability  — drivers_current availability label
#   qual_status   — drivers (compliance) status: active|suspended|expired
#   active_orders, completed_today — utilization workload
#   med_offset_days — days from now the medical card expires
#       (negative = expired, ≤30 = expiring soon → warning in BOTH tabs)
_DRIVER_ROSTER = [
    # id        name              city        cdl hazmat tanker util_status avail        qual_status active completed med_offset
    ("DRV-001", "Mike Johnson",    "Houston",  "A", True,  True,  "active",   "available", "active",    3, 7,  365),
    ("DRV-002", "Sarah Williams",  "Dallas",   "A", False, True,  "active",   "available", "active",    2, 5,  200),
    ("DRV-003", "James Rodriguez", "Chicago",  "A", True,  True,  "active",   "on_route",  "active",    9, 4,  20),   # overloaded + medical expiring soon
    ("DRV-004", "Emily Chen",      "Denver",   "B", False, False, "active",   "available", "active",    2, 6,  540),
    ("DRV-005", "David Thompson",  "Atlanta",  "A", True,  True,  "on_break", "break",     "active",    0, 3,  90),
    ("DRV-006", "Maria Garcia",    "Phoenix",  "A", False, False, "on_break", "break",     "expired",   1, 2,  -5),   # medical card expired
    ("DRV-007", "Robert Kim",      "Detroit",  "B", False, False, "off_duty", "offline",   "active",    0, 0,  410),
    ("DRV-008", "Jennifer Davis",  "Charlotte","A", True,  True,  "off_duty", "offline",   "suspended", 0, 1,  150),
    ("DRV-009", "Carlos Mendez",   "Houston",  "A", True,  True,  "active",   "available", "active",    4, 8,  75),
    ("DRV-010", "Aisha Patel",     "Denver",   "B", False, False, "inactive", "offline",   "active",    0, 0,  300),
]


def _date_only(days_from_now: int) -> str:
    """ISO date string (YYYY-MM-DD) offset by ``days_from_now``."""
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).date().isoformat()


# ---------------------------------------------------------------------------
# 8b. drivers_current (Driver Utilization view)
# ---------------------------------------------------------------------------
def seed_drivers_current(force: bool = False):
    """Seed the ``drivers_current`` index read by the Driver Utilization view.

    Distinct from the ``drivers`` index (compliance qualifications) and
    ``riders_current`` (legacy ops riders). The Driver Utilization endpoint
    (``GET /api/ops/drivers/utilization``) reads this index via
    ``DriverRepository``. Built from :data:`_DRIVER_ROSTER` so driver IDs,
    names, and medical-card dates match the Qualifications tab exactly.
    Fields match :class:`fuel.order_models.Driver` (strict mapping).
    """
    index = "drivers_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Truck assignment: active/on_break drivers get a truck, others don't.
    truck_pool = {
        "DRV-001": "TRK-001", "DRV-002": "TRK-002", "DRV-003": "TRK-003",
        "DRV-004": "TRK-004", "DRV-005": "TRK-005", "DRV-009": "TNK-001",
    }

    actions = []
    for (did, name, city, cdl, hazmat, _tanker, util_status, avail,
         _qual_status, active, completed, med_offset) in _DRIVER_ROSTER:
        last_seen = (
            _ago(minutes=random.randint(1, 90))
            if util_status in ("active", "on_break")
            else _ago(hours=random.randint(6, 48))
        )
        medical_expiry = (
            _future(days=med_offset) if med_offset >= 0
            else _ago(days=abs(med_offset))
        )
        doc = {
            "driver_id": did,
            "tenant_id": TENANT,
            "driver_name": name,
            "phone": f"+1{random.randint(2000000000, 9999999999)}",
            "status": util_status,
            "availability": avail,
            "assigned_truck_id": truck_pool.get(did),
            "cdl_class": cdl,
            "hazmat_endorsement": hazmat,
            "medical_card_expiry": medical_expiry,
            "current_location": _geo(city),
            "last_seen": last_seen,
            "active_order_count": active,
            "completed_today": completed,
            "last_event_timestamp": last_seen,
            "source_schema_version": SCHEMA_VERSION,
            "trace_id": _uid(),
            "created_at": _ago(days=random.randint(30, 365)),
            "updated_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": did}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(_DRIVER_ROSTER)} docs → {index}")


# ---------------------------------------------------------------------------
# 8c. drivers (Driver Qualifications / DQF view)
# ---------------------------------------------------------------------------
def seed_drivers_qualifications(force: bool = False):
    """Seed the ``drivers`` index read by the Driver Qualifications view.

    Built from the SAME :data:`_DRIVER_ROSTER` as ``drivers_current`` so each
    driver_id / full_name / medical-card date is consistent across the
    Utilization and Qualifications tabs. Fields match the strict
    ``DRIVERS_MAPPING`` / :class:`compliance.models.driver.Driver`.
    """
    index = "drivers"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    actions = []
    for i, (did, name, city, cdl, hazmat, tanker, _util_status, _avail,
            qual_status, _active, _completed, med_offset) in enumerate(_DRIVER_ROSTER, start=1):
        state = _DRIVER_CITY_STATE.get(city, "TX")
        doc = {
            "driver_id": did,
            "tenant_id": TENANT,
            "full_name": name,
            "cdl_number": f"{state}-CDL-{100000 + i}",
            "cdl_state": state,
            "cdl_class": cdl,
            # CDL valid well into the future; medical card uses the shared
            # offset so the "expiring soon" / "expired" warning matches the
            # Utilization tab for the same driver.
            "cdl_expiry_date": _date_only(365 + i * 10),
            "medical_card_expiry_date": _date_only(med_offset),
            "hazmat_endorsement_expiry_date": _date_only(300 + i * 5) if hazmat else None,
            "tanker_endorsement_expiry_date": _date_only(320 + i * 5) if tanker else None,
            "last_drug_test_date": _date_only(-random.randint(30, 180)),
            "last_mvr_date": _date_only(-random.randint(30, 200)),
            "status": qual_status,
            "suspension_reason": (
                "Administrative hold" if qual_status == "suspended" else None
            ),
            "external_refs": {},
            "created_at": _ago(days=random.randint(180, 540)),
            "updated_at": _now(),
        }
        actions.append({"index": {"_index": index, "_id": did}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(_DRIVER_ROSTER)} docs → {index}")


# ---------------------------------------------------------------------------
# 9. Fuel Orders
# ---------------------------------------------------------------------------
def seed_fuel_orders(force: bool = False):
    index = "fuel_orders_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create fuel orders that match the stations with urgent forecasts
    # Status must be "placed", "confirmed", or "scheduled" for the pipeline to pick them up
    # customer_tank_id must match the station_id in forecasts for scoring to work
    orders = [
        # Critical stations - need immediate delivery
        ("FS-003", "FS-003", "GASOLINE_REG",  2500, "confirmed", "CUST-001"),
        ("FS-005", "FS-005", "DIESEL_2",      3000, "confirmed", "CUST-002"),
        ("FS-014", "FS-014", "DEF",           1500, "confirmed", "CUST-003"),
        
        # High priority stations
        ("FS-008", "FS-008", "GASOLINE_PREM", 2200, "scheduled", "CUST-001"),
        ("FS-009", "FS-009", "PROPANE",       1800, "scheduled", "CUST-004"),
        ("FS-013", "FS-013", "HEATING_OIL",   2800, "scheduled", "CUST-002"),
        
        # Medium priority stations
        ("FS-011", "FS-011", "GASOLINE_REG",  2000, "placed", "CUST-005"),
        
        # Some additional orders for variety
        ("FS-001", "FS-001", "DIESEL_2",      3500, "placed", "CUST-003"),
        ("FS-002", "FS-002", "GASOLINE_REG",  2800, "placed", "CUST-004"),
        ("FS-006", "FS-006", "DIESEL_2",      3200, "placed", "CUST-001"),
    ]
    
    actions = []
    for idx, (station_id, customer_tank_id, product_code, gallons, status, customer_id) in enumerate(orders, start=1):
        order_id = f"ORD-{idx:04d}"
        city = random.choice(CITY_NAMES)
        geo = _geo(city)
        # Map each customer_id to a readable name so the order list renders.
        customer_name = {
            "CUST-001": "Acme Fuel Distribution",
            "CUST-002": "Metro Transit Authority",
            "CUST-003": "Harbor Logistics Co",
            "CUST-004": "Sunrise Energy Partners",
            "CUST-005": "Industrial Logistics",
        }.get(customer_id, customer_id)
        created = _ago(days=random.randint(1, 7))

        doc = {
            "order_id": order_id,
            "tenant_id": TENANT,
            "customer_id": customer_id,
            "customer_name": customer_name,
            "customer_tank_id": customer_tank_id,  # Must match station_id in forecasts
            "status": status,
            "call_type": "keep_full",  # Type of order for prioritization
            "product_code": product_code,
            "gallons_requested": float(gallons),
            "ship_to_address": f"{random.randint(100, 9999)} {random.choice(['Main', 'Oak', 'Elm'])} St, {city}",
            "ship_to_lat": geo["lat"],
            "ship_to_lon": geo["lon"],
            "fill_to_full": False,
            "delivery_window_start": _now(),
            "delivery_window_end": _future(days=2),
            # Intake provenance — required by the strict FuelOrder model.
            "intake_channel": "dispatcher",
            "intake_channel_id": "dispatcher-default",
            "intake_metadata": {"dispatcher_user_id": "seed-dispatcher"},
            "source_schema_version": "1.0",
            "trace_id": _uid(),
            "created_at": created,
            "updated_at": _ago(hours=random.randint(0, 24)),
            "last_event_timestamp": created,
        }
        actions.append({"index": {"_index": index, "_id": order_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded {len(orders)} fuel orders → {index}")


# ---------------------------------------------------------------------------
# 10. Customer Tanks
# ---------------------------------------------------------------------------
def seed_customer_tanks(force: bool = False):
    index = "customer_tanks"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create customer tanks that match our fuel stations and forecasts
    # Tank IDs must match the station_id in forecasts for the TankForecastingAgent
    tanks = [
        # Critical tanks (low levels, high consumption)
        ("FS-003", "CUST-001", "GASOLINE_REG", 10000, 1200, 400, "Houston"),
        ("FS-005", "CUST-002", "DIESEL_2",      15000, 1800, 500, "Dallas"),
        ("FS-014", "CUST-003", "DEF",           5000,  800,  300, "Chicago"),
        
        # High priority tanks
        ("FS-008", "CUST-001", "GASOLINE_PREM", 8000,  1500, 350, "Atlanta"),
        ("FS-009", "CUST-004", "PROPANE",       12000, 2200, 400, "Phoenix"),
        ("FS-013", "CUST-002", "HEATING_OIL",   10000, 2100, 450, "Houston"),
        
        # Medium priority tanks
        ("FS-011", "CUST-005", "GASOLINE_REG",  9000,  3200, 350, "Detroit"),
        
        # Normal tanks (good levels)
        ("FS-001", "CUST-003", "DIESEL_2",      20000, 15000, 400, "Houston"),
        ("FS-002", "CUST-004", "GASOLINE_REG",  18000, 14000, 380, "Dallas"),
        ("FS-006", "CUST-001", "DIESEL_2",      22000, 17000, 420, "Denver"),
    ]
    
    actions = []
    _CITY_ZIP = {
        "Houston": "77002", "Dallas": "75201", "Chicago": "60601",
        "Denver": "80202", "Atlanta": "30303", "Phoenix": "85003",
        "Detroit": "48226", "Charlotte": "28202",
    }
    # Map a catalog product_code to the narrow Consumption_Model fuel-family
    # enum the ``CustomerTank`` model requires in ``fuel_type`` (distinct from
    # the catalog ``fuel_product_code``). Without this the strict model
    # rejects every seeded row (e.g. ``fuel_type="DIESEL_2"`` is not a valid
    # family) and the list endpoint silently drops them.
    _PRODUCT_TO_FAMILY = {
        "GASOLINE_REG": "gasoline",
        "GASOLINE_PREM": "gasoline",
        "DIESEL_2": "diesel",
        "DEF": "diesel",
        "PROPANE": "propane",
        "HEATING_OIL": "heating_oil",
        "KEROSENE": "heating_oil",
    }
    for tank_id, customer_id, product_code, capacity, current_level, reorder, city in tanks:
        geo = _geo(city)
        # Fields must match the strict CUSTOMER_TANKS_MAPPING exactly.
        doc = {
            "customer_tank_id": tank_id,  # Must match station_id in forecasts
            "tenant_id": TENANT,
            "customer_id": customer_id,
            "customer_type": "commercial",
            # Narrow fuel-family enum (NOT the catalog product_code).
            "fuel_type": _PRODUCT_TO_FAMILY.get(product_code, "diesel"),
            "fuel_product_code": product_code,
            "capacity_gallons": float(capacity),
            "current_level_gallons": float(current_level),
            "last_reading_at": _ago(hours=random.randint(1, 12)),
            "location": geo,
            "location_lat": geo["lat"],
            "location_lon": geo["lon"],
            "zip_code": _CITY_ZIP.get(city, "00000"),
            "k_factor": round(random.uniform(1.5, 4.0), 2),
            # ``use_case`` is the UseCase enum (residential_heat |
            # commercial_heat | generator | farm | other), NOT a
            # customer_type. "auto_fill" was invalid and failed validation.
            "use_case": "commercial_heat",
            "status": "active",
            "created_at": _ago(days=random.randint(90, 365)),
            "updated_at": _ago(hours=random.randint(0, 24)),
        }
        actions.append({"index": {"_index": index, "_id": tank_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded {len(tanks)} customer tanks → {index}")


# ---------------------------------------------------------------------------
# 11. Drivers (Compliance)
# ---------------------------------------------------------------------------
def seed_drivers(force: bool = False):
    index = "drivers"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    driver_names = [
        "Mike Johnson", "Sarah Williams", "James Rodriguez", "Emily Chen",
        "David Thompson", "Maria Garcia", "Robert Kim", "Jennifer Davis",
        "Michael Brown", "Lisa Anderson"
    ]
    
    actions = []
    for i, name in enumerate(driver_names, start=1):
        driver_id = f"DRV-{i:03d}"
        
        doc = {
            "driver_id": driver_id,
            "tenant_id": TENANT,
            "name": name,
            "license_number": f"DL{random.randint(1000000, 9999999)}",
            "license_state": random.choice(["TX", "IL", "CO", "GA"]),
            "license_expiry": _future(days=random.randint(30, 730)),
            "cdl_class": random.choice(["A", "B"]),
            "hazmat_certified": random.choice([True, False]),
            "hazmat_expiry": _future(days=random.randint(30, 365)) if random.random() > 0.5 else None,
            "status": random.choice(["active", "active", "active", "inactive"]),
            "hire_date": _ago(days=random.randint(365, 1825)),
            "created_at": _ago(days=random.randint(365, 1825)),
            "updated_at": _ago(days=random.randint(0, 30)),
        }
        actions.append({"index": {"_index": index, "_id": driver_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded {len(driver_names)} drivers → {index}")


# ---------------------------------------------------------------------------
# 12. Proof of Delivery
# ---------------------------------------------------------------------------
def seed_proof_of_delivery(force: bool = False):
    index = "proof_of_delivery"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    actions = []
    for i in range(1, 11):  # 10 PODs
        pod_id = f"POD-{i:04d}"
        order_id = f"ORD-{i:04d}"
        driver_id = f"DRV-{random.randint(1, 10):03d}"
        
        doc = {
            "pod_id": pod_id,
            "tenant_id": TENANT,
            "order_id": order_id,
            "driver_id": driver_id,
            "delivered_gallons": round(random.uniform(500, 5000), 2),
            "signature_url": f"s3://pods/{pod_id}.png",
            "photo_urls": [f"s3://pods/{pod_id}_photo1.jpg"],
            "delivered_at": _ago(days=random.randint(1, 30)),
            "created_at": _ago(days=random.randint(1, 30)),
        }
        actions.append({"index": {"_index": index, "_id": pod_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded 10 PODs → {index}")


# ---------------------------------------------------------------------------
# 13. Price Books (Commerce)
# ---------------------------------------------------------------------------
def seed_price_books(force: bool = False):
    index = "price_books_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    price_books = [
        ("PB-001", "Standard Fuel Pricing 2026", "active", 4),
        ("PB-002", "Winter Heating Oil Rates", "active", 3),
        ("PB-003", "Commercial Fleet Discount", "active", 5),
    ]
    
    actions = []
    for pb_id, name, status, rule_count in price_books:
        doc = {
            "price_book_id": pb_id,
            "tenant_id": TENANT,
            "name": name,
            "description": f"{name} - Effective pricing rules",
            "status": status,
            "rule_count": rule_count,
            "created_at": _ago(days=random.randint(30, 180)),
            "updated_at": _ago(days=random.randint(0, 30)),
        }
        actions.append({"index": {"_index": index, "_id": pb_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded {len(price_books)} price books → {index}")


# ---------------------------------------------------------------------------
# 14. Pricing Rules (Commerce)
# ---------------------------------------------------------------------------
def seed_pricing_rules(force: bool = False):
    index = "pricing_rules_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    actions = []
    rule_id = 1
    for pb_id in ["PB-001", "PB-002", "PB-003"]:
        for product in ["DIESEL_2", "GASOLINE_REG", "GASOLINE_PREM"]:
            rid = f"RULE-{rule_id:04d}"
            doc = {
                "rule_id": rid,
                "tenant_id": TENANT,
                "price_book_id": pb_id,
                "product_code": product,
                "scope_type": "default",
                "scope_value": "default",
                "effective_from": _ago(days=30),
                "effective_to": _future(days=365),
                "min_quantity_gallons": None,
                "unit_price_cents": random.randint(250, 450),
                "created_at": _ago(days=30),
            }
            actions.append({"index": {"_index": index, "_id": rid}})
            actions.append(doc)
            rule_id += 1
    
    _bulk(actions)
    logger.info(f"✅ Seeded {rule_id - 1} pricing rules → {index}")


# ---------------------------------------------------------------------------
# 15. Inventory
# ---------------------------------------------------------------------------
def seed_inventory(force: bool = False):
    index = "inventory"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    items = [
        # Fuel equipment (required for truck loading)
        # (name, category, location, qty, reorder, status, unit, max_capacity)
        ("Fuel Pump - High Flow", "fuel_equipment", "Houston", 5, 2, "in_stock", "units", 20),
        ("Fuel Hose - 50ft", "fuel_equipment", "Houston", 10, 3, "in_stock", "units", 30),
        ("Fuel Meter - Digital", "fuel_equipment", "Houston", 3, 1, "in_stock", "units", 10),
        ("Fuel Pump - High Flow", "fuel_equipment", "Denver", 5, 2, "in_stock", "units", 20),
        ("Fuel Hose - 50ft", "fuel_equipment", "Denver", 10, 3, "in_stock", "units", 30),
        ("Fuel Meter - Digital", "fuel_equipment", "Denver", 3, 1, "in_stock", "units", 10),
        
        # Regular inventory items — categories must match InventoryCategory enum
        ("Fuel Filter - Heavy Duty", "filters", "Houston", 45, 10, "in_stock", "pieces", 100),
        ("Oil Filter - Standard", "filters", "Dallas", 120, 25, "in_stock", "pieces", 200),
        ("Air Filter - Truck", "filters", "Chicago", 67, 15, "in_stock", "pieces", 150),
        ("Brake Pads - Commercial", "brake_parts", "Denver", 34, 8, "in_stock", "sets", 80),
        ("Wiper Blades - 24in", "general", "Atlanta", 89, 20, "in_stock", "pairs", 150),
        ("Engine Oil 15W-40 (Gallon)", "fluids", "Phoenix", 156, 30, "in_stock", "gallons", 300),
        ("Coolant (Gallon)", "fluids", "Detroit", 78, 15, "in_stock", "gallons", 200),
        ("DEF Fluid (2.5 Gal)", "fluids", "Charlotte", 234, 50, "in_stock", "containers", 500),
    ]
    
    actions = []
    for i, (name, category, location, qty, reorder, status, unit, max_capacity) in enumerate(items, start=1):
        item_id = f"INV-{i:04d}"
        doc = {
            "item_id": item_id,
            "tenant_id": TENANT,
            "name": name,
            "category": category,
            "quantity": qty,
            "unit": unit,
            "min_threshold": reorder,
            "max_capacity": max_capacity,
            "status": status,
            "unit_cost": round(random.uniform(5.00, 50.00), 2),
            "location": location,
            "created_at": _ago(days=random.randint(90, 365)),
            "updated_at": _ago(days=random.randint(0, 30)),
        }
        actions.append({"index": {"_index": index, "_id": item_id}})
        actions.append(doc)
    
    _bulk(actions)
    logger.info(f"✅ Seeded {len(items)} inventory items → {index}")


# ---------------------------------------------------------------------------
# 15b. analytics_events (Analytics → Overview tab: metrics + route performance)
# ---------------------------------------------------------------------------
def seed_analytics_events(force: bool = False):
    """Seed time-series analytics events read by the Analytics Overview tab.

    Powers ``GET /api/analytics/metrics`` (latest ``daily_performance`` doc)
    and ``GET /api/analytics/routes`` (``route_performance`` aggregation).
    Without these the Overview tab renders no KPI cards or charts.
    """
    index = "analytics_events"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    routes = [
        ("Houston → Dallas", "houston-dallas"),
        ("Chicago → Detroit", "chicago-detroit"),
        ("Atlanta → Charlotte", "atlanta-charlotte"),
        ("Denver → Phoenix", "denver-phoenix"),
    ]

    actions: list = []
    # 30 days of daily performance + per-route performance events.
    for days_back in range(30, 0, -1):
        ts = _ago(days=days_back)
        perf_id = f"PERF-{days_back:03d}"
        actions.append({"index": {"_index": index, "_id": perf_id}})
        actions.append({
            "event_id": perf_id,
            "event_type": "daily_performance",
            "tenant_id": TENANT,
            "timestamp": ts,
            "region": "All",
            "metrics": {
                "delivery_performance_pct": round(85 + random.uniform(-10, 10), 1),
                "average_delay_minutes": round(120 + random.uniform(-60, 120), 1),
                "fleet_utilization_pct": round(90 + random.uniform(-15, 10), 1),
                "customer_satisfaction": round(4.0 + random.uniform(-0.5, 1.0), 1),
                "total_deliveries": random.randint(15, 35),
                "on_time_deliveries": random.randint(12, 30),
            },
        })
        for route_name, route_id in routes:
            route_doc_id = f"ROUTE-{route_id}-{days_back:03d}"
            actions.append({"index": {"_index": index, "_id": route_doc_id}})
            actions.append({
                "event_id": route_doc_id,
                "event_type": "route_performance",
                "tenant_id": TENANT,
                "timestamp": ts,
                "route_name": route_name,
                "route_id": route_id,
                "metrics": {
                    "performance_pct": round(75 + random.uniform(-15, 20), 1),
                    "avg_delivery_time": round(300 + random.uniform(-120, 180), 1),
                    "delay_incidents": random.randint(0, 5),
                    "completed_trips": random.randint(2, 8),
                },
            })

    _bulk(actions)
    logger.info(f"✅ Seeded analytics events ({len(actions) // 2} docs) → {index}")


# ---------------------------------------------------------------------------
# 15c. shipments_current (Ops Monitoring → Shipment/SLA metrics + Failure
#      Analytics). Elasticsearch is the only store: rev 0007 dropped the
#      ``shipments_current`` Postgres table, so ``shipment`` is no longer a
#      registered hybrid aggregate and the ops read helpers report
#      ``_NOT_CUT_OVER`` and serve these endpoints from ES.
# ---------------------------------------------------------------------------
def seed_shipments(force: bool = False):
    """Seed ops shipment current-state documents in Elasticsearch.

    The ops metrics endpoints (``/ops/metrics/shipments``, ``/ops/metrics/sla``,
    ``/ops/metrics/failures`` and ``/ops/shipments/failures``) aggregate over
    shipments. Without documents here the Ops Monitoring shipment/SLA sections
    and the Failure Analytics tab render empty.

    This previously wrote to the ``shipment`` hybrid aggregate, which rev 0007
    retired along with the table — the seeder then failed the whole entity with
    ``ValueError: Unknown hybrid aggregate_type: 'shipment'``.
    """
    from ops.services.ops_es_service import OpsElasticsearchService

    index = OpsElasticsearchService.SHIPMENTS_CURRENT
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    statuses = ["delivered", "in_transit", "pending", "failed", "returned"]
    failure_reasons = [
        "customer_unavailable",
        "access_denied",
        "weather",
        "vehicle_breakdown",
        "address_not_found",
    ]
    riders = [f"rider-{i:03d}" for i in range(1, 6)]
    cities = CITY_NAMES

    actions: list = []
    for i in range(1, 61):
        status = random.choice(statuses)
        created = _ago(days=random.uniform(1, 28))
        updated = _ago(days=random.uniform(0, 1), hours=random.uniform(0, 23))
        est = _ago(days=random.uniform(-2, 2))  # some past (breached), some future
        origin = random.choice(cities)
        dest = random.choice([c for c in cities if c != origin])
        doc = {
            "shipment_id": f"SHP-{i:04d}",
            "tenant_id": TENANT,
            "status": status,
            "rider_id": random.choice(riders),
            "origin": origin,
            "destination": dest,
            "created_at": created,
            "updated_at": updated,
            "estimated_delivery": est,
            "last_event_timestamp": updated,
            "source_schema_version": SCHEMA_VERSION,
            "trace_id": _uid(),
        }
        if status == "failed":
            doc["failure_reason"] = random.choice(failure_reasons)
        actions.append({"index": {"_index": index, "_id": doc["shipment_id"]}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(actions) // 2} shipment docs → {index}")


# ---------------------------------------------------------------------------
# 15d. storm_road_restrictions (Fuel Ops → Road Restrictions tab). Seeded
#      programmatically (not from the static fixture) so the active window is
#      always relative to "now" — the GET endpoint hides restrictions whose
#      ``effective_to`` is in the past, so fixed fixture dates go stale and the
#      tab renders empty.
# ---------------------------------------------------------------------------
def seed_road_restrictions(force: bool = False):
    """Seed active Storm-Mode road-restriction polygons with a live window."""
    index = "storm_road_restrictions"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Active window: started 6h ago, ends 48h from now → always "current".
    eff_from = _ago(hours=6)
    eff_to = _future(hours=48)

    restrictions = [
        {
            "restriction_id": "RESTRICT-001",
            "tenant_id": TENANT,
            "polygon": {
                "type": "Polygon",
                "coordinates": [[
                    [-96.8500, 32.7500],
                    [-96.7500, 32.7500],
                    [-96.7500, 32.8500],
                    [-96.8500, 32.8500],
                    [-96.8500, 32.7500],
                ]],
            },
            "effective_from": eff_from,
            "effective_to": eff_to,
            "source": "nws",
            "severity": "severe",
            "reason": "Severe thunderstorm warning - flooding and high winds",
            "created_at": _now(),
            "updated_at": _now(),
        },
        {
            "restriction_id": "RESTRICT-002",
            "tenant_id": TENANT,
            "polygon": {
                "type": "Polygon",
                "coordinates": [[
                    [-95.4500, 29.7000],
                    [-95.3000, 29.7000],
                    [-95.3000, 29.8200],
                    [-95.4500, 29.8200],
                    [-95.4500, 29.7000],
                ]],
            },
            "effective_from": eff_from,
            "effective_to": _future(hours=12),
            "source": "manual",
            "severity": "moderate",
            "reason": "Localized street flooding near the ship channel",
            "created_at": _now(),
            "updated_at": _now(),
        },
    ]

    actions = []
    for doc in restrictions:
        actions.append({"index": {"_index": index, "_id": doc["restriction_id"]}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(restrictions)} docs → {index}")


# ---------------------------------------------------------------------------
# 16. Commerce: customers_current
# ---------------------------------------------------------------------------
def seed_commerce_customers(force: bool = False):
    index = "customers_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    customers = [
        ("CUST-001", "Acme Fuel Distribution", "Acme Fuel Distribution LLC", "acme@fuel.com", "12-3456789", "active"),
        ("CUST-002", "Metro Transit Authority", "Metro Transit Authority", "billing@metro.gov", "98-7654321", "active"),
        ("CUST-003", "Green Energy Co", "Green Energy Corporation", "accounts@greenenergy.com", "45-6789012", "active"),
        ("CUST-004", "City Fleet Services", "City Fleet Services Inc", "fleet@cityservices.com", "78-9012345", "active"),
        ("CUST-005", "Industrial Logistics", "Industrial Logistics Group", "billing@indlog.com", "23-4567890", "active"),
    ]

    actions = []
    for cid, display, legal, email, tax_id, status in customers:
        doc = {
            "customer_id": cid,
            "tenant_id": TENANT,
            "display_name": display,
            "legal_name": legal,
            "primary_email": email,
            "tax_id": tax_id,
            "status": status,
            "created_at": _ago(days=random.randint(180, 730)),
            "updated_at": _ago(days=random.randint(1, 30)),
            "external_refs": {},
            "metadata": {},
        }
        actions.append({"index": {"_index": index, "_id": cid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(customers)} docs → {index}")


# ---------------------------------------------------------------------------
# 10. Commerce: accounts_current
# ---------------------------------------------------------------------------
def seed_commerce_accounts(force: bool = False):
    index = "accounts_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create 2 accounts per customer
    accounts = [
        ("ACC-001", "CUST-001", "Acme Main Account", 5000000, 30, "active", "gold", "Houston"),
        ("ACC-002", "CUST-001", "Acme Secondary", 2000000, 15, "active", "silver", "Dallas"),
        ("ACC-003", "CUST-002", "Metro Transit Main", 10000000, 45, "active", "platinum", "Chicago"),
        ("ACC-004", "CUST-002", "Metro Transit Backup", 3000000, 30, "active", "gold", "Chicago"),
        ("ACC-005", "CUST-003", "Green Energy Primary", 4000000, 30, "active", "gold", "Denver"),
        ("ACC-006", "CUST-003", "Green Energy West", 1500000, 15, "active", "bronze", "Phoenix"),
        ("ACC-007", "CUST-004", "City Fleet Main", 6000000, 30, "active", "gold", "Atlanta"),
        ("ACC-008", "CUST-004", "City Fleet Emergency", 2000000, 15, "active", "silver", "Atlanta"),
        ("ACC-009", "CUST-005", "Industrial Logistics HQ", 8000000, 45, "active", "platinum", "Detroit"),
        ("ACC-010", "CUST-005", "Industrial Logistics Regional", 3000000, 30, "active", "gold", "Charlotte"),
    ]

    actions = []
    for aid, cid, name, credit_limit, net_terms, status, tier, city in accounts:
        # Random open balance (0-50% of credit limit)
        open_balance = random.randint(0, credit_limit // 2)
        available_credit = credit_limit - open_balance
        
        doc = {
            "account_id": aid,
            "tenant_id": TENANT,
            "customer_id": cid,
            "display_name": name,
            "status": status,
            "credit_limit_cents": credit_limit,
            "open_balance_cents": open_balance,
            "available_credit_cents": available_credit,
            "credit_balance_cents": 0,
            "credit_state": "ok",
            "credit_override_expires_at": None,
            "net_terms_days": net_terms,
            "tier": tier,
            "billing_address": {
                "line1": f"{random.randint(100, 9999)} {random.choice(['Main', 'Oak', 'Elm', 'Pine'])} St",
                "city": city,
                "state": "TX" if city == "Houston" else ("IL" if city == "Chicago" else "CO"),
                "postal_code": f"{random.randint(10000, 99999)}",
                "country": "US",
            },
            "payment_method_preference": random.choice(["invoice", "ach", "card"]),
            "created_at": _ago(days=random.randint(180, 730)),
            "updated_at": _ago(days=random.randint(1, 30)),
            "external_refs": {},
        }
        actions.append({"index": {"_index": index, "_id": aid}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(accounts)} docs → {index}")


# ---------------------------------------------------------------------------
# 11. Commerce: invoices_current
# ---------------------------------------------------------------------------
def seed_commerce_invoices(force: bool = False):
    index = "invoices_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create 10 invoices across different accounts
    account_ids = [f"ACC-{i:03d}" for i in range(1, 11)]
    customer_ids = ["CUST-001", "CUST-001", "CUST-002", "CUST-002", "CUST-003", 
                    "CUST-003", "CUST-004", "CUST-004", "CUST-005", "CUST-005"]
    
    statuses = ["open", "open", "open", "partial", "partial", "paid", "paid", "overdue", "overdue", "draft"]
    
    actions = []
    for i in range(1, 11):
        inv_id = f"INV-{i:04d}"
        acc_id = account_ids[i-1]
        cust_id = customer_ids[i-1]
        status = statuses[i-1]
        
        # Generate 1-3 line items
        line_items = []
        subtotal = 0
        for li in range(random.randint(1, 3)):
            product = random.choice(["DIESEL_2", "GASOLINE_REG", "GASOLINE_PREM", "KEROSENE"])
            quantity = round(random.uniform(500, 5000), 2)
            unit_price = random.randint(250, 450)  # $2.50-$4.50 per gallon in cents
            line_subtotal = int(quantity * unit_price)
            subtotal += line_subtotal
            
            line_items.append({
                "line_id": f"LINE-{inv_id}-{li+1}",
                "product_code": product,
                "quantity_gallons": quantity,
                "unit_price_cents": unit_price,
                "subtotal_cents": line_subtotal,
            })
        
        tax = int(subtotal * 0.08)  # 8% tax
        total = subtotal + tax
        
        # Determine payment amounts based on status
        if status == "paid":
            amount_paid = total
            remaining = 0
        elif status == "partial":
            amount_paid = int(total * random.uniform(0.3, 0.7))
            remaining = total - amount_paid
        else:
            amount_paid = 0
            remaining = total
        
        issued_at = _ago(days=random.randint(1, 60))
        due_date = _future(days=random.randint(-10, 30))  # Some overdue
        
        doc = {
            "invoice_id": inv_id,
            "tenant_id": TENANT,
            "customer_id": cust_id,
            "account_id": acc_id,
            "order_id": f"ORD-{i:04d}" if random.random() > 0.3 else None,
            "invoice_number": f"INV-2026-{i:04d}",
            "status": status,
            "total_cents": total,
            "amount_paid_cents": amount_paid,
            "remaining_cents": remaining,
            "tax_cents": tax,
            "subtotal_cents": subtotal,
            "line_items": line_items,
            "issued_at": issued_at,
            "due_date": due_date,
            "finalized_at": issued_at if status != "draft" else None,
            "voided_at": None,
            "void_reason": None,
            "qbo_push_state": random.choice(["pending", "pushed", "pushed", "pushed"]),
            "qbo_push_attempts": random.randint(0, 2),
            "qbo_push_last_error": None,
            "external_refs": {},
            "created_at": _ago(days=random.randint(1, 60)),
            "updated_at": _ago(days=random.randint(0, 5)),
        }
        actions.append({"index": {"_index": index, "_id": inv_id}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded 10 invoices → {index}")


# ---------------------------------------------------------------------------
# 12. Commerce: payments_current
# ---------------------------------------------------------------------------
def seed_commerce_payments(force: bool = False):
    index = "payments_current"
    if not force and _index_count(index) > 0:
        logger.info(f"⏭️  {index} already has data — skipping")
        return

    # Create payments for invoices 4, 5, 6, 7 (partial and paid ones)
    payments = [
        ("PAY-001", "INV-0004", "ACC-004", 150000, "manual", "check", "CHK-12345", "applied"),
        ("PAY-002", "INV-0005", "ACC-005", 200000, "stripe", "card", "ch_3abc123", "applied"),
        ("PAY-003", "INV-0005", "ACC-005", 100000, "manual", "ach", "ACH-98765", "applied"),
        ("PAY-004", "INV-0006", "ACC-006", 180000, "manual", "wire", "WIRE-54321", "applied"),
        ("PAY-005", "INV-0007", "ACC-007", 250000, "qbo", "check", "QBO-INV-007", "applied"),
        ("PAY-006", "INV-0008", "ACC-008", 120000, "manual", "check", "CHK-67890", "reversed"),
    ]

    actions = []
    for pay_id, inv_id, acc_id, amount, source, method, ref, status in payments:
        received_at = _ago(days=random.randint(1, 30))
        doc = {
            "payment_id": pay_id,
            "tenant_id": TENANT,
            "invoice_id": inv_id,
            "account_id": acc_id,
            "amount_cents": amount,
            "source": source,
            "method": method,
            "external_id": ref if source != "manual" else None,
            "reference": ref,
            "status": status,
            "received_at": received_at,
            "applied_at": received_at,
            "reversed_at": _ago(days=1) if status == "reversed" else None,
        }
        actions.append({"index": {"_index": index, "_id": pay_id}})
        actions.append(doc)

    _bulk(actions)
    logger.info(f"✅ Seeded {len(payments)} payments → {index}")


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
    recreate = "--recreate" in sys.argv
    assume_yes = "--yes" in sys.argv
    skip_json = "--skip-json" in sys.argv
    skip_programmatic = "--skip-programmatic" in sys.argv

    print("=" * 60)
    print("  Runsheet — Seed (single entry point)")
    print("=" * 60)
    print(f"  Tenant:            {TENANT}")
    print(f"  Recreate indices:  {'YES (destructive)' if recreate else 'no'}")
    print(f"  Force re-seed:     {'YES' if force else 'no'}")
    print(f"  Skip JSON:         {'YES' if skip_json else 'no'}")
    print(f"  Skip programmatic: {'YES' if skip_programmatic else 'no'}")
    print("=" * 60)

    try:
        if not ES.ping():
            print("❌ Cannot reach Elasticsearch. Check your .env / connection settings.")
            sys.exit(1)
        print("✅ Elasticsearch connection OK\n")
    except Exception as e:
        print(f"❌ Elasticsearch connection failed: {e}")
        sys.exit(1)

    # ----- Step 1: indices -------------------------------------------------
    #
    # Gone with Elasticsearch. There are no indices to create or recreate: the
    # document store is one Postgres table, created by ``alembic upgrade head``.
    # ``--recreate`` used to DROP every managed index and rebuild it from the
    # mapping registries; the equivalent now is a migration, and dropping data is
    # not something a seed script should offer as a flag.
    print(f"{'═' * 60}\n  Step 1: Index mappings — skipped\n{'═' * 60}")
    print("  The document store is PostgreSQL; run 'alembic upgrade head' instead.")
    if recreate:
        print("  --recreate is a no-op: there are no Elasticsearch indices to drop.")

    # ----- Step 2: static JSON fixtures -----------------------------------
    if not skip_json:
        print(f"{'═' * 60}\n  Step 2: Static JSON fixtures\n{'═' * 60}")
        load_json_fixtures(force=force)
    else:
        print("⏭️  Step 2 skipped (--skip-json)")

    # ----- Step 3: programmatic demo data ---------------------------------
    if skip_programmatic:
        print("⏭️  Step 3 skipped (--skip-programmatic)")
    else:
        print(f"{'═' * 60}\n  Step 3: Programmatic demo data\n{'═' * 60}")
        seeders = [
            ("trucks",                seed_trucks),
            ("riders_current",        seed_riders),
            ("jobs_current",          seed_jobs),
            ("fuel_stations",         seed_fuel_stations),
            ("mvp_tank_forecasts",    seed_tank_forecasts),
            ("customer_tanks",        seed_customer_tanks),
            ("truck_compartments",    seed_truck_compartments),
            ("inventory",             seed_inventory),
            ("fuel_events",           seed_fuel_events),
            ("fuel_orders_current",   seed_fuel_orders),
            ("agent_memory",          seed_agent_memory),
            ("agent_approval_queue",  seed_approval_queue),
            ("ops_poison_queue",      seed_poison_queue),
            ("drivers_current",       seed_drivers_current),
            ("drivers",               seed_drivers_qualifications),
            ("analytics_events",      seed_analytics_events),
            ("shipments_current",     seed_shipments),
            ("storm_road_restrictions", seed_road_restrictions),
            ("customers_current",     seed_commerce_customers),
            ("accounts_current",      seed_commerce_accounts),
            ("invoices_current",      seed_commerce_invoices),
            ("payments_current",      seed_commerce_payments),
        ]

        for name, fn in seeders:
            try:
                print(f"{'─' * 40}")
                print(f"  Seeding: {name}")
                fn(force=force)
            except Exception as e:
                logger.exception("Failed to seed %s", name)
                print(f"  ❌ Error seeding {name}: {e}")

    print(f"\n{'=' * 60}")
    print("  ✅ Seeding complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
