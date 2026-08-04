"""The commerce and compliance master flags must actually gate their surface.

Two things are pinned here.

**Commerce.** ``commerce_backbone_enabled``'s own description says "all
commerce endpoints return 404" when off, but ``commerce_price_protection_router``
and ``commerce_pricing_rules_router`` were registered in the unconditional
block, so both commerce pricing surfaces stayed reachable with the master flag
off. A flag that is documented as total and is actually partial is worse than
no flag, because it is relied on.

**Compliance.** The compliance REST surface had no master flag at all — its
eight routers were registered unconditionally. ``compliance_backbone_enabled``
gates them for an MVP pilot.

The compliance *services* are deliberately not gated, and
:func:`test_pipeline_compliance_services_are_not_gated` exists to stop someone
"finishing the job" later. Four of them are load-bearing inside the delivery
pipeline:

    DyedDieselEnforcer  compartment_loading_agent, invoice_service
    delivery_filter     route_planning_agent
    VCFCalculator       reconciliation_service
    HOSStatus           driver/hos_advisory_service

Gating those would silently break load building, routing, reconciliation, and
driver hours-of-service.

Note on how these tests set the flag. ``main.py`` calls
``load_dotenv(env_file, override=True)`` at import, which makes the env FILE
beat real process environment variables — so setting ``COMMERCE_BACKBONE_ENABLED``
in the environment does not work and cannot be used to drive this test. The
settings object is patched directly instead.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import List, Set

import pytest

from config.settings import Settings, get_settings


# ---------------------------------------------------------------------------
# The flags exist and default sanely
# ---------------------------------------------------------------------------


def test_both_master_flags_exist() -> None:
    """Neither module may lose its master switch."""
    fields = Settings.model_fields
    assert "commerce_backbone_enabled" in fields
    assert "compliance_backbone_enabled" in fields


def test_compliance_defaults_on_for_backward_compatibility() -> None:
    """Default True so existing deployments and endpoint tests are unaffected.

    The MVP pilot turns it off in ``.env.production`` rather than by changing
    this default, so a deployment that has not opted in keeps its endpoints.
    """
    assert Settings.model_fields["compliance_backbone_enabled"].default is True


# ---------------------------------------------------------------------------
# Registration honours the flags
# ---------------------------------------------------------------------------


#: ``main.py`` source, parsed rather than executed. Re-importing ``main`` to
#: flip a flag is not viable: module import registers SuperTokens, auth
#: middleware, and the voice self-check, so reloading it repeatedly is both
#: slow and stateful. The regression being guarded is structural — a router
#: sitting in the unconditional tuple instead of inside a flag block — so the
#: source is the right thing to assert on.
_BACKEND = Path(__file__).resolve().parents[2]
_MAIN_SRC = (_BACKEND / "main.py").read_text(encoding="utf-8")

#: Compliance registration was moved out of ``main.py`` into bootstrap to stay
#: under main.py's line-count ceiling, which is a ratchet meant to go down.
_COMPLIANCE_SRC = (
    _BACKEND / "bootstrap" / "compliance_routers.py"
).read_text(encoding="utf-8")

#: The unconditional registration loop: `for _router in ( ... ):`
_UNCONDITIONAL = _MAIN_SRC[
    _MAIN_SRC.index("for _router in ("): _MAIN_SRC.index("    app.include_router(_router)")
]


def _routers_named(prefix: str) -> Set[str]:
    return set(re.findall(rf"\b({prefix}_[a-z_]*router)\b", _MAIN_SRC))


def _block_after(flag: str) -> str:
    """Source from the flag check to the end of its try/except."""
    i = _MAIN_SRC.index(flag)
    tail = _MAIN_SRC[i:]
    stop = tail.find("\nexcept Exception")
    return tail[: stop if stop != -1 else len(tail)]


def test_every_commerce_router_is_registered_behind_its_master_flag() -> None:
    """No ``{prefix}_*_router`` may be in the unconditional registration loop.

    This is the exact shape of the bug that was found: both
    ``commerce_price_protection_router`` and ``commerce_pricing_rules_router``
    were listed in the unconditional tuple, so they served traffic while the
    master flag was off and its description claimed otherwise.
    """
    routers = _routers_named("commerce")
    assert routers, "no commerce_*_router references found in main.py"

    escaping = sorted(r for r in routers if r in _UNCONDITIONAL)
    assert not escaping, (
        f"{escaping} are registered unconditionally and therefore ignore "
        f"commerce_backbone_enabled. Move them inside the flag-guarded block "
        f"so the flag means what its description says."
    )


def test_no_compliance_router_is_registered_in_main() -> None:
    """Compliance registration must stay out of main.py.

    main.py has a line-count ceiling that ratchets down, and re-adding the
    eight imports there would push it back over.
    """
    leaked = sorted(_routers_named("compliance"))
    assert not leaked, (
        f"{leaked} referenced in main.py; compliance routers belong to "
        f"bootstrap/compliance_routers.py"
    )


def test_all_commerce_routers_appear_inside_the_flag_block() -> None:
    """Every router for the module is actually included when the flag is on.

    Guards the opposite mistake from the test above: removing a router from
    the unconditional loop without adding it to the guarded block would
    silently drop the endpoint entirely.
    """
    block = _block_after("commerce_backbone_enabled")
    missing = sorted(r for r in _routers_named("commerce") if r not in block)
    assert not missing, (
        f"{missing} are imported but never registered inside the "
        f"commerce_backbone_enabled block, so those endpoints do not exist "
        f"even when the flag is on."
    )


def test_compliance_module_registers_all_eight_endpoint_modules() -> None:
    """Pin the compliance surface so a router cannot be quietly dropped."""
    for module in (
        "tax_endpoints",
        "terminal_bol_endpoints",
        "ifta_endpoints",
        "kfactor_endpoints",
        "driver_endpoints",
        "asset_certification_endpoints",
        "meter_endpoints",
        "asset_compliance_endpoints",
    ):
        assert module in _COMPLIANCE_SRC, (
            f"compliance.api.{module} is not registered by "
            f"bootstrap/compliance_routers.py"
        )


def test_compliance_registration_is_flag_guarded() -> None:
    """The bootstrap module must actually consult the flag."""
    assert "compliance_backbone_enabled" in _COMPLIANCE_SRC


# ---------------------------------------------------------------------------
# The services the pipeline depends on stay importable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path,symbol,used_by",
    [
        (
            "compliance.services.dyed_diesel_enforcer",
            "DyedDieselEnforcer",
            "compartment_loading_agent + invoice_service (IRS dyed-fuel rules)",
        ),
        (
            "compliance.services.vcf_calculator",
            "VCFCalculator",
            "reconciliation_service (temperature correction on billed volume)",
        ),
        (
            "compliance.services.hos_checker",
            "HOSStatus",
            "driver hos_advisory_service",
        ),
    ],
)
def test_pipeline_compliance_services_are_not_gated(
    module_path: str, symbol: str, used_by: str
) -> None:
    """These must remain importable regardless of the REST flag.

    They are reached by direct import from pipeline code, not through a
    router, so the master flag does not and must not affect them.
    """
    module = importlib.import_module(module_path)
    assert hasattr(module, symbol), (
        f"{module_path}.{symbol} is missing — it is used by {used_by}. "
        f"compliance_backbone_enabled gates the REST surface only; removing "
        f"or gating this service breaks the delivery pipeline."
    )


def test_delivery_filter_is_importable() -> None:
    """``route_planning_agent`` imports this lazily inside a method."""
    module = importlib.import_module("compliance.services.delivery_filter")
    assert module is not None
