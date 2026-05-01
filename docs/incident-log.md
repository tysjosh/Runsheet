# Incident Log — Credential Exposure and Rotation

## Overview

This document records credential exposure incidents discovered during the platform refactoring and hardening effort (Requirement 12). All exposed credentials must be rotated within 24 hours of discovery.

---

## Incident #1: Credentials Committed in `.env.development`

| Field | Details |
|-------|---------|
| **Date of Discovery** | 2025-07-14 |
| **Severity** | High |
| **Discovered By** | Platform Refactor & Hardening — Task 3.1 Audit |
| **Affected Repository** | Runsheet (main branch) |

### Affected Credentials

| Credential | File | Status | Rotation Deadline | Responsible Party |
|------------|------|--------|-------------------|-------------------|
| `ELASTIC_API_KEY` (Elasticsearch Cloud API key) | `Runsheet-backend/.env.development` | **Pending rotation** | Within 24 hours of discovery | DevOps / Infrastructure team |
| `ELASTIC_ENDPOINT` (Elasticsearch Cloud endpoint URL) | `Runsheet-backend/.env.development` | Informational — not a secret, but reveals infrastructure | N/A | — |
| `JWT_SECRET` (JWT signing secret) | `Runsheet-backend/.env.development` | **Pending rotation** | Within 24 hours of discovery | Backend team |
| `DINEE_WEBHOOK_SECRET` (Webhook HMAC secret) | `Runsheet-backend/.env.development` | **Pending rotation** | Within 24 hours of discovery | Backend team |
| `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY` (Google Maps API key) | `runsheet/.env.local` | **Pending rotation** | Within 24 hours of discovery | Frontend team |

### Remediation Steps

1. **Immediate**: Environment files removed from git tracking (Task 3.3)
2. **Immediate**: `.gitignore` updated to prevent future commits of `.env.development`, `.env.staging`, `.env.production`, and `runsheet/.env.local` (Task 3.2)
3. **Immediate**: `.env.example` files updated with placeholder values only (Task 3.4)
4. **Immediate**: Secret scanner script created to prevent future credential commits (Task 3.5)
5. **Within 24 hours**: Rotate all affected credentials listed above
6. **Within 24 hours**: Update all deployment environments with new credentials
7. **Within 24 hours**: Verify old credentials are revoked and no longer functional

### Rotation Checklist

- [ ] Generate new Elasticsearch API key in Elastic Cloud console; revoke old key
- [ ] Generate new JWT signing secret; update all environments
- [ ] Generate new Dinee webhook HMAC secret; coordinate with Dinee team
- [ ] Regenerate or restrict Google Maps API key in Google Cloud Console
- [ ] Verify all services start successfully with new credentials
- [ ] Confirm old credentials return authentication errors

### Root Cause

The `.env.development` file containing real credentials was present in the working directory but was not properly excluded by `.gitignore`. The original `.gitignore` used an overly broad `.env.*` pattern that was later restructured, but the environment files with real credentials were accessible in the repository history and working tree.

### Preventive Measures

- `.gitignore` now explicitly excludes all environment-specific files (Task 3.2)
- `scripts/check-secrets.sh` scans for credential patterns in staged files (Task 3.5)
- `.env.example` files document all required variables with placeholder values (Task 3.4)
- README updated with instructions to copy `.env.example` and fill in own credentials (Task 3.7)

---

## Template for Future Incidents

```
## Incident #N: [Title]

| Field | Details |
|-------|---------|
| **Date of Discovery** | YYYY-MM-DD |
| **Severity** | Low / Medium / High / Critical |
| **Discovered By** | [Name or process] |
| **Affected Repository** | [Repository name and branch] |

### Affected Credentials

| Credential | File | Status | Rotation Deadline | Responsible Party |
|------------|------|--------|-------------------|-------------------|
| `KEY_NAME` | `path/to/file` | Pending / Rotated / Revoked | YYYY-MM-DD | Team |

### Remediation Steps

1. ...

### Root Cause

...
```
