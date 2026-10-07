"""Pin the CSV export page size to the store clamps it relies on (OI-24).

``KeysetSource`` stops paging when a page returns fewer raw rows than it
asked for. If ``EXPORT_PAGE_SIZE`` grew past a store's page clamp, every
page would come back "short" and the export would silently stop after the
first page. These tests fail first if either side changes.
"""
from __future__ import annotations

import commerce.services.invoice_service as invoice_service
import commerce.services.payment_service as payment_service
import persistence.read_repositories as read_repositories
from services import csv_export


def test_export_page_size_is_at_most_200():
    assert csv_export.EXPORT_PAGE_SIZE <= 200


def test_export_page_size_fits_the_hybrid_read_repository_clamp():
    assert csv_export.EXPORT_PAGE_SIZE <= read_repositories._MAX_PAGE_LIMIT
    assert read_repositories._clamp(csv_export.EXPORT_PAGE_SIZE) == csv_export.EXPORT_PAGE_SIZE


def test_export_page_size_fits_the_invoice_service_clamp():
    assert csv_export.EXPORT_PAGE_SIZE <= invoice_service._MAX_PAGE_LIMIT


def test_export_page_size_fits_the_payment_service_clamp():
    assert csv_export.EXPORT_PAGE_SIZE <= payment_service._MAX_PAGE_LIMIT
