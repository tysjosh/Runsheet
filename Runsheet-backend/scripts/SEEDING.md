# Seeding Guide

All seeding is done through **one script**: `seed_all_data.py` (at the backend root).
There is nothing else to run.

## One command

```bash
# Fill empty indices only (safe, idempotent):
SEED_TENANT_ID=demo-tenant python3 seed_all_data.py

# Re-seed everything, overwriting existing seed data:
SEED_TENANT_ID=demo-tenant python3 seed_all_data.py --force

# DROP and recreate all index mappings first, then seed (DESTRUCTIVE):
SEED_TENANT_ID=demo-tenant python3 seed_all_data.py --recreate          # prompts for confirmation
SEED_TENANT_ID=demo-tenant python3 seed_all_data.py --recreate --yes    # no prompt
```

`SEED_TENANT_ID` is **required** — every seeded document is stamped with it so
seed data never leaks into a real tenant's queries.

## What it does (three steps, in order)

| Step | What | Notes |
|------|------|-------|
| 1 | **Create index mappings** | Runs every domain's idempotent `setup_*_indices()`. With `--recreate`, first drops every managed index. The managed-index list is derived from the domain mapping registries, so it can never drift. |
| 2 | **Load static JSON fixtures** | Auto-discovers and loads every `*.json` in `scripts/data/`. |
| 3 | **Generate programmatic demo data** | Trucks, jobs, riders, fuel stations, commerce, agent memory, etc. — randomized, timestamp-relative data written in Python. |

Useful flags:

```bash
--skip-json          # skip step 2 (static fixtures)
--skip-programmatic  # skip step 3 (generated data)
```

## Adding a new static JSON fixture

Drop a `*.json` file into `scripts/data/`. It's auto-discovered — no list to update.

Format: a top-level object mapping **ES index name → list of records**:

```json
{
  "my_index": [
    { "my_index_id": "abc-1", "field": "value" }
  ]
}
```

The loader stamps `tenant_id`, `created_at`, `updated_at` if absent and picks the
document `_id` from the first matching ID field. Idempotent by default
(populated indices are skipped unless `--force`).

## Files

```
seed_all_data.py          # ← the only thing you run (setup + JSON + generated data)
scripts/
├── SEEDING.md            # this file
├── import_tax_jurisdictions.py   # separate CLI for the tax-rate CSV
└── data/                 # static JSON fixtures (auto-discovered)
    ├── compliance_seeds.json
    ├── customer_tanks_seeds.json
    ├── driver_seeds.json
    ├── fuel_ops_seeds.json
    ├── mvp_overlay_seeds.json
    ├── notification_seeds.json
    ├── stripe_payment_seeds.json
    └── sample_tax_jurisdictions.csv   # loaded via import_tax_jurisdictions.py
```

> The tax-jurisdiction CSV is loaded by `scripts/import_tax_jurisdictions.py`
> (it needs CSV-specific parsing). Everything else goes through `seed_all_data.py`.
>
> `services/data_seeder.py` is a separate path the running app uses at startup
> when `SEED_DEMO_DATA=true` (stamps `tenant_id="demo"`). For local/dev setup,
> use `seed_all_data.py`.

## Drivers: one roster, two indices

Drivers appear in two places that read **different** ES indices:

- **Utilization tab** → `drivers_current` (ops workload: status, active orders, location)
- **Qualifications tab** → `drivers` (compliance: CDL, medical card, endorsements)

To keep the same driver consistent across both tabs, `seed_all_data.py` defines
a single `_DRIVER_ROSTER` (driver_id, name, city, medical-card offset, etc.).
`seed_drivers_current()` and `seed_drivers_qualifications()` both build from it,
so `DRV-003` is the same person in both views and its medical-card
expiring/expired warning lines up in each. Edit the roster in one place to
change both.
