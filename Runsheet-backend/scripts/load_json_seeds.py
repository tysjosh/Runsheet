#!/usr/bin/env python3
"""
Load seed data from JSON files into Elasticsearch indices.

Usage:
    SEED_TENANT_ID=tenant-demo python scripts/load_json_seeds.py
    SEED_TENANT_ID=tenant-demo python scripts/load_json_seeds.py --force
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from elasticsearch import Elasticsearch
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
env_file = Path(__file__).parent.parent / ".env.development"
load_dotenv(env_file)

# Initialize Elasticsearch client directly
ELASTIC_ENDPOINT = os.getenv("ELASTIC_ENDPOINT")
ELASTIC_API_KEY = os.getenv("ELASTIC_API_KEY")

if not ELASTIC_ENDPOINT or not ELASTIC_API_KEY:
    raise SystemExit("ELASTIC_ENDPOINT and ELASTIC_API_KEY must be set in .env.development")

ES = Elasticsearch(
    ELASTIC_ENDPOINT,
    api_key=ELASTIC_API_KEY,
    request_timeout=30,
    max_retries=3,
    retry_on_timeout=True
)
TENANT = os.environ.get("SEED_TENANT_ID", "").strip()
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"


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
                    logger.error("Bulk error: %s", detail["error"])


def _add_timestamps(doc: dict) -> dict:
    """Add created_at and updated_at timestamps if not present."""
    now = datetime.now(timezone.utc).isoformat()
    if "created_at" not in doc:
        doc["created_at"] = now
    if "updated_at" not in doc:
        doc["updated_at"] = now
    return doc


def _add_tenant(doc: dict) -> dict:
    """Add tenant_id if not present."""
    if "tenant_id" not in doc:
        doc["tenant_id"] = TENANT
    return doc


def load_json_file(filepath: Path, force: bool = False):
    """Load seed data from a JSON file."""
    if not filepath.exists():
        logger.warning(f"File not found: {filepath}")
        return

    logger.info(f"Loading seed data from: {filepath.name}")
    
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    total_loaded = 0
    
    for index_name, records in data.items():
        if not isinstance(records, list):
            logger.warning(f"Skipping {index_name}: not a list")
            continue
        
        if not force and _index_count(index_name) > 0:
            logger.info(f"⏭️  {index_name} already has data — skipping")
            continue
        
        if not records:
            logger.info(f"⏭️  {index_name} has no records — skipping")
            continue
        
        actions = []
        for record in records:
            # Add tenant_id and timestamps
            doc = _add_tenant(record.copy())
            doc = _add_timestamps(doc)
            
            # Determine document ID - comprehensive list of ID fields
            doc_id = None
            id_fields = [
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
                "truck_id", "rider_id", "station_id", "tank_id", "memory_id", "action_id",
                # Fallback: try singular form of index name
                f"{index_name.rstrip('s')}_id",
                f"{index_name.replace('_current', '').rstrip('s')}_id",
            ]
            
            for id_field in id_fields:
                if id_field in doc:
                    doc_id = doc[id_field]
                    break
            
            if not doc_id:
                logger.warning(f"No ID field found for record in {index_name}: {list(doc.keys())[:5]}")
                continue
            
            actions.append({"index": {"_index": index_name, "_id": doc_id}})
            actions.append(doc)
        
        if actions:
            _bulk(actions)
            count = len(actions) // 2
            total_loaded += count
            logger.info(f"✅ Loaded {count} records → {index_name}")
    
    return total_loaded


def main():
    if not TENANT:
        raise SystemExit(
            "SEED_TENANT_ID is required; refusing to seed records with a "
            "hardcoded/default tenant."
        )

    force = "--force" in sys.argv

    print("=" * 60)
    print("  Runsheet — JSON Seed Data Loader")
    print("=" * 60)
    print(f"  Tenant: {TENANT}")
    print(f"  Data Directory: {DATA_DIR}")
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

    # Load all JSON files in the data directory
    json_files = [
        "compliance_seeds.json",
        "notification_seeds.json",
        "driver_seeds.json",
        "fuel_ops_seeds.json",
        "mvp_overlay_seeds.json",
        "commerce_seeds.json",
        "inventory_seeds.json",
        "scheduling_seeds.json",
        "agent_seeds.json",
        "stripe_payment_seeds.json",
    ]

    total_records = 0
    for filename in json_files:
        filepath = DATA_DIR / filename
        try:
            print(f"{'─' * 40}")
            print(f"  Processing: {filename}")
            count = load_json_file(filepath, force=force)
            if count:
                total_records += count
        except Exception as e:
            logger.exception(f"Failed to load {filename}")
            print(f"  ❌ Error loading {filename}: {e}")

    print(f"\n{'=' * 60}")
    print(f"  Seeding complete! Loaded {total_records} total records")
    print("=" * 60)


if __name__ == "__main__":
    main()
