"""
Unit tests for the POST /api/driver/pod/uploads/presign endpoint.

Covers:
* Happy-path presigned URL issuance via the wired FileStorageService.
* Category allow-list (signature, photo, meter_ticket, bol) rejection.
* MIME allow-list (image/jpeg, image/png, image/heic, application/pdf) rejection.
* Per-tenant max-file-size resolution (Redis override and default fallback).
* 403 translation when FileStorageService raises PermissionError.

Validates: Requirements 4.1.3, 4.1.5
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt


# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.pod_endpoints import (
    DEFAULT_POD_MAX_FILE_BYTES,
    configure_pod_endpoints,
    router as pod_router,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"
TENANT_ID = "t1"
USER_ID = "driver-1"

_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(tenant_id: str = TENANT_ID, sub: str = USER_ID) -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )


def _auth_headers(tenant_id: str = TENANT_ID) -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


def _make_es_service() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


class _StubFileStorage:
    """Minimal FileStorageService stub capturing presign_upload calls."""

    def __init__(self, *, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self._raise = raise_exc

    def presign_upload(
        self,
        *,
        tenant_id: str,
        category: str,
        content_type: str,
        actor: str | None = None,
        max_file_bytes: int | None = None,
        ttl_seconds: int = 900,
    ) -> dict:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "category": category,
                "content_type": content_type,
                "actor": actor,
                "max_file_bytes": max_file_bytes,
                "ttl_seconds": ttl_seconds,
            }
        )
        if self._raise is not None:
            raise self._raise
        return {
            "file_ref": f"tenants/{tenant_id}/{category}/2025/01/02/abc.ext",
            "upload_url": "https://s3.amazonaws.com/bucket/key?X-Amz-Signature=...",
            "expires_at": "2025-01-02T00:15:00+00:00",
            "content_type": content_type,
            "max_file_bytes": max_file_bytes,
        }


class _StubRedis:
    """Async Redis stub supporting ``get`` only."""

    def __init__(self, values: dict[str, str | bytes | None] | None = None):
        self._values = values or {}
        self.get_calls: list[str] = []

    async def get(self, key: str):
        self.get_calls.append(key)
        return self._values.get(key)


def _make_app(
    *,
    file_storage=None,
    redis_client=None,
) -> FastAPI:
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pod_router)
    configure_pod_endpoints(
        es_service=_make_es_service(),
        file_storage_service=file_storage,
        redis_client=redis_client,
    )
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPresignPodUpload:
    """Tests for POST /api/driver/pod/uploads/presign.

    Validates: Requirements 4.1.3, 4.1.5
    """

    def test_returns_presigned_url_for_signature_jpeg(self):
        """Happy path: permitted category + MIME returns file_ref + upload_url."""
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "signature", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        assert data["file_ref"].startswith(f"tenants/{TENANT_ID}/signature/")
        assert data["upload_url"].startswith("https://")
        assert data["content_type"] == "image/jpeg"
        assert data["max_file_bytes"] == DEFAULT_POD_MAX_FILE_BYTES

        # The service was called with the tenant from the JWT and the
        # default max-file-size (Req 4.1.5).
        assert len(fs.calls) == 1
        call = fs.calls[0]
        assert call["tenant_id"] == TENANT_ID
        assert call["category"] == "signature"
        assert call["content_type"] == "image/jpeg"
        assert call["actor"] == USER_ID
        assert call["max_file_bytes"] == DEFAULT_POD_MAX_FILE_BYTES

    @pytest.mark.parametrize(
        "category,content_type",
        [
            ("photo", "image/png"),
            ("photo", "image/heic"),
            ("meter_ticket", "image/jpeg"),
            ("bol", "application/pdf"),
        ],
    )
    def test_accepts_all_documented_category_mime_pairs(
        self, category: str, content_type: str
    ):
        """All documented category/MIME pairs succeed. Validates: Req 4.1.3, 4.1.5."""
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": category, "content_type": content_type},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, resp.text
        assert fs.calls[-1]["category"] == category
        assert fs.calls[-1]["content_type"] == content_type

    def test_rejects_unknown_category_with_400(self):
        """Unknown categories are rejected before hitting the service."""
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "receipt", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400, resp.text
        # The FileStorageService must not be invoked when pre-validation fails.
        assert fs.calls == []

    def test_rejects_disallowed_mime_with_400(self):
        """Disallowed MIME types are rejected before hitting the service."""
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "application/zip"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400, resp.text
        assert fs.calls == []

    def test_uses_tenant_override_from_redis(self):
        """Per-tenant Redis override trumps the default max-file-size."""
        override_bytes = 5 * 1024 * 1024  # 5 MiB
        redis = _StubRedis(
            values={f"tenant:{TENANT_ID}:pod_max_file_bytes": str(override_bytes)}
        )
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs, redis_client=redis)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, resp.text
        assert fs.calls[-1]["max_file_bytes"] == override_bytes
        assert resp.json()["data"]["max_file_bytes"] == override_bytes
        assert redis.get_calls == [f"tenant:{TENANT_ID}:pod_max_file_bytes"]

    def test_falls_back_to_default_when_override_malformed(self):
        """Malformed Redis overrides fall back to the 10 MiB default."""
        redis = _StubRedis(
            values={f"tenant:{TENANT_ID}:pod_max_file_bytes": b"not-a-number"}
        )
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs, redis_client=redis)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, resp.text
        assert fs.calls[-1]["max_file_bytes"] == DEFAULT_POD_MAX_FILE_BYTES

    def test_translates_permission_error_to_403(self):
        """FileStorageService PermissionError surfaces as HTTP 403."""
        fs = _StubFileStorage(raise_exc=PermissionError("cross_tenant_file_ref"))
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 403, resp.text

    def test_translates_value_error_to_400(self):
        """FileStorageValidationError from the service translates to HTTP 400."""
        fs = _StubFileStorage(raise_exc=ValueError("content_type must be non-empty"))
        app = _make_app(file_storage=fs)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "image/jpeg"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400, resp.text

    def test_requires_auth_header(self):
        """Unauthenticated requests are rejected by the tenant guard."""
        fs = _StubFileStorage()
        app = _make_app(file_storage=fs)

        # Patch the environment to non-development so the tenant guard enforces
        # the Bearer token requirement.
        with patch(
            "ops.middleware.tenant_guard.get_settings",
            return_value=MagicMock(
                jwt_secret=JWT_SECRET,
                jwt_algorithm=JWT_ALGORITHM,
                environment=MagicMock(value="production"),
            ),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/driver/pod/uploads/presign",
                json={"category": "photo", "content_type": "image/jpeg"},
            )

        assert resp.status_code == 403, resp.text
        assert fs.calls == []

    def test_requires_file_storage_service_configured(self):
        """When FileStorageService is not wired, the handler raises clearly."""
        app = _make_app(file_storage=None)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            with pytest.raises(RuntimeError, match="file_storage_service"):
                client.post(
                    "/api/driver/pod/uploads/presign",
                    json={"category": "photo", "content_type": "image/jpeg"},
                    headers=_auth_headers(),
                )
