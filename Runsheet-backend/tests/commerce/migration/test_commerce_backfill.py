"""Integration tests for the Commerce Backbone backfill migration.

Uses captured CustomerProfile + QBO fixture sets. Idempotency is asserted
by running the backfill twice and diffing ES state.

Validates: Design §9 (Migration strategy), Tasks 13.1, 13.3
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from commerce.migration.commerce_backfill import CommerceBackfill
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    CUSTOMERS_CURRENT_INDEX,
    INVOICES_CURRENT_INDEX,
    PAYMENTS_CURRENT_INDEX,
    PRICE_BOOKS_CURRENT_INDEX,
    PRICING_RULES_CURRENT_INDEX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-test-001"


def _make_customer_profile(customer_id: str, display_name: str = "Test Customer") -> dict:
    """Create a CustomerProfile fixture."""
    return {
        "customer_id": customer_id,
        "tenant_id": TENANT_ID,
        "display_name": display_name,
        "legal_name": f"{display_name} LLC",
        "primary_email": f"{customer_id}@example.com",
        "tax_id": "12-3456789",
        "zip_code": "10001",
        "external_refs": {"legacy": f"legacy:{customer_id}"},
    }


def _make_product(product_code: str, rack_price_cents: int = 35000) -> dict:
    """Create a product catalog fixture."""
    return {
        "product_code": product_code,
        "tenant_id": TENANT_ID,
        "name": f"Product {product_code}",
        "rack_price_cents": rack_price_cents,
    }


def _make_qbo_invoice(qbo_id: str, total: float = 1500.00, balance: float = 0.0) -> dict:
    """Create a QBO invoice fixture."""
    return {
        "Id": qbo_id,
        "DocNumber": f"INV-{qbo_id}",
        "TotalAmt": total,
        "Balance": balance,
        "TxnDate": "2025-01-15",
        "DueDate": "2025-02-15",
        "customer_id": "cust-001",
        "account_id": "acct-001",
        "TxnTaxDetail": {"TotalTax": 0.0},
    }


def _make_qbo_payment(qbo_id: str, total: float = 1500.00) -> dict:
    """Create a QBO payment fixture."""
    return {
        "Id": qbo_id,
        "TotalAmt": total,
        "TxnDate": "2025-01-20",
        "PaymentRefNum": f"CHK-{qbo_id}",
        "PaymentMethodRef": {"value": "check"},
        "invoice_id": "inv-001",
        "account_id": "acct-001",
    }


CUSTOMER_PROFILES = [
    _make_customer_profile("cust-001", "Acme Fuel Co"),
    _make_customer_profile("cust-002", "Beta Energy"),
    _make_customer_profile("cust-003", "Gamma Logistics"),
]

PRODUCTS = [
    _make_product("ULSD", 35000),
    _make_product("DEF", 12000),
    _make_product("GASOLINE-87", 32000),
]

QBO_INVOICES = [
    _make_qbo_invoice("1001", total=1500.00, balance=0.0),
    _make_qbo_invoice("1002", total=2500.00, balance=1000.00),
    _make_qbo_invoice("1003", total=800.00, balance=800.00),
]

QBO_PAYMENTS = [
    _make_qbo_payment("2001", total=1500.00),
    _make_qbo_payment("2002", total=1500.00),
]


# ---------------------------------------------------------------------------
# Mock ES service
# ---------------------------------------------------------------------------


class MockElasticsearchService:
    """In-memory mock of ElasticsearchService for testing backfill logic."""

    def __init__(self):
        self._indices: dict[str, dict[str, dict]] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        """Store a document in the mock index."""
        if index not in self._indices:
            self._indices[index] = {}
        self._indices[index][doc_id] = document

    async def update_document(self, index: str, doc_id: str, partial_doc: dict) -> None:
        """Update a document in the mock index."""
        if index in self._indices and doc_id in self._indices[index]:
            self._indices[index][doc_id].update(partial_doc)

    async def search_documents(self, index: str, query: dict, size: int = 100) -> dict:
        """Search documents in the mock index."""
        if index not in self._indices:
            return {"hits": {"hits": [], "total": {"value": 0}}}

        docs = list(self._indices[index].values())

        # Apply basic tenant_id filter if present
        must_clauses = query.get("query", {}).get("bool", {}).get("must", [])
        for clause in must_clauses:
            if "term" in clause:
                field, value = next(iter(clause["term"].items()))
                docs = [d for d in docs if d.get(field) == value]
            elif "terms" in clause:
                field, values = next(iter(clause["terms"].items()))
                docs = [d for d in docs if d.get(field) in values]
            elif "exists" in clause:
                field = clause["exists"]["field"]
                # Handle nested field paths like "external_refs.qbo"
                parts = field.split(".")
                if len(parts) == 2:
                    docs = [
                        d for d in docs
                        if isinstance(d.get(parts[0]), dict) and d[parts[0]].get(parts[1]) is not None
                    ]
                else:
                    docs = [d for d in docs if d.get(field) is not None]

        # Apply source filter
        source_fields = query.get("_source")

        hits = []
        for doc in docs[:size]:
            source = doc
            if source_fields and isinstance(source_fields, list):
                source = {k: doc.get(k) for k in source_fields if k in doc}
                # Handle nested fields
                for sf in source_fields:
                    if "." in sf:
                        parts = sf.split(".")
                        if parts[0] in doc and isinstance(doc[parts[0]], dict):
                            if parts[0] not in source:
                                source[parts[0]] = {}
                            source[parts[0]][parts[1]] = doc[parts[0]].get(parts[1])
            hits.append({"_source": source})

        return {
            "hits": {
                "hits": hits,
                "total": {"value": len(docs)},
            }
        }

    async def get_document(self, index: str, doc_id: str) -> dict | None:
        """Get a single document by ID."""
        if index in self._indices and doc_id in self._indices[index]:
            return {"_source": self._indices[index][doc_id]}
        return None

    def get_all_docs(self, index: str) -> list[dict]:
        """Test helper: return all docs in an index."""
        if index not in self._indices:
            return []
        return list(self._indices[index].values())

    def doc_count(self, index: str) -> int:
        """Test helper: return document count for an index."""
        return len(self._indices.get(index, {}))


# ---------------------------------------------------------------------------
# Mock QBO connector
# ---------------------------------------------------------------------------


class MockQBOConnector:
    """Mock QBO connector that returns fixture data."""

    def __init__(self, invoices: list[dict] = None, payments: list[dict] = None):
        self._invoices = invoices or []
        self._payments = payments or []

    async def query_invoices(self, tenant_id: str, since=None) -> list[dict]:
        return self._invoices

    async def query_payments(self, tenant_id: str, since=None) -> list[dict]:
        return self._payments


# ---------------------------------------------------------------------------
# Tests — Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Test that dry-run mode scans but writes no data."""

    @pytest.mark.asyncio
    async def test_dry_run_writes_no_data(self):
        """Dry-run should scan and report counts without writing any documents."""
        es = MockElasticsearchService()

        # Seed customer profiles
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=QBO_PAYMENTS)
        backfill = CommerceBackfill(es, qbo)

        report = await backfill.run(TENANT_ID, dry_run=True)

        # Verify no commerce records were created
        assert es.doc_count(CUSTOMERS_CURRENT_INDEX) == 0
        assert es.doc_count(ACCOUNTS_CURRENT_INDEX) == 0
        assert es.doc_count(PRICE_BOOKS_CURRENT_INDEX) == 0
        assert es.doc_count(INVOICES_CURRENT_INDEX) == 0
        assert es.doc_count(PAYMENTS_CURRENT_INDEX) == 0

        # Verify report contains scan results
        assert report["dry_run"] is True
        assert "scan" in report["phases"]
        scan = report["phases"]["scan"]
        assert scan["customer_profiles_found"] == 3
        assert scan["customers_to_migrate"] == 3

    @pytest.mark.asyncio
    async def test_dry_run_reports_already_migrated(self):
        """Dry-run should correctly report already-migrated counts."""
        es = MockElasticsearchService()

        # Seed profiles
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        # Pre-migrate one customer
        await es.index_document(
            CUSTOMERS_CURRENT_INDEX,
            "cust-001",
            {"customer_id": "cust-001", "tenant_id": TENANT_ID, "display_name": "Acme"},
        )

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, dry_run=True)

        scan = report["phases"]["scan"]
        assert scan["customers_already_migrated"] == 1
        assert scan["customers_to_migrate"] == 2


# ---------------------------------------------------------------------------
# Tests — Phase 1 (Customers + Accounts)
# ---------------------------------------------------------------------------


class TestPhase1CustomersAccounts:
    """Test Phase 1: Customer + Account creation from CustomerProfile."""

    @pytest.mark.asyncio
    async def test_creates_customers_and_accounts(self):
        """Phase 1 should create one Customer + one Account per profile."""
        es = MockElasticsearchService()

        # Seed customer profiles
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=1)

        assert report["status"] == "success"
        phase_1 = report["phases"]["phase_1"]
        assert phase_1["created_customers"] == 3
        assert phase_1["created_accounts"] == 3
        assert phase_1["skipped_existing"] == 0

        # Verify documents in ES
        assert es.doc_count(CUSTOMERS_CURRENT_INDEX) == 3
        assert es.doc_count(ACCOUNTS_CURRENT_INDEX) == 3

    @pytest.mark.asyncio
    async def test_customer_fields_mapped_correctly(self):
        """Migrated Customer should carry display_name, email, and metadata."""
        es = MockElasticsearchService()
        await es.index_document("customers", "cust-001", CUSTOMER_PROFILES[0])

        backfill = CommerceBackfill(es)
        await backfill.run(TENANT_ID, phase=1)

        customers = es.get_all_docs(CUSTOMERS_CURRENT_INDEX)
        assert len(customers) == 1
        cust = customers[0]
        assert cust["customer_id"] == "cust-001"
        assert cust["display_name"] == "Acme Fuel Co"
        assert cust["tenant_id"] == TENANT_ID
        assert cust["status"] == "active"
        assert cust["metadata"]["migrated_from"] == "customer_profile"

    @pytest.mark.asyncio
    async def test_account_linked_to_customer(self):
        """Each Account should reference its parent Customer."""
        es = MockElasticsearchService()
        await es.index_document("customers", "cust-001", CUSTOMER_PROFILES[0])

        backfill = CommerceBackfill(es)
        await backfill.run(TENANT_ID, phase=1)

        accounts = es.get_all_docs(ACCOUNTS_CURRENT_INDEX)
        assert len(accounts) == 1
        acct = accounts[0]
        assert acct["customer_id"] == "cust-001"
        assert acct["tenant_id"] == TENANT_ID
        assert acct["status"] == "active"
        assert acct["credit_state"] == "ok"
        assert acct["net_terms_days"] == 30

    @pytest.mark.asyncio
    async def test_skips_already_migrated_customers(self):
        """Phase 1 should skip profiles that already have a Customer record."""
        es = MockElasticsearchService()

        # Seed profiles
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        # Pre-migrate cust-001
        await es.index_document(
            CUSTOMERS_CURRENT_INDEX,
            "cust-001",
            {"customer_id": "cust-001", "tenant_id": TENANT_ID, "display_name": "Acme"},
        )

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=1)

        phase_1 = report["phases"]["phase_1"]
        assert phase_1["created_customers"] == 2
        assert phase_1["skipped_existing"] == 1


# ---------------------------------------------------------------------------
# Tests — Phase 2 (PriceBook)
# ---------------------------------------------------------------------------


class TestPhase2PriceBook:
    """Test Phase 2: Default PriceBook creation from product catalog."""

    @pytest.mark.asyncio
    async def test_creates_default_price_book(self):
        """Phase 2 should create one Default Price Book with rules per product."""
        es = MockElasticsearchService()

        # Seed product catalog
        for product in PRODUCTS:
            await es.index_document("product_catalog", product["product_code"], product)

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=2)

        phase_2 = report["phases"]["phase_2"]
        assert phase_2["status"] == "completed"
        assert phase_2["rules_created"] == 3

        # Verify price book
        assert es.doc_count(PRICE_BOOKS_CURRENT_INDEX) == 1
        pb = es.get_all_docs(PRICE_BOOKS_CURRENT_INDEX)[0]
        assert pb["name"] == "Default Price Book"
        assert pb["status"] == "active"

        # Verify pricing rules
        assert es.doc_count(PRICING_RULES_CURRENT_INDEX) == 3

    @pytest.mark.asyncio
    async def test_skips_if_price_book_exists(self):
        """Phase 2 should skip if Default Price Book already exists."""
        es = MockElasticsearchService()

        # Pre-create a default price book
        await es.index_document(
            PRICE_BOOKS_CURRENT_INDEX,
            "pb-existing",
            {
                "price_book_id": "pb-existing",
                "tenant_id": TENANT_ID,
                "name": "Default Price Book",
                "status": "active",
            },
        )

        # Seed products
        for product in PRODUCTS:
            await es.index_document("product_catalog", product["product_code"], product)

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=2)

        phase_2 = report["phases"]["phase_2"]
        assert phase_2["status"] == "skipped"
        assert phase_2["reason"] == "default_price_book_exists"

        # No new rules created
        assert es.doc_count(PRICING_RULES_CURRENT_INDEX) == 0

    @pytest.mark.asyncio
    async def test_pricing_rules_have_correct_scope(self):
        """Each pricing rule should have scope_type=default."""
        es = MockElasticsearchService()
        for product in PRODUCTS:
            await es.index_document("product_catalog", product["product_code"], product)

        backfill = CommerceBackfill(es)
        await backfill.run(TENANT_ID, phase=2)

        rules = es.get_all_docs(PRICING_RULES_CURRENT_INDEX)
        for rule in rules:
            assert rule["scope_type"] == "default"
            assert rule["scope_value"] == "default"
            assert rule["tenant_id"] == TENANT_ID


# ---------------------------------------------------------------------------
# Tests — Phase 3 (Invoices + Payments)
# ---------------------------------------------------------------------------


class TestPhase3InvoicesPayments:
    """Test Phase 3: QBO invoice + payment import."""

    @pytest.mark.asyncio
    async def test_imports_qbo_invoices_and_payments(self):
        """Phase 3 should import QBO invoices and payments."""
        es = MockElasticsearchService()
        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=QBO_PAYMENTS)

        backfill = CommerceBackfill(es, qbo)
        report = await backfill.run(TENANT_ID, phase=3)

        phase_3 = report["phases"]["phase_3"]
        assert phase_3["status"] == "completed"
        assert phase_3["invoices_created"] == 3
        assert phase_3["payments_created"] == 2

        assert es.doc_count(INVOICES_CURRENT_INDEX) == 3
        assert es.doc_count(PAYMENTS_CURRENT_INDEX) == 2

    @pytest.mark.asyncio
    async def test_skips_without_qbo_connector(self):
        """Phase 3 should skip gracefully when no QBO connector is provided."""
        es = MockElasticsearchService()
        backfill = CommerceBackfill(es, qbo_connector=None)

        report = await backfill.run(TENANT_ID, phase=3)

        phase_3 = report["phases"]["phase_3"]
        assert phase_3["status"] == "skipped"
        assert phase_3["reason"] == "no_qbo_connector"

    @pytest.mark.asyncio
    async def test_invoice_status_mapping(self):
        """Imported invoices should have correct status based on balance."""
        es = MockElasticsearchService()
        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=[])

        backfill = CommerceBackfill(es, qbo)
        await backfill.run(TENANT_ID, phase=3)

        invoices = es.get_all_docs(INVOICES_CURRENT_INDEX)
        statuses = {inv["external_refs"]["qbo"]: inv["status"] for inv in invoices}

        # 1001: balance=0 -> paid
        assert statuses["inv:1001"] == "paid"
        # 1002: balance=1000, paid=1500 -> partial
        assert statuses["inv:1002"] == "partial"
        # 1003: balance=800, paid=0 -> open
        assert statuses["inv:1003"] == "open"

    @pytest.mark.asyncio
    async def test_skips_already_imported_invoices(self):
        """Phase 3 should skip invoices that already exist (by QBO ref)."""
        es = MockElasticsearchService()

        # Pre-import one invoice
        await es.index_document(
            INVOICES_CURRENT_INDEX,
            "inv-existing",
            {
                "invoice_id": "inv-existing",
                "tenant_id": TENANT_ID,
                "external_refs": {"qbo": "inv:1001"},
                "status": "paid",
            },
        )

        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=[])
        backfill = CommerceBackfill(es, qbo)
        report = await backfill.run(TENANT_ID, phase=3)

        phase_3 = report["phases"]["phase_3"]
        assert phase_3["invoices_created"] == 2
        assert phase_3["invoices_skipped"] == 1


# ---------------------------------------------------------------------------
# Tests — Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Test that running the backfill twice produces no duplicates."""

    @pytest.mark.asyncio
    async def test_full_backfill_idempotent(self):
        """Running the full backfill twice should produce identical ES state."""
        es = MockElasticsearchService()

        # Seed source data
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)
        for product in PRODUCTS:
            await es.index_document("product_catalog", product["product_code"], product)

        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=QBO_PAYMENTS)
        backfill = CommerceBackfill(es, qbo)

        # First run
        report_1 = await backfill.run(TENANT_ID)

        # Capture state after first run
        customers_after_1 = es.doc_count(CUSTOMERS_CURRENT_INDEX)
        accounts_after_1 = es.doc_count(ACCOUNTS_CURRENT_INDEX)
        price_books_after_1 = es.doc_count(PRICE_BOOKS_CURRENT_INDEX)
        rules_after_1 = es.doc_count(PRICING_RULES_CURRENT_INDEX)
        invoices_after_1 = es.doc_count(INVOICES_CURRENT_INDEX)
        payments_after_1 = es.doc_count(PAYMENTS_CURRENT_INDEX)

        # Second run
        report_2 = await backfill.run(TENANT_ID)

        # State should be identical
        assert es.doc_count(CUSTOMERS_CURRENT_INDEX) == customers_after_1
        assert es.doc_count(ACCOUNTS_CURRENT_INDEX) == accounts_after_1
        assert es.doc_count(PRICE_BOOKS_CURRENT_INDEX) == price_books_after_1
        assert es.doc_count(PRICING_RULES_CURRENT_INDEX) == rules_after_1
        assert es.doc_count(INVOICES_CURRENT_INDEX) == invoices_after_1
        assert es.doc_count(PAYMENTS_CURRENT_INDEX) == payments_after_1

    @pytest.mark.asyncio
    async def test_phase_1_idempotent(self):
        """Running Phase 1 twice creates no duplicate customers or accounts."""
        es = MockElasticsearchService()
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        backfill = CommerceBackfill(es)

        # First run
        report_1 = await backfill.run(TENANT_ID, phase=1)
        assert report_1["phases"]["phase_1"]["created_customers"] == 3

        # Second run
        report_2 = await backfill.run(TENANT_ID, phase=1)
        assert report_2["phases"]["phase_1"]["created_customers"] == 0
        assert report_2["phases"]["phase_1"]["skipped_existing"] == 3

        # Still only 3 customers
        assert es.doc_count(CUSTOMERS_CURRENT_INDEX) == 3

    @pytest.mark.asyncio
    async def test_phase_2_idempotent(self):
        """Running Phase 2 twice creates no duplicate price books."""
        es = MockElasticsearchService()
        for product in PRODUCTS:
            await es.index_document("product_catalog", product["product_code"], product)

        backfill = CommerceBackfill(es)

        # First run
        report_1 = await backfill.run(TENANT_ID, phase=2)
        assert report_1["phases"]["phase_2"]["status"] == "completed"

        # Second run
        report_2 = await backfill.run(TENANT_ID, phase=2)
        assert report_2["phases"]["phase_2"]["status"] == "skipped"

        # Still only 1 price book
        assert es.doc_count(PRICE_BOOKS_CURRENT_INDEX) == 1

    @pytest.mark.asyncio
    async def test_phase_3_idempotent(self):
        """Running Phase 3 twice creates no duplicate invoices or payments."""
        es = MockElasticsearchService()
        qbo = MockQBOConnector(invoices=QBO_INVOICES, payments=QBO_PAYMENTS)

        backfill = CommerceBackfill(es, qbo)

        # First run
        report_1 = await backfill.run(TENANT_ID, phase=3)
        assert report_1["phases"]["phase_3"]["invoices_created"] == 3
        assert report_1["phases"]["phase_3"]["payments_created"] == 2

        # Second run
        report_2 = await backfill.run(TENANT_ID, phase=3)
        assert report_2["phases"]["phase_3"]["invoices_created"] == 0
        assert report_2["phases"]["phase_3"]["invoices_skipped"] == 3
        assert report_2["phases"]["phase_3"]["payments_created"] == 0
        assert report_2["phases"]["phase_3"]["payments_skipped"] == 2

        # Counts unchanged
        assert es.doc_count(INVOICES_CURRENT_INDEX) == 3
        assert es.doc_count(PAYMENTS_CURRENT_INDEX) == 2


# ---------------------------------------------------------------------------
# Tests — Phase 4 (Verification)
# ---------------------------------------------------------------------------


class TestPhase4Verify:
    """Test Phase 4: Verification report."""

    @pytest.mark.asyncio
    async def test_verification_pass(self):
        """Phase 4 should report 'pass' when all profiles are migrated."""
        es = MockElasticsearchService()

        # Seed profiles and migrate them
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        backfill = CommerceBackfill(es)
        await backfill.run(TENANT_ID, phase=1)

        # Run verification
        report = await backfill.run(TENANT_ID, phase=4)
        phase_4 = report["phases"]["phase_4"]

        assert phase_4["status"] == "pass"
        assert phase_4["customer_coverage_pct"] == 100.0
        assert phase_4["migrated_customers"] == 3
        assert phase_4["migrated_accounts"] == 3
        assert phase_4["issues"] == []

    @pytest.mark.asyncio
    async def test_verification_warns_on_incomplete(self):
        """Phase 4 should report 'warn' when not all profiles are migrated."""
        es = MockElasticsearchService()

        # Seed 3 profiles but only migrate 2
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        # Only migrate first 2
        for profile in CUSTOMER_PROFILES[:2]:
            await es.index_document(
                CUSTOMERS_CURRENT_INDEX,
                profile["customer_id"],
                {"customer_id": profile["customer_id"], "tenant_id": TENANT_ID},
            )
            await es.index_document(
                ACCOUNTS_CURRENT_INDEX,
                f"acct-{profile['customer_id']}",
                {"account_id": f"acct-{profile['customer_id']}", "tenant_id": TENANT_ID, "customer_id": profile["customer_id"]},
            )

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=4)
        phase_4 = report["phases"]["phase_4"]

        assert phase_4["status"] == "warn"
        assert phase_4["customer_coverage_pct"] < 100.0
        assert len(phase_4["issues"]) > 0


# ---------------------------------------------------------------------------
# Tests — Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_handles_empty_tenant(self):
        """Backfill should handle a tenant with no source data gracefully."""
        es = MockElasticsearchService()
        backfill = CommerceBackfill(es)

        report = await backfill.run(TENANT_ID)

        assert report["status"] == "success"
        phase_1 = report["phases"]["phase_1"]
        assert phase_1["created_customers"] == 0
        assert phase_1["total_profiles"] == 0

    @pytest.mark.asyncio
    async def test_single_phase_execution(self):
        """Running with --phase should only execute that phase."""
        es = MockElasticsearchService()
        for profile in CUSTOMER_PROFILES:
            await es.index_document("customers", profile["customer_id"], profile)

        backfill = CommerceBackfill(es)
        report = await backfill.run(TENANT_ID, phase=1)

        # Only phase_1 should be in the report
        assert "phase_1" in report["phases"]
        assert "phase_2" not in report["phases"]
        assert "phase_3" not in report["phases"]
        assert "phase_4" not in report["phases"]
