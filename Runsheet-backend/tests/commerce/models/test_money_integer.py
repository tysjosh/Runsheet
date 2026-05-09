"""Property test: every _cents attribute on every commerce model is typed int.

**Validates: Requirements C1 (Money is integer cents)**

Addresses finding F27 from the 2026-05-08 code review: no float-based money
math is permitted. This test dynamically introspects all commerce model classes
and asserts that every field ending in ``_cents`` is annotated as ``int``.

If a developer adds a new model or a new ``_cents`` field typed as ``float``,
this test will catch the regression immediately.
"""

from __future__ import annotations

import inspect
import typing
from typing import get_type_hints

import pytest
from pydantic import BaseModel

from commerce.models.customer import Customer
from commerce.models.account import Account
from commerce.models.price_book import PriceBook, PricingRule, PricingResult
from commerce.models.invoice import Invoice, InvoiceLineItem
from commerce.models.payment import Payment
from commerce.models.events import InvoiceEvent, AccountEvent


# ---------------------------------------------------------------------------
# Model registry — all commerce model classes to introspect
# ---------------------------------------------------------------------------

ALL_COMMERCE_MODELS: list[type[BaseModel]] = [
    Customer,
    Account,
    PriceBook,
    PricingRule,
    PricingResult,
    Invoice,
    InvoiceLineItem,
    Payment,
    InvoiceEvent,
    AccountEvent,
]


def _get_cents_fields(model_cls: type[BaseModel]) -> list[tuple[str, type]]:
    """Return (field_name, annotation) pairs for fields ending in '_cents'."""
    cents_fields = []
    for field_name, field_info in model_cls.model_fields.items():
        if field_name.endswith("_cents"):
            cents_fields.append((field_name, field_info.annotation))
    return cents_fields


def _resolve_type(annotation: typing.Any) -> type | None:
    """Unwrap Optional/Union to get the core type for comparison."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        # Optional[X] is Union[X, None] — extract the non-None type
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return args[0] if args else None
    return annotation


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMoneyIntegerConstraint:
    """Assert Constraint C1: every _cents field across all commerce models is int."""

    @pytest.mark.parametrize(
        "model_cls",
        ALL_COMMERCE_MODELS,
        ids=[cls.__name__ for cls in ALL_COMMERCE_MODELS],
    )
    def test_all_cents_fields_are_int(self, model_cls: type[BaseModel]) -> None:
        """Every field ending in _cents on {model_cls.__name__} must be typed int."""
        cents_fields = _get_cents_fields(model_cls)

        for field_name, annotation in cents_fields:
            resolved = _resolve_type(annotation)
            assert resolved is int, (
                f"{model_cls.__name__}.{field_name} is typed as {annotation}, "
                f"expected int. Constraint C1 requires all monetary values to be "
                f"integer cents — no float-based money math is permitted."
            )

    def test_at_least_one_model_has_cents_fields(self) -> None:
        """Sanity check: at least some models define _cents fields.

        Guards against the test silently passing if all models are refactored
        to remove _cents fields (which would indicate a design violation).
        """
        total_cents_fields = sum(
            len(_get_cents_fields(m)) for m in ALL_COMMERCE_MODELS
        )
        assert total_cents_fields > 0, (
            "No _cents fields found across any commerce model. "
            "This likely indicates a regression in the model definitions."
        )

    def test_discovery_covers_expected_models(self) -> None:
        """Verify the model registry includes all expected commerce models."""
        expected_names = {
            "Customer",
            "Account",
            "PriceBook",
            "PricingRule",
            "PricingResult",
            "Invoice",
            "InvoiceLineItem",
            "Payment",
            "InvoiceEvent",
            "AccountEvent",
        }
        actual_names = {cls.__name__ for cls in ALL_COMMERCE_MODELS}
        assert expected_names == actual_names, (
            f"Model registry mismatch. Missing: {expected_names - actual_names}, "
            f"Extra: {actual_names - expected_names}"
        )

    @pytest.mark.parametrize(
        "model_cls",
        ALL_COMMERCE_MODELS,
        ids=[cls.__name__ for cls in ALL_COMMERCE_MODELS],
    )
    def test_cents_fields_are_not_optional(self, model_cls: type[BaseModel]) -> None:
        """No _cents field should be Optional — money amounts must always be present."""
        for field_name, field_info in model_cls.model_fields.items():
            if field_name.endswith("_cents"):
                annotation = field_info.annotation
                origin = typing.get_origin(annotation)
                if origin is typing.Union:
                    args = typing.get_args(annotation)
                    assert type(None) not in args, (
                        f"{model_cls.__name__}.{field_name} is Optional, "
                        f"but _cents fields must always have a value (Constraint C1)."
                    )
