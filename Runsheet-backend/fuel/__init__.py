"""
Fuel Monitoring module for the Runsheet backend.

This module provides fuel inventory tracking, consumption analytics,
and alert management across fuel stations and depots. It tracks stock
levels, records consumption and refill events, generates low-stock
alerts, and provides trend analytics for fuel usage.
"""

from fuel.services.order_id_generator import mint_event_id, mint_order_id

__all__ = [
    "mint_order_id",
    "mint_event_id",
]
