"""
Unit tests for fuel notification template rendering with sample data
and per-customer opt-out integration.

Tests that:
1. Each of the 4 fuel templates renders correctly with sample data
2. All placeholders are correctly substituted in both subject and body
3. Missing placeholders produce a safe fallback ("[missing]") rather than crashing
4. Opt-out integration works correctly:
   - Opted-out templates are suppressed for non-mandatory templates
   - Mandatory templates (past_due_invoice, e_bol_delivery) cannot be opted out of
5. Both email and SMS variants render correctly where applicable

Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.9
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from notifications.templates.fuel_templates import (
    LOW_TANK_AUTOFILL_ALERT_TEMPLATES,
    PAST_DUE_INVOICE_TEMPLATES,
    DELIVERY_COMPLETED_TEMPLATES,
    E_BOL_DELIVERY_TEMPLATES,
    FUEL_NOTIFICATION_TEMPLATES,
)
from notifications.services.template_renderer import render_template, SafeDict
from notifications.services.preference_resolver import (
    MANDATORY_TEMPLATE_KEYS,
    PreferenceResolver,
)


# ---------------------------------------------------------------------------
# Sample data fixtures for each template
# ---------------------------------------------------------------------------

SAMPLE_LOW_TANK_DATA = {
    "customer_name": "Johnson Heating Co",
    "tank_location": "123 Main St, Springfield IL",
    "current_level_percent": "22",
    "estimated_days_to_empty": "5",
    "scheduled_delivery_date": "2025-02-15",
}

SAMPLE_PAST_DUE_DATA = {
    "customer_name": "Acme Fuel Corp",
    "invoice_number": "INV-2025-0042",
    "amount_due_dollars": "1,247.50",
    "days_past_due": "15",
    "payment_link": "https://pay.example.com/inv/INV-2025-0042",
}

SAMPLE_DELIVERY_COMPLETED_DATA = {
    "customer_name": "Midwest Propane LLC",
    "delivery_date": "2025-01-28",
    "product_name": "Heating Oil #2",
    "gross_gallons": "275.3",
    "net_gallons": "272.8",
    "unit_price": "3.45",
    "total_amount": "941.16",
    "PO_number": "PO-8891",
    "driver_name": "Mike Thompson",
}

SAMPLE_E_BOL_DATA = {
    "customer_name": "Delta Logistics",
    "load_number": "LD-2025-1234",
    "product": "Ultra Low Sulfur Diesel",
    "gross_gallons": "8,200",
    "net_gallons": "8,145",
    "terminal": "Marathon Petroleum - Wood River",
    "driver": "Carlos Rivera",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _es_response(docs: list[dict]) -> dict:
    """Build a mock ES search response from a list of documents."""
    return {
        "hits": {
            "hits": [{"_source": d} for d in docs],
            "total": {"value": len(docs)},
        }
    }


def _preference_doc(
    customer_id: str = "cust-1",
    tenant_id: str = "tenant-1",
    template_opt_outs: list | None = None,
) -> dict:
    """Return a sample preference document with opt-outs."""
    return {
        "preference_id": customer_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "customer_name": "Test Customer",
        "channels": {"sms": "+15551234567", "email": "test@example.com"},
        "event_preferences": [],
        "template_opt_outs": template_opt_outs or [],
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Test: low_tank_autofill_alert rendering (Email + SMS)
# Validates: Requirement 12.1
# ---------------------------------------------------------------------------


class TestLowTankAutofillAlertRendering:
    """Tests for low_tank_autofill_alert template rendering."""

    def test_email_template_exists(self):
        """Email variant is defined for low_tank_autofill_alert."""
        email_templates = [
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES
            if t["channel"] == "email"
        ]
        assert len(email_templates) == 1

    def test_sms_template_exists(self):
        """SMS variant is defined for low_tank_autofill_alert."""
        sms_templates = [
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES
            if t["channel"] == "sms"
        ]
        assert len(sms_templates) == 1

    def test_email_subject_renders_with_sample_data(self):
        """Email subject renders all placeholders correctly."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "email"
        )
        rendered = render_template(template["subject_template"], SAMPLE_LOW_TANK_DATA)

        assert "123 Main St, Springfield IL" in rendered
        assert "{" not in rendered

    def test_email_body_renders_all_placeholders(self):
        """Email body renders all placeholders correctly."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "email"
        )
        rendered = render_template(template["body_template"], SAMPLE_LOW_TANK_DATA)

        assert "Johnson Heating Co" in rendered
        assert "123 Main St, Springfield IL" in rendered
        assert "22" in rendered
        assert "5" in rendered
        assert "2025-02-15" in rendered
        assert "{" not in rendered

    def test_sms_body_renders_all_placeholders(self):
        """SMS body renders all placeholders correctly."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "sms"
        )
        rendered = render_template(template["body_template"], SAMPLE_LOW_TANK_DATA)

        assert "Johnson Heating Co" in rendered
        assert "123 Main St, Springfield IL" in rendered
        assert "22" in rendered
        assert "5" in rendered
        assert "2025-02-15" in rendered
        assert "{" not in rendered

    def test_email_body_missing_placeholder_produces_safe_fallback(self):
        """Missing placeholders produce [missing] instead of crashing."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "email"
        )
        partial_data = {"customer_name": "Johnson Heating Co"}
        rendered = render_template(template["body_template"], partial_data)

        assert "Johnson Heating Co" in rendered
        assert "[missing]" in rendered
        # Should not raise

    def test_sms_subject_renders_with_sample_data(self):
        """SMS subject renders placeholders correctly."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "sms"
        )
        rendered = render_template(template["subject_template"], SAMPLE_LOW_TANK_DATA)

        assert "123 Main St, Springfield IL" in rendered
        assert "{" not in rendered

    def test_placeholders_match_expected_set(self):
        """Template declares the expected set of placeholders."""
        template = next(
            t for t in LOW_TANK_AUTOFILL_ALERT_TEMPLATES if t["channel"] == "email"
        )
        expected = {
            "customer_name",
            "tank_location",
            "current_level_percent",
            "estimated_days_to_empty",
            "scheduled_delivery_date",
        }
        assert set(template["placeholders"]) == expected


# ---------------------------------------------------------------------------
# Test: past_due_invoice rendering (Email only)
# Validates: Requirement 12.2
# ---------------------------------------------------------------------------


class TestPastDueInvoiceRendering:
    """Tests for past_due_invoice template rendering."""

    def test_email_template_exists(self):
        """Email variant is defined for past_due_invoice."""
        email_templates = [
            t for t in PAST_DUE_INVOICE_TEMPLATES if t["channel"] == "email"
        ]
        assert len(email_templates) == 1

    def test_no_sms_template(self):
        """past_due_invoice has no SMS variant (email only)."""
        sms_templates = [
            t for t in PAST_DUE_INVOICE_TEMPLATES if t["channel"] == "sms"
        ]
        assert len(sms_templates) == 0

    def test_email_subject_renders_with_sample_data(self):
        """Email subject renders invoice_number correctly."""
        template = PAST_DUE_INVOICE_TEMPLATES[0]
        rendered = render_template(template["subject_template"], SAMPLE_PAST_DUE_DATA)

        assert "INV-2025-0042" in rendered
        assert "{" not in rendered

    def test_email_body_renders_all_placeholders(self):
        """Email body renders all placeholders correctly."""
        template = PAST_DUE_INVOICE_TEMPLATES[0]
        rendered = render_template(template["body_template"], SAMPLE_PAST_DUE_DATA)

        assert "Acme Fuel Corp" in rendered
        assert "INV-2025-0042" in rendered
        assert "1,247.50" in rendered
        assert "15" in rendered
        assert "https://pay.example.com/inv/INV-2025-0042" in rendered
        assert "{" not in rendered

    def test_email_body_missing_placeholder_produces_safe_fallback(self):
        """Missing placeholders produce [missing] instead of crashing."""
        template = PAST_DUE_INVOICE_TEMPLATES[0]
        partial_data = {"customer_name": "Acme Fuel Corp", "invoice_number": "INV-001"}
        rendered = render_template(template["body_template"], partial_data)

        assert "Acme Fuel Corp" in rendered
        assert "INV-001" in rendered
        assert "[missing]" in rendered

    def test_placeholders_match_expected_set(self):
        """Template declares the expected set of placeholders."""
        template = PAST_DUE_INVOICE_TEMPLATES[0]
        expected = {
            "customer_name",
            "invoice_number",
            "amount_due_dollars",
            "days_past_due",
            "payment_link",
        }
        assert set(template["placeholders"]) == expected


# ---------------------------------------------------------------------------
# Test: delivery_completed rendering (Email + SMS)
# Validates: Requirement 12.3
# ---------------------------------------------------------------------------


class TestDeliveryCompletedRendering:
    """Tests for delivery_completed template rendering."""

    def test_email_template_exists(self):
        """Email variant is defined for delivery_completed."""
        email_templates = [
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "email"
        ]
        assert len(email_templates) == 1

    def test_sms_template_exists(self):
        """SMS variant is defined for delivery_completed."""
        sms_templates = [
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "sms"
        ]
        assert len(sms_templates) == 1

    def test_email_subject_renders_with_sample_data(self):
        """Email subject renders product_name and customer_name correctly."""
        template = next(
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "email"
        )
        rendered = render_template(
            template["subject_template"], SAMPLE_DELIVERY_COMPLETED_DATA
        )

        assert "Heating Oil #2" in rendered
        assert "Midwest Propane LLC" in rendered
        assert "{" not in rendered

    def test_email_body_renders_all_placeholders(self):
        """Email body renders all 9 placeholders correctly."""
        template = next(
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "email"
        )
        rendered = render_template(
            template["body_template"], SAMPLE_DELIVERY_COMPLETED_DATA
        )

        assert "Midwest Propane LLC" in rendered
        assert "2025-01-28" in rendered
        assert "Heating Oil #2" in rendered
        assert "275.3" in rendered
        assert "272.8" in rendered
        assert "3.45" in rendered
        assert "941.16" in rendered
        assert "PO-8891" in rendered
        assert "Mike Thompson" in rendered
        assert "{" not in rendered

    def test_sms_body_renders_all_placeholders(self):
        """SMS body renders all placeholders correctly."""
        template = next(
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "sms"
        )
        rendered = render_template(
            template["body_template"], SAMPLE_DELIVERY_COMPLETED_DATA
        )

        assert "Midwest Propane LLC" in rendered
        assert "Heating Oil #2" in rendered
        assert "275.3" in rendered
        assert "272.8" in rendered
        assert "3.45" in rendered
        assert "941.16" in rendered
        assert "PO-8891" in rendered
        assert "Mike Thompson" in rendered
        assert "{" not in rendered

    def test_email_body_missing_placeholder_produces_safe_fallback(self):
        """Missing placeholders produce [missing] instead of crashing."""
        template = next(
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "email"
        )
        partial_data = {"customer_name": "Midwest Propane LLC", "delivery_date": "2025-01-28"}
        rendered = render_template(template["body_template"], partial_data)

        assert "Midwest Propane LLC" in rendered
        assert "[missing]" in rendered

    def test_placeholders_match_expected_set(self):
        """Template declares the expected set of placeholders."""
        template = next(
            t for t in DELIVERY_COMPLETED_TEMPLATES if t["channel"] == "email"
        )
        expected = {
            "customer_name",
            "delivery_date",
            "product_name",
            "gross_gallons",
            "net_gallons",
            "unit_price",
            "total_amount",
            "PO_number",
            "driver_name",
        }
        assert set(template["placeholders"]) == expected


# ---------------------------------------------------------------------------
# Test: e_bol_delivery rendering (Email with attachment)
# Validates: Requirement 12.4
# ---------------------------------------------------------------------------


class TestEBolDeliveryRendering:
    """Tests for e_bol_delivery template rendering."""

    def test_email_template_exists(self):
        """Email variant is defined for e_bol_delivery."""
        email_templates = [
            t for t in E_BOL_DELIVERY_TEMPLATES if t["channel"] == "email"
        ]
        assert len(email_templates) == 1

    def test_no_sms_template(self):
        """e_bol_delivery has no SMS variant (email with attachment only)."""
        sms_templates = [
            t for t in E_BOL_DELIVERY_TEMPLATES if t["channel"] == "sms"
        ]
        assert len(sms_templates) == 0

    def test_email_subject_renders_with_sample_data(self):
        """Email subject renders load_number correctly."""
        template = E_BOL_DELIVERY_TEMPLATES[0]
        rendered = render_template(template["subject_template"], SAMPLE_E_BOL_DATA)

        assert "LD-2025-1234" in rendered
        assert "{" not in rendered

    def test_email_body_renders_all_placeholders(self):
        """Email body renders all placeholders correctly."""
        template = E_BOL_DELIVERY_TEMPLATES[0]
        rendered = render_template(template["body_template"], SAMPLE_E_BOL_DATA)

        assert "Delta Logistics" in rendered
        assert "LD-2025-1234" in rendered
        assert "Ultra Low Sulfur Diesel" in rendered
        assert "8,200" in rendered
        assert "8,145" in rendered
        assert "Marathon Petroleum - Wood River" in rendered
        assert "Carlos Rivera" in rendered
        assert "{" not in rendered

    def test_email_body_missing_placeholder_produces_safe_fallback(self):
        """Missing placeholders produce [missing] instead of crashing."""
        template = E_BOL_DELIVERY_TEMPLATES[0]
        partial_data = {"customer_name": "Delta Logistics", "load_number": "LD-001"}
        rendered = render_template(template["body_template"], partial_data)

        assert "Delta Logistics" in rendered
        assert "LD-001" in rendered
        assert "[missing]" in rendered

    def test_has_attachment_flag(self):
        """e_bol_delivery template has has_attachment=True."""
        template = E_BOL_DELIVERY_TEMPLATES[0]
        assert template.get("has_attachment") is True
        assert template.get("attachment_type") == "signed_bol_pdf"

    def test_placeholders_match_expected_set(self):
        """Template declares the expected set of placeholders."""
        template = E_BOL_DELIVERY_TEMPLATES[0]
        expected = {
            "customer_name",
            "load_number",
            "product",
            "gross_gallons",
            "net_gallons",
            "terminal",
            "driver",
        }
        assert set(template["placeholders"]) == expected


# ---------------------------------------------------------------------------
# Test: SafeDict fallback behavior
# ---------------------------------------------------------------------------


class TestSafeDictFallback:
    """Tests for SafeDict missing-key behavior across all templates."""

    def test_completely_empty_data_renders_all_missing(self):
        """Rendering with empty data produces [missing] for every placeholder."""
        for tmpl in FUEL_NOTIFICATION_TEMPLATES:
            rendered_subject = render_template(tmpl["subject_template"], {})
            rendered_body = render_template(tmpl["body_template"], {})
            # Should not raise — graceful degradation
            assert "[missing]" in rendered_subject or "[missing]" in rendered_body

    def test_partial_data_renders_provided_values(self):
        """Provided values render correctly even when others are missing."""
        template = LOW_TANK_AUTOFILL_ALERT_TEMPLATES[0]
        partial = {"customer_name": "Test Corp"}
        rendered = render_template(template["body_template"], partial)

        assert "Test Corp" in rendered
        assert "[missing]" in rendered


# ---------------------------------------------------------------------------
# Test: Opt-out integration with fuel templates
# Validates: Requirement 12.9
# ---------------------------------------------------------------------------


class TestFuelTemplateOptOutIntegration:
    """Tests verifying opt-out integration with fuel notification templates."""

    async def test_low_tank_autofill_alert_opted_out_is_suppressed(self):
        """When customer opts out of low_tank_autofill_alert, it is suppressed."""
        pref = _preference_doc(
            template_opt_outs=["low_tank_autofill_alert"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        result = await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        )

        assert result is True

    async def test_delivery_completed_opted_out_is_suppressed(self):
        """When customer opts out of delivery_completed, it is suppressed."""
        pref = _preference_doc(
            template_opt_outs=["delivery_completed"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        result = await resolver.is_template_opted_out(
            "cust-1", "delivery_completed", "tenant-1"
        )

        assert result is True

    async def test_past_due_invoice_cannot_be_opted_out(self):
        """past_due_invoice is mandatory — opt-out always returns False."""
        pref = _preference_doc(
            template_opt_outs=["past_due_invoice"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        result = await resolver.is_template_opted_out(
            "cust-1", "past_due_invoice", "tenant-1"
        )

        assert result is False

    async def test_e_bol_delivery_cannot_be_opted_out(self):
        """e_bol_delivery is mandatory — opt-out always returns False."""
        pref = _preference_doc(
            template_opt_outs=["e_bol_delivery"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        result = await resolver.is_template_opted_out(
            "cust-1", "e_bol_delivery", "tenant-1"
        )

        assert result is False

    async def test_non_opted_out_template_is_not_suppressed(self):
        """Templates not in the opt-out list are not suppressed."""
        pref = _preference_doc(
            template_opt_outs=["low_tank_autofill_alert"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        # delivery_completed is NOT in the opt-out list
        result = await resolver.is_template_opted_out(
            "cust-1", "delivery_completed", "tenant-1"
        )

        assert result is False

    async def test_customer_with_no_preferences_gets_all_templates(self):
        """Customer with no preference document receives all templates."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = PreferenceResolver(es_service=es)

        for template_key in [
            "low_tank_autofill_alert",
            "past_due_invoice",
            "delivery_completed",
            "e_bol_delivery",
        ]:
            result = await resolver.is_template_opted_out(
                "cust-new", template_key, "tenant-1"
            )
            assert result is False, f"{template_key} should not be suppressed for new customer"

    async def test_multiple_opt_outs_respected(self):
        """Customer can opt out of multiple non-mandatory templates."""
        pref = _preference_doc(
            template_opt_outs=["low_tank_autofill_alert", "delivery_completed"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = PreferenceResolver(es_service=es)

        assert await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        ) is True
        assert await resolver.is_template_opted_out(
            "cust-1", "delivery_completed", "tenant-1"
        ) is True
        # Mandatory templates still not suppressed
        assert await resolver.is_template_opted_out(
            "cust-1", "past_due_invoice", "tenant-1"
        ) is False
        assert await resolver.is_template_opted_out(
            "cust-1", "e_bol_delivery", "tenant-1"
        ) is False


# ---------------------------------------------------------------------------
# Test: FUEL_NOTIFICATION_TEMPLATES combined registry
# ---------------------------------------------------------------------------


class TestFuelNotificationTemplatesRegistry:
    """Tests for the combined FUEL_NOTIFICATION_TEMPLATES list."""

    def test_contains_all_four_event_types(self):
        """Registry contains all 4 fuel event types."""
        event_types = {t["event_type"] for t in FUEL_NOTIFICATION_TEMPLATES}
        assert "low_tank_autofill_alert" in event_types
        assert "past_due_invoice" in event_types
        assert "delivery_completed" in event_types
        assert "e_bol_delivery" in event_types

    def test_total_template_count(self):
        """Registry has 6 templates total (2+1+2+1)."""
        assert len(FUEL_NOTIFICATION_TEMPLATES) == 6

    def test_all_templates_have_required_fields(self):
        """Every template has event_type, channel, subject_template, body_template, placeholders."""
        for tmpl in FUEL_NOTIFICATION_TEMPLATES:
            assert "event_type" in tmpl
            assert "channel" in tmpl
            assert "subject_template" in tmpl
            assert "body_template" in tmpl
            assert "placeholders" in tmpl
            assert isinstance(tmpl["placeholders"], list)
            assert len(tmpl["placeholders"]) > 0

    def test_mandatory_templates_are_correctly_identified(self):
        """past_due_invoice and e_bol_delivery are in MANDATORY_TEMPLATE_KEYS."""
        mandatory_event_types = {
            t["event_type"]
            for t in FUEL_NOTIFICATION_TEMPLATES
            if t["event_type"] in MANDATORY_TEMPLATE_KEYS
        }
        assert "past_due_invoice" in mandatory_event_types
        assert "e_bol_delivery" in mandatory_event_types

    def test_non_mandatory_templates_are_correctly_identified(self):
        """low_tank_autofill_alert and delivery_completed are NOT mandatory."""
        assert "low_tank_autofill_alert" not in MANDATORY_TEMPLATE_KEYS
        assert "delivery_completed" not in MANDATORY_TEMPLATE_KEYS
