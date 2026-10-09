"""Load stored documents into a model without letting one bad row break a list.

A list endpoint that builds ``Model(**hit["_source"])`` for every hit turns a
single malformed document (a legacy row, or one written by an older import
path) into a 500 for the whole tenant. ``load_valid_documents`` skips such
hits, logs them, and returns how many were skipped.
"""

import logging
from typing import Any, Iterable, Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


def load_valid_documents(
    hits: Iterable[dict[str, Any]],
    model: Type[M],
    *,
    index: str,
    tenant_id: str,
) -> tuple[list[M], int]:
    """Validate each hit's ``_source`` as ``model``; skip the ones that fail.

    A skipped hit is logged at WARNING with the index, doc id, tenant, error
    count and the failing field locations. Field values are never logged
    because they may hold PII.

    Returns:
        ``(loaded, skipped)``.
    """
    loaded: list[M] = []
    skipped = 0
    for hit in hits:
        try:
            loaded.append(model.model_validate(hit.get("_source") or {}))
        except ValidationError as exc:
            skipped += 1
            errors = exc.errors(include_url=False)
            fields = sorted(
                {".".join(map(str, err.get("loc") or ())) or "record" for err in errors}
            )
            logger.warning(
                "Skipping malformed %s document id=%s tenant=%s: "
                "%d validation error(s) on fields %s",
                index,
                hit.get("_id"),
                tenant_id,
                len(errors),
                ", ".join(fields),
            )
    if skipped:
        logger.warning(
            "Skipped %d malformed %s document(s) for tenant %s",
            skipped,
            index,
            tenant_id,
        )
    return loaded, skipped
