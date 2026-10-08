"""Builders and a harness for the CostBasisResolver tests.

Entries live in the SQLite ``MarginRepository`` (``repo`` fixture); BOLs,
rack rows, plans and contracts live in a :class:`FakeDocStore` read through
the production ``Store*Reader`` classes, so the tests exercise the real
query shapes, tenant filters, paging and caps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from commerce.services.margin_cost_basis import (
    CostBasisReaders,
    CostBasisResolver,
    CostBasisSettings,
    ReaderCache,
)

from ._margin_fakes import CountingEntries, FakeDocStore
from .conftest import TENANT_A

UTC = timezone.utc
AS_OF = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
T1 = "TERM-1"
T2 = "TERM-2"


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def bol_doc(
    bol_id: str,
    ts: datetime,
    gallons: Any = 1000.0,
    *,
    tenant: str = TENANT_A,
    terminal: Optional[str] = T1,
    product: str = "DIESEL_2",
    plan: Optional[str] = None,
    supplier: str = "Acme",
    status: str = "verified",
    needs_confirmation: bool = False,
) -> Dict[str, Any]:
    return {
        "bol_id": bol_id,
        "tenant_id": tenant,
        "terminal_id": terminal,
        "product_code": product,
        "net_gallons": gallons,
        "gross_gallons": gallons,
        "timestamp": iso(ts),
        "load_plan_id": plan,
        "supplier_name": supplier,
        "status": status,
        "needs_operator_confirmation": needs_confirmation,
    }


def rack_doc(
    rack_id: str,
    ts: datetime,
    price: Any,
    *,
    tenant: str = TENANT_A,
    terminal: str = T1,
    product: str = "DIESEL_2",
    branded: bool = False,
    brand: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "rack_price_id": rack_id,
        "tenant_id": tenant,
        "terminal_id": terminal,
        "product_code": product,
        "price_per_gallon_usd": price,
        "branded_flag": branded,
        "supplier_brand": brand,
        "provider": "test",
        "effective_at": iso(ts),
        "retrieved_at": iso(ts),
    }


def plan_doc(
    plan_id: str,
    *,
    contract_id: Optional[str] = None,
    tenant: str = TENANT_A,
    terminal_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {"plan_id": plan_id, "tenant_id": tenant, "contract_id": contract_id, "terminal_id": terminal_id}


def contract_doc(
    contract_id: str,
    price: Optional[float],
    *,
    tenant: str = TENANT_A,
    effective_from: date = date(2026, 1, 1),
    effective_to: Optional[date] = None,
    status: str = "active",
    supplier: str = "Acme",
    product: str = "DIESEL_2",
) -> Dict[str, Any]:
    return {
        "contract_id": contract_id,
        "tenant_id": tenant,
        "supplier_name": supplier,
        "product_code": product,
        "preferred_terminal_ids": [T1],
        "contract_price_per_gallon_usd": price,
        "branded_required": False,
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else None,
        "status": status,
    }


async def add_entry(
    repo: Any,
    tenant: str,
    kind: str,
    *,
    unit_cost_micros: int,
    effective_at: datetime,
    product: str = "DIESEL_2",
    terminal: Optional[str] = None,
    effective_to: Optional[datetime] = None,
    gallons_milli: Optional[int] = None,
    adder_type: Optional[str] = None,
    bol_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
    entry_id: Optional[str] = None,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        "kind": kind,
        "product_code": product,
        "terminal_id": terminal,
        "effective_at": effective_at,
        "effective_to": effective_to,
        "unit_cost_micros": unit_cost_micros,
        "gallons_milli": gallons_milli,
        "adder_type": adder_type,
        "bol_id": bol_id,
        "natural_key": uuid.uuid4().hex,
        "source": "manual",
        "created_by": "test",
        "created_at": created_at or effective_at,
    }
    if entry_id is not None:
        values["entry_id"] = entry_id
    return await repo.insert_entry(tenant, values)


async def add_purchase(repo: Any, tenant: str = TENANT_A, *, gallons: int, micros: int, at: datetime, terminal: str = T1, **kw: Any) -> Dict[str, Any]:
    return await add_entry(
        repo, tenant, "purchase", unit_cost_micros=micros, effective_at=at, terminal=terminal, gallons_milli=gallons * 1_000, **kw
    )


@dataclass
class Harness:
    repo: Any
    store: FakeDocStore
    entries: CountingEntries

    @property
    def readers(self) -> CostBasisReaders:
        return CostBasisReaders.from_store(self.store, self.entries)

    def resolver(self, settings: Optional[CostBasisSettings] = None, cache: Optional[ReaderCache] = None) -> CostBasisResolver:
        return CostBasisResolver(self.readers, settings or CostBasisSettings(), cache=cache)

    async def resolve(self, product: str = "DIESEL_2", terminal: Optional[str] = T1, as_of: datetime = AS_OF, tenant: str = TENANT_A, **kw: Any):
        return await self.resolver(**kw).resolve(tenant, product, terminal, as_of)

    def query_count(self) -> int:
        return len(self.store.calls) + len(self.entries.calls)

    def reset_counts(self) -> None:
        self.store.reset_calls()
        self.entries.calls.clear()


def days(n: float) -> timedelta:
    return timedelta(days=n)
