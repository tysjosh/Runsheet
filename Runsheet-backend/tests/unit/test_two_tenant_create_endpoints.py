"""Every create endpoint that accepts a client id is create-if-absent (L1).

One parametrized table, so it is the single regression list for this class of
leak. Each case runs the real router and the real repository over one shared
in-memory store keyed like production, by ``(index_name, doc_id)`` with no
tenant (the SQLite document store can't answer ``term`` queries, which use the
Postgres ``@>`` operator):

1. tenant A creates id X → 201;
2. tenant B creates X → 409 ``RESOURCE_ALREADY_EXISTS``, and the body never
   names tenant A;
3. A still reads its own record, unchanged; B can't see it.

Stations and fleet assets are fixed the same way, but their endpoints live on
``main.app`` and are covered by ``tests/postgres/test_create_if_absent_two_tenant.py``
and ``tests/unit/test_create_asset_endpoint.py``.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

A = "tenant-a"
B = "tenant-b"


def _field(doc: Dict[str, Any], path: str) -> Any:
    value: Any = doc
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _clauses(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _matches(doc: Dict[str, Any], query: Optional[Dict[str, Any]]) -> bool:
    """The query subset the repositories under test send. Unknown → raise."""
    if not query or "match_all" in query:
        return True
    if "term" in query:
        ((path, expected),) = query["term"].items()
        if isinstance(expected, dict):
            expected = expected.get("value")
        actual = _field(doc, path.removesuffix(".keyword"))
        return expected in actual if isinstance(actual, list) else actual == expected
    if "terms" in query:
        ((path, options),) = query["terms"].items()
        actual = _field(doc, path.removesuffix(".keyword"))
        values = actual if isinstance(actual, list) else [actual]
        return any(v in options for v in values)
    if "exists" in query:
        return _field(doc, query["exists"]["field"]) is not None
    if "range" in query:
        return True
    if "bool" in query:
        b = query["bool"]
        if not all(_matches(doc, c) for c in _clauses(b.get("must")) + _clauses(b.get("filter"))):
            return False
        if any(_matches(doc, c) for c in _clauses(b.get("must_not"))):
            return False
        should = _clauses(b.get("should"))
        if should and not any(_matches(doc, c) for c in should):
            return False
        return True
    raise AssertionError(f"query clause not supported by the test store: {query}")


class _GlobalIdStore:
    """ElasticsearchService data plane keyed like production: (index, doc_id)."""

    def __init__(self) -> None:
        self.docs: Dict[tuple, Dict[str, Any]] = {}

    async def index_document(self, index, doc_id, document, **kw):
        self.docs[(index, doc_id)] = deepcopy(document)
        return {"result": "created"}

    async def create_document(self, index, doc_id, document):
        if (index, doc_id) in self.docs:
            return False
        self.docs[(index, doc_id)] = deepcopy(document)
        return True

    async def document_exists(self, index, doc_id):
        return (index, doc_id) in self.docs

    async def get_document(self, index, doc_id):
        doc = self.docs.get((index, doc_id))
        return deepcopy(doc) if doc is not None else None

    async def update_document(self, index, doc_id, partial):
        self.docs[(index, doc_id)].update(deepcopy(partial))
        return {"result": "updated"}

    async def delete_document(self, index, doc_id):
        return self.docs.pop((index, doc_id), None) is not None

    async def search_documents(self, index, query, size=100, **kw):
        body = query or {}
        hits = [
            {"_id": doc_id, "_source": deepcopy(doc)}
            for (idx, doc_id), doc in self.docs.items()
            if idx == index and _matches(doc, body.get("query"))
        ][: body.get("size", size)]
        return {"hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}}


class _Vault:
    def __init__(self) -> None:
        self.n = 0
        self.deleted = []

    async def put(self, tenant_id, key, plaintext, provider_name=None, **kw):
        self.n += 1
        return f"cred:{tenant_id}:{key}:{self.n}"

    async def delete(self, tenant_id, ref):
        self.deleted.append(ref)
        return True


class _Scheduler:
    async def schedule_instance(self, instance):
        return True

    async def unschedule_instance(self, instance_id):
        return True


class _Redis:
    async def get(self, key):
        return None

    async def incrbyfloat(self, key, amount):
        return float(amount)

    async def expire(self, key, ttl):
        return True


@dataclass
class Case:
    post: str
    body: Dict[str, Any]
    id_field: str
    wire: Callable[[FastAPI, "_GlobalIdStore"], None]
    #: GET path for one record, or ``None`` to check the list endpoint.
    get: Optional[str] = None
    list_path: Optional[str] = None
    changed: Dict[str, Any] = field(default_factory=dict)


def _fuel_ops(**repos):
    def wire(app: FastAPI, es: _GlobalIdStore) -> None:
        from fuel.api.fuel_ops_endpoints import (
            configure_fuel_ops_endpoints,
            mvp_router,
            router,
        )

        built = {name: factory(es) for name, factory in repos.items()}
        if "supplier_contract_repository" in built:
            from fuel.services.contract_lift_service import ContractLiftService

            built["contract_lift_service"] = ContractLiftService(redis_client=_Redis())
        configure_fuel_ops_endpoints(es_service=es, **built)
        app.include_router(router)
        app.include_router(mvp_router)

    return wire


def _drivers(app: FastAPI, es: _GlobalIdStore) -> None:
    from fuel.api.driver_endpoints import configure_driver_endpoints, router
    from fuel.driver_repository import DriverRepository

    configure_driver_endpoints(driver_repository=DriverRepository(es))
    app.include_router(router)


def _intake_channels(app: FastAPI, es: _GlobalIdStore) -> None:
    from fuel.intake_channel_repository import IntakeChannelRepository
    from integrations.api.intake_channel_endpoints import (
        configure_intake_channel_endpoints,
        router,
    )

    configure_intake_channel_endpoints(repository=IntakeChannelRepository(es, _Vault()))
    app.include_router(router)


def _integrations(app: FastAPI, es: _GlobalIdStore) -> None:
    from integrations.api.integrations_endpoints import (
        configure_integrations_endpoints,
        router,
    )
    from integrations.connector_base import IntegrationInstanceRepository

    configure_integrations_endpoints(
        repository=IntegrationInstanceRepository(es_service=es),
        scheduler=_Scheduler(),  # type: ignore[arg-type]
        credentials_vault=_Vault(),
        es_service=es,
    )
    app.include_router(router)


def _tank_repo(es):
    from fuel.customer_tank_models import CustomerTankRepository

    return CustomerTankRepository(es_service=es)


def _terminal_repo(es):
    from fuel.terminal_models import TerminalRepository

    return TerminalRepository(es_service=es)


def _contract_repo(es):
    from fuel.terminal_models import SupplierContractRepository

    return SupplierContractRepository(es_service=es)


def _depot_repo(es):
    from fuel.depot_models import DepotRepository

    return DepotRepository(es_service=es)


CASES: Dict[str, Case] = {
    "customer_tanks": Case(
        post="/api/fuel/mvp/customer-tanks",
        get="/api/fuel/mvp/customer-tanks/QA-TANK-X",
        id_field="customer_tank_id",
        wire=_fuel_ops(customer_tank_repository=_tank_repo),
        body={
            "customer_tank_id": "QA-TANK-X",
            "customer_id": "cust_001",
            "customer_type": "residential",
            "fuel_type": "propane",
            "fuel_product_code": "PROPANE",
            "capacity_gallons": 500.0,
            "current_level_gallons": 250.0,
            "location_lat": 40.7128,
            "location_lon": -74.0060,
            "zip_code": "10001",
            "status": "active",
        },
        changed={"current_level_gallons": 10.0},
    ),
    "terminals": Case(
        post="/api/fuel/terminals",
        get="/api/fuel/terminals/QA-TERM-X",
        id_field="terminal_id",
        wire=_fuel_ops(terminal_repository=_terminal_repo),
        body={
            "terminal_id": "QA-TERM-X",
            "name": "A's rack",
            "operator": "Buckeye",
            "location_lat": 40.7357,
            "location_lon": -74.1724,
            "address": "1 Fuel Lane, Newark, NJ",
            "timezone": "America/New_York",
            "operating_hours": [
                {"day_of_week": "mon", "open": "06:00", "close": "22:00"}
            ],
            "supported_products": ["DIESEL_2"],
            "branded": False,
            "status": "active",
        },
        changed={"name": "B's rack"},
    ),
    "supplier_contracts": Case(
        post="/api/fuel/supplier-contracts",
        get="/api/fuel/supplier-contracts/QA-SC-X",
        id_field="contract_id",
        wire=_fuel_ops(supplier_contract_repository=_contract_repo),
        body={
            "contract_id": "QA-SC-X",
            "supplier_name": "A's supplier",
            "product_code": "DIESEL_2",
            "preferred_terminal_ids": ["term_001"],
            "contract_price_per_gallon_usd": 3.25,
            "branded_required": False,
            "minimum_lift_gallons_per_month": 50000.0,
            "effective_from": "2025-01-01",
            "effective_to": "2026-01-01",
            "status": "active",
        },
        changed={"supplier_name": "B's supplier"},
    ),
    "depots": Case(
        post="/api/fuel/mvp/depots",
        get="/api/fuel/mvp/depots/QA-DEPOT-X",
        id_field="depot_id",
        wire=_fuel_ops(depot_repository=_depot_repo),
        body={
            "depot_id": "QA-DEPOT-X",
            "name": "A's depot",
            "location_lat": 40.7357,
            "location_lon": -74.1724,
            "address": "1 Fuel Lane, Newark, NJ",
            "timezone": "America/New_York",
            "fuel_types_supported": ["DIESEL_2"],
            "status": "active",
        },
        changed={"name": "B's depot"},
    ),
    "drivers": Case(
        post="/api/ops/drivers",
        get="/api/ops/drivers/QA-DRV-X",
        id_field="driver_id",
        wire=_drivers,
        body={"driver_id": "QA-DRV-X", "driver_name": "A's driver", "status": "active"},
        changed={"driver_name": "B's driver"},
    ),
    "intake_channels": Case(
        post="/api/integrations/intake-channels",
        list_path="/api/integrations/intake-channels",
        id_field="channel_id",
        wire=_intake_channels,
        body={
            "channel_id": "qa-channel-x",
            "channel_type": "api_partner",
            "display_name": "A's partner",
            "supported_schema_versions": ["1.0"],
        },
        changed={"display_name": "B's partner"},
    ),
    "integrations": Case(
        post="/api/integrations",
        list_path="/api/integrations",
        id_field="instance_id",
        wire=_integrations,
        body={
            "instance_id": "QA-INT-X",
            "provider_name": "quickbooks_online",
            "category": "accounting",
            "config": {"realm_id": "A-realm"},
        },
        changed={"config": {"realm_id": "B-realm"}},
    ),
}


@pytest.fixture
def app_factory():
    """Build an app over a fresh shared store whose tenant is switchable."""
    es = _GlobalIdStore()
    current = {"tenant": A}

    def _ctx() -> TenantContext:
        return TenantContext(
            tenant_id=current["tenant"],
            user_id=f"user-{current['tenant']}",
            has_pii_access=False,
            roles=["admin", "dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    def build(case: Case) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)
        case.wire(app, es)
        app.dependency_overrides[get_tenant_context] = _ctx
        return app

    # Mirror writes are covered by tests/persistence/test_mirror_no_rehome.py;
    # here they are recorded so a refused create can be shown to write none.
    mirror = AsyncMock()
    with patch(
        "commerce.services.commerce_persistence_bridge.mirror_current_state_upsert",
        mirror,
    ), patch(
        "commerce.services.commerce_persistence_bridge.mirror_compliance_config_upsert",
        mirror,
    ), patch("fuel.customer_tank_models.mirror_current_state_upsert", mirror):
        yield build, current, mirror


def _record(client: TestClient, case: Case, record_id: str):
    if case.get:
        resp = client.get(case.get)
        return resp.status_code, (resp.json() if resp.status_code == 200 else None)
    resp = client.get(case.list_path)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    items = payload.get("items") or payload.get("data") or []
    matches = [i for i in items if i.get(case.id_field) == record_id]
    return (200, matches[0]) if matches else (404, None)


@pytest.mark.parametrize("name", sorted(CASES))
def test_tenant_b_cannot_create_over_tenant_a_id(app_factory, name):
    build, current, mirror = app_factory
    case = CASES[name]
    record_id = case.body[case.id_field]

    with TestClient(build(case)) as client:
        current["tenant"] = A
        created = client.post(case.post, json=case.body)
        assert created.status_code == 201, created.text
        status_a, before = _record(client, case, record_id)
        assert status_a == 200 and before is not None
        mirror.reset_mock()

        current["tenant"] = B
        resp = client.post(case.post, json={**case.body, **case.changed})
        assert resp.status_code == 409, resp.text
        assert "RESOURCE_ALREADY_EXISTS" in resp.text
        assert A not in resp.text
        assert mirror.await_count == 0
        status_b, _ = _record(client, case, record_id)
        assert status_b == 404

        current["tenant"] = A
        status_after, after = _record(client, case, record_id)
        assert status_after == 200
        assert after == before


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_tenant_duplicate_is_409(app_factory, name):
    build, current, _ = app_factory
    case = CASES[name]
    record_id = case.body[case.id_field]

    with TestClient(build(case)) as client:
        current["tenant"] = A
        assert client.post(case.post, json=case.body).status_code == 201
        _, before = _record(client, case, record_id)
        resp = client.post(case.post, json={**case.body, **case.changed})
        assert resp.status_code == 409, resp.text
        assert _record(client, case, record_id)[1] == before


def test_refused_integration_create_deletes_its_credential(app_factory):
    """Each vault put mints a new ref, so a refused create used to leave an
    orphaned ciphertext behind (tenant-leak review issue 3)."""
    from integrations.api.integrations_endpoints import (
        configure_integrations_endpoints,
        router,
    )
    from integrations.connector_base import IntegrationInstanceRepository

    build, current, _ = app_factory
    vault = _Vault()
    store = _GlobalIdStore()

    def _wire(app: FastAPI, es: _GlobalIdStore) -> None:
        configure_integrations_endpoints(
            repository=IntegrationInstanceRepository(es_service=store),
            scheduler=_Scheduler(),  # type: ignore[arg-type]
            credentials_vault=vault,
            es_service=store,
        )
        app.include_router(router)

    case = CASES["integrations"]
    body = {**case.body, "credentials": {"api_key": "QA-not-a-real-key"}}
    with TestClient(build(Case(**{**case.__dict__, "wire": _wire}))) as client:
        current["tenant"] = A
        assert client.post(case.post, json=body).status_code == 201
        current["tenant"] = B
        resp = client.post(case.post, json=body)
        assert resp.status_code == 409, resp.text
    assert vault.deleted == [f"cred:{B}:quickbooks_online_credentials:2"]
