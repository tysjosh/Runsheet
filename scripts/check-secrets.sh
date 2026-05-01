#!/usr/bin/env bash
# =============================================================================
# Secret Scanner — Detect leaked credentials in staged or tracked files
# =============================================================================
#
# Usage:
#   Pre-commit hook mode (scans staged files only):
#     ./scripts/check-secrets.sh --pre-commit
#
#   Manual scan mode (scans all tracked files):
#     ./scripts/check-secrets.sh
#
# Exit codes:
#   0 — No secrets detected
#   1 — Secrets detected (commit should be blocked)
#
# Skips:
#   - .env.example files (contain placeholder values by design)
#   - Binary files
#   - This script itself
# =============================================================================

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

FOUND_SECRETS=0
MODE="manual"

if [[ "${1:-}" == "--pre-commit" ]]; then
    MODE="pre-commit"
fi

# -------------------------------------------------------------------------
# Collect files to scan
# -------------------------------------------------------------------------
get_files() {
    if [[ "$MODE" == "pre-commit" ]]; then
        git diff --cached --name-only --diff-filter=ACM 2>/dev/null
    else
        git ls-files 2>/dev/null
    fi
}

# -------------------------------------------------------------------------
# Check if a file should be skipped
# -------------------------------------------------------------------------
should_skip() {
    local file="$1"

    # Skip .env.example files
    if [[ "$file" == *".env.example"* ]]; then
        return 0
    fi

    # Skip this script itself
    if [[ "$file" == "scripts/check-secrets.sh" ]]; then
        return 0
    fi

    # Skip common non-text extensions (binary files)
    case "$file" in
        *.png|*.jpg|*.jpeg|*.gif|*.ico|*.svg|*.woff|*.woff2|*.ttf|*.eot|*.pdf|*.zip|*.gz|*.tar|*.bin|*.pyc|*.pyo|*.so|*.dll|*.exe|*.class)
            return 0
            ;;
    esac

    return 1
}

# -------------------------------------------------------------------------
# Report a finding
# -------------------------------------------------------------------------
report_finding() {
    local file="$1"
    local description="$2"
    echo -e "${RED}  ⚠ $description: $file${NC}"
    FOUND_SECRETS=1
}

# -------------------------------------------------------------------------
# Secret detection patterns
# Uses grep -Ei (extended regex, case-insensitive) for portability
# across macOS and Linux (avoids -P which requires GNU grep).
# -------------------------------------------------------------------------
check_file() {
    local file="$1"

    # Skip files that don't exist (deleted files in git)
    if [[ ! -f "$file" ]]; then
        return
    fi

    # Pattern 1: Base64-encoded API keys (40+ characters) assigned to sensitive vars
    # Matches: API_KEY=<base64 40+ chars>, SECRET=<base64 40+ chars>, etc.
    if grep -Eiq '(API_KEY|SECRET_KEY|ACCESS_KEY|TOKEN)[ ]*=[ ]*[A-Za-z0-9+/=]{40,}' "$file" 2>/dev/null; then
        report_finding "$file" "Possible base64-encoded secret"
    fi

    # Pattern 2: AWS Access Key IDs (start with AKIA followed by 16 uppercase alphanumeric)
    if grep -Eq 'AKIA[0-9A-Z]{16}' "$file" 2>/dev/null; then
        report_finding "$file" "Possible AWS access key"
    fi

    # Pattern 3: AWS Secret Access Keys (40-char base64)
    if grep -Eiq '(aws_secret_access_key|aws_secret)[ ]*=[ ]*[A-Za-z0-9+/]{40}' "$file" 2>/dev/null; then
        report_finding "$file" "Possible AWS secret key"
    fi

    # Pattern 4: Elasticsearch API keys (base64, 20+ chars, non-placeholder)
    if grep -Eiq 'ELASTIC_API_KEY[ ]*=[ ]*[A-Za-z0-9+/=]{20,}' "$file" 2>/dev/null; then
        if ! grep -Eiq 'ELASTIC_API_KEY[ ]*=[ ]*your-' "$file" 2>/dev/null; then
            report_finding "$file" "Possible Elasticsearch API key"
        fi
    fi

    # Pattern 5: JWT secrets in env-style files (KEY=value, no quotes, non-placeholder)
    # Skips Python assignments (KEY = "value") and test fixtures
    if grep -Eiq '^JWT_SECRET=[^ "'"'"']+' "$file" 2>/dev/null; then
        if ! grep -Eiq '^JWT_SECRET=your-' "$file" 2>/dev/null; then
            report_finding "$file" "Possible JWT secret"
        fi
    fi

    # Pattern 6: Webhook secrets in env-style files (KEY=value, non-placeholder)
    if grep -Eiq '^[A-Z_]*WEBHOOK_SECRET=[^ "'"'"']+' "$file" 2>/dev/null; then
        if ! grep -Eiq 'WEBHOOK_SECRET=your-' "$file" 2>/dev/null; then
            report_finding "$file" "Possible webhook secret"
        fi
    fi

    # Pattern 7: Redis URLs with passwords (redis://:<password>@host)
    if grep -Eiq 'redis://:[^@]+@' "$file" 2>/dev/null; then
        report_finding "$file" "Possible Redis URL with password"
    fi

    # Pattern 8: Generic password assignments in env-style files (KEY=value, non-placeholder)
    if grep -Eiq '^[A-Z_]*(PASSWORD|PASSWD|DB_PASS)=[^ "'"'"']{8,}' "$file" 2>/dev/null; then
        if ! grep -Eiq '(PASSWORD|PASSWD|DB_PASS)=your-' "$file" 2>/dev/null; then
            report_finding "$file" "Possible password assignment"
        fi
    fi

    # Pattern 9: Google API keys (AIzaSy followed by 33 chars)
    if grep -Eq 'AIzaSy[0-9A-Za-z_-]{33}' "$file" 2>/dev/null; then
        report_finding "$file" "Possible Google API key"
    fi

    # Pattern 10: Private keys
    if grep -Eq '\-\-\-\-\-BEGIN (RSA |EC |DSA )?PRIVATE KEY\-\-\-\-\-' "$file" 2>/dev/null; then
        report_finding "$file" "Possible private key"
    fi
}

# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------
echo -e "${YELLOW}🔍 Secret Scanner — Mode: $MODE${NC}"
echo "-------------------------------------------"

while IFS= read -r file; do
    [[ -z "$file" ]] && continue

    if should_skip "$file"; then
        continue
    fi

    check_file "$file"
done < <(get_files)

echo "-------------------------------------------"

if [[ "$FOUND_SECRETS" -eq 1 ]]; then
    echo -e "${RED}❌ Secrets detected! Review the findings above.${NC}"
    echo -e "${RED}   Remove secrets and use .env.example with placeholders instead.${NC}"
    exit 1
else
    echo -e "${GREEN}✅ No secrets detected. Repository is clean.${NC}"
    exit 0
fi
