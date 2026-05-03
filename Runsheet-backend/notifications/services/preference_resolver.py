"""
Preference resolver for the Customer Notification Pipeline.

Resolves customer notification preferences to determine which channels
and contact details to use when dispatching notifications. Provides
CRUD operations for managing notification preferences stored in the
``notification_preferences`` Elasticsearch index.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import logging
import uuid
from datetime import datetime, timezone

from errors.exceptions import resource_not_found, validation_error
from notifications.services.notification_es_mappings import NOTIFICATION_PREFERENCES_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class PreferenceResolver:
    """Resolve and manage customer notification preferences stored in Elasticsearch.

    Each preference maps a customer to their channel contact details and
    per-event-type channel selections, scoped to a tenant.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    # ------------------------------------------------------------------
    # Resolve channels
    # ------------------------------------------------------------------

    async def resolve_channels(
        self, customer_id: str, event_type: str, tenant_id: str
    ) -> list[dict]:
        """Return a list of ``{channel, contact_detail}`` dicts for the customer+event combo.

        Looks up the customer's preference document, finds the
        ``event_preferences`` entry matching *event_type*, and returns one
        dict per enabled channel with the corresponding contact detail from
        the ``channels`` map.

        If no preference exists for the customer, returns an empty list.
        The caller (``NotificationService``) is responsible for falling back
        to default channels from the notification rule.

        Validates: Requirements 4.5, 4.6

        Args:
            customer_id: The customer identifier.
            event_type: The operational event type (e.g. ``delay_alert``).
            tenant_id: Tenant scope from JWT.

        Returns:
            List of dicts, each with ``channel`` and ``contact_detail`` keys.
            Empty list when no preference is found.
        """
        preference = await self._find_preference(customer_id, tenant_id)

        if preference is None:
            logger.debug(
                "No notification preference found for customer_id=%s tenant_id=%s — "
                "caller should fall back to default channels",
                customer_id,
                tenant_id,
            )
            return []

        # Find the event_preferences entry matching the event_type
        channels_map: dict[str, str] = preference.get("channels", {})
        event_preferences: list[dict] = preference.get("event_preferences", [])

        matching_event_pref = None
        for ep in event_preferences:
            if ep.get("event_type") == event_type:
                matching_event_pref = ep
                break

        if matching_event_pref is None:
            logger.debug(
                "No event_preference entry for event_type=%s in customer_id=%s preference",
                event_type,
                customer_id,
            )
            return []

        enabled_channels: list[str] = matching_event_pref.get("enabled_channels", [])

        result: list[dict] = []
        for channel in enabled_channels:
            contact_detail = channels_map.get(channel)
            if contact_detail:
                result.append({
                    "channel": channel,
                    "contact_detail": contact_detail,
                })
            else:
                logger.warning(
                    "Channel '%s' enabled for event_type=%s but no contact detail "
                    "in channels map for customer_id=%s",
                    channel,
                    event_type,
                    customer_id,
                )

        return result

    # ------------------------------------------------------------------
    # List preferences (paginated)
    # ------------------------------------------------------------------

    async def list_preferences(
        self,
        tenant_id: str,
        page: int,
        size: int,
        search: str | None = None,
    ) -> dict:
        """Return a paginated list of notification preferences for a tenant.

        Supports optional search on ``customer_name``.

        Validates: Requirement 4.2

        Args:
            tenant_id: Tenant scope from JWT.
            page: 1-based page number.
            size: Number of results per page.
            search: Optional search string matched against ``customer_name``.

        Returns:
            Dict with ``items``, ``total``, ``page``, ``size`` keys.
        """
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
        ]

        if search:
            must_clauses.append({
                "match": {"customer_name": {"query": search, "fuzziness": "AUTO"}},
            })

        from_offset = (page - 1) * size

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                }
            },
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        response = await self._es.search_documents(
            NOTIFICATION_PREFERENCES_INDEX, query, size=size
        )

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]
        total_count = total["value"] if isinstance(total, dict) else total

        return {
            "items": [hit["_source"] for hit in hits],
            "total": total_count,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Get single preference
    # ------------------------------------------------------------------

    async def get_preference(self, customer_id: str, tenant_id: str) -> dict:
        """Return a single notification preference by customer_id + tenant_id.

        Validates: Requirement 4.3

        Args:
            customer_id: The customer identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            The preference document dict.

        Raises:
            AppException: 404 if no preference is found for this customer+tenant.
        """
        preference = await self._find_preference(customer_id, tenant_id)

        if preference is None:
            raise resource_not_found(
                f"Notification preference not found for customer '{customer_id}'",
                details={"customer_id": customer_id},
            )

        return preference

    # ------------------------------------------------------------------
    # Upsert preference
    # ------------------------------------------------------------------

    async def upsert_preference(
        self, customer_id: str, tenant_id: str, data: dict
    ) -> dict:
        """Create or update a notification preference for a customer.

        Uses ``customer_id`` as the ``preference_id`` for simplicity.
        Sets ``created_at`` on create, ``updated_at`` on both create and update.

        Validates: Requirement 4.4

        Args:
            customer_id: The customer identifier.
            tenant_id: Tenant scope from JWT.
            data: Dict containing preference fields to set/update. Expected
                keys include ``customer_name``, ``channels``, and
                ``event_preferences``.

        Returns:
            The full preference document dict after upsert.
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = await self._find_preference(customer_id, tenant_id)

        if existing is not None:
            # Update existing preference
            partial_doc = {
                **{k: v for k, v in data.items() if k not in (
                    "preference_id", "tenant_id", "customer_id", "created_at"
                )},
                "updated_at": now,
            }

            preference_id = existing["preference_id"]
            await self._es.update_document(
                NOTIFICATION_PREFERENCES_INDEX, preference_id, partial_doc
            )

            existing.update(partial_doc)
            return existing
        else:
            # Create new preference
            preference_id = customer_id
            doc = {
                "preference_id": preference_id,
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "customer_name": data.get("customer_name", ""),
                "channels": data.get("channels", {}),
                "event_preferences": data.get("event_preferences", []),
                "created_at": now,
                "updated_at": now,
            }

            await self._es.index_document(
                NOTIFICATION_PREFERENCES_INDEX, preference_id, doc
            )

            return doc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_preference(
        self, customer_id: str, tenant_id: str
    ) -> dict | None:
        """Fetch a single preference by customer_id with tenant scoping.

        Returns ``None`` when no matching document exists (does not raise).

        Args:
            customer_id: The customer identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            The preference document dict, or ``None``.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(
            NOTIFICATION_PREFERENCES_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            return None

        return hits[0]["_source"]
