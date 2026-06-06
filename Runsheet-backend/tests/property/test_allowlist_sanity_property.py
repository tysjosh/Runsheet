"""
Property-based test for Public_Route_Allowlist sanity.

**Validates: Requirements 6.3, 6.6**

Property 16: Allowlist contains only sanctioned categories — every entry that
``middleware.auth_enforcement.is_public_route`` accepts belongs to exactly one
of the four sanctioned categories:

  1. a health-check route          (``HEALTH_ROUTES``),
  2. an API-documentation route    (``DOCS_ROUTES``),
  3. a self-verifying webhook HMAC route (``WEBHOOK_HMAC_ROUTES`` /
     ``WEBHOOK_HMAC_PREFIXES``), or
  4. a SuperTokens auth route       (the ``/auth`` ``PUBLIC_PREFIXES``).

The allowlist is finite, so the concrete-sets portion of this test iterates the
actual entries and asserts each one is categorizable and accepted by
``is_public_route``. The generated-path portion uses hypothesis to manufacture
random paths that fall outside every sanctioned category and asserts they are
NOT public — i.e. ``is_public_route`` never widens the allowlist beyond the four
categories (fail-closed by default, Req 6.2/6.3). No network calls to the
managed SaaS core are made; this exercises pure routing predicate logic.
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from middleware.auth_enforcement import (
    DOCS_ROUTES,
    HEALTH_ROUTES,
    PUBLIC_PREFIXES,
    PUBLIC_ROUTE_ALLOWLIST,
    WEBHOOK_HMAC_PREFIXES,
    WEBHOOK_HMAC_ROUTES,
    is_public_route,
)


# ---------------------------------------------------------------------------
# Sanctioned-category oracle
# ---------------------------------------------------------------------------
# A path is "sanctioned" iff it is categorizable into exactly one of the four
# permitted buckets. This mirrors is_public_route's own structure but is written
# independently so the test can detect drift in either direction.
def _category_of(path: str) -> str | None:
    """Return the single sanctioned category name for ``path`` or None."""
    if path in HEALTH_ROUTES:
        return "health"
    if path in DOCS_ROUTES:
        return "docs"
    if path in WEBHOOK_HMAC_ROUTES:
        return "webhook"
    for prefix in WEBHOOK_HMAC_PREFIXES:
        if path.startswith(prefix):
            return "webhook"
    for prefix in PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "auth"
    return None


# Feature: supertokens-auth-migration, Property 16: Allowlist contains only sanctioned categories
class TestAllowlistOnlySanctionedCategories:
    """**Validates: Requirements 6.3, 6.6**"""

    # -- Concrete-set assertions (finite allowlist iteration) ---------------

    def test_health_and_docs_partition_the_exact_allowlist(self):
        """The exact-match allowlist is exactly health ∪ docs, disjointly."""
        assert PUBLIC_ROUTE_ALLOWLIST == HEALTH_ROUTES | DOCS_ROUTES
        # Each sanctioned category is mutually exclusive (exactly one category).
        assert HEALTH_ROUTES.isdisjoint(DOCS_ROUTES)
        assert PUBLIC_ROUTE_ALLOWLIST.isdisjoint(WEBHOOK_HMAC_ROUTES)

    def test_every_concrete_allowlist_entry_is_categorized_and_public(self):
        """Every literal allowlist/webhook entry maps to one category and is public."""
        for path in (
            HEALTH_ROUTES
            | DOCS_ROUTES
            | WEBHOOK_HMAC_ROUTES
        ):
            assert _category_of(path) is not None, (
                f"allowlist entry {path!r} belongs to no sanctioned category"
            )
            assert is_public_route(path), (
                f"allowlist entry {path!r} must be accepted by is_public_route"
            )

    def test_prefixes_are_only_auth_and_webhooks(self):
        """The only sanctioned prefixes are /auth and the self-verifying webhooks."""
        assert PUBLIC_PREFIXES == ("/auth",)
        for prefix in PUBLIC_PREFIXES:
            assert _category_of(prefix) == "auth"
            assert is_public_route(prefix)
        for prefix in WEBHOOK_HMAC_PREFIXES:
            # A representative sub-path under each webhook prefix is a webhook route.
            sample = prefix + "abc123"
            assert _category_of(sample) == "webhook"
            assert is_public_route(sample)

    # -- Generated-path property -------------------------------------------

    @given(
        path=st.one_of(
            # Arbitrary URL-ish paths.
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N", "P"),
                    whitelist_characters="/-_.",
                ),
                min_size=0,
                max_size=40,
            ).map(lambda s: "/" + s.lstrip("/")),
            # Concrete API-ish paths that should be protected.
            st.sampled_from(
                [
                    "/api/fuel/mvp/approvals",
                    "/api/orders",
                    "/api/drivers",
                    "/webhooks",
                    "/webhooks/unknown",
                    "/authentication",  # superstring of /auth, must NOT match
                    "/health-extended",  # superstring of /health, must NOT match
                    "/docs-internal",  # superstring of /docs, must NOT match
                    "/api/health/details",
                ]
            ),
        )
    )
    @settings(max_examples=100)
    def test_is_public_iff_path_is_in_a_sanctioned_category(self, path: str):
        """is_public_route(path) is True iff the path falls in a sanctioned category.

        This proves the predicate never grants public access to anything outside
        the four sanctioned buckets (no accidental widening), and conversely
        always grants it for sanctioned paths.
        """
        expected_category = _category_of(path)
        is_public = is_public_route(path)

        assert is_public == (expected_category is not None), (
            f"path={path!r}: is_public_route={is_public} but sanctioned "
            f"category={expected_category!r}"
        )

    @given(
        suffix=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
                whitelist_characters="-_.",
            ),
            min_size=1,
            max_size=20,
        )
    )
    @settings(max_examples=100)
    def test_superstrings_of_sanctioned_prefixes_are_not_public(self, suffix: str):
        """A path that only *contains* '/auth' as a name-prefix is not public.

        e.g. '/authxyz' must NOT be treated as a SuperTokens auth route — only
        '/auth' itself or '/auth/...' is sanctioned (Req 6.3 tight matching).
        """
        path = "/auth" + suffix
        # Exclude the genuinely-sanctioned '/auth/...' sub-path case.
        assume(not path.startswith("/auth/"))
        assert _category_of(path) is None
        assert is_public_route(path) is False
