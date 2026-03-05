#!/usr/bin/env python3
"""
Backfill migration script: sets asset_type, asset_subtype, and asset_name
on all existing truck documents that lack these fields.

This script is idempotent — it skips documents that already have asset_type set.

Usage:
    python -m scripts.backfill_asset_type          # from Runsheet-backend/
    python scripts/backfill_asset_type.py           # standalone
"""

import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path so config imports work
# when running as a standalone script.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def _connect(settings) -> Elasticsearch:
    """Create an Elasticsearch client using app settings."""
    client = Elasticsearch(
        settings.elastic_endpoint.strip('"'),
        api_key=settings.elastic_api_key.strip('"'),
        verify_certs=True,
        request_timeout=30,
    )
    if not client.ping():
        raise ConnectionError("Failed to ping Elasticsearch")
    return client


def _find_trucks_without_asset_type(client: Elasticsearch, index: str) -> list:
    """
    Return all document IDs (and plate_number) for trucks that do NOT
    already have an ``asset_type`` field set.
    Uses scroll API to handle large result sets.
    """
    query = {
        "query": {
            "bool": {
                "must_not": [
                    {"exists": {"field": "asset_type"}}
                ]
            }
        },
        "_source": ["truck_id", "plate_number"],
    }

    docs: list[dict] = []
    resp = client.search(index=index, body=query, scroll="2m", size=BATCH_SIZE)
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]

    while hits:
        for hit in hits:
            docs.append({
                "_id": hit["_id"],
                "plate_number": hit["_source"].get("plate_number", ""),
            })
        resp = client.scroll(scroll_id=scroll_id, scroll="2m")
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]

    if scroll_id:
        try:
            client.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass  # best-effort cleanup

    return docs


def _build_bulk_actions(index: str, docs: list) -> list:
    """Build Elasticsearch bulk-update action dicts."""
    actions = []
    for doc in docs:
        asset_name = doc["plate_number"] or doc["_id"]
        actions.append({
            "_op_type": "update",
            "_index": index,
            "_id": doc["_id"],
            "doc": {
                "asset_type": "vehicle",
                "asset_subtype": "truck",
                "asset_name": asset_name,
            },
        })
    return actions


def run_backfill() -> int:
    """
    Execute the backfill migration.

    Returns the number of documents updated.
    """
    settings = get_settings()
    client = _connect(settings)
    index = "trucks"

    logger.info("Searching for truck documents without asset_type …")
    docs = _find_trucks_without_asset_type(client, index)

    if not docs:
        logger.info("No documents need updating — backfill already complete.")
        return 0

    logger.info("Found %d document(s) to update.", len(docs))

    actions = _build_bulk_actions(index, docs)
    success, errors = bulk(client, actions, raise_on_error=False)

    if errors:
        logger.warning("Bulk update completed with %d error(s):", len(errors))
        for err in errors[:10]:  # log first 10 errors
            logger.warning("  %s", err)
    else:
        logger.info("Bulk update completed successfully.")

    logger.info("Updated %d document(s) (asset_type='vehicle', asset_subtype='truck').", success)
    return success


if __name__ == "__main__":
    try:
        count = run_backfill()
        logger.info("Backfill finished. Total updated: %d", count)
    except Exception as exc:
        logger.error("Backfill failed: %s", exc)
        sys.exit(1)
