"""Drift guard: the gallon/litre factor has exactly one definition.

Context. The backend converts between US gallons and litres in several places
because the two units are both canonical in different layers — gallons on every
driver-facing and billing contract, litres inside ``mvp_plan_executions`` and
the compartment solver (whose densities are kg/L). That split is not itself a
defect; a *second definition of the factor* would be, because the two copies
can drift silently and every volume downstream of the drifted one is wrong.

Before this guard there were six independent literal declarations of
``3.785411784`` in the backend, one of them a variable local to a function and
two of them bare literals inline. They all happened to agree, and one carried a
comment instructing humans to "keep in sync" by hand. This module removes the
possibility rather than the coincidence:

* :func:`test_every_alias_binds_to_the_platform_factor` pins each surviving
  name to :data:`services.unit_conversion.GAL_TO_L` by value.
* :func:`test_no_module_redeclares_the_factor_literal` walks the AST of every
  backend module and fails if the literal reappears anywhere outside the single
  definition site. AST-based scanning means comments and docstrings that
  *mention* the number do not trip the guard — only real numeric constants do.
* :func:`test_frontend_constant_matches_backend` pins the web copy, which lives
  in TypeScript and is therefore outside the reach of the AST scan.

The exact value is the US liquid gallon per NIST Handbook 44 and is not a
tunable: 1 gal = 3.785411784 L by definition, so a test asserting the literal
is pinning a physical definition, not duplicating a business rule.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from Agents.overlay.compartment_loading_agent import GALLONS_TO_LITERS
from Agents.overlay.route_planning_agent import LITERS_PER_GALLON
from Agents.support.volume_units import LITERS_PER_US_GALLON
from integrations.veeder_root import _GAL_TO_L
from services.unit_conversion import GAL_TO_L

#: The exact US-liquid-gallon definition (NIST Handbook 44).
NIST_GAL_TO_L = 3.785411784

#: Repository root, derived from this file's location rather than a CWD
#: assumption so the test passes under any pytest invocation directory.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent

#: The one file permitted to contain the literal: the definition site.
_DEFINITION_SITE = _BACKEND_ROOT / "services" / "unit_conversion.py"

#: Directories excluded from the AST scan. ``tests`` is excluded because tests
#: legitimately assert against the literal (including this file). ``venv`` and
#: caches are third-party or generated.
_EXCLUDED_DIRS = frozenset(
    {"venv", ".venv", "tests", "__pycache__", ".git", "node_modules"}
)

#: Seed/fixture modules that embed the factor in illustrative sample data
#: rather than in a conversion. Empty by design — every entry added here is a
#: place the guard is knowingly not protecting, so it must carry a reason.
_ALLOWLIST: dict[str, str] = {}


def _backend_modules() -> Iterator[Path]:
    """Yield every backend ``.py`` file outside the excluded directories."""
    for path in _BACKEND_ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(_BACKEND_ROOT).parts):
            continue
        yield path


def _factor_literals(path: Path) -> List[Tuple[int, float]]:
    """Return ``(lineno, value)`` for each numeric constant equal to the factor.

    Uses :mod:`ast` so that comments and docstrings mentioning the number are
    ignored — only genuine numeric constants are reported. Both the factor and
    its reciprocal are treated as redeclarations, since dividing by a hardcoded
    0.264172052... is the same defect wearing a different hat.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
        pytest.fail(f"could not parse {path}: {exc}")

    reciprocal = 1.0 / NIST_GAL_TO_L
    found: List[Tuple[int, float]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        # bool is a subclass of int; exclude it explicitly.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        as_float = float(value)
        if as_float == NIST_GAL_TO_L or abs(as_float - reciprocal) < 1e-12:
            found.append((node.lineno, as_float))
    return found


# ---------------------------------------------------------------------------
# The aliases
# ---------------------------------------------------------------------------


def test_the_platform_factor_is_the_nist_definition() -> None:
    """``GAL_TO_L`` is the exact NIST value, not an approximation."""
    assert GAL_TO_L == NIST_GAL_TO_L


@pytest.mark.parametrize(
    "alias_name, alias_value",
    [
        ("Agents.support.volume_units.LITERS_PER_US_GALLON", LITERS_PER_US_GALLON),
        (
            "Agents.overlay.compartment_loading_agent.GALLONS_TO_LITERS",
            GALLONS_TO_LITERS,
        ),
        ("Agents.overlay.route_planning_agent.LITERS_PER_GALLON", LITERS_PER_GALLON),
        ("integrations.veeder_root._GAL_TO_L", _GAL_TO_L),
    ],
)
def test_every_alias_binds_to_the_platform_factor(
    alias_name: str, alias_value: float
) -> None:
    """Each module-level name for the factor is a binding, not a redeclaration.

    Equality here is exact, not approximate. A binding to ``GAL_TO_L`` is
    bit-identical by construction; a re-typed literal that differs in the last
    place would still pass an ``approx`` comparison, which is exactly the drift
    this guard exists to catch.
    """
    assert alias_value == GAL_TO_L, (
        f"{alias_name} = {alias_value!r} does not equal "
        f"services.unit_conversion.GAL_TO_L = {GAL_TO_L!r}. Bind it to "
        f"GAL_TO_L instead of declaring its own value."
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def test_the_definition_site_is_the_only_declaration() -> None:
    """``services/unit_conversion.py`` really does declare the factor.

    Guards the scan itself: if the definition ever moved, the exclusion below
    would silently stop excluding anything meaningful and the whole guard would
    pass vacuously.
    """
    assert _DEFINITION_SITE.is_file()
    literals = _factor_literals(_DEFINITION_SITE)
    assert literals, (
        f"{_DEFINITION_SITE} no longer contains the factor literal — the "
        f"definition moved, so this test's exclusion list is stale."
    )


def test_no_module_redeclares_the_factor_literal() -> None:
    """No backend module outside the definition site hardcodes the factor.

    This is the guard that actually prevents regression. Reintroducing a
    literal — as a module constant, a function-local variable, or inline in an
    expression — fails here with the file and line, and the fix is always the
    same: import ``GAL_TO_L``.
    """
    offenders: List[str] = []
    for path in _backend_modules():
        if path == _DEFINITION_SITE:
            continue
        rel = str(path.relative_to(_BACKEND_ROOT))
        if rel in _ALLOWLIST:
            continue
        for lineno, value in _factor_literals(path):
            offenders.append(f"{rel}:{lineno} declares {value!r}")

    assert not offenders, (
        "The gallon/litre factor must have exactly one definition, "
        "services.unit_conversion.GAL_TO_L. These sites redeclare it:\n  "
        + "\n  ".join(offenders)
        + "\n\nImport GAL_TO_L instead. If a site genuinely cannot import it, "
        "add it to _ALLOWLIST with a reason."
    )


# ---------------------------------------------------------------------------
# The frontend copy
# ---------------------------------------------------------------------------


def test_frontend_constant_matches_backend() -> None:
    """The web copy of the factor agrees with the backend.

    ``runsheet/src/services/fuelApi.ts`` converts gallons to litres on the wire
    for the tank/compartment endpoints, so a divergence there would corrupt
    volumes without any backend test noticing. TypeScript is outside the AST
    scan's reach, so the value is pinned by parsing the declaration. The
    frontend comment asserting that "a backend unit test asserts they agree"
    was untrue until this test existed.
    """
    fuel_api = _REPO_ROOT / "runsheet" / "src" / "services" / "fuelApi.ts"
    if not fuel_api.is_file():
        pytest.skip(f"web frontend not present at {fuel_api}")

    match = re.search(
        r"export\s+const\s+LITERS_PER_GALLON\s*=\s*([0-9.]+)\s*;",
        fuel_api.read_text(encoding="utf-8"),
    )
    assert match is not None, (
        f"could not find 'export const LITERS_PER_GALLON = ...' in {fuel_api}. "
        f"If it was renamed or removed, update this guard."
    )

    frontend_value = float(match.group(1))
    assert frontend_value == GAL_TO_L, (
        f"{fuel_api} declares LITERS_PER_GALLON = {frontend_value!r} but the "
        f"backend GAL_TO_L = {GAL_TO_L!r}. Volumes crossing this boundary "
        f"would be wrong by a factor of {frontend_value / GAL_TO_L!r}."
    )


def test_no_frontend_module_redeclares_the_factor() -> None:
    """``fuelApi.ts`` is the only TypeScript declaration of the factor.

    The sibling test above pins ``fuelApi.ts`` but says nothing about the rest
    of the frontend, which is how ``FuelConsumptionChart.tsx`` came to declare
    its own private copy despite ``fuelApi.ts`` already exporting one two
    directories away. This closes that gap: the AST scan covers the backend,
    and this covers the web app.

    Scanned textually rather than by parsing TypeScript — a regex over numeric
    literals is enough, because the only way the factor can appear is as a
    literal, and a false positive here is a genuine redeclaration by
    definition.
    """
    web_root = _REPO_ROOT / "runsheet" / "src"
    if not web_root.is_dir():
        pytest.skip(f"web frontend not present at {web_root}")

    # The factor family: any literal beginning 3.78, so a truncated copy such
    # as 3.7854 is caught alongside an exact one.
    factor_re = re.compile(r"(?<![\w.])3\.78\d*")
    allowed = {web_root / "services" / "fuelApi.ts"}

    offenders: List[str] = []
    for path in sorted(web_root.rglob("*.ts")) + sorted(web_root.rglob("*.tsx")):
        if path in allowed or "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for hit in factor_re.finditer(line):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno} declares {hit.group(0)}"
                )

    assert not offenders, (
        "The gallon/litre factor must have one frontend declaration, the "
        "LITERS_PER_GALLON exported from services/fuelApi.ts. These sites "
        "redeclare it:\n  "
        + "\n  ".join(offenders)
        + "\n\nImport it instead: "
        "`import { LITERS_PER_GALLON } from \"<path>/services/fuelApi\";`"
    )
