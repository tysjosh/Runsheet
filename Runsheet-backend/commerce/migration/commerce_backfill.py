"""Idempotent CustomerProfile + QBO backfill.

Implements the per-tenant dry-run -> Phase 1..4 migration flow per design section 9.
Each phase is idempotent: it checks for existing records before creating new ones,
using customer_id for CustomerProfile-derived records and external_refs.qbo for
imported records.

Usage:
    backfill = CommerceBackfill(es_service, qbo_connector)
    report = await backfill.run(tenant_id, dry_run=True)   # scan only
    report = await backfill.run(tenant_id, phase=1)        # customers + accounts only
    report = await backfill.run(tenant_id)                 # all phases
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    CUSTOMERS_CURRENT_INDEX,
    INVOICES_CURRENT_INDEX,
    PAYMENTS_CURRENT_INDEX,
    PRICE_BOOKS_CURRENT_INDEX,
    PRICING_RULES_CURRENT_INDEX,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CUSTOMER_PROFILES_INDEX = "customers"
PRODUCT_CATALOG_INDEX = "product_catalog"
QBO_LOOKBACK_MONTHS = 24


# ---------------------------------------------------------------------------
# CommerceBackfill
# ---------------------------------------------------------------------------


class CommerceBackfill:
    """Orchestrates the per-tenant commerce data backfill.

    Args:
        es_service: ElasticsearchService instance for reading/writing ES.
        qbo_connector: Optional QBO connector for pulling invoices/payments.
            When None, Phase 3 is skipped.
    """

    def __init__(self, es_service: Any, qbo_connector: Any = None) -> None:
        self._es = es_service
        self._qbo = qbo_connector

    # ------------------------------------------------------------------
    # Public orchestrator
    # ------------------------------------------------------------------

    async def run(
        self,
        tenant_id: str,
        dry_run: bool = False,
        phase: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run the backfill for a single tenant.

        Args:
            tenant_id: Target tenant identifier.
            dry_run: When True, scan and report only — write no data.
            phase: Run only the specified phase (1-4). None runs all phases.

        Returns:
            A migration report dict with counts and status per phase.
        """
        report: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "dry_run": dry_run,
            "started_at": utcnow().isoformat(),
            "phases": {},
            "status": "success",
            "errors": [],
        }

        try:
            if dry_run or phase is None or phase == 0:
                scan_result = await self._dry_run_scan(tenant_id)
                report["phases"]["scan"] = scan_result
                if dry_run:
                    report["completed_at"] = utcnow().isoformat()
                    return report

            if phase is None or phase == 1:
                result = await self.phase_1_customers_accounts(tenant_id)
                report["phases"]["phase_1"] = result

            if phase is None or phase == 2:
                result = await self.phase_2_price_book(tenant_id)
                report["phases"]["phase_2"] = result

            if phase is None or phase == 3:
                result = await self.phase_3_invoices_payments(tenant_id)
                report["phases"]["phase_3"] = result

            if phase is None or phase == 4:
                result = await self.phase_4_verify(tenant_id)
                report["phases"]["phase_4"] = result

        except Exception as exc:
            logger.exception("Backfill failed for tenant %s", tenant_id)
            report["status"] = "failed"
            report["errors"].append(str(exc))

        report["completed_at"] = utcnow().isoformat()
        return report

    # ------------------------------------------------------------------
    # Dry-run scan
    # ------------------------------------------------------------------

    async def _dry_run_scan(self, tenant_id: str) -> Dict[str, Any]:
        """Scan CustomerProfile + connector state, print counts + unmatched records.

        Writes no data. Returns a summary dict.
        """
        # Count existing CustomerProfiles for this tenant
        profile_count = await self._count_customer_profiles(tenant_id)

        # Count already-migrated customers
        existing_customers = await self._count_existing_customers(tenant_id)

        # Count product catalog entries
        product_count = await self._count_products(tenant_id)

        # Count existing price books
        existing_price_books = await self._count_existing_price_books(tenant_id)

        # QBO invoice/payment counts
        qbo_invoice_count = 0
        qbo_payment_count = 0
        if self._qbo:
            qbo_invoice_count = await self._count_qbo_invoices(tenant_id)
            qbo_payment_count = await self._count_qbo_payments(tenant_id)

        # Existing commerce invoices/payments
        existing_invoices = await self._count_existing_invoices(tenant_id)
        existing_payments = await self._count_existing_payments(tenant_id)

        scan_result = {
            "customer_profiles_found": profile_count,
            "customers_already_migrated": existing_customers,
            "customers_to_migrate": max(0, profile_count - existing_customers),
            "products_found": product_count,
            "price_books_existing": existing_price_books,
            "qbo_invoices_found": qbo_invoice_count,
            "qbo_payments_found": qbo_payment_count,
            "invoices_already_migrated": existing_invoices,
            "payments_already_migrated": existing_payments,
        }

        logger.info(
            "Dry-run scan for tenant %s: %s",
            tenant_id,
            scan_result,
        )
        return scan_result

    # ------------------------------------------------------------------
    # Phase 1 — Customers + Accounts
    # ------------------------------------------------------------------

    async def phase_1_customers_accounts(self, tenant_id: str) -> Dict[str, Any]:
        """Create Customer records from CustomerProfile; create one default Account per Customer.

        Idempotent: skips profiles that already have a corresponding Customer
        record (correlated by customer_id).
        """
        logger.info("Phase 1: Migrating customers + accounts for tenant %s", tenant_id)

        profiles = await self._fetch_customer_profiles(tenant_id)
        existing_customer_ids = await self._fetch_existing_customer_ids(tenant_id)

        created_customers = 0
        created_accounts = 0
        skipped = 0

        for profile in profiles:
            customer_id = profile.get("customer_id")
            if not customer_id:
                continue

            # Idempotency check: skip if already migrated
            if customer_id in existing_customer_ids:
                skipped += 1
                continue

            # Create Customer record
            now = utcnow()
            customer_doc = {
                "customer_id": customer_id,
                "tenant_id": tenant_id,
                "display_name": profile.get("display_name") or profile.get("customer_id", "Unknown"),
                "legal_name": profile.get("legal_name"),
                "primary_email": profile.get("primary_email"),
                "tax_id": profile.get("tax_id"),
                "status": "active",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "external_refs": profile.get("external_refs", {}),
                "metadata": {"migrated_from": "customer_profile", "migration_ts": now.isoformat()},
            }
            await self._es.index_document(CUSTOMERS_CURRENT_INDEX, customer_id, customer_doc)
            created_customers += 1

            # Create one default Account per Customer
            account_id = f"acct_{uuid4()}"
            account_doc = {
                "account_id": account_id,
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "display_name": f"{customer_doc['display_name']} - Default",
                "status": "active",
                "credit_limit_cents": 0,
                "open_balance_cents": 0,
                "available_credit_cents": 0,
                "credit_balance_cents": 0,
                "credit_state": "ok",
                "credit_override_expires_at": None,
                "net_terms_days": 30,
                "tier": "default",
                "billing_address": None,
                "payment_method_preference": "invoice",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "external_refs": profile.get("external_refs", {}),
            }
            await self._es.index_document(ACCOUNTS_CURRENT_INDEX, account_id, account_doc)
            created_accounts += 1

        result = {
            "status": "completed",
            "created_customers": created_customers,
            "created_accounts": created_accounts,
            "skipped_existing": skipped,
            "total_profiles": len(profiles),
        }
        logger.info("Phase 1 complete for tenant %s: %s", tenant_id, result)
        return result

    # ------------------------------------------------------------------
    # Phase 2 — PriceBook
    # ------------------------------------------------------------------

    async def phase_2_price_book(self, tenant_id: str) -> Dict[str, Any]:
        """Read tenant rack prices + product catalog; emit a single Default Price Book.

        Creates one PricingRule per (product_code, default scope).
        Idempotent: skips if a price book named 'Default Price Book' already exists.
        """
        logger.info("Phase 2: Creating default price book for tenant %s", tenant_id)

        # Idempotency check: see if default price book already exists
        existing_pb = await self._find_default_price_book(tenant_id)
        if existing_pb:
            logger.info("Phase 2: Default price book already exists for tenant %s, skipping", tenant_id)
            return {
                "status": "skipped",
                "reason": "default_price_book_exists",
                "price_book_id": existing_pb.get("price_book_id"),
            }

        # Fetch product catalog
        products = await self._fetch_product_catalog(tenant_id)
        if not products:
            logger.info("Phase 2: No products found for tenant %s", tenant_id)
            return {"status": "completed", "rules_created": 0, "reason": "no_products"}

        # Create the price book
        now = utcnow()
        price_book_id = f"pb_{uuid4()}"
        price_book_doc = {
            "price_book_id": price_book_id,
            "tenant_id": tenant_id,
            "name": "Default Price Book",
            "description": "Auto-generated during commerce migration backfill",
            "status": "active",
            "rule_count": len(products),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        await self._es.index_document(PRICE_BOOKS_CURRENT_INDEX, price_book_id, price_book_doc)

        # Create one pricing rule per product
        rules_created = 0
        for product in products:
            product_code = product.get("product_code") or product.get("code")
            if not product_code:
                continue

            rule_id = f"rule_{uuid4()}"
            unit_price_cents = product.get("rack_price_cents") or product.get("unit_price_cents", 0)

            rule_doc = {
                "rule_id": rule_id,
                "price_book_id": price_book_id,
                "tenant_id": tenant_id,
                "product_code": product_code,
                "scope_type": "default",
                "scope_value": "default",
                "effective_from": now.isoformat(),
                "effective_to": None,
                "min_quantity_gallons": None,
                "unit_price_cents": unit_price_cents,
                "created_at": now.isoformat(),
            }
            await self._es.index_document(PRICING_RULES_CURRENT_INDEX, rule_id, rule_doc)
            rules_created += 1

        result = {
            "status": "completed",
            "price_book_id": price_book_id,
            "rules_created": rules_created,
        }
        logger.info("Phase 2 complete for tenant %s: %s", tenant_id, result)
        return result

    # ------------------------------------------------------------------
    # Phase 3 — Invoices + Payments
    # ------------------------------------------------------------------

    async def phase_3_invoices_payments(self, tenant_id: str) -> Dict[str, Any]:
        """Pull QBO invoices + payments for the last 24 months via the QBO connector.

        Idempotent: skips invoices/payments that already exist (correlated by
        external_refs.qbo).
        """
        logger.info("Phase 3: Importing QBO invoices + payments for tenant %s", tenant_id)

        if not self._qbo:
            logger.info("Phase 3: No QBO connector provided, skipping")
            return {"status": "skipped", "reason": "no_qbo_connector"}

        # Fetch QBO invoices for the last 24 months
        since = utcnow() - timedelta(days=QBO_LOOKBACK_MONTHS * 30)
        qbo_invoices = await self._fetch_qbo_invoices(tenant_id, since)
        qbo_payments = await self._fetch_qbo_payments(tenant_id, since)

        # Get existing external refs to check for duplicates
        existing_invoice_refs = await self._fetch_existing_invoice_qbo_refs(tenant_id)
        existing_payment_refs = await self._fetch_existing_payment_qbo_refs(tenant_id)

        invoices_created = 0
        invoices_skipped = 0
        payments_created = 0
        payments_skipped = 0

        # Import invoices
        for qbo_inv in qbo_invoices:
            qbo_id = qbo_inv.get("Id") or qbo_inv.get("id")
            if not qbo_id:
                continue

            qbo_ref = f"inv:{qbo_id}"
            if qbo_ref in existing_invoice_refs:
                invoices_skipped += 1
                continue

            now = utcnow()
            invoice_id = f"inv_{uuid4()}"
            total_cents = int(qbo_inv.get("TotalAmt", 0) * 100) if qbo_inv.get("TotalAmt") else 0
            balance_cents = int(qbo_inv.get("Balance", 0) * 100) if qbo_inv.get("Balance") else 0
            paid_cents = total_cents - balance_cents

            # Determine status
            if balance_cents == 0 and total_cents > 0:
                status = "paid"
            elif paid_cents > 0:
                status = "partial"
            else:
                status = "open"

            invoice_doc = {
                "invoice_id": invoice_id,
                "tenant_id": tenant_id,
                "customer_id": qbo_inv.get("customer_id", "unknown"),
                "account_id": qbo_inv.get("account_id", "unknown"),
                "order_id": None,
                "invoice_number": qbo_inv.get("DocNumber"),
                "status": status,
                "total_cents": total_cents,
                "amount_paid_cents": paid_cents,
                "remaining_cents": balance_cents,
                "tax_cents": int(qbo_inv.get("TxnTaxDetail", {}).get("TotalTax", 0) * 100) if isinstance(qbo_inv.get("TxnTaxDetail"), dict) else 0,
                "subtotal_cents": total_cents,
                "line_items": [],
                "issued_at": qbo_inv.get("TxnDate"),
                "due_date": qbo_inv.get("DueDate"),
                "external_refs": {"qbo": qbo_ref},
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "qbo_push_state": "pushed",
                "qbo_push_attempts": 0,
                "qbo_push_last_error": None,
            }
            await self._es.index_document(INVOICES_CURRENT_INDEX, invoice_id, invoice_doc)
            invoices_created += 1

        # Import payments
        for qbo_pay in qbo_payments:
            qbo_id = qbo_pay.get("Id") or qbo_pay.get("id")
            if not qbo_id:
                continue

            qbo_ref = f"pay:{qbo_id}"
            if qbo_ref in existing_payment_refs:
                payments_skipped += 1
                continue

            now = utcnow()
            payment_id = f"pay_{uuid4()}"
            amount_cents = int(qbo_pay.get("TotalAmt", 0) * 100) if qbo_pay.get("TotalAmt") else 0

            payment_doc = {
                "payment_id": payment_id,
                "tenant_id": tenant_id,
                "invoice_id": qbo_pay.get("invoice_id", "unknown"),
                "account_id": qbo_pay.get("account_id", "unknown"),
                "amount_cents": amount_cents,
                "source": "qbo",
                "method": qbo_pay.get("PaymentMethodRef", {}).get("value", "other") if isinstance(qbo_pay.get("PaymentMethodRef"), dict) else "other",
                "external_id": str(qbo_id),
                "reference": qbo_pay.get("PaymentRefNum"),
                "status": "applied",
                "received_at": qbo_pay.get("TxnDate"),
                "applied_at": now.isoformat(),
                "reversed_at": None,
            }
            await self._es.index_document(PAYMENTS_CURRENT_INDEX, payment_id, payment_doc)
            payments_created += 1

        result = {
            "status": "completed",
            "invoices_created": invoices_created,
            "invoices_skipped": invoices_skipped,
            "payments_created": payments_created,
            "payments_skipped": payments_skipped,
        }
        logger.info("Phase 3 complete for tenant %s: %s", tenant_id, result)
        return result

    # ------------------------------------------------------------------
    # Phase 4 — Verify
    # ------------------------------------------------------------------

    async def phase_4_verify(self, tenant_id: str) -> Dict[str, Any]:
        """Write a verification report comparing source counts to migrated counts.

        Checks data integrity and completeness after migration.
        """
        logger.info("Phase 4: Verifying migration for tenant %s", tenant_id)

        # Count source records
        profile_count = await self._count_customer_profiles(tenant_id)

        # Count migrated records
        customer_count = await self._count_existing_customers(tenant_id)
        account_count = await self._count_existing_accounts(tenant_id)
        price_book_count = await self._count_existing_price_books(tenant_id)
        invoice_count = await self._count_existing_invoices(tenant_id)
        payment_count = await self._count_existing_payments(tenant_id)

        # Verify customer coverage
        customer_coverage_pct = (
            (customer_count / profile_count * 100) if profile_count > 0 else 100.0
        )

        # Verify account-to-customer ratio (should be >= 1:1)
        account_ratio = (account_count / customer_count) if customer_count > 0 else 0.0

        issues: List[str] = []
        if customer_coverage_pct < 100.0:
            issues.append(
                f"Customer coverage {customer_coverage_pct:.1f}% "
                f"({customer_count}/{profile_count})"
            )
        if account_ratio < 1.0 and customer_count > 0:
            issues.append(
                f"Account-to-customer ratio {account_ratio:.2f} (expected >= 1.0)"
            )

        verification_status = "pass" if not issues else "warn"

        result = {
            "status": verification_status,
            "source_profiles": profile_count,
            "migrated_customers": customer_count,
            "migrated_accounts": account_count,
            "migrated_price_books": price_book_count,
            "migrated_invoices": invoice_count,
            "migrated_payments": payment_count,
            "customer_coverage_pct": round(customer_coverage_pct, 2),
            "account_to_customer_ratio": round(account_ratio, 2),
            "issues": issues,
            "verified_at": utcnow().isoformat(),
        }
        logger.info("Phase 4 verification for tenant %s: %s", tenant_id, result)
        return result

    # ------------------------------------------------------------------
    # Private helpers — counting
    # ------------------------------------------------------------------

    async def _count_customer_profiles(self, tenant_id: str) -> int:
        """Count CustomerProfile records for the tenant."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(CUSTOMER_PROFILES_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_existing_customers(self, tenant_id: str) -> int:
        """Count already-migrated Customer records."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(CUSTOMERS_CURRENT_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_existing_accounts(self, tenant_id: str) -> int:
        """Count existing Account records."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(ACCOUNTS_CURRENT_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_existing_price_books(self, tenant_id: str) -> int:
        """Count existing PriceBook records."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(PRICE_BOOKS_CURRENT_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_existing_invoices(self, tenant_id: str) -> int:
        """Count existing Invoice records."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(INVOICES_CURRENT_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_existing_payments(self, tenant_id: str) -> int:
        """Count existing Payment records."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(PAYMENTS_CURRENT_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_products(self, tenant_id: str) -> int:
        """Count product catalog entries."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 0,
            "track_total_hits": True,
        }
        try:
            resp = await self._es.search_documents(PRODUCT_CATALOG_INDEX, query, size=0)
            return resp["hits"]["total"]["value"]
        except Exception:
            return 0

    async def _count_qbo_invoices(self, tenant_id: str) -> int:
        """Count QBO invoices available for import."""
        try:
            since = utcnow() - timedelta(days=QBO_LOOKBACK_MONTHS * 30)
            invoices = await self._fetch_qbo_invoices(tenant_id, since)
            return len(invoices)
        except Exception:
            return 0

    async def _count_qbo_payments(self, tenant_id: str) -> int:
        """Count QBO payments available for import."""
        try:
            since = utcnow() - timedelta(days=QBO_LOOKBACK_MONTHS * 30)
            payments = await self._fetch_qbo_payments(tenant_id, since)
            return len(payments)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Private helpers — fetching
    # ------------------------------------------------------------------

    async def _fetch_customer_profiles(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Fetch all CustomerProfile records for the tenant."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 10000,
        }
        try:
            resp = await self._es.search_documents(CUSTOMER_PROFILES_INDEX, query, size=10000)
            return [hit["_source"] for hit in resp["hits"]["hits"]]
        except Exception:
            return []

    async def _fetch_existing_customer_ids(self, tenant_id: str) -> set:
        """Fetch the set of customer_ids already in customers_current."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "_source": ["customer_id"],
            "size": 10000,
        }
        try:
            resp = await self._es.search_documents(CUSTOMERS_CURRENT_INDEX, query, size=10000)
            return {hit["_source"]["customer_id"] for hit in resp["hits"]["hits"]}
        except Exception:
            return set()

    async def _find_default_price_book(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """Find an existing 'Default Price Book' for the tenant."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"name": "Default Price Book"}},
                    ]
                }
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(PRICE_BOOKS_CURRENT_INDEX, query, size=1)
            hits = resp["hits"]["hits"]
            return hits[0]["_source"] if hits else None
        except Exception:
            return None

    async def _fetch_product_catalog(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Fetch product catalog entries for the tenant."""
        query = {
            "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
            "size": 10000,
        }
        try:
            resp = await self._es.search_documents(PRODUCT_CATALOG_INDEX, query, size=10000)
            return [hit["_source"] for hit in resp["hits"]["hits"]]
        except Exception:
            return []

    async def _fetch_qbo_invoices(
        self, tenant_id: str, since: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch QBO invoices via the connector's query endpoint."""
        try:
            return await self._qbo.query_invoices(tenant_id, since=since)
        except Exception:
            return []

    async def _fetch_qbo_payments(
        self, tenant_id: str, since: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch QBO payments via the connector's query endpoint."""
        try:
            return await self._qbo.query_payments(tenant_id, since=since)
        except Exception:
            return []

    async def _fetch_existing_invoice_qbo_refs(self, tenant_id: str) -> set:
        """Fetch the set of QBO refs already in invoices_current."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"exists": {"field": "external_refs.qbo"}},
                    ]
                }
            },
            "_source": ["external_refs.qbo"],
            "size": 10000,
        }
        try:
            resp = await self._es.search_documents(INVOICES_CURRENT_INDEX, query, size=10000)
            return {
                hit["_source"].get("external_refs", {}).get("qbo")
                for hit in resp["hits"]["hits"]
                if hit["_source"].get("external_refs", {}).get("qbo")
            }
        except Exception:
            return set()

    async def _fetch_existing_payment_qbo_refs(self, tenant_id: str) -> set:
        """Fetch the set of QBO refs already in payments_current."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"source": "qbo"}},
                    ]
                }
            },
            "_source": ["external_id"],
            "size": 10000,
        }
        try:
            resp = await self._es.search_documents(PAYMENTS_CURRENT_INDEX, query, size=10000)
            return {
                f"pay:{hit['_source']['external_id']}"
                for hit in resp["hits"]["hits"]
                if hit["_source"].get("external_id")
            }
        except Exception:
            return set()
