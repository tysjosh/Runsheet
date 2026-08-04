#!/usr/bin/env bash
# =============================================================================
# Tests for scripts/check-donor-values.sh
# =============================================================================
#
# Usage:
#   ./scripts/tests/check-donor-values.test.sh
#
# Exit codes:
#   0 — every case passed
#   1 — at least one case failed
#
# Each case builds a throwaway fixture directory, runs the guard against it, and
# asserts the exit code. The real /driver-app tree is asserted clean as the last
# case, which is the assertion CI depends on.
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GUARD="$REPO_ROOT/scripts/check-donor-values.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

FAILURES=0
FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

# Donor values assembled from fragments so this test file is not itself a match.
DONOR_EAS_OWNER="para""hack"
DONOR_IDENTIFIER="com.sphade012.""azumirider"
DONOR_EAS_PROJECT_ID="141a4787-fe3d-4aff-8667""-0dc943c77ffb"

assert_exit() {
    local name="$1"
    local expected="$2"
    local target="$3"
    local actual

    bash "$GUARD" "$target" >/dev/null 2>&1
    actual=$?

    if [[ "$actual" -eq "$expected" ]]; then
        echo -e "${GREEN}  ✓ $name${NC}"
    else
        echo -e "${RED}  ✗ $name (expected exit $expected, got $actual)${NC}"
        FAILURES=1
    fi
}

make_fixture() {
    local name="$1"
    local dir="$FIXTURE_ROOT/$name"
    mkdir -p "$dir"
    printf '{ "expo": { "name": "driver-app", "slug": "runsheet-driver" } }\n' >"$dir/app.json"
    echo "$dir"
}

echo "Donor-value guard tests"
echo "-------------------------------------------"

# A clean scaffold passes.
clean_dir="$(make_fixture clean)"
assert_exit "clean app passes" 0 "$clean_dir"

# Each of the three donor string values fails on its own.
owner_dir="$(make_fixture owner)"
printf '{ "expo": { "owner": "%s" } }\n' "$DONOR_EAS_OWNER" >"$owner_dir/app.json"
assert_exit "donor EAS owner fails" 1 "$owner_dir"

id_dir="$(make_fixture identifier)"
printf '{ "expo": { "ios": { "bundleIdentifier": "%s" } } }\n' "$DONOR_IDENTIFIER" >"$id_dir/app.json"
assert_exit "donor bundle identifier fails" 1 "$id_dir"

project_dir="$(make_fixture project)"
printf '{ "extra": { "eas": { "projectId": "%s" } } }\n' "$DONOR_EAS_PROJECT_ID" >"$project_dir/app.json"
assert_exit "donor EAS project id fails" 1 "$project_dir"

# A committed google-services.json fails even with no donor string in it.
gs_dir="$(make_fixture google_services)"
printf '{ "project_info": {} }\n' >"$gs_dir/google-services.json"
assert_exit "committed google-services.json fails" 1 "$gs_dir"

# A donor value inside an excluded directory does not fail the guard.
excluded_dir="$(make_fixture excluded)"
mkdir -p "$excluded_dir/node_modules/some-dep"
printf 'owner: %s\n' "$DONOR_EAS_OWNER" >"$excluded_dir/node_modules/some-dep/fixture.txt"
printf '{ "project_info": {} }\n' >"$excluded_dir/node_modules/some-dep/google-services.json"
assert_exit "donor value under node_modules is ignored" 0 "$excluded_dir"

# A donor value in a nested source file is still found.
nested_dir="$(make_fixture nested)"
mkdir -p "$nested_dir/lib/deep"
printf 'export const PROJECT_ID = "%s";\n' "$DONOR_EAS_PROJECT_ID" >"$nested_dir/lib/deep/config.ts"
assert_exit "donor value in a nested source file fails" 1 "$nested_dir"

# A missing target directory fails rather than passing vacuously.
assert_exit "missing target directory fails" 1 "$FIXTURE_ROOT/does-not-exist"

# The real app tree must be clean — this is what the CI step asserts.
assert_exit "the real driver-app tree is clean" 0 "$REPO_ROOT/driver-app"

echo "-------------------------------------------"

if [[ "$FAILURES" -eq 1 ]]; then
    echo -e "${RED}❌ Donor-value guard tests failed.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Donor-value guard tests passed.${NC}"
exit 0
