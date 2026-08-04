#!/usr/bin/env bash
# =============================================================================
# Donor-value guard — fail on any donor build credential surviving in the app
# =============================================================================
#
# The driver app was scaffolded fresh, but six artifacts were copied from the
# donor `azumi-rider/` tree. None of the donor's four build-credential values may
# survive into `/driver-app`, and two of the replacements (the iOS bundle
# identifier and the Android application id) are immutable once a build carrying
# them is published to either store — so this check runs before any build step.
#
# The four donor values, all declared in `azumi-rider/app.json`:
#   1. EAS owner                 `parahack`                              (:5)
#   2. iOS bundleIdentifier /
#      Android package           `com.sphade012.azumirider`         (:14, :26)
#   3. EAS projectId             `141a4787-...`                         (:72)
#   4. googleServicesFile        a committed `google-services.json`      (:27)
#
# Usage:
#   ./scripts/check-donor-values.sh              # scans ./driver-app
#   ./scripts/check-donor-values.sh <directory>  # scans <directory>
#
# Exit codes:
#   0 — no donor value found
#   1 — a donor value found, or the target directory does not exist
#
# Requirements: 16.22, 16.29
# =============================================================================

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

TARGET_DIR="${1:-driver-app}"
FOUND=0

# Generated, vendored, and native-build directories are excluded: they are not in
# the git index (see driver-app/.gitignore) and a transitive dependency's own
# fixtures must not fail the repository's build.
EXCLUDES=(
    --exclude-dir=node_modules
    --exclude-dir=.expo
    --exclude-dir=.git
    --exclude-dir=dist
    --exclude-dir=web-build
    --exclude-dir=ios
    --exclude-dir=android
    --exclude-dir=coverage
)

# -------------------------------------------------------------------------
# Donor string values. Assembled from fragments so that this guard script is
# not itself a match when it is scanned by any other tree-wide scan.
# -------------------------------------------------------------------------
DONOR_EAS_OWNER="para""hack"
DONOR_IDENTIFIER="com.sphade012.""azumirider"
DONOR_EAS_PROJECT_ID="141a4787-fe3d-4aff-8667""-0dc943c77ffb"

report() {
    local description="$1"
    echo -e "${RED}  ✗ $description${NC}"
    FOUND=1
}

# -------------------------------------------------------------------------
# Fail on any occurrence of a donor string value
# -------------------------------------------------------------------------
check_value() {
    local label="$1"
    local value="$2"
    local matches

    matches=$(grep -rnF "${EXCLUDES[@]}" -- "$value" "$TARGET_DIR" 2>/dev/null || true)

    if [[ -n "$matches" ]]; then
        report "Donor $label \`$value\` found in $TARGET_DIR:"
        echo "$matches" | sed 's/^/      /'
    fi
}

# -------------------------------------------------------------------------
# Fail on a committed google-services.json
# -------------------------------------------------------------------------
check_google_services() {
    local matches

    matches=$(find "$TARGET_DIR" \
        \( -name node_modules -o -name .expo -o -name .git -o -name dist \
           -o -name web-build -o -name ios -o -name android \) -prune -o \
        -name 'google-services.json' -print 2>/dev/null || true)

    if [[ -n "$matches" ]]; then
        report "Committed google-services.json found in $TARGET_DIR:"
        echo "$matches" | sed 's/^/      /'
        echo -e "${YELLOW}      Supply the Runsheet Firebase file to EAS as a file secret instead.${NC}"
    fi
}

# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------
echo -e "${YELLOW}🔍 Donor-value guard — scanning $TARGET_DIR${NC}"
echo "-------------------------------------------"

if [[ ! -d "$TARGET_DIR" ]]; then
    echo -e "${RED}❌ Target directory not found: $TARGET_DIR${NC}"
    exit 1
fi

check_value "EAS owner" "$DONOR_EAS_OWNER"
check_value "identifier" "$DONOR_IDENTIFIER"
check_value "EAS project id" "$DONOR_EAS_PROJECT_ID"
check_google_services

echo "-------------------------------------------"

if [[ "$FOUND" -eq 1 ]]; then
    echo -e "${RED}❌ Donor build credentials detected.${NC}"
    echo -e "${RED}   Replace them with the Runsheet-owned values before building.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ No donor build credentials in $TARGET_DIR.${NC}"
exit 0
