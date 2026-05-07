"""
File Storage Service — S3-backed, tenant-scoped object store for POD artifacts.

Implements the File_Storage_Service abstraction required by Capability 4
(POD + Reconciliation) for signatures, photos, meter-ticket images, and
generated BOL PDFs, and by Capability 8 (Terminal / Rack Sourcing) for rack
CSV uploads. All object keys are laid out as::

    tenants/{tenant_id}/{category}/{yyyy}/{mm}/{dd}/{uuid}.{ext}

Every operation (put, get, presign_upload, presign_get, validate_ref) is
scoped to ``tenant_id`` and rejects cross-tenant file_refs with
``PermissionError`` so callers can translate that into HTTP 403. A
configurable per-tenant maximum file size (default 10 MB) and a fixed set of
permitted MIME types (image/jpeg, image/png, image/heic, application/pdf,
text/csv for the ``rack_csv`` category) are enforced on every upload path.

Presigned URL TTLs default to 900 seconds (Requirement 4.1).

Each operation emits an audit-log entry via the injected audit logger with
tenant_id, file_ref, category, operation, actor, timestamp so tenants retain
a durable record of artifact access (Requirement 4.1.7).

Validates: Requirement 4.1 (File Storage Service for POD artifacts),
Requirement 10.1 (multi-tenant isolation — tenant-prefixed keys, reject
cross-tenant access).
"""
from __future__ import annotations

import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Supported upload/read categories. ``bol`` and ``rack_csv`` are server-side
#: outputs; ``signature``, ``photo``, and ``meter_ticket`` come from drivers;
#: ``attachment`` is a catch-all for tenant-generated files.
VALID_CATEGORIES: frozenset[str] = frozenset(
    {"signature", "photo", "meter_ticket", "bol", "rack_csv", "attachment"}
)

#: MIME types accepted on upload for driver-facing categories + BOL PDFs.
_DEFAULT_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/heic", "application/pdf"}
)

#: Additional MIME types permitted per category.
_CATEGORY_EXTRA_MIME_TYPES: Dict[str, frozenset[str]] = {
    # Rack-price CSV uploads (Capability 8) flow through the same service.
    "rack_csv": frozenset({"text/csv"}),
    # BOL generation always produces PDFs — narrow the allow-list.
    "bol": frozenset({"application/pdf"}),
}

#: Default per-tenant max upload size (Req 4.1.5). 10 MiB.
DEFAULT_MAX_FILE_BYTES: int = 10 * 1024 * 1024

#: Default presigned URL TTL in seconds (Req 4.1 — presign_upload/get TTL 900).
DEFAULT_PRESIGN_TTL_SECONDS: int = 900

#: Stable extension mapping by MIME type — avoids relying on the ambient
#: ``mimetypes`` database which differs across hosts.
_MIME_TO_EXT: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "application/pdf": "pdf",
    "text/csv": "csv",
}

#: Matches valid tenant-scoped keys of the documented form.
_KEY_PATTERN = re.compile(
    r"^tenants/(?P<tenant>[^/]+)/(?P<category>[^/]+)/\d{4}/\d{2}/\d{2}/"
    r"[0-9a-fA-F\-]{36}(?:\.[A-Za-z0-9]+)?$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _ext_for_mime(content_type: str) -> str:
    """Return the file extension we persist for a given MIME type.

    Falls back to ``mimetypes.guess_extension`` (stripping the leading dot)
    for anything not in our curated table, and ultimately to ``bin`` when no
    mapping is available. The extension is only metadata — tenant isolation
    and validation are enforced elsewhere.
    """
    if content_type in _MIME_TO_EXT:
        return _MIME_TO_EXT[content_type]
    guessed = mimetypes.guess_extension(content_type) or ""
    return guessed.lstrip(".") or "bin"


def _allowed_mime_types(category: str) -> frozenset[str]:
    """Compute the union of default and category-specific permitted MIME types."""
    extras = _CATEGORY_EXTRA_MIME_TYPES.get(category, frozenset())
    if category == "bol":
        # BOL intentionally narrows rather than extends.
        return extras
    return _DEFAULT_ALLOWED_MIME_TYPES | extras


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------


#: Shape of each audit record. Kept deliberately small so tenants and
#: downstream log-sinks can aggregate without a schema migration.
@dataclass(frozen=True)
class FileStorageAuditEvent:
    operation: str
    tenant_id: str
    category: Optional[str]
    file_ref: Optional[str]
    actor: Optional[str]
    timestamp: str
    extra: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "operation": self.operation,
            "tenant_id": self.tenant_id,
            "category": self.category,
            "file_ref": self.file_ref,
            "actor": self.actor,
            "timestamp": self.timestamp,
        }
        if self.extra:
            record.update(self.extra)
        return record


class FileStorageAuditLogger:
    """Default audit logger — writes structured entries to ``logger`` at INFO.

    Callers that need to persist audit events elsewhere (ES, a queue, an
    external SIEM) can implement any callable compatible with
    ``emit(event: FileStorageAuditEvent) -> None``. The service accepts
    either this class or a raw callable via dependency injection.
    """

    def __init__(self, sink: Optional[Callable[[FileStorageAuditEvent], None]] = None) -> None:
        self._sink = sink

    def emit(self, event: FileStorageAuditEvent) -> None:
        if self._sink is not None:
            self._sink(event)
            return
        logger.info("file_storage_audit %s", event.to_dict())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FileStorageValidationError(ValueError):
    """Raised when a caller violates a MIME-type / size / category rule."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FileStorageService:
    """S3-backed object store with tenant-prefixed keys and audit logging.

    The service deliberately keeps the S3 client *lazy*: it is constructed on
    first use via ``boto3.client("s3", ...)`` so modules can import this
    service in environments without AWS credentials (unit tests, CI). Callers
    can also inject a pre-built client via the ``s3_client`` kwarg, which is
    the pattern every test in this repo uses.

    Args:
        bucket: S3 bucket name that holds all tenant artifacts.
        region: AWS region of ``bucket``. Required even when a client is
            injected so presigned URLs include the right host.
        s3_client: Optional pre-built boto3 S3 client. Injected by tests to
            avoid real AWS calls.
        audit_logger: Optional audit sink. Defaults to
            :class:`FileStorageAuditLogger` which logs to ``logger``.
        max_file_bytes: Per-tenant maximum upload size (default 10 MiB).
        allowed_mime_types: Override of the default-category allow-list.
            Category-specific extras are still applied on top.
    """

    def __init__(
        self,
        bucket: str,
        region: str,
        s3_client: Any = None,
        audit_logger: Any = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        allowed_mime_types: Optional[Iterable[str]] = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket must be non-empty")
        if not region:
            raise ValueError("region must be non-empty")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")

        self._bucket = bucket
        self._region = region
        self._s3_client = s3_client
        self._audit = audit_logger or FileStorageAuditLogger()
        self._max_file_bytes = int(max_file_bytes)
        self._allowed_mime_types_override = (
            frozenset(allowed_mime_types) if allowed_mime_types is not None else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        tenant_id: str,
        category: str,
        content_bytes: bytes,
        content_type: str,
        actor: Optional[str] = None,
    ) -> str:
        """Upload ``content_bytes`` and return the resulting file_ref.

        Enforces category, MIME, and size validation; emits a ``put`` audit
        event on success.
        """
        self._require_tenant(tenant_id)
        self._validate_category(category)
        self._validate_content_type(category, content_type)
        self._validate_size(len(content_bytes))

        key = self._build_key(tenant_id, category, content_type)
        self._s3().put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content_bytes,
            ContentType=content_type,
        )
        self._audit_event(
            "put",
            tenant_id=tenant_id,
            category=category,
            file_ref=key,
            actor=actor,
            extra={"content_type": content_type, "size_bytes": len(content_bytes)},
        )
        return key

    def get(
        self,
        tenant_id: str,
        file_ref: str,
        actor: Optional[str] = None,
    ) -> bytes:
        """Download the object referenced by ``file_ref`` and return bytes.

        Rejects cross-tenant access with ``PermissionError`` before the S3
        call is made. Emits a ``get`` audit event on success.
        """
        self._require_tenant(tenant_id)
        self._assert_tenant_prefix(tenant_id, file_ref)

        response = self._s3().get_object(Bucket=self._bucket, Key=file_ref)
        body = response["Body"]
        try:
            data = body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass

        self._audit_event(
            "get",
            tenant_id=tenant_id,
            category=self._category_from_key(file_ref),
            file_ref=file_ref,
            actor=actor,
            extra={"size_bytes": len(data)},
        )
        return data

    def presign_upload(
        self,
        tenant_id: str,
        category: str,
        content_type: str,
        ttl_seconds: int = DEFAULT_PRESIGN_TTL_SECONDS,
        actor: Optional[str] = None,
        max_file_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generate a short-lived presigned PUT URL for a new object.

        Returns a dict with ``file_ref``, ``upload_url``, ``expires_at``,
        ``content_type``, ``max_file_bytes``. The caller stores ``file_ref``
        and passes it back on POD submission. The presigned URL is bound to
        the exact ``Content-Type`` header so clients cannot silently upload a
        disallowed MIME type.
        """
        self._require_tenant(tenant_id)
        self._validate_category(category)
        self._validate_content_type(category, content_type)
        self._validate_ttl(ttl_seconds)
        effective_max = int(max_file_bytes) if max_file_bytes is not None else self._max_file_bytes
        if effective_max <= 0:
            raise ValueError("max_file_bytes must be positive")

        key = self._build_key(tenant_id, category, content_type)
        params: Dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "ContentType": content_type,
        }
        url = self._s3().generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=int(ttl_seconds),
        )

        expires_at = _utcnow().timestamp() + int(ttl_seconds)
        self._audit_event(
            "presign_upload",
            tenant_id=tenant_id,
            category=category,
            file_ref=key,
            actor=actor,
            extra={
                "content_type": content_type,
                "ttl_seconds": int(ttl_seconds),
                "max_file_bytes": effective_max,
            },
        )
        return {
            "file_ref": key,
            "upload_url": url,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "content_type": content_type,
            "max_file_bytes": effective_max,
        }

    def presign_get(
        self,
        tenant_id: str,
        file_ref: str,
        ttl_seconds: int = DEFAULT_PRESIGN_TTL_SECONDS,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a short-lived presigned GET URL for an existing object.

        Rejects cross-tenant refs with ``PermissionError``. Emits a
        ``presign_get`` audit event on success.
        """
        self._require_tenant(tenant_id)
        self._assert_tenant_prefix(tenant_id, file_ref)
        self._validate_ttl(ttl_seconds)

        url = self._s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": file_ref},
            ExpiresIn=int(ttl_seconds),
        )
        expires_at = _utcnow().timestamp() + int(ttl_seconds)
        self._audit_event(
            "presign_get",
            tenant_id=tenant_id,
            category=self._category_from_key(file_ref),
            file_ref=file_ref,
            actor=actor,
            extra={"ttl_seconds": int(ttl_seconds)},
        )
        return {
            "file_ref": file_ref,
            "download_url": url,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        }

    def validate_ref(
        self,
        tenant_id: str,
        file_ref: str,
        actor: Optional[str] = None,
    ) -> bool:
        """Assert ``file_ref`` is well-formed and belongs to ``tenant_id``.

        Used by the driver POD endpoint to verify signature/photo/meter
        ticket refs before persisting a POD record (Req 4.1.4/4.1.6).
        Returns ``True`` on success and raises ``PermissionError`` on a
        tenant-prefix mismatch. Also emits a ``validate_ref`` audit event.
        """
        self._require_tenant(tenant_id)
        self._assert_tenant_prefix(tenant_id, file_ref)
        self._audit_event(
            "validate_ref",
            tenant_id=tenant_id,
            category=self._category_from_key(file_ref),
            file_ref=file_ref,
            actor=actor,
            extra={},
        )
        return True

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")

    @staticmethod
    def _validate_category(category: str) -> None:
        if category not in VALID_CATEGORIES:
            raise FileStorageValidationError(
                f"invalid category '{category}'; expected one of {sorted(VALID_CATEGORIES)}"
            )

    def _validate_content_type(self, category: str, content_type: str) -> None:
        if not content_type:
            raise FileStorageValidationError("content_type must be non-empty")
        allowed = (
            self._allowed_mime_types_override | _CATEGORY_EXTRA_MIME_TYPES.get(category, frozenset())
            if self._allowed_mime_types_override is not None
            else _allowed_mime_types(category)
        )
        if content_type not in allowed:
            raise FileStorageValidationError(
                f"content_type '{content_type}' not permitted for category '{category}'; "
                f"allowed: {sorted(allowed)}"
            )

    def _validate_size(self, size_bytes: int) -> None:
        if size_bytes < 0:
            raise FileStorageValidationError("size_bytes must be non-negative")
        if size_bytes > self._max_file_bytes:
            raise FileStorageValidationError(
                f"payload size {size_bytes} exceeds max {self._max_file_bytes} bytes"
            )

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise ValueError("ttl_seconds must be an int")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        # Match S3 SigV4 hard cap (7 days) so we never mint URLs that would
        # be rejected at sign time.
        if ttl_seconds > 7 * 24 * 3600:
            raise ValueError("ttl_seconds exceeds SigV4 7-day maximum")

    # ------------------------------------------------------------------
    # Key layout
    # ------------------------------------------------------------------

    @staticmethod
    def _build_key(tenant_id: str, category: str, content_type: str) -> str:
        """Return a tenant-scoped S3 key of the documented form.

        Format: ``tenants/{tenant_id}/{category}/{yyyy}/{mm}/{dd}/{uuid}.{ext}``.
        """
        now = _utcnow()
        ext = _ext_for_mime(content_type)
        return (
            f"tenants/{tenant_id}/{category}/"
            f"{now.year:04d}/{now.month:02d}/{now.day:02d}/"
            f"{uuid.uuid4()}.{ext}"
        )

    @staticmethod
    def _assert_tenant_prefix(tenant_id: str, file_ref: str) -> None:
        """Reject refs whose tenant prefix does not match the caller.

        Validates the full documented key shape so callers can't sneak in
        stray paths like ``tenants/other/../{tenant_id}/...``.
        """
        if not file_ref:
            raise PermissionError("cross_tenant_file_ref")
        expected_prefix = f"tenants/{tenant_id}/"
        if not file_ref.startswith(expected_prefix) or ".." in file_ref:
            logger.warning(
                "Cross-tenant file_ref access denied: requester=%s ref=%s",
                tenant_id,
                file_ref,
            )
            raise PermissionError("cross_tenant_file_ref")
        match = _KEY_PATTERN.match(file_ref)
        if not match or match.group("tenant") != tenant_id:
            logger.warning(
                "Malformed or cross-tenant file_ref denied: requester=%s ref=%s",
                tenant_id,
                file_ref,
            )
            raise PermissionError("cross_tenant_file_ref")

    @staticmethod
    def _category_from_key(file_ref: str) -> Optional[str]:
        match = _KEY_PATTERN.match(file_ref)
        if not match:
            return None
        category = match.group("category")
        return category if category in VALID_CATEGORIES else None

    # ------------------------------------------------------------------
    # S3 client construction + audit
    # ------------------------------------------------------------------

    def _s3(self):
        """Lazily construct a boto3 S3 client on first use."""
        if self._s3_client is not None:
            return self._s3_client
        import boto3  # imported lazily so modules can import without AWS creds
        self._s3_client = boto3.client("s3", region_name=self._region)
        return self._s3_client

    def _audit_event(
        self,
        operation: str,
        *,
        tenant_id: str,
        category: Optional[str],
        file_ref: Optional[str],
        actor: Optional[str],
        extra: Dict[str, Any],
    ) -> None:
        event = FileStorageAuditEvent(
            operation=operation,
            tenant_id=tenant_id,
            category=category,
            file_ref=file_ref,
            actor=actor,
            timestamp=_utcnow_iso(),
            extra=dict(extra) if extra else {},
        )
        try:
            if hasattr(self._audit, "emit"):
                self._audit.emit(event)
            else:
                self._audit(event)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover - audit must never break ops
            logger.error("file_storage audit emit failed: %s", exc)
