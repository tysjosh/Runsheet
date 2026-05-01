"""
Test verifying main.py line count does not exceed 200 lines.

The count excludes blank lines, comments, and import statements,
matching the requirement specification.

Validates: Requirement 1.6, Correctness Property P13
"""
import os

import pytest


class TestMainPyLineCount:
    """Verify main.py stays within the 200-line budget."""

    def test_main_py_total_lines_under_200(self):
        """Total lines of main.py should not exceed 200."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "..", "main.py")
        main_path = os.path.abspath(main_path)

        with open(main_path) as f:
            total_lines = len(f.readlines())

        assert total_lines <= 200, (
            f"main.py has {total_lines} total lines, exceeding the 200-line limit"
        )

    def test_main_py_code_lines_under_200(self):
        """Code lines (excluding imports, comments, blank) should be ≤200."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "..", "main.py")
        main_path = os.path.abspath(main_path)

        with open(main_path) as f:
            lines = f.readlines()

        code_lines = 0
        in_docstring = False
        for line in lines:
            stripped = line.strip()

            # Track multi-line docstrings
            if '"""' in stripped:
                count = stripped.count('"""')
                if count == 2:
                    # Single-line docstring — skip it
                    continue
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue

            # Skip blank lines
            if not stripped:
                continue
            # Skip comments
            if stripped.startswith("#"):
                continue
            # Skip imports
            if stripped.startswith("import ") or stripped.startswith("from "):
                continue

            code_lines += 1

        assert code_lines <= 200, (
            f"main.py has {code_lines} code lines (excl imports/comments/blank), "
            f"exceeding the 200-line limit"
        )
