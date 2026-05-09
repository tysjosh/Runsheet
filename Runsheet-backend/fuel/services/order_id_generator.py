"""
Order and event ID generation for the fuel order intake pipeline.

Both functions are pure — no persistence, no clock reads — producing
deterministic-format IDs from uuid4 entropy only.
"""

from uuid import uuid4


def mint_order_id() -> str:
    """Generate a platform-assigned order ID.

    Returns a string matching ``^ord_[0-9a-f]{32}$``.
    """
    return f"ord_{uuid4().hex}"


def mint_event_id() -> str:
    """Generate a platform-assigned event ID.

    Returns a string matching ``^evt_[0-9a-f]{32}$``.
    """
    return f"evt_{uuid4().hex}"
