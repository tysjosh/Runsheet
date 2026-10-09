"""Every JSON seed fixture must load without silently losing rows.

``_resolve_json_doc_id`` picks the Elasticsearch ``_id`` for each record in
``scripts/data/*.json``. It used to consult only the ordered
:data:`seed_all_data._JSON_ID_FIELDS` list, so a record carrying both its own key
and a foreign key was keyed by whichever name appeared earlier. Two fixtures did:

* ``rack_prices`` was keyed by ``terminal_id``, which precedes ``rack_price_id``
  in the list. The 5-row fixture loaded as **3 documents** — RACK-002 overwrote
  RACK-001 and RACK-004 overwrote RACK-003, each pair sharing a terminal. Rack
  prices are per (terminal, product) and the sourcing recommender scores
  candidates on them, so every terminal kept only its last product's price.
* ``customer_tanks`` was keyed by ``customer_id``. Latent, because no fixture
  gives one customer two tanks — but the domain supports it and the production
  repository keys on ``customer_tank_id``.

A duplicate ``_id`` in a bulk index is an ordinary overwrite, so neither case
logged anything. The general property below is what catches the next one:
**within an index, N fixture records must produce N distinct ids.**
"""
from __future__ import annotations

import json
import pathlib

import pytest

from seed_all_data import (
    TENANT,
    _JSON_ID_FIELDS,
    _natural_id_fields,
    _resolve_json_doc_id,
)

DATA_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "data"


def _as_loaded(record: dict) -> dict:
    """Apply the same defaults ``_load_json_file`` applies before resolving an id.

    The loader stamps ``tenant_id`` onto every record first, and
    ``weather_observations``' composite id needs it. Resolving against the raw
    fixture row would test a pipeline that does not exist.
    """
    doc = dict(record)
    doc.setdefault("tenant_id", TENANT)
    return doc


def _fixture_indices():
    """Yield ``(filename, index_name, records)`` for every seeded index."""
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # pragma: no cover - a broken fixture
            continue
        if not isinstance(data, dict):
            continue
        for index_name, records in data.items():
            if isinstance(records, list) and records:
                yield path.name, index_name, records


ALL_FIXTURES = list(_fixture_indices())


def test_the_data_directory_was_found():
    """Guard the guard: a wrong path would make every test below vacuous."""
    assert DATA_DIR.is_dir(), DATA_DIR
    assert ALL_FIXTURES, f"no JSON fixtures discovered under {DATA_DIR}"


@pytest.mark.parametrize(
    "filename,index_name,records",
    [pytest.param(f, i, r, id=f"{f}:{i}") for f, i, r in ALL_FIXTURES],
)
def test_no_fixture_collapses_rows_on_load(filename, index_name, records):
    """N records in, N documents out. Anything less is silent data loss."""
    ids = [_resolve_json_doc_id(index_name, _as_loaded(r)) for r in records]

    unresolved = [i for i, v in enumerate(ids) if v is None]
    assert not unresolved, (
        f"{filename}:{index_name} has {len(unresolved)} record(s) with no "
        f"resolvable id; the seeder logs a warning and SKIPS them"
    )

    duplicates = {v for v in ids if ids.count(v) > 1}
    assert not duplicates, (
        f"{filename}:{index_name} maps {len(records)} record(s) onto "
        f"{len(set(ids))} distinct id(s) — the duplicates {sorted(duplicates)} "
        f"overwrite each other on bulk load, silently. Give the index its own "
        f"key, or add it to _natural_id_fields."
    )


class TestOwnKeyWinsOverForeignKey:
    """The two fixtures that were wrong, pinned by name."""

    def test_rack_prices_are_keyed_by_rack_price_id(self):
        doc = {
            "rack_price_id": "RACK-001",
            "terminal_id": "TERM-001",
            "product_code": "DIESEL_2",
        }
        assert _resolve_json_doc_id("rack_prices", doc) == "RACK-001", (
            "keyed by terminal_id again: every terminal keeps only its last "
            "product's rack price"
        )

    def test_customer_tanks_are_keyed_by_customer_tank_id(self):
        doc = {
            "customer_tank_id": "TANK-001",
            "customer_id": "CUST-001",
            "fuel_type": "heating_oil",
        }
        assert _resolve_json_doc_id("customer_tanks", doc) == "TANK-001", (
            "keyed by customer_id again: a customer's second tank overwrites "
            "the first, and the production repository keys on customer_tank_id"
        )

    def test_two_tanks_for_one_customer_stay_two_documents(self):
        """The latent case, made explicit."""
        records = [
            {"customer_tank_id": "TANK-001", "customer_id": "CUST-001"},
            {"customer_tank_id": "TANK-002", "customer_id": "CUST-001"},
        ]
        ids = [_resolve_json_doc_id("customer_tanks", dict(r)) for r in records]
        assert len(set(ids)) == 2, ids

    def test_two_products_at_one_terminal_stay_two_documents(self):
        records = [
            {"rack_price_id": "RACK-001", "terminal_id": "TERM-001"},
            {"rack_price_id": "RACK-002", "terminal_id": "TERM-001"},
        ]
        ids = [_resolve_json_doc_id("rack_prices", dict(r)) for r in records]
        assert len(set(ids)) == 2, ids


class TestOverridesMatchTheProductionWriter:
    """A seeded document and a live one must be the SAME document.

    If the seeder invented its own id, the same fact would exist twice: once
    under the seeded id and once under the id the service writes. Each override
    therefore mirrors its production writer.
    """

    def test_atg_readings_use_reading_id(self):
        """``TankImportService`` indexes under ``reading_id``."""
        doc = {
            "reading_id": "atg_import_abc123",
            "instance_id": "INT-001",
            "customer_tank_id": "TANK-001",
        }
        assert _resolve_json_doc_id("atg_readings", doc) == "atg_import_abc123", (
            "keyed by instance_id again: every ATG reading from one integration "
            "instance overwrites the previous one"
        )

    def test_two_readings_from_one_instance_stay_two_documents(self):
        records = [
            {"reading_id": "r1", "instance_id": "INT-001"},
            {"reading_id": "r2", "instance_id": "INT-001"},
        ]
        ids = [_resolve_json_doc_id("atg_readings", dict(r)) for r in records]
        assert len(set(ids)) == 2, ids

    def test_weather_observations_match_the_providers_composite_key(self):
        """``WeatherProvider._persist_observations`` builds this exact string."""
        doc = {
            "tenant_id": "demo-tenant",
            "provider": "openweather",
            "zip_code": "77002",
            "date": "2026-01-15",
        }
        assert (
            _resolve_json_doc_id("weather_observations", doc)
            == "wxobs:demo-tenant:openweather:77002:2026-01-15"
        )

    def test_weather_observations_without_the_composite_parts_resolve_to_none(self):
        """Better a skipped row with a warning than a wrong id."""
        assert _resolve_json_doc_id("weather_observations", {"zip_code": "77002"}) is None

    def test_one_zip_across_two_dates_stays_two_observations(self):
        records = [
            {"tenant_id": "t", "provider": "p", "zip_code": "77002", "date": "2026-01-15"},
            {"tenant_id": "t", "provider": "p", "zip_code": "77002", "date": "2026-01-16"},
        ]
        ids = [_resolve_json_doc_id("weather_observations", dict(r)) for r in records]
        assert len(set(ids)) == 2, ids


class TestTheGenericListIsStillTheFallback:
    """Indices whose key does not follow the naming convention must still work.

    Preferring the natural key would be a regression if it broke these, so they
    are pinned rather than assumed.
    """

    def test_fuel_orders_current_falls_back_to_order_id(self):
        doc = {"order_id": "ORD-1", "customer_id": "CUST-001"}
        assert _resolve_json_doc_id("fuel_orders_current", doc) == "ORD-1"

    def test_an_index_with_a_bare_id_field_uses_it(self):
        doc = {"id": "X-1"}
        assert _resolve_json_doc_id("anything", doc) == "X-1"

    def test_no_id_at_all_returns_none_so_the_seeder_can_warn(self):
        assert _resolve_json_doc_id("mystery", {"name": "no id here"}) is None


class TestNaturalIdFields:
    def test_plural_index_yields_singular_and_plural_forms(self):
        assert _natural_id_fields("rack_prices") == ("rack_prices_id", "rack_price_id")

    def test_current_suffix_is_stripped(self):
        assert _natural_id_fields("jobs_current") == ("jobs_id", "job_id")

    def test_singular_index_is_handled(self):
        assert _natural_id_fields("trucks") == ("trucks_id", "truck_id")

    def test_the_generic_list_is_still_populated(self):
        """If this list were emptied the fallback tests above would pass
        vacuously."""
        assert len(_JSON_ID_FIELDS) > 20
