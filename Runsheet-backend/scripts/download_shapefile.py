"""Download and extract US Census TIGER state boundary shapefile.

This script downloads the US Census Bureau's Cartographic Boundary File
(2022, 1:500k resolution) and extracts it into the ``data/`` directory
with standardized filenames expected by the StateBoundaryDetector.

Source: US Census Bureau (public domain)
URL: https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_state_500k.zip

Usage:
    python scripts/download_shapefile.py

    Run from the Runsheet-backend/ directory. The script will create the
    data/ directory if it doesn't exist.

Options:
    --output-dir PATH   Override the output directory (default: data/)
    --force             Re-download even if files already exist
    --verify-only       Check if shapefile is present and valid without downloading
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOWNLOAD_URL = (
    "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_state_500k.zip"
)

# Source filename prefix inside the ZIP
SOURCE_PREFIX = "cb_2022_us_state_500k"

# Target filename prefix in the output directory
TARGET_PREFIX = "us_states"

# Required shapefile extensions
REQUIRED_EXTENSIONS = [".shp", ".shx", ".dbf", ".prj"]

# Optional extensions (nice to have but not required)
OPTIONAL_EXTENSIONS = [".cpg", ".shp.xml"]

# Expected field in the .dbf that StateBoundaryDetector uses
EXPECTED_STATE_FIELD = "STUSPS"


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def get_project_root() -> Path:
    """Determine the project root (Runsheet-backend/) directory."""
    # Script is at scripts/download_shapefile.py
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent


def download_file(url: str, dest: Path) -> None:
    """Download a file from a URL with progress reporting."""
    logger.info("Downloading %s ...", url)

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Runsheet-Backend/1.0 (shapefile-download)"},
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            total_size = response.headers.get("Content-Length")
            if total_size:
                total_size = int(total_size)
                logger.info("File size: %.1f MB", total_size / (1024 * 1024))

            with open(dest, "wb") as f:
                downloaded = 0
                chunk_size = 64 * 1024  # 64 KB chunks
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = (downloaded / total_size) * 100
                        print(
                            f"\r  Progress: {downloaded:,} / {total_size:,} bytes ({pct:.1f}%)",
                            end="",
                            flush=True,
                        )

            if total_size:
                print()  # newline after progress

        logger.info("Download complete: %s", dest)

    except urllib.error.URLError as exc:
        logger.error("Failed to download %s: %s", url, exc)
        raise SystemExit(1) from exc


def extract_and_rename(zip_path: Path, output_dir: Path) -> None:
    """Extract shapefile components from ZIP and rename to target prefix."""
    logger.info("Extracting shapefile components...")

    output_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.namelist()
        logger.info("ZIP contains %d files", len(members))

        extracted_count = 0
        for ext in REQUIRED_EXTENSIONS + OPTIONAL_EXTENSIONS:
            source_name = f"{SOURCE_PREFIX}{ext}"
            target_name = f"{TARGET_PREFIX}{ext}"

            # Handle case where files might be in a subdirectory
            matching = [m for m in members if m.endswith(source_name)]
            if not matching:
                # Try without prefix (some ZIPs have different naming)
                matching = [m for m in members if m.endswith(ext)]

            if matching:
                source_member = matching[0]
                target_path = output_dir / target_name

                # Extract to temp location then move
                with zf.open(source_member) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                logger.info("  %s → %s", source_member, target_name)
                extracted_count += 1
            elif ext in REQUIRED_EXTENSIONS:
                logger.error(
                    "  MISSING required file: *%s (looked for %s)",
                    ext,
                    source_name,
                )
                raise SystemExit(1)
            else:
                logger.debug("  Optional file not found: *%s", ext)

    logger.info("Extracted %d shapefile components to %s", extracted_count, output_dir)


def verify_shapefile(output_dir: Path) -> bool:
    """Verify that the shapefile is present and has the expected fields."""
    logger.info("Verifying shapefile...")

    # Check required files exist
    for ext in REQUIRED_EXTENSIONS:
        filepath = output_dir / f"{TARGET_PREFIX}{ext}"
        if not filepath.exists():
            logger.error("  Missing: %s", filepath)
            return False
        logger.info("  ✓ %s (%d bytes)", filepath.name, filepath.stat().st_size)

    # Try to verify the STUSPS field exists in the .dbf
    try:
        import shapefile as shp

        sf = shp.Reader(str(output_dir / f"{TARGET_PREFIX}.shp"))
        fields = [field[0] for field in sf.fields[1:]]

        if EXPECTED_STATE_FIELD in fields:
            logger.info("  ✓ Found expected field: %s", EXPECTED_STATE_FIELD)
        else:
            logger.warning(
                "  ⚠ Expected field '%s' not found. Available fields: %s",
                EXPECTED_STATE_FIELD,
                fields,
            )
            return False

        record_count = len(sf)
        logger.info("  ✓ Shapefile contains %d records (states/territories)", record_count)

    except ImportError:
        logger.info(
            "  ℹ pyshp not installed — skipping field verification. "
            "Install with: pip install pyshp"
        )

    except Exception as exc:
        logger.warning("  ⚠ Could not verify shapefile contents: %s", exc)
        return False

    logger.info("Shapefile verification passed!")
    return True


def main() -> None:
    """Main entry point for the download script."""
    parser = argparse.ArgumentParser(
        description="Download US Census TIGER state boundary shapefile"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: data/ relative to project root)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if files already exist",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify existing shapefile without downloading",
    )
    args = parser.parse_args()

    # Determine output directory
    project_root = get_project_root()
    output_dir = args.output_dir or (project_root / "data")

    logger.info("Project root: %s", project_root)
    logger.info("Output directory: %s", output_dir)

    # Verify-only mode
    if args.verify_only:
        success = verify_shapefile(output_dir)
        sys.exit(0 if success else 1)

    # Check if files already exist
    if not args.force:
        all_exist = all(
            (output_dir / f"{TARGET_PREFIX}{ext}").exists()
            for ext in REQUIRED_EXTENSIONS
        )
        if all_exist:
            logger.info("Shapefile already exists. Use --force to re-download.")
            verify_shapefile(output_dir)
            return

    # Download to a temporary file
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        download_file(DOWNLOAD_URL, tmp_path)
        extract_and_rename(tmp_path, output_dir)
    finally:
        # Clean up temp file
        if tmp_path.exists():
            tmp_path.unlink()

    # Verify the result
    verify_shapefile(output_dir)

    logger.info("Done! Shapefile is ready at %s", output_dir)


if __name__ == "__main__":
    main()
