"""
``DeviceRegistry`` — one record per ``(tenant_id, driver_id, device_id)``.

The registry is the store behind push delivery: ``Driver_Push_Service`` reads
the ``push_token`` values it holds, and nothing else in the backend writes
``driver_devices``. Three properties define it.

**The composite document id is the uniqueness rule.** Every write lands on
``{tenant_id}:{driver_id}:{device_id}``, so a re-registration of the same
device — a rotated token, or the app's 24-hour refresh — *replaces* the record
rather than creating a second one (R9.2). No search, no delete-then-write, no
window in which a driver has two rows for one handset. The id shape is shared
with the push dispatcher, which deletes on the same id when the provider
reports a token dead (R9.4), so the two sides of the lifecycle address the same
document by construction.

**The token is opaque.** ``push_token`` is stored exactly as the app supplied
it and is never parsed, normalized, or format-checked here, so swapping push
providers needs no migration of this store (R9.18). The mapping declares it
``index: False`` — retrievable, never queryable, and out of every inverted
index. Token *format* validation belongs to the dispatcher alone (R9.16), and
this module deliberately names no provider (R9.15).

**The subject comes from the session.** Both the ``tenant_id`` and the
``driver_id`` on a record are the verified caller's; the endpoint layer derives
them from ``require_driver_identity`` and passes them in. A record for one
driver is therefore unreachable from another driver's session, and a record for
one tenant unreachable from another's, because neither can address the id.

``registered_at`` is set once, on the first registration of a device, and
survives every subsequent replacement; ``last_seen_at`` moves on every write,
which is what makes the app's 24-hour re-registration a liveness signal rather
than a no-op.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Device registry
lifecycle.

Validates: Requirements 9.1, 9.2, 9.3, 9.18
- 9.1: a registration persists ``tenant_id``, ``driver_id``, ``device_id``,
  ``push_token``, ``platform``, and a registration timestamp
- 9.2: a new token for a known ``device_id`` replaces the stored token rather
  than creating a second record
- 9.3: sign-out deletes the registration record for that ``device_id``
- 9.18: the ``push_token`` is stored without its format being interpreted
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from driver.services.driver_es_mappings import DRIVER_DEVICES_INDEX
from errors.exceptions import internal_error, invalid_request
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: The two platforms Driver_App ships on, matching the ``platform`` field's
#: declared vocabulary. Unlike ``push_token``, this value *is* interpreted —
#: it is Runsheet's own enumeration, not a provider's opaque string.
DEVICE_PLATFORMS: tuple[str, ...] = ("ios", "android")

#: Upper bound on a stored token. A length ceiling is not format validation
#: (R9.18): it bounds what one record can cost, and interprets nothing.
MAX_PUSH_TOKEN_LENGTH: int = 2048

#: Upper bound on the registrations one push fan-out reads for a driver. A
#: driver carries a handful of handsets, so this bounds an unbounded scan rather
#: than paging a large result set.
MAX_DEVICES_PER_DRIVER: int = 25


def driver_device_doc_id(tenant_id: str, driver_id: str, device_id: str) -> str:
    """Return the ``driver_devices`` document id for one device registration.

    The same shape the push dispatcher builds when it prunes a dead token, so a
    registration and its pruning address one document.
    """
    return f"{tenant_id}:{driver_id}:{device_id}"


class DeviceRegistry:
    """Upserts, reads, and deletes ``driver_devices`` records.

    Args:
        es_service: The shared ``ElasticsearchService``. Required — it is the
            only store behind this registry.
    """

    def __init__(self, *, es_service) -> None:
        self._es_service = es_service

    # ------------------------------------------------------------------
    # Register (R9.1, R9.2, R9.18)
    # ------------------------------------------------------------------

    async def register(
        self,
        tenant_id: str,
        driver_id: str,
        device_id: str,
        *,
        push_token: str,
        platform: str,
        app_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert the registration record for one device.

        The write is a full-document index on the composite id, so the stored
        record is exactly the one built here: a rotated token replaces its
        predecessor and no second row can appear for the same device (R9.2).

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The verified caller's driver id — never a body value.
            device_id: The app's stable per-installation device identifier.
            push_token: The provider token, stored verbatim (R9.18).
            platform: ``ios`` or ``android``, matched case-insensitively.
            app_version: Optional app build identifier.

        Returns:
            The persisted record, with the ``push_token`` omitted — a caller
            that just supplied the token has no need to be handed it back, and
            leaving it out keeps it out of response bodies and access logs.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` for a blank identifier, a
                blank or over-long token, or an unknown platform.

        Validates: Requirements 9.1, 9.2, 9.18
        """
        es = self._require_es()
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")
        device_id = self._require_text(device_id, "device_id")
        token = self._validated_push_token(push_token)
        platform_value = self._validated_platform(platform)

        doc_id = driver_device_doc_id(tenant_id, driver_id, device_id)
        now = utcnow().isoformat()

        # A device that is already registered keeps its original
        # ``registered_at``; only ``last_seen_at`` moves, which is what the
        # app's 24-hour refresh is for.
        existing = await self.get(tenant_id, driver_id, device_id)
        registered_at = now
        if existing is not None:
            previous = existing.get("registered_at")
            if isinstance(previous, str) and previous.strip():
                registered_at = previous

        document: Dict[str, Any] = {
            "device_registration_id": doc_id,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "device_id": device_id,
            # Verbatim. Not trimmed, not normalized, not validated for shape.
            "push_token": token,
            "platform": platform_value,
            "app_version": (app_version or "").strip() or None,
            "registered_at": registered_at,
            "last_seen_at": now,
        }

        await es.index_document(DRIVER_DEVICES_INDEX, doc_id, document)

        logger.info(
            "Device registration upserted for tenant=%s driver=%s device=%s "
            "(replaced=%s)",
            tenant_id,
            driver_id,
            device_id,
            existing is not None,
        )

        summary = {
            key: value for key, value in document.items() if key != "push_token"
        }
        summary["replaced"] = existing is not None
        return summary

    # ------------------------------------------------------------------
    # Unregister (R9.3)
    # ------------------------------------------------------------------

    async def unregister(
        self, tenant_id: str, driver_id: str, device_id: str
    ) -> bool:
        """Delete the registration record for one device.

        Sign-out calls this (R9.3). The delete addresses the composite id, so a
        caller can only ever remove its own tenant's and its own driver's
        record — the same ``device_id`` registered by a different driver, or in
        a different tenant, lives under a different id and is untouched.

        Absence is not an error: the app signs out after a token refresh that
        may have failed, and a repeated sign-out must not surface a failure to
        a driver who has already gone.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The verified caller's driver id.
            device_id: The device whose registration to remove.

        Returns:
            ``True`` when a record was deleted, ``False`` when there was none.

        Validates: Requirements 9.3
        """
        es = self._require_es()
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")
        device_id = self._require_text(device_id, "device_id")

        doc_id = driver_device_doc_id(tenant_id, driver_id, device_id)
        deleted = bool(await es.delete_document(DRIVER_DEVICES_INDEX, doc_id))
        logger.info(
            "Device registration delete for tenant=%s driver=%s device=%s: %s",
            tenant_id,
            driver_id,
            device_id,
            "removed" if deleted else "no record",
        )
        return deleted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, driver_id: str, device_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return one registration record, or ``None`` when there is none.

        Reads by id rather than by query, so the tenant and driver scope is the
        id itself. The per-document re-validation below is a belt-and-braces
        guard: a record whose stored scope disagrees with the requested one is
        treated as absent rather than returned.
        """
        es = self._require_es()
        doc_id = driver_device_doc_id(tenant_id, driver_id, device_id)
        try:
            record = await es.get_document(DRIVER_DEVICES_INDEX, doc_id)
        except Exception as exc:
            logger.warning(
                "DeviceRegistry: read failed for tenant=%s driver=%s "
                "device=%s: %s",
                tenant_id,
                driver_id,
                device_id,
                exc,
            )
            return None

        if not isinstance(record, dict):
            return None
        if record.get("tenant_id") != tenant_id or (
            record.get("driver_id") != driver_id
        ):
            return None
        return dict(record)

    async def list_for_driver(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        limit: int = MAX_DEVICES_PER_DRIVER,
    ) -> list[Dict[str, Any]]:
        """Return every registration held for one driver, tokens included.

        This is the read behind a push fan-out: ``Driver_Push_Service`` sends to
        *every* registered device for a ``driver_id`` (R9.5, R9.6, R9.7), so the
        set has to be enumerated rather than addressed by id.

        Every returned record is re-validated on both ``tenant_id`` and
        ``driver_id`` before inclusion, so a filter regression on either axis
        drops the document instead of leaking another tenant's or another
        driver's token. A read failure returns an empty list rather than
        raising: a push fan-out must never fail the request that triggered it.

        Args:
            tenant_id: The tenant scope.
            driver_id: The driver whose devices to enumerate.
            limit: Upper bound on returned records.

        Returns:
            The matching records, each carrying its ``push_token``. Empty when
            the driver has no registered device.
        """
        es = self._require_es()
        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = limit

        try:
            response = await es.search_documents(
                DRIVER_DEVICES_INDEX, query, limit
            )
        except Exception as exc:
            logger.warning(
                "DeviceRegistry: device list read failed for tenant=%s "
                "driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return []

        records: list[Dict[str, Any]] = []
        for hit in ((response or {}).get("hits", {}) or {}).get("hits", []):
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "DeviceRegistry.list_for_driver: dropping record outside "
                    "tenant=%s",
                    tenant_id,
                )
                continue
            if source.get("driver_id") != driver_id:
                logger.warning(
                    "DeviceRegistry.list_for_driver: dropping record outside "
                    "driver=%s",
                    driver_id,
                )
                continue
            records.append(dict(source))
        return records

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _require_es(self):
        """Return the Elasticsearch service, failing closed when absent."""
        if self._es_service is None:
            raise internal_error(
                message="Device registration is temporarily unavailable",
                details={"reason": "device_registry_not_configured"},
            )
        return self._es_service

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        """Return ``value`` stripped, rejecting a blank."""
        text = str(value or "").strip()
        if not text:
            raise invalid_request(
                message=f"{field} is required",
                details={"field": field},
            )
        return text

    @staticmethod
    def _validated_push_token(value: Any) -> str:
        """Return the token unchanged, rejecting only a blank or over-long one.

        The token's *format* is the provider's business, not the registry's
        (R9.18), so nothing here inspects its shape and the returned value is
        the caller's string, untrimmed.
        """
        if not isinstance(value, str) or not value.strip():
            raise invalid_request(
                message="push_token is required",
                details={"field": "push_token"},
            )
        if len(value) > MAX_PUSH_TOKEN_LENGTH:
            raise invalid_request(
                message="push_token is too long",
                details={
                    "field": "push_token",
                    "max_length": MAX_PUSH_TOKEN_LENGTH,
                },
            )
        return value

    @staticmethod
    def _validated_platform(value: Any) -> str:
        """Return the platform lowercased, rejecting anything unknown."""
        candidate = str(value or "").strip().lower()
        if candidate not in DEVICE_PLATFORMS:
            raise invalid_request(
                message="platform must be one of: "
                + ", ".join(DEVICE_PLATFORMS),
                details={
                    "field": "platform",
                    "allowed": list(DEVICE_PLATFORMS),
                },
            )
        return candidate


__all__ = [
    "DeviceRegistry",
    "DEVICE_PLATFORMS",
    "MAX_DEVICES_PER_DRIVER",
    "MAX_PUSH_TOKEN_LENGTH",
    "driver_device_doc_id",
]
