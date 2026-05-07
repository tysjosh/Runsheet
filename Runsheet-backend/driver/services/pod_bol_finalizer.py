"""
POD → BOL finalizer: synchronous Bill of Lading generation on POD finalization.

Implements Task 8.6 of the fuel-ops-hardening spec (Requirements 4.3.4 and
4.3.5).

Responsibilities
----------------
* Consulted by the driver POD submission endpoint immediately after a POD is
  persisted to ``proof_of_delivery``.
* Checks the per-tenant overlay feature flag ``overlay.bol_generation``. When
  the flag is not in an ``active_gated`` / ``active_auto`` state, the
  finalizer no-ops — the tenant has not opted in to automatic BOL generation
  (Req 4.3.5 "when overlay.bol_generation is enabled").
* Gathers the surrounding POD context (order, depot, driver, truck,
  destination, tenant) best-effort via the supplied :class:`ContextLoader`.
  Missing lookups degrade to the default UNKNOWN placeholders baked into
  :class:`services.bol_service.BOLService` — the PDF still renders, and the
  tenant sees a clearly-marked "pending_regeneration" record if something was
  mis-wired.
* Invokes :class:`BOLService.generate` synchronously. On any exception the
  finalizer swallows the error, logs it, and persists a minimal
  ``bill_of_lading`` stub document with ``status: pending_regeneration`` so a
  downstream batch job (out of scope for this task) can retry without
  needing the full POD payload again (Req 4.3.5 "on failure mark BOL status
  pending_regeneration without blocking POD persistence").
* **Never raises** — a BOL failure must not break POD persistence.

The finalizer is deliberately dependency-light: it accepts pre-constructed
collaborators (feature flag service, BOL service, ES service) rather than
constructing them itself. The driver POD endpoint wires them in via
:func:`driver.api.pod_endpoints.configure_pod_endpoints`.

Validates:
    * Requirement 4.3.4 — BOL_Service invoked synchronously on POD
      finalization when ``overlay.bol_generation`` is enabled; presigned
      download URL is exposed by the companion
      ``GET /api/fuel/pod/{pod_id}/bol`` endpoint (see
      :mod:`fuel.api.fuel_ops_endpoints`).
    * Requirement 4.3.5 — BOL failure marks the BOL record
      ``status: pending_regeneration`` without blocking POD persistence.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from fuel.services.fuel_ops_es_mappings import BILL_OF_LADING_INDEX
from services.bol_service import BOLDocument, BOLRenderInputs, BOLService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Overlay feature-flag name gating automatic BOL generation on POD
#: finalization. Resolved per tenant via the shared FeatureFlagService
#: (Redis key ``overlay_ff:overlay.bol_generation:{tenant_id}``).
BOL_GENERATION_FLAG_KEY: str = "overlay.bol_generation"

#: Overlay states that mean "BOL generation is on for this tenant". Matches
#: the convention used by other overlay agents (Route_Planning_Agent,
#: etc.) — ``shadow`` and ``disabled`` states keep the finalizer idle so
#: tenants can preview before flipping to active.
_ACTIVE_OVERLAY_STATES: frozenset[str] = frozenset({"active_gated", "active_auto"})

#: BOL document status written when the synchronous generation path fails.
#: Surfaced by ``GET /api/fuel/pod/{pod_id}/bol`` so operators can triage
#: which BOLs need regeneration.
BOL_STATUS_PENDING_REGENERATION: str = "pending_regeneration"

#: Status written when BOL generation succeeds. Mirrors the default used
#: by :class:`BOLService`.
BOL_STATUS_GENERATED: str = "generated"


# ---------------------------------------------------------------------------
# Context loading protocol
# ---------------------------------------------------------------------------


class PODContextLoader(Protocol):
    """Optional collaborator that fetches the POD-surrounding records.

    The driver POD endpoint doesn't naturally have a handle on the order,
    depot, driver, truck, tenant, or destination. Rather than hard-wiring
    those lookups into the finalizer (and coupling this module to every
    domain repository), we accept a single async loader. Production
    bootstrap wires a real loader; tests inject a stub that returns the
    preconfigured documents.

    Implementations are best-effort: return ``None`` for any record that
    cannot be resolved in the tenant's scope. The finalizer will degrade
    to the BOL service's built-in UNKNOWN placeholders rather than failing
    the whole render.
    """

    async def load(
        self, *, tenant_id: str, pod: Mapping[str, Any]
    ) -> "PODContext":
        ...


class PODContext:
    """Resolved POD surrounding context, passed to :class:`BOLService`.

    Lightweight value object (not a Pydantic model) because every field is
    already sanitized to a mapping/string by the loader. ``tenant_name``
    and ``tenant_logo_bytes`` are surfaced separately because
    :class:`BOLRenderInputs` requires them at the top level.
    """

    __slots__ = (
        "tenant_name",
        "tenant_logo_bytes",
        "order",
        "depot",
        "driver",
        "truck",
        "destination",
    )

    def __init__(
        self,
        *,
        tenant_name: str,
        tenant_logo_bytes: Optional[bytes],
        order: Mapping[str, Any],
        depot: Mapping[str, Any],
        driver: Mapping[str, Any],
        truck: Mapping[str, Any],
        destination: Mapping[str, Any],
    ) -> None:
        self.tenant_name = tenant_name
        self.tenant_logo_bytes = tenant_logo_bytes
        self.order = order
        self.depot = depot
        self.driver = driver
        self.truck = truck
        self.destination = destination

    @classmethod
    def empty(cls, tenant_name: str = "") -> "PODContext":
        """Default context used when a loader is not wired. Produces a BOL
        populated with ``UNKNOWN`` placeholders — still persistable, still
        downloadable, still auditable as ``pending_regeneration`` if the
        caller prefers to retry later."""
        return cls(
            tenant_name=tenant_name,
            tenant_logo_bytes=None,
            order={},
            depot={},
            driver={},
            truck={},
            destination={},
        )


#: A function-style shortcut for the loader protocol so callers that don't
#: need a full class can just pass a coroutine.
LoaderCallable = Callable[..., Awaitable[PODContext]]


# ---------------------------------------------------------------------------
# Finalizer
# ---------------------------------------------------------------------------


class PODBOLFinalizer:
    """Synchronously generate a BOL when a POD is finalized.

    Args:
        bol_service: Configured :class:`BOLService`.
        es_service: ES-service-compatible handle used to persist the
            ``pending_regeneration`` stub on failure. The happy-path record
            is persisted by :class:`BOLService` itself; this handle is only
            used for the failure branch so :class:`BOLService` doesn't need
            its own separate error-reporting code path.
        feature_flag_service: Provides ``get_overlay_state(flag_key,
            tenant_id) -> str``. Same contract used by the Route_Planning_Agent.
        context_loader: Optional loader that fetches the order / depot /
            driver / truck / destination for the POD. When absent, the
            finalizer renders a BOL using :meth:`PODContext.empty` and the
            BOL service's built-in UNKNOWN placeholders.
    """

    def __init__(
        self,
        *,
        bol_service: BOLService,
        es_service: Any,
        feature_flag_service: Any,
        context_loader: Optional[PODContextLoader] = None,
    ) -> None:
        if bol_service is None:
            raise ValueError("bol_service is required")
        if es_service is None:
            raise ValueError("es_service is required")
        # feature_flag_service is allowed to be None so tests and early
        # bootstrap paths can exercise the pipeline without Redis wiring;
        # in that case we treat the flag as "disabled" and no-op.
        self._bol_service = bol_service
        self._es = es_service
        self._feature_flags = feature_flag_service
        self._context_loader = context_loader

    # ------------------------------------------------------------------

    async def maybe_generate(
        self,
        *,
        tenant_id: str,
        pod: Mapping[str, Any],
        actor: Optional[str] = None,
    ) -> Optional[BOLDocument]:
        """Generate a BOL for ``pod`` if the feature flag is enabled.

        Returns the persisted :class:`BOLDocument` on success. Returns
        ``None`` when the flag is disabled. When generation fails, returns
        ``None`` and persists a ``pending_regeneration`` stub record — the
        caller does not receive any exception.

        This method is called by the driver POD submission endpoint after
        the POD has been written to ``proof_of_delivery``. The POD write
        has already committed; nothing here may roll that back.
        """
        if not tenant_id:
            logger.debug("PODBOLFinalizer: no tenant_id, skipping")
            return None

        pod_id = pod.get("pod_id")
        if not pod_id:
            logger.debug("PODBOLFinalizer: POD missing pod_id, skipping")
            return None

        enabled = await self._is_enabled(tenant_id)
        if not enabled:
            logger.debug(
                "PODBOLFinalizer: overlay.bol_generation disabled for tenant=%s,"
                " skipping pod=%s",
                tenant_id,
                pod_id,
            )
            return None

        context = await self._load_context(tenant_id, pod)
        inputs = BOLRenderInputs(
            tenant_id=tenant_id,
            tenant_name=context.tenant_name or tenant_id,
            tenant_logo_bytes=context.tenant_logo_bytes,
            pod=pod,
            order=context.order,
            depot=context.depot,
            driver=context.driver,
            truck=context.truck,
            destination=context.destination,
        )

        try:
            doc = await self._bol_service.generate(
                tenant_id=tenant_id,
                inputs=inputs,
                actor=actor,
            )
            logger.info(
                "PODBOLFinalizer: BOL generated tenant=%s pod=%s bol_id=%s",
                tenant_id,
                pod_id,
                doc.bol_id,
            )
            return doc
        except Exception as exc:
            # Never propagate — POD persistence must not be blocked by a
            # BOL failure (Req 4.3.5). Record a stub so the record is
            # discoverable via GET /api/fuel/pod/{pod_id}/bol.
            logger.exception(
                "PODBOLFinalizer: BOL generation failed tenant=%s pod=%s: %s",
                tenant_id,
                pod_id,
                exc,
            )
            await self._persist_pending_regeneration(
                tenant_id=tenant_id,
                pod=pod,
                error=str(exc) or exc.__class__.__name__,
            )
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _is_enabled(self, tenant_id: str) -> bool:
        """Return ``True`` when ``overlay.bol_generation`` is active.

        Mirrors the ``active_gated`` / ``active_auto`` semantics used
        elsewhere in the overlay stack. ``shadow`` and ``disabled`` keep
        the finalizer idle.
        """
        ff = self._feature_flags
        if ff is None:
            return False
        try:
            state = await ff.get_overlay_state(BOL_GENERATION_FLAG_KEY, tenant_id)
        except AttributeError:
            # Legacy services expose only ``is_enabled``. Treat its boolean
            # as "active" for backward compatibility.
            try:
                return bool(await ff.is_enabled(tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "PODBOLFinalizer: feature flag lookup failed tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "PODBOLFinalizer: overlay state lookup failed tenant=%s: %s",
                tenant_id,
                exc,
            )
            return False
        return state in _ACTIVE_OVERLAY_STATES

    async def _load_context(
        self, tenant_id: str, pod: Mapping[str, Any]
    ) -> PODContext:
        """Resolve POD-surrounding context via the injected loader.

        A missing loader or a loader exception degrades to an empty
        context so the BOL still renders with ``UNKNOWN`` placeholders.
        """
        loader = self._context_loader
        if loader is None:
            return PODContext.empty(tenant_name=tenant_id)
        try:
            return await loader.load(tenant_id=tenant_id, pod=pod)
        except Exception as exc:
            logger.warning(
                "PODBOLFinalizer: context loader failed tenant=%s pod=%s: %s",
                tenant_id,
                pod.get("pod_id"),
                exc,
            )
            return PODContext.empty(tenant_name=tenant_id)

    async def _persist_pending_regeneration(
        self,
        *,
        tenant_id: str,
        pod: Mapping[str, Any],
        error: str,
    ) -> None:
        """Write a minimal BOL stub with ``status: pending_regeneration``.

        Kept best-effort: if the ES write itself fails (index outage),
        we log and move on — the POD write has already committed and a
        subsequent GET request will simply return 404, which the UI can
        surface as "BOL missing, retry".
        """
        now = datetime.now(timezone.utc)
        pod_id = str(pod.get("pod_id") or "")
        order_id = pod.get("order_id")
        stub_id = f"bol-{tenant_id}-pending-{uuid.uuid4()}"

        document = {
            "bol_id": stub_id,
            "tenant_id": tenant_id,
            "pod_id": pod_id,
            "order_id": str(order_id) if order_id else None,
            "file_ref": "",
            "hash": "",
            "status": BOL_STATUS_PENDING_REGENERATION,
            "fields": {
                "error": error[:500],
                "pod_id": pod_id,
            },
            "generated_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        try:
            await self._es.index_document(
                BILL_OF_LADING_INDEX, stub_id, document
            )
            logger.warning(
                "PODBOLFinalizer: wrote pending_regeneration stub tenant=%s"
                " pod=%s bol_id=%s",
                tenant_id,
                pod_id,
                stub_id,
            )
        except Exception as es_exc:  # pragma: no cover - defensive
            logger.error(
                "PODBOLFinalizer: failed to persist pending_regeneration"
                " stub tenant=%s pod=%s: %s",
                tenant_id,
                pod_id,
                es_exc,
            )


__all__ = [
    "BOL_GENERATION_FLAG_KEY",
    "BOL_STATUS_GENERATED",
    "BOL_STATUS_PENDING_REGENERATION",
    "PODBOLFinalizer",
    "PODContext",
    "PODContextLoader",
]
