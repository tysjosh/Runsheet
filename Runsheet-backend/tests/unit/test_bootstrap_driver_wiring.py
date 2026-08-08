"""
Bootstrap wiring tests that pin the two defects tasks 2.1-2.4 fixed.

Both defects were invisible to the existing bootstrap tests because those tests
replace every module in ``_BOOT_ORDER`` with a mock: nothing real ever ran, so
nothing real could be found missing.

These tests boot ``core``, ``fuel``, and ``driver`` for real through
``initialize_all`` and stub only the modules that would reach Postgres,
SuperTokens, or Redis. Elasticsearch is the process-wide
``services.elasticsearch_service`` singleton, whose ``connect()``
short-circuits under ``ENVIRONMENT=test``, so no network I/O happens.

Defect 1 (Requirements 4.1, 4.5) — ``OrderService`` had no runtime
construction site: every call site lived in ``tests/``, so
``container.has("order_service")`` was false for the whole process. Every
driver-initiated status transition was unreachable and all three
``order.delivered`` subscribers were dormant. The invoice subscriber is now
late-bound from ``bootstrap/fuel.py``; the ``else``-only arm in
``bootstrap/core.py`` still runs on every boot (``core`` precedes ``fuel``,
so its ``container.has("order_service")`` guard is false), which is why the
count has to be exactly one and not two.

Defect 2 (Requirement 15.12) — ``setup_driver_indices`` was called only by the
seeder, so a deployment that skipped the seeder had its driver indices
auto-created by Elasticsearch on first write with ``dynamic: true``. It is now
called during boot from ``bootstrap/driver.py``, with the mapping-validator
pass immediately after it: ``validate_all`` skips an index that does not
exist, so create-then-remediate has to happen in that order.

Validates: Requirements 4.1, 4.5, 15.12
"""
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

import bootstrap
from bootstrap import _BOOT_ORDER, initialize_all, shutdown_all
from bootstrap.container import ServiceContainer
from commerce.hooks.order_delivered_subscriber import OrderDeliveredInvoiceSubscriber
from fuel.services.order_service import OrderService

#: Bootstrap modules that run for real. Everything else in ``_BOOT_ORDER``
#: needs Postgres (``persistence``), SuperTokens (``middleware``), or Redis
#: (``ops``, ``agents``), none of which this test has.
_REAL_MODULES = ("core", "fuel", "driver")

_real_import_module = importlib.import_module


def _stubbed_import(real_modules):
    """Build an ``importlib.import_module`` replacement for ``initialize_all``.

    Bootstrap modules named in ``real_modules`` are imported for real; the rest
    resolve to no-op mocks. Any other import (third-party code reaching for
    ``importlib`` during a real module's own imports) passes through untouched.
    """
    stubs = {}
    for name in _BOOT_ORDER:
        if name in real_modules:
            continue
        stub = MagicMock(name=f"bootstrap.{name}")
        stub.initialize = AsyncMock()
        stub.shutdown = AsyncMock()
        stubs[name] = stub

    def _import(name, package=None):
        if package == bootstrap.__name__ and name.lstrip(".") in stubs:
            return stubs[name.lstrip(".")]
        return _real_import_module(name, package=package)

    return _import


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def container():
    return ServiceContainer()


@pytest.fixture
async def boot(app, container):
    """Boot ``initialize_all`` for real, then shut the same modules back down.

    Yields a coroutine function so a test can choose which modules are real and
    what the driver index-setup collaborators are patched to. Teardown runs
    ``shutdown_all`` so the background tasks ``bootstrap/core.py`` starts do not
    outlive the test.
    """
    booted_with = []

    async def _boot(real_modules=_REAL_MODULES):
        booted_with.append(real_modules)
        with patch("importlib.import_module", side_effect=_stubbed_import(real_modules)):
            await initialize_all(app, container)

    yield _boot

    for real_modules in booted_with:
        with patch("importlib.import_module", side_effect=_stubbed_import(real_modules)):
            await shutdown_all(app, container)


class TestOrderServiceWiring:
    """Defect 1 — ``order_service`` on the container after a real boot."""

    @pytest.mark.asyncio
    async def test_order_service_is_registered_by_boot(self, boot, container):
        """``container.has("order_service")`` is true once boot finishes.

        This is the assertion the driver surface rests on: without it no
        driver-initiated transition reaches ``apply_status_transition``, so
        neither the state-machine guard, the delivery-window guard, nor the
        driver counter side-effects can run.

        Validates: Requirements 4.1
        """
        await boot()

        assert container.has("order_service")
        assert isinstance(container.order_service, OrderService)

    @pytest.mark.asyncio
    async def test_order_service_holds_the_boot_repositories(self, boot, container):
        """The registered service is wired to the container's own collaborators.

        A service constructed against a throwaway repository would satisfy
        ``has("order_service")`` while writing somewhere nobody reads.

        Validates: Requirements 4.1
        """
        await boot()

        order_service = container.order_service
        assert order_service._order_repo is container.order_repository
        assert order_service._driver_counter_service is (
            container.driver_counter_service
        )

    @pytest.mark.asyncio
    async def test_exactly_one_invoice_subscriber_on_order_delivered(
        self, boot, container
    ):
        """One ``order.delivered`` invoice subscriber — not zero, not two.

        Zero was the defect: ``bootstrap/core.py`` reaches its subscription
        attempt before ``fuel`` runs, so its ``container.has("order_service")``
        guard is false and only the ``else`` warning fires. Two is the
        regression that the fix could introduce, since that ``core`` arm still
        executes on every boot — a duplicate registration would invoice the
        same delivery twice.

        Validates: Requirements 4.5
        """
        await boot()

        handlers = container.order_service._event_subscribers.get(
            "order.delivered", []
        )
        invoice_subscribers = [
            handler
            for handler in handlers
            if isinstance(handler, OrderDeliveredInvoiceSubscriber)
        ]

        assert len(invoice_subscribers) == 1, (
            "expected exactly one order.delivered invoice subscriber, got "
            f"{len(invoice_subscribers)} (handlers: {handlers})"
        )

    @pytest.mark.asyncio
    async def test_invoice_subscriber_uses_the_container_invoice_service(
        self, boot, container
    ):
        """The subscriber invoices through the InvoiceService ``core`` registered.

        ``bootstrap/fuel.py`` late-binds against
        ``container.commerce_invoice_service`` rather than building a second
        InvoiceService, so the subscriber and the invoice REST surface share
        one instance.

        Validates: Requirements 4.5
        """
        await boot()

        (subscriber,) = [
            handler
            for handler in container.order_service._event_subscribers[
                "order.delivered"
            ]
            if isinstance(handler, OrderDeliveredInvoiceSubscriber)
        ]

        assert container.has("commerce_invoice_service")
        assert subscriber._invoice_service is container.commerce_invoice_service

    @pytest.mark.asyncio
    async def test_core_runs_before_fuel_can_register_order_service(self):
        """The ordering that made ``core``'s subscription arm dead code.

        Pinned so that moving ``fuel`` after ``core`` — which would make both
        arms live and double-register the subscriber — fails here rather than
        in production billing.
        """
        assert _BOOT_ORDER.index("core") < _BOOT_ORDER.index("fuel")


# ``TestDriverIndexSetupWiring`` and its ``_RecordingValidator`` were removed here.
#
# They asserted two things about boot, both from Defect 2 of the driver spec
# (R15.12): that ``bootstrap/driver.py`` called ``setup_driver_indices``, so a
# deployment which skipped the seeder still got strict mappings; and that the mapping
# validator ran immediately afterwards, closing the window where a freshly-created
# index went unvalidated until the next boot.
#
# Phase 6 deleted both. There are no indices to create — the document store is one
# Postgres table created by a migration — and no live mappings to drift from.
#
# The surviving property is the boot ORDER, asserted above. What did NOT survive is
# ``dynamic: strict`` rejecting an undeclared field; that now rests on
# ``extra="forbid"`` in the driver-surface Pydantic models, upstream of the store.
