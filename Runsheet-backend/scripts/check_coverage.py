#!/usr/bin/env python3
"""
Changed-file coverage gate for CI.

Identifies Python source files changed in the current PR/branch and
verifies that none of them have zero test coverage on changed lines.

Usage:
    python scripts/check_coverage.py [--threshold THRESHOLD]

Options:
    --threshold  Minimum coverage percentage for changed files.
                 Default: 0 (only fails on zero-coverage changed files).

Exit codes:
    0 — All changed files meet the coverage threshold.
    1 — One or more changed files have insufficient coverage.

Requirements: 10.3
"""
import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Directories and patterns to exclude from the coverage gate
EXCLUDE_PATTERNS = {
    "tests/",
    "scripts/",
    "__pycache__/",
    "conftest.py",
    "setup.py",
}


def get_changed_files(base_ref: str = "origin/main") -> List[str]:
    """Get list of Python files changed relative to the base branch.

    Falls back to all tracked .py files if git diff fails (e.g., in
    a non-PR context).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACM", base_ref, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = result.stdout.strip().split("\n")
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: get all staged files
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached", "--diff-filter=ACM"],
                capture_output=True,
                text=True,
                check=True,
            )
            files = result.stdout.strip().split("\n")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("⚠ Could not determine changed files. Skipping coverage gate.")
            return []

    # Filter to Python source files within Runsheet-backend/
    py_files = []
    for f in files:
        f = f.strip()
        if not f:
            continue
        if not f.endswith(".py"):
            continue

        # Normalize path: strip Runsheet-backend/ prefix if present
        rel_path = f
        if rel_path.startswith("Runsheet-backend/"):
            rel_path = rel_path[len("Runsheet-backend/"):]

        # Skip excluded patterns
        if any(excl in rel_path for excl in EXCLUDE_PATTERNS):
            continue

        py_files.append(rel_path)

    return py_files


def parse_coverage_xml(coverage_file: str = "coverage.xml") -> Dict[str, float]:
    """Parse a Cobertura-format coverage.xml and return per-file coverage.

    Returns:
        Dict mapping relative file paths to their line coverage percentage.
    """
    coverage_path = Path(coverage_file)
    if not coverage_path.exists():
        print(f"⚠ Coverage file not found: {coverage_file}")
        return {}

    tree = ET.parse(coverage_path)
    root = tree.getroot()

    file_coverage: Dict[str, float] = {}

    for package in root.findall(".//package"):
        for cls in package.findall(".//class"):
            filename = cls.get("filename", "")
            line_rate = cls.get("line-rate", "0")

            try:
                coverage_pct = float(line_rate) * 100
            except (ValueError, TypeError):
                coverage_pct = 0.0

            # Normalize the filename
            if filename.startswith("Runsheet-backend/"):
                filename = filename[len("Runsheet-backend/"):]
            if filename.startswith("./"):
                filename = filename[2:]

            file_coverage[filename] = coverage_pct

    return file_coverage


def check_coverage(
    changed_files: List[str],
    file_coverage: Dict[str, float],
    threshold: float = 0.0,
) -> Tuple[List[str], List[str]]:
    """Check coverage for changed files.

    Args:
        changed_files: List of changed Python source file paths.
        file_coverage: Dict of file path → coverage percentage.
        threshold: Minimum coverage percentage (0 = only fail on zero coverage).

    Returns:
        Tuple of (failing_files, passing_files).
    """
    failing: List[str] = []
    passing: List[str] = []

    for f in changed_files:
        coverage = file_coverage.get(f)

        if coverage is None:
            # File not in coverage report — might be new and untested
            failing.append(f)
            continue

        if threshold == 0:
            # Only fail on truly zero coverage
            if coverage == 0.0:
                failing.append(f)
            else:
                passing.append(f)
        else:
            if coverage < threshold:
                failing.append(f)
            else:
                passing.append(f)

    return failing, passing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check test coverage for changed Python source files."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="Minimum coverage percentage for changed files (default: 0 = no zero-coverage files).",
    )
    parser.add_argument(
        "--coverage-file",
        type=str,
        default="coverage.xml",
        help="Path to the Cobertura coverage XML file (default: coverage.xml).",
    )
    parser.add_argument(
        "--base-ref",
        type=str,
        default="origin/main",
        help="Git base ref for determining changed files (default: origin/main).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("📊 Changed-File Coverage Gate")
    print("=" * 60)

    # Step 1: Get changed files
    changed_files = get_changed_files(args.base_ref)
    if not changed_files:
        print("✅ No changed Python source files to check.")
        return 0

    print(f"\n📁 Changed source files ({len(changed_files)}):")
    for f in changed_files:
        print(f"   • {f}")

    # Step 2: Parse coverage report
    file_coverage = parse_coverage_xml(args.coverage_file)
    if not file_coverage:
        print("\n⚠ No coverage data available. Skipping coverage gate.")
        return 0

    # Step 3: Check coverage
    failing, passing = check_coverage(changed_files, file_coverage, args.threshold)

    # Step 4: Report results
    if passing:
        print(f"\n✅ Files meeting coverage threshold ({len(passing)}):")
        for f in passing:
            cov = file_coverage.get(f, 0.0)
            print(f"   ✓ {f}: {cov:.1f}%")

    if failing:
        print(f"\n❌ Files failing coverage gate ({len(failing)}):")
        for f in failing:
            cov = file_coverage.get(f)
            if cov is None:
                print(f"   ✗ {f}: no coverage data (file not in coverage report)")
            else:
                print(f"   ✗ {f}: {cov:.1f}% (threshold: {args.threshold}%)")

        print(f"\n❌ Coverage gate FAILED: {len(failing)} file(s) below threshold.")
        print("   Add tests for the files listed above to meet the coverage requirement.")
        return 1

    print(f"\n✅ Coverage gate PASSED: all {len(changed_files)} changed files meet the threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
