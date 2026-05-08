"""
Unit tests for :mod:`integrations.provider_registry` (Task 9.10).

Covers:

* :func:`register_all_providers` registers exactly four built-in
  entries (QuickBooks Online, Veeder-Root, Geotab, Stripe).
* Each registered entry exposes the Marketplace-level feature flag
  ``overlay.integration.{provider_name}`` via
  :meth:`ProviderCatalogEntry.effective_feature_flag_key` per
  Requirement 5.6.6.
* Registration is deterministic: the order is
  QBO → Veeder-Root → Geotab → Stripe.
* Calling :func:`register_all_providers` twice is idempotent — the
  shared registry still contains exactly four entries, matching the
  contract documented on the bootstrap helper.

Validates: Requirements 5.6.2, 5.6.6.
"""
from __future__ import annotations

import pytest

from integrations.provider_catalog import (
    ProviderCatalogEntry,
    clear_registry,
    get_provider,
    list_providers,
)
from integrations.provider_registry import register_all_providers


_EXPECTED_ORDER = ("quickbooks_online", "veeder_root", "geotab", "stripe")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Clear the shared catalog before and after every test."""

    clear_registry()
    try:
        yield
    finally:
        clear_registry()


class TestRegisterAllProviders:
    def test_registers_exactly_four_entries(self):
        registered = register_all_providers()

        assert len(registered) == 4
        assert len(list_providers()) == 4

    def test_registration_order_is_deterministic(self):
        registered = register_all_providers()

        assert [entry.provider_name for entry in registered] == list(
            _EXPECTED_ORDER
        )
        assert [entry.provider_name for entry in list_providers()] == list(
            _EXPECTED_ORDER
        )

    def test_each_entry_uses_marketplace_level_feature_flag(self):
        register_all_providers()

        for provider_name in _EXPECTED_ORDER:
            entry = get_provider(provider_name)
            assert entry is not None, f"missing catalog entry: {provider_name}"
            assert isinstance(entry, ProviderCatalogEntry)
            # Marketplace-level visibility flag (Req 5.6.6) — defaults
            # to overlay.integration.{provider_name}. The per-connector
            # behaviour-level flags (overlay.qbo_invoice_push,
            # overlay.stripe_autocharge) live inside the connectors
            # and are intentionally separate from this key.
            assert entry.effective_feature_flag_key() == (
                f"overlay.integration.{provider_name}"
            )

    def test_calling_twice_is_idempotent(self):
        first = register_all_providers()
        second = register_all_providers()

        assert len(first) == 4
        assert len(second) == 4
        # After two calls the shared registry still only holds four
        # entries — register_provider replaces entries atomically so
        # duplicates are impossible.
        assert len(list_providers()) == 4
        # The observed order is unchanged across the second call.
        assert [entry.provider_name for entry in list_providers()] == list(
            _EXPECTED_ORDER
        )

    def test_required_credential_fields_are_populated(self):
        register_all_providers()

        # A conservative smoke-check — each provider advertises at
        # least one required credential field. The per-connector unit
        # tests assert the exact schema; here we only guarantee the
        # bootstrap didn't drop the field list on the way through.
        for provider_name in _EXPECTED_ORDER:
            entry = get_provider(provider_name)
            assert entry is not None
            assert len(entry.required_credential_fields) >= 1

    def test_doc_url_is_populated_for_each_provider(self):
        register_all_providers()

        for provider_name in _EXPECTED_ORDER:
            entry = get_provider(provider_name)
            assert entry is not None
            assert entry.doc_url, (
                f"{provider_name} catalog entry must expose a doc_url for "
                "the Marketplace 'Learn more' anchor"
            )

    def test_override_registrations_preserve_order(self):
        """Custom registration tuple list is respected verbatim.

        The override hook lets bootstrap tests reproduce a smaller
        catalog without importing every connector module.
        """

        def _make_entry(name: str, category: str) -> ProviderCatalogEntry:
            def _register() -> ProviderCatalogEntry:
                from integrations.provider_catalog import register_provider

                return register_provider(
                    ProviderCatalogEntry(
                        provider_name=name,
                        category=category,
                        description=f"{name} test entry",
                    )
                )

            return _register  # type: ignore[return-value]

        registered = register_all_providers(
            registrations=[
                ("alpha", _make_entry("alpha", "accounting")),
                ("beta", _make_entry("beta", "payment")),
            ]
        )

        assert [entry.provider_name for entry in registered] == ["alpha", "beta"]
        assert [entry.provider_name for entry in list_providers()] == [
            "alpha",
            "beta",
        ]

    def test_rejects_non_catalog_return_value(self):
        def _bad_register() -> ProviderCatalogEntry:
            return "not-an-entry"  # type: ignore[return-value]

        with pytest.raises(TypeError):
            register_all_providers(registrations=[("bad", _bad_register)])

    def test_rejects_provider_name_mismatch(self):
        from integrations.provider_catalog import register_provider

        def _register() -> ProviderCatalogEntry:
            return register_provider(
                ProviderCatalogEntry(
                    provider_name="actual_name",
                    category="accounting",
                    description="Mismatch probe",
                )
            )

        with pytest.raises(ValueError):
            register_all_providers(
                registrations=[("expected_name", _register)]
            )
