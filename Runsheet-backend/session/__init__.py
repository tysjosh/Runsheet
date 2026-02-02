"""
Session management module for stateless backend support.

This module provides session store abstractions for storing AI agent
conversation memory in external stores (Redis or DynamoDB) rather than
in-process memory, enabling horizontal scaling.
"""

from session.store import SessionStore
from session.redis_store import RedisSessionStore, DEFAULT_SESSION_TTL

__all__ = ["SessionStore", "RedisSessionStore", "DEFAULT_SESSION_TTL"]
