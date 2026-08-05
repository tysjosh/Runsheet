"""Static contracts for the agent tool layer.

Two defects reached a live LLM surface and were only found by a soak run driving
real conversations. Both were statically detectable, and these are the checks
that detect them.

**A tool searched an index that does not exist.** ``search_tools.search_orders``
queried ``orders``; live orders are in ``fuel_orders_current``. It returned no
data and, because it also reported ``len(page)`` as a count, contributed to a
reply that claimed two different order totals in one answer. Nothing failed —
the tool caught its own exception and returned a friendly string.

**Two tools shared one name.** ``search_orders`` existed in both
``search_tools`` and ``order_tools``, reaching different data. The package
exported one, the ops specialist imported the other directly, and ``mainagent``'s
prompt documented the second one's signature while ``ALL_TOOLS`` handed over the
first.

These tests are cheap and they move both classes from "found by a twelve-hour
soak" to "found by CI".
"""
from __future__ import annotations

import inspect
import re

import pytest

from Agents.tools import ALL_TOOLS


def _tool_name(tool) -> str:
    for attr in ("tool_name", "__name__"):
        value = getattr(tool, attr, None)
        if isinstance(value, str):
            return value
    inner = getattr(tool, "_tool_func", None) or getattr(tool, "func", None)
    if inner is not None and hasattr(inner, "__name__"):
        return inner.__name__
    return str(tool)


class TestToolNamesAreUnambiguous:
    def test_no_duplicate_tool_names_in_all_tools(self):
        """ALL_TOOLS is handed to the model; one name must mean one thing."""
        names = [_tool_name(t) for t in ALL_TOOLS]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, (
            f"duplicate tool names presented to the model: {duplicates}. "
            "Two tools of the same name reaching different data is how one reply "
            "reported two different counts for the same question."
        )

    def test_search_orders_resolves_to_the_live_index_implementation(self):
        """The surviving implementation must be the one on fuel_orders_current."""
        from Agents.tools import search_orders

        module = getattr(search_orders, "__module__", "")
        if not module:
            inner = getattr(search_orders, "_tool_func", None) or getattr(
                search_orders, "func", None
            )
            module = getattr(inner, "__module__", "")
        assert module.endswith("order_tools"), (
            f"search_orders resolves to {module!r}; it must be order_tools, the "
            "implementation that queries fuel_orders_current and returns a true "
            "total"
        )

    def test_the_dead_search_tools_implementation_is_gone(self):
        import Agents.tools.search_tools as search_tools

        assert not hasattr(search_tools, "search_orders"), (
            "search_tools.search_orders is back. It queries the non-existent "
            "'orders' index, caps at 5, and reports len(page) as a count."
        )


class TestToolsDoNotReferenceUnknownIndices:
    """Every index a tool searches must be one this platform actually creates.

    The dead ``orders`` reference survived a rename precisely because no test
    compared tool source against the index inventory.
    """

    #: Indices the platform declares. Sourced from the mapping modules rather
    #: than restated, so this cannot drift as indices are added.
    @staticmethod
    def _known_indices() -> set[str]:
        known: set[str] = set()
        modules = [
            "services.order_es_mappings",
            "fuel.services.fuel_ops_es_mappings",
            "fuel.services.order_es_mappings",
            "Agents.agent_es_mappings",
            "Agents.overlay.overlay_es_mappings",
            "Agents.support.mvp_es_mappings",
            "fuel.voice.voice_es_mappings",
        ]
        for name in modules:
            try:
                module = __import__(name, fromlist=["*"])
            except Exception:
                continue
            for attr, value in vars(module).items():
                if attr.endswith("_INDEX") and isinstance(value, str):
                    known.add(value)
                if attr.endswith("_INDICES") and isinstance(value, (list, tuple, set, frozenset)):
                    known.update(v for v in value if isinstance(v, str))
        return known

    #: Indices that exist but are declared elsewhere (legacy or created ad hoc).
    #: Explicit so the check stays meaningful rather than being widened silently.
    ALLOWED_EXTRA = {
        "trucks",
        "locations",
        "inventory",
        "inventory_events",
        "support_tickets",
        "analytics_events",
        "drivers",
        "drivers_current",
        "riders_current",
        "shipments_current",
        "jobs_current",
        "fuel_stations",
        "fuel_events",
        "customer_tanks",
        "restock_requests",
        "assets",
        "import_sessions",
        "import_sessions_active",
        "weather_observations",
        "weather_alerts",
        "storm_mode_overrides",
        "integration_instances",
        "invoices_current",
        "proof_of_delivery",
    }

    def test_every_index_named_in_a_tool_is_known(self):
        import Agents.tools as tools_pkg

        known = self._known_indices() | self.ALLOWED_EXTRA
        assert known, "could not resolve any declared index names"

        # Matches the two ES entry points the tools use, capturing the literal
        # index argument: search_documents("x", ...) and
        # semantic_search(tenant, "x", ...).
        patterns = (
            re.compile(r"search_documents\(\s*[\"']([a-z0-9_\-]+)[\"']"),
            re.compile(r"semantic_search\(\s*[^,]+,\s*[\"']([a-z0-9_\-]+)[\"']"),
        )

        offenders: list[str] = []
        package_dir = tools_pkg.__path__[0]
        import pathlib

        for path in sorted(pathlib.Path(package_dir).glob("*.py")):
            source = path.read_text()
            for pattern in patterns:
                for index in pattern.findall(source):
                    if index not in known:
                        offenders.append(f"{path.name}: {index!r}")

        assert not offenders, (
            "agent tools reference indices this platform does not declare — a "
            f"renamed or deleted index will silently return nothing: {offenders}"
        )


class TestCappedResultsAreNotReportedAsTotals:
    """A page length phrased as a count is a wrong number stated confidently."""

    def test_no_tool_says_found_len_results(self):
        import pathlib

        import Agents.tools as tools_pkg

        pattern = re.compile(r"[Ff]ound \{len\(results\)\}")
        offenders = []
        for path in sorted(pathlib.Path(tools_pkg.__path__[0]).glob("*.py")):
            if pattern.search(path.read_text()):
                offenders.append(path.name)

        assert not offenders, (
            "these tools phrase a capped page as a total, which invites the model "
            f"to report a count it never measured: {offenders}. Say 'Showing N' "
            "or return the real total alongside."
        )


class TestRunoutRiskToolIsReachable:
    """The market's central question must be answerable by a specialist."""

    def test_fuel_specialist_carries_the_runout_tool(self):
        from Agents.specialists.fuel_agent import FuelAgent

        # Compare by name: a strands DecoratedFunctionTool has an opaque repr, so
        # an identity assertion fails with a wall of object addresses.
        names = sorted(_tool_name(t) for t in FuelAgent.TOOLS)
        assert "get_runout_risk_list" in names, (
            "the fuel specialist cannot read tank run-out risk, so the agent will "
            "again answer 'I am unable to identify the specific tanks most at "
            f"risk' while mvp_tank_forecasts holds thousands of forecasts. Its "
            f"tools are: {names}"
        )

    def test_the_specialist_prompt_tells_the_model_the_tool_exists(self):
        """Carrying a tool is not the same as knowing to reach for it.

        The disclaimers came from a model working off a prompt that enumerated
        its tools; an unlisted tool invites "I am unable to" even when it is
        bound.
        """
        from Agents.specialists.fuel_agent import FuelAgent

        prompt = FuelAgent.SYSTEM_PROMPT
        assert "get_runout_risk_list" in prompt, (
            "the fuel specialist's prompt enumerates its tools and omits the "
            "run-out tool, so the model is unlikely to call it"
        )
        # And the two counts must be distinguished, or the model will report the
        # page length as the answer.
        assert "total_at_risk" in prompt and "shown" in prompt, prompt

    def test_it_is_exported_and_documented(self):
        from Agents.tools.tank_forecast_tools import (
            FORECAST_INDEX,
            get_runout_risk_list,
        )

        assert FORECAST_INDEX == "mvp_tank_forecasts"
        inner = getattr(get_runout_risk_list, "_tool_func", None) or get_runout_risk_list
        doc = inspect.getdoc(inner) or ""
        # The model routes on the docstring, so it must name the question.
        assert "run out" in doc.lower() or "runout" in doc.lower()
