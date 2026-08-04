"""Canonical customer-tank and tank-reading import operations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fuel.customer_tank_models import CustomerTank, CustomerTankRepository
from fuel.services.fuel_ops_es_mappings import ATG_READINGS_INDEX
from services.time_utils import utcnow


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TankImportOutcome:
    status: str
    customer_tank_id: str


@dataclass(frozen=True)
class TankReadingImportOutcome:
    status: str
    reading_id: str
    customer_tank_id: str
    current_level_updated: bool


class TankImportService:
    """Upsert tank master data and append telemetry with stale-read protection."""

    def __init__(
        self,
        *,
        es_service: Any,
        customer_tank_repository: CustomerTankRepository,
        readings_index: str = ATG_READINGS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if customer_tank_repository is None:
            raise ValueError("customer_tank_repository must not be None")
        self._es = es_service
        self._tanks = customer_tank_repository
        self._readings_index = readings_index

    async def import_tank(
        self,
        tenant_id: str,
        payload: Dict[str, Any],
    ) -> TankImportOutcome:
        source_system = str(payload.get("source_system") or "").strip()
        external_tank_id = str(payload.get("external_tank_id") or "").strip()
        if not source_system:
            raise ValueError("source_system is required")
        if not external_tank_id:
            raise ValueError("external_tank_id is required")

        existing = await self._tanks.get_by_external_id(
            tenant_id, source_system, external_tank_id
        )
        clean = {key: value for key, value in payload.items() if value is not None}
        clean["source_system"] = source_system
        clean["external_tank_id"] = external_tank_id

        if existing is None:
            created = await self._tanks.create(tenant_id, clean)
            return TankImportOutcome("created", created.customer_tank_id)

        supplied_id = clean.get("customer_tank_id")
        if supplied_id and supplied_id != existing.customer_tank_id:
            raise ValueError(
                "external tank identity is already linked to "
                f"{existing.customer_tank_id}"
            )

        clean.pop("customer_tank_id", None)
        incoming_reading = clean.get("last_reading_at")
        if incoming_reading and existing.last_reading_at:
            parsed_incoming = _parse_datetime(
                incoming_reading, field_name="last_reading_at"
            )
            current = existing.last_reading_at
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if parsed_incoming <= current.astimezone(timezone.utc):
                clean.pop("last_reading_at", None)
                clean.pop("current_level_gallons", None)

        updated = await self._tanks.update(
            tenant_id, existing.customer_tank_id, clean
        )
        if updated is None:
            raise ValueError("customer tank disappeared during import")
        return TankImportOutcome("updated", updated.customer_tank_id)

    async def import_reading(
        self,
        tenant_id: str,
        payload: Dict[str, Any],
    ) -> TankReadingImportOutcome:
        (
            tank,
            source_system,
            external_tank_id,
            volume_gallons,
            reading_at,
        ) = await self._validate_reading_payload(tenant_id, payload)

        source_reading_id = str(payload.get("source_reading_id") or "").strip()
        identity = source_reading_id or _iso(reading_at)
        digest = hashlib.sha256(
            f"{tenant_id}|{source_system}|{external_tank_id}|{identity}".encode()
        ).hexdigest()[:32]
        reading_id = f"atg_import_{digest}"

        existing_reading = await self._es.get_document(
            self._readings_index, reading_id
        )
        if existing_reading is not None:
            return TankReadingImportOutcome(
                "duplicate", reading_id, tank.customer_tank_id, False
            )

        now = utcnow()
        document = {
            "reading_id": reading_id,
            "tenant_id": tenant_id,
            "instance_id": f"import:{source_system}",
            "source_system": source_system,
            "external_tank_id": external_tank_id,
            "source_reading_id": source_reading_id or None,
            "tank_ref": f"external:{source_system}:{external_tank_id}",
            "customer_tank_id": tank.customer_tank_id,
            "station_id": None,
            "volume_gallons": volume_gallons,
            "water_level_in": _optional_float(payload.get("water_level_in")),
            "temperature_f": _optional_float(payload.get("temperature_f")),
            "product_code": tank.fuel_product_code,
            "reading_at": _iso(reading_at),
            "retrieved_at": _iso(now),
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        await self._es.index_document(self._readings_index, reading_id, document)

        latest = tank.last_reading_at
        if latest is not None and latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        should_update = latest is None or reading_at > latest.astimezone(timezone.utc)
        if should_update:
            updated = await self._tanks.update(
                tenant_id,
                tank.customer_tank_id,
                {
                    "current_level_gallons": volume_gallons,
                    "last_reading_at": reading_at,
                },
            )
            should_update = updated is not None

        return TankReadingImportOutcome(
            "created" if should_update else "stored_stale",
            reading_id,
            tank.customer_tank_id,
            should_update,
        )

    async def validate_reading(
        self,
        tenant_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Validate a reading against its mapped tank without writing it."""

        await self._validate_reading_payload(tenant_id, payload)
        _optional_float(payload.get("water_level_in"))
        _optional_float(payload.get("temperature_f"))

    async def _validate_reading_payload(
        self,
        tenant_id: str,
        payload: Dict[str, Any],
    ) -> tuple[CustomerTank, str, str, float, datetime]:
        source_system = str(payload.get("source_system") or "").strip()
        external_tank_id = str(payload.get("external_tank_id") or "").strip()
        if not source_system:
            raise ValueError("source_system is required")
        if not external_tank_id:
            raise ValueError("external_tank_id is required")

        customer_tank_id = str(payload.get("customer_tank_id") or "").strip()
        tank: Optional[CustomerTank]
        if customer_tank_id:
            tank = await self._tanks.get(tenant_id, customer_tank_id)
        else:
            tank = await self._tanks.get_by_external_id(
                tenant_id, source_system, external_tank_id
            )
        if tank is None:
            raise ValueError(
                "no customer tank mapping for "
                f"{source_system}:{external_tank_id}"
            )
        if (
            tank.source_system
            and tank.external_tank_id
            and tank.source_system == source_system
            and tank.external_tank_id != external_tank_id
        ):
            raise ValueError("customer_tank_id does not match the external tank identity")

        try:
            volume_gallons = float(payload.get("volume_gallons"))
        except (TypeError, ValueError) as exc:
            raise ValueError("volume_gallons must be a number") from exc
        if volume_gallons < 0:
            raise ValueError("volume_gallons cannot be negative")
        if volume_gallons > tank.capacity_gallons:
            raise ValueError(
                f"volume_gallons cannot exceed tank capacity {tank.capacity_gallons}"
            )

        reading_at = _parse_datetime(payload.get("reading_at"), field_name="reading_at")
        return (
            tank,
            source_system,
            external_tank_id,
            volume_gallons,
            reading_at,
        )


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a number, got {value!r}") from exc


__all__ = [
    "TankImportOutcome",
    "TankImportService",
    "TankReadingImportOutcome",
]
