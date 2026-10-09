"""Source scan: no ``float`` arithmetic on money in the margin modules (NFR1).

Scans ``commerce/models/margin.py`` and every ``commerce/services/margin_*.py``
with ``ast``:

* no call to ``float(...)``;
* no ``/`` or ``//`` whose operands mention a money name (cents, micros, usd,
  cost, margin, price, revenue, gallons, ugal, milli, bp).

The only allowed exception is a line marked ``# margin: decimal-division``
(the documented ``Decimal`` divisions) or ``# margin: float-ok`` (a value
handed to an external float API, never used for margin maths). ``Decimal(str(x))``
needs no marker because it is not a float operation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[4]
_MONEY_TOKENS = (
    "cents",
    "micros",
    "usd",
    "cost",
    "margin",
    "price",
    "revenue",
    "gallon",
    "ugal",
    "milli",
    "bp",
)
_DIVISION_MARK = "# margin: decimal-division"
_FLOAT_MARK = "# margin: float-ok"


def _modules() -> list[Path]:
    files = [_BACKEND / "commerce" / "models" / "margin.py"]
    files += sorted((_BACKEND / "commerce" / "services").glob("margin_*.py"))
    return files


def _names(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id.lower())
        elif isinstance(child, ast.Attribute):
            found.add(child.attr.lower())
        elif isinstance(child, ast.arg):
            found.add(child.arg.lower())
    return found


def _is_money(node: ast.AST) -> bool:
    return any(token in name for name in _names(node) for token in _MONEY_TOKENS)


def _violations(path: Path) -> list[str]:
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    problems: list[str] = []

    def marked(node: ast.AST, mark: str) -> bool:
        end = getattr(node, "end_lineno", node.lineno)
        return any(mark in lines[i - 1] for i in range(node.lineno, end + 1))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
            and not marked(node, _FLOAT_MARK)
        ):
            problems.append(f"{path.name}:{node.lineno} float(...)")
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, (ast.Div, ast.FloorDiv))
            and (_is_money(node.left) or _is_money(node.right))
            and not marked(node, _DIVISION_MARK)
        ):
            problems.append(f"{path.name}:{node.lineno} division on money")
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.op, (ast.Div, ast.FloorDiv))
            and (_is_money(node.target) or _is_money(node.value))
            and not marked(node, _DIVISION_MARK)
        ):
            problems.append(f"{path.name}:{node.lineno} augmented division on money")
    return problems


def test_scan_covers_the_margin_modules():
    names = {p.name for p in _modules()}
    assert "margin.py" in names
    assert "margin_repository.py" in names
    for path in _modules():
        assert path.is_file(), path


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_float_arithmetic_on_money(path):
    assert _violations(path) == []


def test_documented_divisions_are_decimal():
    """Every marked division in margin.py divides ``Decimal`` operands."""

    path = _BACKEND / "commerce" / "models" / "margin.py"
    lines = path.read_text().splitlines()
    # Marked code lines (the module docstring also names the marker in prose).
    marked = [
        line.split(_DIVISION_MARK, 1)[0]
        for line in lines
        if _DIVISION_MARK in line and "/" in line.split(_DIVISION_MARK, 1)[0]
    ]
    assert len(marked) == 2, "margin.py documents exactly two Decimal divisions"
    for code in marked:
        assert "Decimal(" in code.split("/", 1)[0] and "Decimal(" in code.split("/", 1)[1], code


def test_scanner_catches_violations(tmp_path):
    bad = tmp_path / "margin_bad.py"
    bad.write_text(
        "def f(cost_cents, gallons):\n"
        "    a = float(cost_cents)\n"
        "    b = cost_cents / gallons\n"
        "    c = cost_cents // 3\n"
        "    cost_cents /= 2\n"
        "    ok = 10 / 2\n"
        "    return a, b, c, ok\n"
    )
    assert len(_violations(bad)) == 4
