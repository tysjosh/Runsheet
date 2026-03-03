"""
Test fixtures package.

Provides helpers for loading Dinee webhook JSON fixtures used in
contract, integration, and unit tests.
"""

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).parent


def load_fixture(name: str) -> dict[str, Any]:
    """
    Load a JSON fixture by name from the ``dinee_webhooks`` subdirectory.

    Args:
        name: Fixture filename without the ``.json`` extension,
              e.g. ``"shipment_created"``.

    Returns:
        Parsed JSON as a dict.

    Raises:
        FileNotFoundError: If the fixture file does not exist.
    """
    path = _FIXTURES_DIR / "dinee_webhooks" / f"{name}.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_all_webhook_fixtures() -> dict[str, dict[str, Any]]:
    """
    Load all Dinee webhook fixtures.

    Returns:
        Dict mapping fixture name (without extension) to parsed JSON.
    """
    fixtures_dir = _FIXTURES_DIR / "dinee_webhooks"
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(fixtures_dir.glob("*.json")):
        result[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return result
