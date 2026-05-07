"""
Unit tests for FileStorageService — S3-backed, tenant-scoped object store.

Validates Req 4.1 (put/get/presign_upload/presign_get, tenant-prefixed key
layout, MIME + size validation, audit-log emission) and Req 10.1 (cross-
tenant access rejected with PermissionError). boto3 S3 is mocked so no AWS
calls are made.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from services.file_storage_service import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_PRESIGN_TTL_SECONDS,
    VALID_CATEGORIES,
    FileStorageAuditEvent,
    FileStorageService,
    FileStorageValidationError,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """In-memory stand-in for boto3 S3 client capturing all calls."""

    def __init__(self) -> None:
        self.put_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []
        self.presign_calls: List[Dict[str, Any]] = []
        self._objects: Dict[str, Dict[str, Any]] = {}

    def put_object(self, **kwargs: Any) -> Dict[str, Any]:
        self.put_calls.append(kwargs)
        self._objects[kwargs["Key"]] = {
            "Body": kwargs.get("Body", b""),
            "ContentType": kwargs.get("ContentType"),
        }
        return {"ETag": "fake-etag"}

    def get_object(self, **kwargs: Any) -> Dict[str, Any]:
        self.get_calls.append(kwargs)
        obj = self._objects.get(kwargs["Key"])
        if obj is None:
            raise KeyError(kwargs["Key"])
        return {"Body": _Body(obj["Body"]), "ContentType": obj["ContentType"]}

    def generate_presigned_url(self, method: str, *, Params: Dict[str, Any], ExpiresIn: int) -> str:
        self.presign_calls.append({"method": method, "Params": Params, "ExpiresIn": ExpiresIn})
        key = Params.get("Key", "unknown")
        return f"https://s3.example.com/{Params['Bucket']}/{key}?X-Amz-Expires={ExpiresIn}&op={method}"


class _Body:
    """Mimics the ``StreamingBody`` returned by boto3."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


class _RecordingAuditLogger:
    def __init__(self) -> None:
        self.events: List[FileStorageAuditEvent] = []

    def emit(self, event: FileStorageAuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def fake_s3() -> _FakeS3Client:
    return _FakeS3Client()


@pytest.fixture
def audit() -> _RecordingAuditLogger:
    return _RecordingAuditLogger()


@pytest.fixture
def service(fake_s3: _FakeS3Client, audit: _RecordingAuditLogger) -> FileStorageService:
    return FileStorageService(
        bucket="runsheet-pod-test",
        region="us-east-1",
        s3_client=fake_s3,
        audit_logger=audit,
    )


# Regex that the built key must satisfy for tenant prefix + layout.
_KEY_RE = re.compile(
    r"^tenants/(?P<tenant>[^/]+)/(?P<category>[^/]+)/"
    r"\d{4}/\d{2}/\d{2}/[0-9a-fA-F\-]{36}\.[a-z]+$"
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_empty_bucket(self, fake_s3: _FakeS3Client):
        with pytest.raises(ValueError):
            FileStorageService(bucket="", region="us-east-1", s3_client=fake_s3)

    def test_rejects_empty_region(self, fake_s3: _FakeS3Client):
        with pytest.raises(ValueError):
            FileStorageService(bucket="b", region="", s3_client=fake_s3)

    def test_rejects_non_positive_max_size(self, fake_s3: _FakeS3Client):
        with pytest.raises(ValueError):
            FileStorageService(bucket="b", region="r", s3_client=fake_s3, max_file_bytes=0)

    def test_does_not_call_boto3_at_construction(self):
        # Constructing without injecting a client and without AWS creds must
        # not raise — boto3 is only invoked on first real operation.
        FileStorageService(bucket="b", region="us-east-1")


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


class TestPut:
    def test_put_builds_tenant_prefixed_key(
        self, service: FileStorageService, fake_s3: _FakeS3Client
    ):
        file_ref = service.put(
            tenant_id="tenant-A",
            category="photo",
            content_bytes=b"\xff\xd8\xff",  # JPEG magic bytes
            content_type="image/jpeg",
        )
        match = _KEY_RE.match(file_ref)
        assert match, f"key did not match documented layout: {file_ref}"
        assert match.group("tenant") == "tenant-A"
        assert match.group("category") == "photo"
        assert file_ref.endswith(".jpg")
        # S3 was called with the expected params.
        assert len(fake_s3.put_calls) == 1
        call = fake_s3.put_calls[0]
        assert call["Bucket"] == "runsheet-pod-test"
        assert call["Key"] == file_ref
        assert call["ContentType"] == "image/jpeg"
        assert call["Body"] == b"\xff\xd8\xff"

    def test_put_keys_are_unique_across_calls(self, service: FileStorageService):
        a = service.put("tenant-A", "photo", b"a", "image/jpeg")
        b = service.put("tenant-A", "photo", b"b", "image/jpeg")
        assert a != b, "each put must mint a unique UUID-suffixed key"

    def test_put_rejects_invalid_category(self, service: FileStorageService):
        with pytest.raises(FileStorageValidationError):
            service.put("tenant-A", "not_a_category", b"x", "image/jpeg")

    def test_put_rejects_disallowed_mime_for_category(self, service: FileStorageService):
        # GIF is not in the allow-list.
        with pytest.raises(FileStorageValidationError):
            service.put("tenant-A", "photo", b"x", "image/gif")

    def test_put_rejects_text_csv_for_non_rack_category(self, service: FileStorageService):
        # text/csv is only permitted for rack_csv.
        with pytest.raises(FileStorageValidationError):
            service.put("tenant-A", "photo", b"a,b\n1,2", "text/csv")

    def test_put_accepts_text_csv_for_rack_csv_category(self, service: FileStorageService):
        ref = service.put("tenant-A", "rack_csv", b"a,b\n1,2", "text/csv")
        assert ref.endswith(".csv")
        assert "/rack_csv/" in ref

    def test_put_narrows_bol_category_to_pdf(self, service: FileStorageService):
        ref = service.put("tenant-A", "bol", b"%PDF-1.4", "application/pdf")
        assert ref.endswith(".pdf")
        # Even JPEG (default-allowed elsewhere) is rejected for BOL.
        with pytest.raises(FileStorageValidationError):
            service.put("tenant-A", "bol", b"\xff\xd8", "image/jpeg")

    def test_put_rejects_oversize_payload(self, fake_s3: _FakeS3Client):
        svc = FileStorageService(
            bucket="b", region="r", s3_client=fake_s3, max_file_bytes=1024
        )
        with pytest.raises(FileStorageValidationError):
            svc.put("tenant-A", "photo", b"x" * 1025, "image/jpeg")

    def test_put_accepts_default_10mib_boundary(self, service: FileStorageService):
        # Exactly the default max must succeed.
        payload = b"\x00" * DEFAULT_MAX_FILE_BYTES
        ref = service.put("tenant-A", "photo", payload, "image/jpeg")
        assert ref

    def test_put_rejects_empty_tenant_id(self, service: FileStorageService):
        with pytest.raises(ValueError):
            service.put("", "photo", b"x", "image/jpeg")

    def test_put_emits_audit_event(
        self, service: FileStorageService, audit: _RecordingAuditLogger
    ):
        service.put("tenant-A", "photo", b"x", "image/jpeg", actor="driver-9")
        assert len(audit.events) == 1
        e = audit.events[0]
        assert e.operation == "put"
        assert e.tenant_id == "tenant-A"
        assert e.category == "photo"
        assert e.actor == "driver-9"
        assert e.file_ref.startswith("tenants/tenant-A/photo/")
        assert e.extra["content_type"] == "image/jpeg"
        assert e.extra["size_bytes"] == 1
        # Timestamp parses as ISO-8601 UTC.
        assert e.timestamp.endswith("+00:00") or e.timestamp.endswith("Z")


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_round_trips_bytes(
        self, service: FileStorageService, fake_s3: _FakeS3Client
    ):
        ref = service.put("tenant-A", "meter_ticket", b"ticket-bytes", "image/png")
        got = service.get("tenant-A", ref)
        assert got == b"ticket-bytes"
        # S3 was called with the expected params.
        assert fake_s3.get_calls[-1]["Bucket"] == "runsheet-pod-test"
        assert fake_s3.get_calls[-1]["Key"] == ref

    def test_get_rejects_cross_tenant_ref(
        self, service: FileStorageService
    ):
        ref = service.put("tenant-A", "photo", b"x", "image/jpeg")
        with pytest.raises(PermissionError):
            service.get("tenant-B", ref)

    def test_get_rejects_path_traversal(self, service: FileStorageService):
        with pytest.raises(PermissionError):
            service.get("tenant-A", "tenants/tenant-A/../tenant-B/photo/2024/01/01/uuid.jpg")

    def test_get_rejects_empty_ref(self, service: FileStorageService):
        with pytest.raises(PermissionError):
            service.get("tenant-A", "")

    def test_get_emits_audit_event(
        self, service: FileStorageService, audit: _RecordingAuditLogger
    ):
        ref = service.put("tenant-A", "signature", b"sig", "image/png")
        audit.events.clear()
        service.get("tenant-A", ref, actor="dispatcher-1")
        assert len(audit.events) == 1
        e = audit.events[0]
        assert e.operation == "get"
        assert e.tenant_id == "tenant-A"
        assert e.category == "signature"
        assert e.actor == "dispatcher-1"
        assert e.file_ref == ref


# ---------------------------------------------------------------------------
# presign_upload
# ---------------------------------------------------------------------------


class TestPresignUpload:
    def test_returns_url_and_ref_with_layout(
        self, service: FileStorageService, fake_s3: _FakeS3Client
    ):
        out = service.presign_upload(
            tenant_id="tenant-A",
            category="photo",
            content_type="image/jpeg",
        )
        assert _KEY_RE.match(out["file_ref"]), out["file_ref"]
        assert out["file_ref"].startswith("tenants/tenant-A/photo/")
        assert "https://s3.example.com" in out["upload_url"]
        # Defaults: TTL 900s, default max size, content_type echoed back.
        assert out["content_type"] == "image/jpeg"
        assert out["max_file_bytes"] == DEFAULT_MAX_FILE_BYTES
        # The underlying presign call used the right method and expiry.
        call = fake_s3.presign_calls[-1]
        assert call["method"] == "put_object"
        assert call["ExpiresIn"] == DEFAULT_PRESIGN_TTL_SECONDS
        assert call["Params"]["Bucket"] == "runsheet-pod-test"
        assert call["Params"]["ContentType"] == "image/jpeg"
        # expires_at is a parseable ISO timestamp.
        parsed = datetime.fromisoformat(out["expires_at"])
        assert parsed.tzinfo is not None

    def test_presign_upload_default_ttl_is_900s(
        self, service: FileStorageService, fake_s3: _FakeS3Client
    ):
        service.presign_upload("tenant-A", "photo", "image/jpeg")
        assert fake_s3.presign_calls[-1]["ExpiresIn"] == 900

    def test_presign_upload_rejects_non_positive_ttl(self, service: FileStorageService):
        with pytest.raises(ValueError):
            service.presign_upload("tenant-A", "photo", "image/jpeg", ttl_seconds=0)

    def test_presign_upload_rejects_invalid_category(self, service: FileStorageService):
        with pytest.raises(FileStorageValidationError):
            service.presign_upload("tenant-A", "bogus", "image/jpeg")

    def test_presign_upload_rejects_disallowed_mime(self, service: FileStorageService):
        with pytest.raises(FileStorageValidationError):
            service.presign_upload("tenant-A", "photo", "application/zip")

    def test_presign_upload_emits_audit_event(
        self, service: FileStorageService, audit: _RecordingAuditLogger
    ):
        audit.events.clear()
        out = service.presign_upload(
            "tenant-A", "photo", "image/jpeg", actor="driver-9"
        )
        assert len(audit.events) == 1
        e = audit.events[0]
        assert e.operation == "presign_upload"
        assert e.tenant_id == "tenant-A"
        assert e.category == "photo"
        assert e.actor == "driver-9"
        assert e.file_ref == out["file_ref"]
        assert e.extra["ttl_seconds"] == DEFAULT_PRESIGN_TTL_SECONDS


# ---------------------------------------------------------------------------
# presign_get
# ---------------------------------------------------------------------------


class TestPresignGet:
    def test_returns_download_url_for_owned_ref(
        self, service: FileStorageService, fake_s3: _FakeS3Client
    ):
        ref = service.put("tenant-A", "bol", b"%PDF-", "application/pdf")
        out = service.presign_get("tenant-A", ref)
        assert out["file_ref"] == ref
        assert "https://s3.example.com" in out["download_url"]
        call = fake_s3.presign_calls[-1]
        assert call["method"] == "get_object"
        assert call["ExpiresIn"] == DEFAULT_PRESIGN_TTL_SECONDS

    def test_presign_get_rejects_cross_tenant_ref(self, service: FileStorageService):
        ref = service.put("tenant-A", "bol", b"%PDF-", "application/pdf")
        with pytest.raises(PermissionError):
            service.presign_get("tenant-B", ref)

    def test_presign_get_rejects_invalid_ttl(self, service: FileStorageService):
        ref = service.put("tenant-A", "photo", b"x", "image/jpeg")
        with pytest.raises(ValueError):
            service.presign_get("tenant-A", ref, ttl_seconds=-5)
        with pytest.raises(ValueError):
            service.presign_get("tenant-A", ref, ttl_seconds=8 * 24 * 3600)

    def test_presign_get_emits_audit_event(
        self, service: FileStorageService, audit: _RecordingAuditLogger
    ):
        ref = service.put("tenant-A", "photo", b"x", "image/jpeg")
        audit.events.clear()
        service.presign_get("tenant-A", ref, actor="dispatcher-1")
        assert len(audit.events) == 1
        e = audit.events[0]
        assert e.operation == "presign_get"
        assert e.tenant_id == "tenant-A"
        assert e.category == "photo"
        assert e.file_ref == ref


# ---------------------------------------------------------------------------
# validate_ref
# ---------------------------------------------------------------------------


class TestValidateRef:
    def test_validate_ref_accepts_owned_ref(
        self, service: FileStorageService, audit: _RecordingAuditLogger
    ):
        ref = service.put("tenant-A", "photo", b"x", "image/jpeg")
        audit.events.clear()
        assert service.validate_ref("tenant-A", ref) is True
        assert audit.events[0].operation == "validate_ref"

    def test_validate_ref_rejects_cross_tenant(self, service: FileStorageService):
        ref = service.put("tenant-A", "photo", b"x", "image/jpeg")
        with pytest.raises(PermissionError):
            service.validate_ref("tenant-B", ref)

    def test_validate_ref_rejects_malformed_key(self, service: FileStorageService):
        with pytest.raises(PermissionError):
            service.validate_ref("tenant-A", "tenants/tenant-A/photo/not-a-date")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategories:
    @pytest.mark.parametrize(
        "category, content_type, expected_ext",
        [
            ("signature", "image/png", "png"),
            ("photo", "image/jpeg", "jpg"),
            ("photo", "image/heic", "heic"),
            ("meter_ticket", "image/png", "png"),
            ("meter_ticket", "application/pdf", "pdf"),
            ("bol", "application/pdf", "pdf"),
            ("rack_csv", "text/csv", "csv"),
            ("attachment", "application/pdf", "pdf"),
        ],
    )
    def test_category_mime_combinations(
        self,
        service: FileStorageService,
        category: str,
        content_type: str,
        expected_ext: str,
    ):
        ref = service.put("tenant-A", category, b"x", content_type)
        assert f"/{category}/" in ref
        assert ref.endswith(f".{expected_ext}")

    def test_valid_categories_constant_matches_spec(self):
        assert VALID_CATEGORIES >= {
            "signature", "photo", "meter_ticket", "bol", "rack_csv", "attachment",
        }
