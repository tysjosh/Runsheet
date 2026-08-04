"""
Property-based tests for Surface B per-tenant Bearer authentication.

# Feature: dinee-voice-integration, Property 13: Bearer authentication
# decision and response hygiene

**Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 12.2**

Property 13 asserts that ``fuel/voice/voice_auth.py::get_voice_tenant`` (the
Surface B / ``GET /auth/ping`` authentication dependency) implements the full
Requirement 10 decision table over every combination of ``Authorization`` and
``X-Runsheet-Tenant`` header values:

    * missing / malformed / empty Bearer                 -> HTTP 401 (Req 10.1)
    * Bearer that does not resolve to a configured key   -> HTTP 401 (Req 10.2)
    * missing ``X-Runsheet-Tenant`` header               -> HTTP 401 (Req 10.3)
    * ``X-Runsheet-Tenant`` != the tenant bound to key   -> HTTP 403 (Req 10.4)
    * resolvable key + matching tenant header            -> authorize with the
      ``tenant_id`` taken from the credential binding    (Req 10.5, 11.4, 12.2)

and that every rejection envelope excludes tenant data and credential values
(Req 10.6): neither the bound ``tenant_id``/``channel_id``, the plaintext API
key, nor its salted hash ever appears in the serialized error body / message.

The test drives the real ``VoiceApiKeyRepository`` against recording in-memory
fakes for Elasticsearch and the credentials vault (no mocking of the unit under
test itself), provisioning real keys via ``repository.provision`` so the
resolve path exercises the true salted-hash reverse lookup.
"""

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis.strategies import from_regex, sampled_from

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.voice.voice_auth import (
    VoiceApiKeyRepository,
    VoiceTenantContext,
    configure_voice_auth,
    get_voice_tenant,
)
from fuel.voice.voice_es_mappings import VOICE_API_KEYS_INDEX


# ---------------------------------------------------------------------------
# Recording in-memory fakes (ES + vault)
# ---------------------------------------------------------------------------
class FakeES:
    """Minimal recording fake matching the ``ElasticsearchService`` surface
    used by ``VoiceApiKeyRepository`` (``index_document`` / ``search_documents``).

    ``search_documents`` honours the ``bool.filter`` term clauses the
    repository builds, so the salted-hash reverse lookup behaves like a real
    exact-match query.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}
        self.index_calls: list[tuple[str, str, dict]] = []
        self.search_calls: list[tuple[str, dict]] = []

    async def index_document(self, index: str, doc_id: str, document: dict):
        self.index_calls.append((index, doc_id, dict(document)))
        self.docs.setdefault(index, {})[doc_id] = dict(document)

    async def search_documents(self, index, query, size=100, request_timeout=10):
        self.search_calls.append((index, query))
        filters = (
            query.get("query", {}).get("bool", {}).get("filter", [])
        )
        wanted: dict = {}
        for clause in filters:
            wanted.update(clause.get("term", {}))
        hits = []
        for source in self.docs.get(index, {}).values():
            if all(source.get(k) == v for k, v in wanted.items()):
                hits.append({"_source": source})
        return {"hits": {"hits": hits[:size]}}


class FakeVault:
    """Recording fake for the ``TenantCredentialsVault`` ``put`` surface."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str], dict] = {}

    async def put(self, *, tenant_id, key, plaintext, provider_name=None):
        self.stored[(tenant_id, key)] = plaintext


_SALT = "property-test-voice-salt"


async def _provision(tenant_id: str, channel_id: str):
    """Build a repository over fresh fakes and mint one real API key."""
    es = FakeES()
    vault = FakeVault()
    repo = VoiceApiKeyRepository(es, vault, _SALT)
    api_key = await repo.provision(tenant_id, channel_id)
    return repo, api_key, es, vault


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Distinctive identifiers so the hygiene check can assert the exact value is
# absent from a rejection body without colliding with fixed error-message text.
_tenant_ids = from_regex(r"tenant-[a-z0-9]{8,20}", fullmatch=True)
_channel_ids = from_regex(r"chan-[a-z0-9]{8,20}", fullmatch=True)

# Header values that must all be treated as "no valid Bearer present".
_bad_authorization = sampled_from(
    [
        None,
        "",
        "   ",
        "Bearer",
        "Bearer ",
        "Bearer    ",
        "Token abcdef",
        "abcdef",
        "Basic dXNlcjpwYXNz",
    ]
)


# ---------------------------------------------------------------------------
# Hygiene helper
# ---------------------------------------------------------------------------
def _assert_no_leak(exc: AppException, secrets: list[str]) -> None:
    """Assert no tenant data or credential value appears in the rejection.

    Checks the JSON-serializable body (``to_dict``), the human message, and the
    ``repr`` — the three surfaces an error could leak through.
    """
    surfaces = [str(exc.to_dict()), str(exc.message), repr(exc)]
    for secret in secrets:
        if not secret:
            continue
        for surface in surfaces:
            assert secret not in surface, (
                f"rejection leaked sensitive value {secret!r} in {surface!r}"
            )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Property 13a - missing / malformed Bearer -> 401 (Req 10.1)
# ---------------------------------------------------------------------------
class TestMissingOrMalformedBearer:
    """# Feature: dinee-voice-integration, Property 13 (missing Bearer -> 401)

    **Validates: Requirements 10.1, 10.6**
    """

    @given(tenant_id=_tenant_ids, channel_id=_channel_ids, authorization=_bad_authorization)
    @settings(max_examples=100)
    def test_missing_bearer_rejected_401(self, tenant_id, channel_id, authorization):
        async def scenario():
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            with pytest.raises(AppException) as exc_info:
                # A valid tenant header is supplied to prove the Bearer branch
                # short-circuits before any tenant comparison.
                await get_voice_tenant(None, authorization, tenant_id)
            exc = exc_info.value
            assert exc.status_code == 401
            assert exc.error_code == ErrorCode.VOICE_UNAUTHORIZED
            hashed = repo.hash_api_key(api_key)
            _assert_no_leak(exc, [tenant_id, channel_id, api_key, hashed])

        _run(scenario())


# ---------------------------------------------------------------------------
# Property 13b - unresolvable Bearer -> 401 (Req 10.2)
# ---------------------------------------------------------------------------
class TestUnresolvableKey:
    """# Feature: dinee-voice-integration, Property 13 (unknown key -> 401)

    **Validates: Requirements 10.2, 10.6**
    """

    @given(
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        wrong_key=from_regex(r"wrong-[A-Za-z0-9_\-]{16,48}", fullmatch=True),
    )
    @settings(max_examples=100)
    def test_unknown_key_rejected_401(self, tenant_id, channel_id, wrong_key):
        async def scenario():
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            # The presented key was never provisioned, so it cannot resolve.
            assert wrong_key != api_key
            with pytest.raises(AppException) as exc_info:
                await get_voice_tenant(None, f"Bearer {wrong_key}", tenant_id)
            exc = exc_info.value
            assert exc.status_code == 401
            assert exc.error_code == ErrorCode.VOICE_UNAUTHORIZED
            hashed = repo.hash_api_key(api_key)
            _assert_no_leak(exc, [tenant_id, channel_id, api_key, hashed, wrong_key])

        _run(scenario())


# ---------------------------------------------------------------------------
# Property 13c - resolvable key but missing tenant header -> 401 (Req 10.3)
# ---------------------------------------------------------------------------
class TestMissingTenantHeader:
    """# Feature: dinee-voice-integration, Property 13 (missing tenant -> 401)

    **Validates: Requirements 10.3, 10.6**
    """

    @given(
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        tenant_header=sampled_from([None, "", "   "]),
    )
    @settings(max_examples=100)
    def test_missing_tenant_header_rejected_401(self, tenant_id, channel_id, tenant_header):
        async def scenario():
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            with pytest.raises(AppException) as exc_info:
                await get_voice_tenant(None, f"Bearer {api_key}", tenant_header)
            exc = exc_info.value
            assert exc.status_code == 401
            assert exc.error_code == ErrorCode.VOICE_UNAUTHORIZED
            hashed = repo.hash_api_key(api_key)
            _assert_no_leak(exc, [tenant_id, channel_id, api_key, hashed])

        _run(scenario())


# ---------------------------------------------------------------------------
# Property 13d - tenant header != bound tenant -> 403 (Req 10.4)
# ---------------------------------------------------------------------------
class TestTenantMismatch:
    """# Feature: dinee-voice-integration, Property 13 (tenant mismatch -> 403)

    **Validates: Requirements 10.4, 10.6**
    """

    @given(
        tenant_id=_tenant_ids,
        channel_id=_channel_ids,
        other_tenant=_tenant_ids,
    )
    @settings(max_examples=100)
    def test_tenant_mismatch_rejected_403(self, tenant_id, channel_id, other_tenant):
        async def scenario():
            if other_tenant == tenant_id:
                return  # only mismatched headers exercise the 403 branch
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            with pytest.raises(AppException) as exc_info:
                await get_voice_tenant(None, f"Bearer {api_key}", other_tenant)
            exc = exc_info.value
            assert exc.status_code == 403
            assert exc.error_code == ErrorCode.VOICE_TENANT_MISMATCH
            hashed = repo.hash_api_key(api_key)
            # Neither the bound tenant nor the asserted (wrong) tenant leaks.
            _assert_no_leak(exc, [tenant_id, channel_id, api_key, hashed, other_tenant])

        _run(scenario())


# ---------------------------------------------------------------------------
# Property 13e - match -> authorize with tenant from the credential binding
# ---------------------------------------------------------------------------
class TestMatchAuthorizes:
    """# Feature: dinee-voice-integration, Property 13 (match -> authorize)

    **Validates: Requirements 10.5, 11.4, 12.2**
    """

    @given(tenant_id=_tenant_ids, channel_id=_channel_ids)
    @settings(max_examples=100)
    def test_valid_credentials_authorize_with_bound_tenant(self, tenant_id, channel_id):
        async def scenario():
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            context = await get_voice_tenant(None, f"Bearer {api_key}", tenant_id)
            assert isinstance(context, VoiceTenantContext)
            # Scope is derived from the credential binding (Req 11.4), not the
            # header — both must reflect the provisioned binding.
            assert context.tenant_id == tenant_id
            assert context.channel_id == channel_id

        _run(scenario())

    @given(tenant_id=_tenant_ids, channel_id=_channel_ids)
    @settings(max_examples=100)
    def test_bearer_prefix_is_case_and_whitespace_tolerant(self, tenant_id, channel_id):
        async def scenario():
            repo, api_key, es, vault = await _provision(tenant_id, channel_id)
            configure_voice_auth(repo)
            for header in (
                f"bearer {api_key}",
                f"BEARER {api_key}",
                f"  Bearer   {api_key}  ",
            ):
                context = await get_voice_tenant(None, header, tenant_id)
                assert context.tenant_id == tenant_id
                assert context.channel_id == channel_id

        _run(scenario())
