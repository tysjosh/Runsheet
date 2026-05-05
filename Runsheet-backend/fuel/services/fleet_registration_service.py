"""
Fleet Registration Service — ensures fuel tankers configured in truck_compartments
are registered in the trucks (fleet) index for Fleet UI visibility.
"""

import logging
from typing import Dict, List

from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class FleetRegistrationService:
    """Ensures fuel tankers in truck_compartments are registered in the trucks index."""

    FLEET_INDEX = "trucks"

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    async def ensure_fleet_registration(
        self,
        truck_id: str,
        tenant_id: str,
        compartments: List[Dict],
    ) -> None:
        """Create or update fleet document for a fuel tanker.

        - If truck_id doesn't exist in trucks index: create with asset_type=fuel_tanker
        - If truck_id exists: update only cargo.volume (preserve other fields)
        - On failure: log error, don't block compartment operation
        """
        try:
            total_capacity = self._compute_total_capacity(compartments)
            existing_doc = await self._get_fleet_document(truck_id)

            if existing_doc is None:
                await self._create_fleet_document(truck_id, tenant_id, total_capacity)
            else:
                await self._update_fleet_cargo_volume(truck_id, total_capacity)
        except Exception as e:
            logger.error(
                f"Fleet registration failed for truck_id={truck_id}: {e}"
            )

    def _compute_total_capacity(self, compartments: List[Dict]) -> float:
        """Sum capacity_liters across all compartments."""
        total = 0.0
        for compartment in compartments:
            capacity = compartment.get("capacity_liters", 0)
            try:
                total += float(capacity)
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid capacity_liters value: {capacity}, defaulting to 0"
                )
        return total

    async def _get_fleet_document(self, truck_id: str) -> Dict | None:
        """Retrieve existing fleet document, returning None if not found."""
        try:
            doc = await self._es.get_document(self.FLEET_INDEX, truck_id)
            return doc
        except Exception:
            return None

    async def _create_fleet_document(
        self, truck_id: str, tenant_id: str, total_capacity: float
    ) -> None:
        """Create a new fleet document for a fuel tanker."""
        document = {
            "truck_id": truck_id,
            "asset_type": "fuel_tanker",
            "tenant_id": tenant_id,
            "status": "available",
            "cargo": {
                "type": "fuel",
                "volume": total_capacity,
            },
        }
        await self._es.index_document(self.FLEET_INDEX, truck_id, document)
        logger.info(
            f"Created fleet document for fuel tanker {truck_id} "
            f"with cargo volume {total_capacity}L"
        )

    async def _update_fleet_cargo_volume(
        self, truck_id: str, total_capacity: float
    ) -> None:
        """Update only the cargo.volume field, preserving existing fields."""
        partial_doc = {
            "cargo": {
                "type": "fuel",
                "volume": total_capacity,
            },
        }
        await self._es.update_document(self.FLEET_INDEX, truck_id, partial_doc)
        logger.info(
            f"Updated fleet cargo volume for {truck_id} to {total_capacity}L"
        )
