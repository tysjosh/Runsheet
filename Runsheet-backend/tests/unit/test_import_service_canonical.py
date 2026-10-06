"""Focused tests for the canonical fuel import workflow."""

from types import SimpleNamespace

import pytest

from ops.middleware.tenant_guard import TenantContext
from services.import_service import ImportService
from services.schema_templates import SchemaTemplates


pytestmark = pytest.mark.asyncio


class _Elasticsearch:
    def __init__(self):
        self.documents = {}
        self.bulk_calls = []

    async def index_document(self, index, document_id, document):
        self.documents[(index, document_id)] = dict(document)
        return {"result": "created"}

    async def get_document(self, index, document_id):
        return self.documents.get((index, document_id))

    async def bulk_index_documents(self, index, documents):
        self.bulk_calls.append((index, list(documents)))
        return {"successful": len(documents), "failed": 0, "errors": []}


class _OrderPipeline:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or ["processed", "processed"])
        self.calls = []

    async def ingest_csv(self, **kwargs):
        self.calls.append(kwargs)
        status = self.statuses.pop(0)
        return SimpleNamespace(status=status)


def _tenant(tenant_id="tenant-a"):
    return TenantContext(
        tenant_id=tenant_id,
        user_id="dispatcher-a",
        has_pii_access=True,
        roles=["dispatcher"],
    )


async def _parse_and_validate(service, data_type, tenant_id="tenant-a"):
    content = SchemaTemplates().generate_csv_template(data_type).encode()
    parsed = await service.parse_csv(
        content,
        data_type,
        tenant_id=tenant_id,
        source_name=f"{data_type}.csv",
    )
    validated = await service.validate(
        parsed.session_id,
        parsed.suggested_mapping,
        tenant_id=tenant_id,
    )
    assert validated.error_count == 0
    return parsed.session_id


async def test_order_import_routes_through_intake_pipeline_not_generic_bulk_index():
    es = _Elasticsearch()
    pipeline = _OrderPipeline()
    service = ImportService(es, order_intake_pipeline=pipeline)
    session_id = await _parse_and_validate(service, "orders")

    result = await service.commit(session_id, tenant=_tenant())

    assert result.imported_records == 2
    assert result.es_index == "fuel_orders_current"
    assert es.bulk_calls == []
    assert [call["client_event_id"] for call in pipeline.calls] == [
        "csv:sample_erp:SO-1001:2026-07-29T12:00:00Z",
        "csv:sample_erp:SO-1002:2026-07-29T12:05:00Z",
    ]
    assert pipeline.calls[0]["payload"]["source_updated_at"].endswith("Z")
    assert pipeline.calls[0]["import_batch_id"] == session_id


async def test_duplicate_source_order_is_counted_as_skipped():
    es = _Elasticsearch()
    pipeline = _OrderPipeline(["processed", "duplicate"])
    service = ImportService(es, order_intake_pipeline=pipeline)
    session_id = await _parse_and_validate(service, "orders")

    result = await service.commit(session_id, tenant=_tenant())

    assert result.imported_records == 1
    assert result.skipped_records == 1
    assert result.error_count == 0


async def test_active_session_survives_service_restart_and_remains_tenant_scoped():
    es = _Elasticsearch()
    first_service = ImportService(es)
    content = SchemaTemplates().generate_csv_template("inventory").encode()
    parsed = await first_service.parse_csv(
        content,
        "inventory",
        tenant_id="tenant-a",
        source_name="inventory.csv",
    )

    restarted_service = ImportService(es)
    with pytest.raises(ValueError, match="not found"):
        await restarted_service.validate(
            parsed.session_id,
            parsed.suggested_mapping,
            tenant_id="tenant-b",
        )

    validated = await restarted_service.validate(
        parsed.session_id,
        parsed.suggested_mapping,
        tenant_id="tenant-a",
    )
    assert validated.valid_rows == 3


# ---------------------------------------------------------------------------
# B1: /api/import/validate reports a customer that doesn't exist in the tenant
# ---------------------------------------------------------------------------


def _customer_resolver(customers, *, register=True):
    """RefResolver whose ``customer`` loader knows ``customers`` (id -> tenant)."""
    from services.ref_resolver import RefResolver

    resolver = RefResolver()

    async def _load(tenant_id, customer_id):
        if customers.get(customer_id) == tenant_id:
            return {"customer_id": customer_id}
        return None

    if register:
        resolver.register("customer", _load)
    return resolver


async def _validate_tanks(service, tenant_id="tenant-a"):
    content = SchemaTemplates().generate_csv_template("customer_tanks").encode()
    parsed = await service.parse_csv(
        content, "customer_tanks", tenant_id=tenant_id, source_name="tanks.csv"
    )
    return await service.validate(
        parsed.session_id, parsed.suggested_mapping, tenant_id=tenant_id
    )


def _tank_service_with(resolver):
    return ImportService(
        _Elasticsearch(),
        tank_import_service=SimpleNamespace(_ref_resolver=resolver),
    )


async def test_validate_reports_customer_not_found_for_tank_rows():
    # The template has CUST-100 and CUST-200; only CUST-100 is tenant-a's,
    # CUST-200 belongs to another tenant.
    resolver = _customer_resolver({"CUST-100": "tenant-a", "CUST-200": "tenant-b"})

    result = await _validate_tanks(_tank_service_with(resolver))

    assert result.total_rows == 2
    assert result.valid_rows == 1
    assert len(result.errors) == 1
    issue = result.errors[0]
    assert issue.row_number == 2
    assert issue.field_name == "customer_id"
    assert issue.value == "CUST-200"
    assert "CUST-200" in issue.description


async def test_validate_accepts_known_customers():
    resolver = _customer_resolver({"CUST-100": "tenant-a", "CUST-200": "tenant-a"})

    result = await _validate_tanks(_tank_service_with(resolver))

    assert result.valid_rows == 2
    assert result.errors == []


async def test_validate_skips_lookup_for_existing_tank_with_same_customer():
    """Commit doesn't re-check an unchanged customer, so validate doesn't either."""

    class _Tanks:
        async def get_by_external_id(self, tenant_id, source_system, external_id):
            if external_id == "T-200":
                return SimpleNamespace(customer_id="CUST-200")
            return None

    resolver = _customer_resolver({"CUST-100": "tenant-a"})
    service = ImportService(
        _Elasticsearch(),
        tank_import_service=SimpleNamespace(_ref_resolver=resolver, _tanks=_Tanks()),
    )

    result = await _validate_tanks(service)

    assert result.valid_rows == 2


TANK_HEADER = (
    "source_system,external_tank_id,customer_tank_id,customer_id,customer_type,"
    "fuel_type,fuel_product_code,capacity_gallons,current_level_gallons,"
    "last_reading_at,location_lat,location_lon,zip_code,k_factor,use_case,status\n"
)


async def _validate_tank_rows(rows, tenant_id="tenant-a"):
    service = _tank_service_with(_customer_resolver({}, register=False))
    parsed = await service.parse_csv(
        (TANK_HEADER + "".join(rows)).encode(),
        "customer_tanks",
        tenant_id=tenant_id,
        source_name="tanks.csv",
    )
    return await service.validate(
        parsed.session_id, parsed.suggested_mapping, tenant_id=tenant_id
    )


def _tank_row(fuel_type="diesel", product="DIESEL_2", capacity=1000, level=275):
    return (
        f"sample_erp,T-1,tank-1,CUST-1,commercial,{fuel_type},{product},"
        f"{capacity},{level},2026-07-29T12:00:00Z,39.7,-86.1,46201,,farm,active\n"
    )


async def test_validate_rejects_product_outside_the_tank_fuel_family():
    result = await _validate_tank_rows([_tank_row("propane", "DIESEL_2")])

    assert result.valid_rows == 0
    assert len(result.errors) == 1
    issue = result.errors[0]
    assert issue.field_name == "fuel_product_code"
    assert "DIESEL_2" in issue.description
    assert "propane" in issue.description


async def test_validate_accepts_product_inside_the_tank_fuel_family():
    result = await _validate_tank_rows(
        [_tank_row("diesel", "DIESEL_2"), _tank_row("farm_fuel", "OFF_ROAD_DIESEL")]
    )

    assert result.valid_rows == 2
    assert result.errors == []


async def test_validation_messages_are_field_level_without_placeholders_or_urls():
    result = await _validate_tank_rows([_tank_row(capacity=100, level=500)])

    assert result.valid_rows == 0
    assert result.errors
    for issue in result.errors:
        assert "validation-tenant" not in issue.description
        assert "errors.pydantic.dev" not in issue.description
        assert "https://" not in issue.description
        assert not issue.description.startswith("Value error,")


async def test_unknown_field_value_names_the_field():
    result = await _validate_tank_rows([_tank_row(fuel_type="jet_fuel")])

    assert result.valid_rows == 0
    assert [issue.field_name for issue in result.errors] == ["fuel_type"]
    assert "https://" not in result.errors[0].description


async def test_validate_without_customer_loader_stays_additive():
    resolver = _customer_resolver({}, register=False)

    result = await _validate_tanks(_tank_service_with(resolver))

    assert result.valid_rows == 2
