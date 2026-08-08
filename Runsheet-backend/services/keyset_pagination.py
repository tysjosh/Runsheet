"""``search_after`` pagination for the nine list endpoints that got it wrong.

Nine list endpoints across commerce and compliance paginate with ``search_after``,
and every one built the boundary the same wrong way::

    base_query["search_after"] = [cursor, cursor]     # cursor is an id

The sort is ``[{created_at: desc}, {id: asc}]``, so an id was passed where a date
boundary belongs. Against the live cluster::

    page 1: HTTP 200
        _id=ACC-004  sort=[1767978188875, 'ACC-004']
        _id=ACC-008  sort=[1762448588875, 'ACC-008']

    page 2 with [cursor, cursor]: HTTP 400
        failed to parse date field [ACC-008] with format
        [strict_date_optional_time||epoch_millis]

    page 2 with the real sort values [1762448588875, 'ACC-008']: HTTP 200
        _id=ACC-010
        _id=ACC-007

So page 2 has never worked on any of the nine. That is a live defect independent of
the Elasticsearch → Postgres migration; the migration only made it visible, because
the Postgres store would have dropped the key and returned page 1 instead.

The cursor stays an id
----------------------

``search_after`` needs the trailing row's *sort values*, not its id — so the
tempting fix is to hand the sort values to the client as an opaque cursor. That
would be wrong here, because the same endpoints have a second implementation:
when ``COMMERCE_READ_FROM_POSTGRES`` is on, ``persistence.read_repositories``
serves these lists from the relational tables, and it already does this correctly
with an **id** cursor — it looks the cursor row up and derives the boundary from
it (``_keyset_page``).

Two implementations of one endpoint must not disagree about the cursor format. A
client would get an opaque cursor from one and an id from the other, and flipping
either flag mid-flight would invalidate every cursor in the wild. So this module
reproduces the working approach: the cursor remains the trailing row's id, and the
boundary is resolved server-side.

That also means there is no API change. The documented contract — "cursor is the
id of the last item on the previous page" — was always the intent; it just was not
implemented.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from errors.codes import ErrorCode
from errors.exceptions import AppException

__all__ = [
    "InvalidCursorError",
    "next_cursor_from_hits",
    "search_after_for_cursor",
    "sort_fields_of",
]


class InvalidCursorError(AppException, ValueError):
    """The cursor does not name a row we can page from.

    An :class:`AppException` so the registered handler renders a 400. The only
    handler below ``AppException`` is the catch-all that returns 500, so a plain
    ``ValueError`` would turn a stale query parameter into an apparent server
    fault.

    Raised rather than ignored, which is a deliberate difference from the
    relational path: that one drops an unresolvable cursor and returns the first
    page again. For the common ``while next_cursor:`` loop that does not terminate
    — page 1 comes back with the same cursor attached, forever. A 400 tells the
    client to restart.
    """

    def __init__(self, reason: str) -> None:
        AppException.__init__(
            self,
            error_code=ErrorCode.VALIDATION_ERROR,
            message=f"invalid pagination cursor: {reason}",
            details={"parameter": "cursor", "reason": reason},
        )
        self.reason = reason


def sort_fields_of(sort: Any) -> List[str]:
    """The field names a ``sort`` clause orders by, in order.

    Accepts the shapes these call sites use — ``[{"field": {"order": "desc"}}]``,
    ``[{"field": "desc"}]``, ``["field"]`` — and skips ``_score`` / ``_doc``, which
    name no document field.

    Deliberately re-implemented here rather than imported from
    ``persistence.document_query``: this module is used on the Elasticsearch path
    too, and importing the translator would pull SQLAlchemy and the persistence
    package into a deployment that has no database.
    """
    entries = sort if isinstance(sort, (list, tuple)) else [sort] if sort else []
    fields: List[str] = []
    for item in entries:
        if isinstance(item, str):
            fields.append(item)
        elif isinstance(item, dict):
            fields.extend(item.keys())
        else:
            raise TypeError(f"unsupported sort entry: {type(item).__name__}")
    return [field for field in fields if field not in ("_score", "_doc")]


async def search_after_for_cursor(
    es_service: Any, index: str, cursor: str, sort: Any
) -> List[Any]:
    """Resolve an id cursor into the ``search_after`` values for the next page.

    Reads the cursor row and returns its values for each sort field, in sort
    order — the same derivation ``persistence.read_repositories._keyset_page``
    performs in SQL.

    The final sort key is the id itself at every one of these call sites, so the
    boundary is fully determined and pages cannot repeat or skip a row when many
    rows share a ``created_at``.

    Raises:
        InvalidCursorError: the cursor names no document, or names one missing a
            sort field. Both mean the boundary cannot be built; guessing one would
            silently return the wrong page.
    """
    fields = sort_fields_of(sort)
    if not fields:
        raise InvalidCursorError("the query has no sort to page along")

    document = await es_service.get_document(index, cursor)
    if not document:
        raise InvalidCursorError(f"no {index} record with id {cursor!r}")

    values: List[Any] = []
    for field in fields:
        if field not in document:
            raise InvalidCursorError(
                f"the {index} record {cursor!r} has no {field!r} to page from"
            )
        values.append(document[field])
    return values


def next_cursor_from_hits(
    hits: Sequence[Dict[str, Any]], limit: int, *, id_field: str
) -> Optional[str]:
    """The cursor for the page after ``hits``, or ``None`` when this is the last.

    ``None`` on a short page: fewer hits than requested is the end of the result
    set, and emitting a cursor there promises a page that comes back empty.

    Identical in shape to ``persistence.read_repositories._page_result``, so the
    two implementations of these endpoints hand out interchangeable cursors.
    """
    if not hits or len(hits) < limit:
        return None
    source = hits[-1].get("_source") or {}
    value = source.get(id_field)
    return str(value) if value is not None else None
