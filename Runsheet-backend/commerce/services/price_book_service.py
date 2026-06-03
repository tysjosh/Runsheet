"""CRUD for PriceBook + denormalized fan-out into pricing_rules_current.

Implements the PriceBookService with create, get, list, update, and activate
methods. Every method takes tenant_id as its first parameter and every ES
query passes through inject_tenant_filter.

On activate, the service fans out the book's rules into pricing_rules_current
(the resolver's hot path). Book mutations bump the cache invalidation key so
the PricingEngine's Redis cache is invalidated within the 5-minute TTL window.

Validates: Requirements 3.1, 3.4, 3.6, C1, C2, C3, C6
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from commerce.models.price_book import (
    PriceBook,
    PriceBookStatus,
    PricingRule,
    PricingScopeType,
)
from commerce.services.commerce_es_mappings import (
    PRICE_BOOKS_CURRENT_INDEX,
    PRICING_RULES_CURRENT_INDEX,
)
from errors.exceptions import conflict, resource_not_found, validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200
_CACHE_KEY_PREFIX = "commerce:pricing"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PriceBookService:
    """Service layer for PriceBook CRUD and pricing rule fan-out.

    Every public method takes ``tenant_id`` as its first positional argument
    and every Elasticsearch query is wrapped with ``inject_tenant_filter``
    to enforce strict tenant isolation (Constraint C3).

    On activate, rules are fanned out into ``pricing_rules_current`` for
    fast resolution by the PricingEngine. Book mutations bump the cache
    invalidation key so the PricingEngine's Redis cache is invalidated
    (Req 3.6).

    Args:
        es_service: ElasticsearchService instance for persistence.
        redis_client: Async Redis client for cache invalidation.
            When None, cache invalidation is skipped.
        canonicalize_fn: Callable that canonicalizes a product code string.
            Expected to raise on unknown products (Constraint C6).
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        redis_client: Any = None,
        canonicalize_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._es = es_service
        self._redis = redis_client
        self._canonicalize_fn = canonicalize_fn

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_rules(
        self, rules: List[Dict[str, Any]], tenant_id: str, price_book_id: str
    ) -> List[Dict[str, Any]]:
        """Validate and normalize a list of pricing rules.

        For each rule:
        - product_code is canonicalized via canonicalize_fn (Constraint C6)
        - unit_price_cents >= 0 (Constraint C1)
        - effective window is coherent (effective_to > effective_from when present)

        Returns the validated rule dicts ready for persistence.

        Raises:
            validation_error: On any rule validation failure.
        """
        validated: List[Dict[str, Any]] = []

        for idx, rule_data in enumerate(rules):
            # Canonicalize product_code
            product_code = rule_data.get("product_code", "")
            if not product_code or not str(product_code).strip():
                raise validation_error(
                    f"Rule {idx}: product_code must not be empty",
                    details={"rule_index": idx, "product_code": product_code},
                )

            if self._canonicalize_fn is not None:
                try:
                    product_code = self._canonicalize_fn(str(product_code))
                except (ValueError, TypeError, Exception) as exc:
                    raise validation_error(
                        f"Rule {idx}: unknown product_code '{product_code}'",
                        details={
                            "rule_index": idx,
                            "product_code": product_code,
                            "error": str(exc),
                        },
                    )
            else:
                product_code = str(product_code).strip()

            # Validate unit_price_cents
            unit_price_cents = rule_data.get("unit_price_cents")
            if unit_price_cents is None:
                raise validation_error(
                    f"Rule {idx}: unit_price_cents is required",
                    details={"rule_index": idx},
                )
            if not isinstance(unit_price_cents, int):
                raise validation_error(
                    f"Rule {idx}: unit_price_cents must be an integer",
                    details={"rule_index": idx, "unit_price_cents": unit_price_cents},
                )
            if unit_price_cents < 0:
                raise validation_error(
                    f"Rule {idx}: unit_price_cents must be >= 0",
                    details={"rule_index": idx, "unit_price_cents": unit_price_cents},
                )

            # Validate effective window
            effective_from = rule_data.get("effective_from")
            if effective_from is None:
                raise validation_error(
                    f"Rule {idx}: effective_from is required",
                    details={"rule_index": idx},
                )

            effective_to = rule_data.get("effective_to")
            if effective_to is not None:
                # Parse both for comparison
                from_dt = _parse_datetime(effective_from)
                to_dt = _parse_datetime(effective_to)
                if from_dt is not None and to_dt is not None and to_dt <= from_dt:
                    raise validation_error(
                        f"Rule {idx}: effective_to must be after effective_from",
                        details={
                            "rule_index": idx,
                            "effective_from": str(effective_from),
                            "effective_to": str(effective_to),
                        },
                    )

            # Validate scope
            scope_type = rule_data.get("scope_type")
            if scope_type is None:
                # Try extracting from nested scope object
                scope = rule_data.get("scope", {})
                scope_type = scope.get("scope_type") or scope.get("tier") or scope.get("type")
                if scope_type is None:
                    scope_type = PricingScopeType.DEFAULT.value

            # Normalize scope_type to enum value
            if isinstance(scope_type, PricingScopeType):
                scope_type = scope_type.value
            elif scope_type not in [s.value for s in PricingScopeType]:
                raise validation_error(
                    f"Rule {idx}: invalid scope_type '{scope_type}'",
                    details={"rule_index": idx, "scope_type": scope_type},
                )

            scope_value = rule_data.get("scope_value", "default")
            if isinstance(scope_value, str):
                scope_value = scope_value.strip() or "default"

            # Build the validated rule dict
            now = utcnow()
            rule_id = rule_data.get("rule_id") or f"rule_{uuid4()}"

            validated_rule: Dict[str, Any] = {
                "rule_id": rule_id,
                "price_book_id": price_book_id,
                "tenant_id": tenant_id,
                "product_code": product_code,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "effective_from": _serialize_datetime(effective_from),
                "effective_to": _serialize_datetime(effective_to) if effective_to else None,
                "min_quantity_gallons": rule_data.get("min_quantity_gallons"),
                "unit_price_cents": unit_price_cents,
                "created_at": _serialize_datetime(rule_data.get("created_at") or now),
            }
            validated.append(validated_rule)

        return validated

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        *,
        name: str,
        description: Optional[str] = None,
        status: str = "draft",
        rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a new PriceBook with optional rules.

        Validates every rule (product_code canonicalized, unit_price_cents >= 0,
        effective window coherent), persists the book to price_books_current,
        and fans rules into pricing_rules_current if the book is created with
        status=active.

        Validates: Requirements 3.1, C1, C3, C6
        """
        if not name or not name.strip():
            raise validation_error(
                "name must not be empty",
                details={"name": name},
            )

        now = utcnow()
        price_book_id = f"pb_{uuid4()}"
        rules = rules or []

        # Validate rules
        validated_rules = self._validate_rules(rules, tenant_id, price_book_id)

        # Persist the book (without embedded rules — rules go to pricing_rules_current)
        book_doc: Dict[str, Any] = {
            "price_book_id": price_book_id,
            "tenant_id": tenant_id,
            "name": name.strip(),
            "description": description,
            "status": status,
            "rule_count": len(validated_rules),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        await self._es.index_document(
            PRICE_BOOKS_CURRENT_INDEX, price_book_id, book_doc
        )

        # Dual-write the price book to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_price_book_create,
        )
        await mirror_price_book_create(book_doc)

        # Persist rules to pricing_rules_current
        if validated_rules:
            await self._persist_rules(validated_rules)

        # If created as active, bump cache invalidation
        if status == PriceBookStatus.ACTIVE.value:
            await self._invalidate_cache(tenant_id, validated_rules)

        logger.info(
            "Created price book %s with %d rules for tenant %s",
            price_book_id,
            len(validated_rules),
            tenant_id,
        )

        # Return the book with embedded rules for the API response
        book_doc["rules"] = validated_rules
        return book_doc

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str, price_book_id: str) -> Dict[str, Any]:
        """Retrieve a single PriceBook by ID, scoped to tenant.

        Also fetches the associated rules from pricing_rules_current.

        Validates: Requirement C3
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"price_book_id": price_book_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PRICE_BOOKS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"PriceBook '{price_book_id}' not found",
                details={"price_book_id": price_book_id},
            )

        book = hits[0]["_source"]

        # Fetch associated rules
        book["rules"] = await self._get_rules_for_book(tenant_id, price_book_id)

        return book

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list(
        self,
        tenant_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List PriceBooks for a tenant with cursor/limit pagination.

        Validates: Requirement C3
        """
        if limit < 1:
            limit = _DEFAULT_PAGE_LIMIT
        if limit > _MAX_PAGE_LIMIT:
            limit = _MAX_PAGE_LIMIT

        must_clauses: List[Dict[str, Any]] = []
        if status:
            must_clauses.append({"term": {"status": status}})

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": limit,
            "sort": [
                {"created_at": {"order": "desc"}},
                {"price_book_id": {"order": "asc"}},
            ],
        }

        if cursor:
            base_query["search_after"] = [cursor, cursor]

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PRICE_BOOKS_CURRENT_INDEX, query, size=limit
        )

        hits = response["hits"]["hits"]
        items = [hit["_source"] for hit in hits]

        next_cursor: Optional[str] = None
        if hits and len(hits) == limit:
            last_sort = hits[-1].get("sort")
            if last_sort and len(last_sort) >= 2:
                next_cursor = hits[-1]["_source"]["price_book_id"]

        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        price_book_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = ...,  # type: ignore[assignment]
        status: Optional[str] = None,
        rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing PriceBook.

        Edits do NOT retroactively re-price already-invoiced orders (Req 3.4).
        Book mutations bump the cache invalidation key (Req 3.6).

        Validates: Requirements 3.4, 3.6, C1, C3, C6
        """
        existing = await self.get(tenant_id, price_book_id)

        partial: Dict[str, Any] = {}

        if name is not None:
            if not name.strip():
                raise validation_error(
                    "name must not be empty",
                    details={"name": name},
                )
            partial["name"] = name.strip()

        if description is not ...:
            partial["description"] = description

        if status is not None:
            # Validate status transition
            current_status = existing.get("status", "draft")
            if not self._is_valid_status_transition(current_status, status):
                raise conflict(
                    f"Cannot transition price book from '{current_status}' to '{status}'",
                    error_code="INVALID_STATUS_TRANSITION",
                    details={
                        "price_book_id": price_book_id,
                        "current_status": current_status,
                        "requested_status": status,
                    },
                )
            partial["status"] = status

        # Handle rule updates
        new_rules: Optional[List[Dict[str, Any]]] = None
        if rules is not None:
            new_rules = self._validate_rules(rules, tenant_id, price_book_id)
            partial["rule_count"] = len(new_rules)

            # Remove old rules and persist new ones
            await self._remove_rules_for_book(tenant_id, price_book_id)
            if new_rules:
                await self._persist_rules(new_rules)

        if not partial and new_rules is None:
            return existing

        now = utcnow()
        partial["updated_at"] = now.isoformat()

        await self._es.update_document(
            PRICE_BOOKS_CURRENT_INDEX, price_book_id, partial
        )

        # Mirror the book field changes (+ recomputed rule_count) to Postgres.
        from commerce.services.commerce_persistence_bridge import (
            mirror_price_book_fields,
        )
        _pg = {k: v for k, v in partial.items() if k != "updated_at"}
        if new_rules is not None:
            _pg["rule_count"] = len(new_rules)
        await mirror_price_book_fields(tenant_id, price_book_id, _pg)

        # Bump cache invalidation on any mutation (Req 3.6)
        rules_for_invalidation = new_rules or existing.get("rules", [])
        await self._invalidate_cache(tenant_id, rules_for_invalidation)

        merged = {**existing, **partial}
        if new_rules is not None:
            merged["rules"] = new_rules

        logger.info(
            "Updated price book %s for tenant %s",
            price_book_id,
            tenant_id,
        )
        return merged

    # ------------------------------------------------------------------
    # Activate
    # ------------------------------------------------------------------

    async def activate(
        self, tenant_id: str, price_book_id: str
    ) -> Dict[str, Any]:
        """Activate a PriceBook, fanning its rules into pricing_rules_current.

        When a PriceBook is activated:
        1. Status transitions to 'active'
        2. Rules are fanned out into pricing_rules_current (denormalized)
        3. Cache invalidation key is bumped so PricingEngine picks up changes

        Validates: Requirements 3.1, 3.6, C3
        """
        existing = await self.get(tenant_id, price_book_id)
        current_status = existing.get("status", "draft")

        if current_status == PriceBookStatus.ACTIVE.value:
            # Already active — re-fan rules for idempotency
            rules = existing.get("rules", [])
            await self._remove_rules_for_book(tenant_id, price_book_id)
            if rules:
                await self._persist_rules(rules)
            await self._invalidate_cache(tenant_id, rules)
            return existing

        if current_status == PriceBookStatus.ARCHIVED.value:
            raise conflict(
                "Cannot activate an archived price book",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "price_book_id": price_book_id,
                    "current_status": current_status,
                },
            )

        # Transition to active
        now = utcnow()
        partial: Dict[str, Any] = {
            "status": PriceBookStatus.ACTIVE.value,
            "updated_at": now.isoformat(),
        }

        await self._es.update_document(
            PRICE_BOOKS_CURRENT_INDEX, price_book_id, partial
        )

        # Mirror the activation status change to Postgres.
        from commerce.services.commerce_persistence_bridge import (
            mirror_price_book_fields,
        )
        await mirror_price_book_fields(
            tenant_id, price_book_id, {"status": PriceBookStatus.ACTIVE.value}
        )

        # Fan out rules into pricing_rules_current
        rules = existing.get("rules", [])
        # Remove any stale rules for this book first
        await self._remove_rules_for_book(tenant_id, price_book_id)
        if rules:
            await self._persist_rules(rules)

        # Bump cache invalidation (Req 3.6)
        await self._invalidate_cache(tenant_id, rules)

        merged = {**existing, **partial}
        merged["rules"] = rules

        logger.info(
            "Activated price book %s with %d rules for tenant %s",
            price_book_id,
            len(rules),
            tenant_id,
        )
        return merged

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_rules_for_book(
        self, tenant_id: str, price_book_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch all pricing rules for a given price book."""
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"price_book_id": price_book_id}},
                    ]
                }
            },
            "size": 1000,
            "sort": [{"created_at": {"order": "asc"}}],
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PRICING_RULES_CURRENT_INDEX, query, size=1000
        )

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]

    async def _persist_rules(self, rules: List[Dict[str, Any]]) -> None:
        """Persist validated rules to pricing_rules_current."""
        for rule in rules:
            rule_id = rule["rule_id"]
            await self._es.index_document(
                PRICING_RULES_CURRENT_INDEX, rule_id, rule
            )
        # Dual-write the rule batch to Postgres (parent book mirrored above).
        from commerce.services.commerce_persistence_bridge import (
            mirror_pricing_rules_upsert,
        )
        await mirror_pricing_rules_upsert(rules)

    async def _remove_rules_for_book(
        self, tenant_id: str, price_book_id: str
    ) -> None:
        """Remove all existing rules for a price book from pricing_rules_current.

        Since ElasticsearchService doesn't expose delete_by_query, we
        search for all rules and delete them individually.
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"price_book_id": price_book_id}},
                    ]
                }
            },
            "size": 1000,
            "_source": ["rule_id"],
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PRICING_RULES_CURRENT_INDEX, query, size=1000
        )

        hits = response.get("hits", {}).get("hits", [])
        removed_ids = []
        for hit in hits:
            rule_id = hit["_source"]["rule_id"]
            await self._es.delete_document(PRICING_RULES_CURRENT_INDEX, rule_id)
            removed_ids.append(rule_id)
        # Mirror the deletions to Postgres.
        from commerce.services.commerce_persistence_bridge import (
            mirror_pricing_rules_delete,
        )
        await mirror_pricing_rules_delete(tenant_id, removed_ids)

    async def _invalidate_cache(
        self, tenant_id: str, rules: List[Dict[str, Any]]
    ) -> None:
        """Bump cache invalidation for affected product codes.

        Deletes Redis keys matching commerce:pricing:{tenant_id}:{product_code}
        for every product_code in the affected rules. This ensures the
        PricingEngine's Redis cache is invalidated within the 5-minute TTL
        window (Req 3.6).

        If no Redis client is configured, this is a no-op.
        """
        if self._redis is None:
            return

        # Collect unique product codes from the rules
        product_codes = set()
        for rule in rules:
            pc = rule.get("product_code")
            if pc:
                product_codes.add(pc)

        # Delete cache keys for each affected product code
        for product_code in product_codes:
            cache_key = f"{_CACHE_KEY_PREFIX}:{tenant_id}:{product_code}"
            try:
                await self._redis.delete(cache_key)
                logger.debug(
                    "Invalidated cache key %s for tenant %s",
                    cache_key,
                    tenant_id,
                )
            except Exception:
                logger.warning(
                    "Failed to invalidate cache key %s",
                    cache_key,
                    exc_info=True,
                )

        logger.info(
            "Cache invalidation complete for tenant %s: %d product codes",
            tenant_id,
            len(product_codes),
        )

    @staticmethod
    def _is_valid_status_transition(current: str, target: str) -> bool:
        """Check if a status transition is valid.

        Valid transitions:
        - draft -> active
        - draft -> archived
        - active -> archived
        - active -> draft (for deactivation / editing)
        """
        valid_transitions = {
            PriceBookStatus.DRAFT.value: {
                PriceBookStatus.ACTIVE.value,
                PriceBookStatus.ARCHIVED.value,
            },
            PriceBookStatus.ACTIVE.value: {
                PriceBookStatus.ARCHIVED.value,
                PriceBookStatus.DRAFT.value,
            },
            PriceBookStatus.ARCHIVED.value: set(),  # Terminal state
        }
        allowed = valid_transitions.get(current, set())
        return target in allowed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> Optional[Any]:
    """Parse a datetime value, returning None on failure."""
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
    return None


def _serialize_datetime(value: Any) -> Optional[str]:
    """Serialize a datetime value to ISO format string."""
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)
