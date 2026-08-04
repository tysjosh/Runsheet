"""Guard: the documented Surface A submission path matches the served one.

Three separate docstrings/comments — ``dinee_voice_bridge``, ``voice_models``,
and the self-check comment in ``main.py`` — named ``POST /voice/orders`` as the
Dinee voice submission endpoint. The app has never served that path. The
submission router carries no prefix and registers the bare ``/voice-intake``
specifically so Surface A stays distinct from the Surface B ``/voice`` prefix,
under which ``/voice/orders/...`` routes do exist but are all reads.

An integrator reading those docstrings would have posted signed orders to a 404.
Nothing caught it because a docstring is not executable, so this test makes the
claim executable: the path the router registers is the single source of truth,
and no production module may name a conflicting submission path.

Validates: Requirements 1.1, 9.1 (documented contract == served contract).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from fuel.voice.voice_submission_router import router as submission_router

#: The one true Surface A submission path.
SUBMISSION_PATH = "/voice-intake"

#: Production modules that describe the submission surface in prose.
_DOCUMENTING_MODULES = (
    "fuel/voice/dinee_voice_bridge.py",
    "fuel/voice/voice_models.py",
    "fuel/voice/voice_submission_router.py",
    "main.py",
)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Matches a claim that the submission verb+path is ``POST /voice/orders``.
#:
#: Deliberately does NOT match the legitimate Surface B reads
#: (``GET /voice/orders/lookup``, ``/voice/orders/{id}/status``, ``.../eta``):
#: those carry a trailing path segment, so the negative lookahead excludes them.
#:
#: ``{0,2}`` on the backticks, not ``?``: the first draft of this pattern used
#: ```` ``? ````, which requires ONE literal backtick and makes the second
#: optional. Real docstrings write ``(``POST /voice/orders``)`` — the backticks
#: sit *before* the verb, so nothing separates ``POST `` from the path and the
#: pattern matched nothing. The test passed against the very text it existed to
#: reject. Zero backticks must be allowed.
_WRONG_SUBMISSION_CLAIM = re.compile(r"POST\s+`{0,2}/voice/orders(?!/)")


class TestServedSubmissionPath:
    """The router registers exactly one submission route, at SUBMISSION_PATH."""

    def test_router_registers_the_bare_voice_intake_path(self):
        post_paths = sorted(
            route.path
            for route in submission_router.routes
            if "POST" in getattr(route, "methods", set())
        )
        assert post_paths == [SUBMISSION_PATH], (
            f"Surface A must serve exactly one POST route at {SUBMISSION_PATH!r}; "
            f"found {post_paths!r}"
        )

    def test_submission_path_is_not_under_the_surface_b_prefix(self):
        """``/voice-intake`` must not be nested under Surface B's ``/voice/``.

        Surface B is authenticated by a per-tenant API key via
        ``get_voice_tenant``; Surface A is authenticated by HMAC over the raw
        body. Collapsing the two prefixes would invite one auth policy to be
        applied to the other surface's routes.
        """
        assert not SUBMISSION_PATH.startswith("/voice/")


class TestDocumentedSubmissionPath:
    """No production module may document a submission path that isn't served."""

    @pytest.mark.parametrize("rel_path", _DOCUMENTING_MODULES)
    def test_module_does_not_claim_post_voice_orders(self, rel_path: str):
        text = (_BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
        offenders = _WRONG_SUBMISSION_CLAIM.findall(text)
        assert not offenders, (
            f"{rel_path} documents 'POST /voice/orders', which the app does not "
            f"serve. The submission path is {SUBMISSION_PATH!r}."
        )

    @pytest.mark.parametrize("rel_path", _DOCUMENTING_MODULES)
    def test_module_names_the_real_submission_path(self, rel_path: str):
        """Each documenting module must actually mention ``/voice-intake``.

        Guards the guard: without this, deleting every mention of the submission
        path from a docstring would satisfy the negative test above while
        leaving the module silent about the contract.
        """
        text = (_BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
        assert SUBMISSION_PATH in text, (
            f"{rel_path} describes the voice submission surface but never names "
            f"{SUBMISSION_PATH!r}"
        )
