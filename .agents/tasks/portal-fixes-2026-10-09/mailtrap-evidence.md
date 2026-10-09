# Mailtrap email for staging (2026-10-09): evidence

Branch `production-readiness/mailtrap` (worktree `.worktrees/mailtrap`), off `origin/production-readiness/go-live-blockers` at `ab5f933`. Iteration 1. Code commit `f257d06`; this file and `mailtrap-setup.md` follow in a docs commit (`git add -f`, same as portal-fixes D16). Draft PR: https://github.com/tysjosh/Runsheet/pull/21 (base `production-readiness/go-live-blockers`). No staging deploy in this step.

## Wait for the sibling release (first poll, 2026-10-09 03:10Z)

All three held on the first poll, so no waiting was needed:

| Condition | Command | Result |
|---|---|---|
| portal-fixes landed | `git merge-base --is-ancestor origin/production-readiness/portal-fixes origin/production-readiness/go-live-blockers` | exit 0. Both refs at `ab5f933`. |
| No ECS rollout | `aws ecs describe-services --cluster runsheet-staging --services runsheet-staging-api runsheet-staging-ui` | one PRIMARY deployment each, `COMPLETED` (`api:45`, `ui:28`) |
| No CodeBuild build | `aws codebuild list-builds-for-project --project-name runsheet-staging-image-build` + `batch-get-builds` (last 5) | all `SUCCEEDED` |

## Secret check (ARN only)

`aws secretsmanager describe-secret --secret-id runsheet-staging/mailtrap-api-token --query '{ARN:ARN,LastChanged:LastChangedDate}'` returns `arn:aws:secretsmanager:us-east-2:224535575204:secret:runsheet-staging/mailtrap-api-token-1W4WIb`, last changed 2026-10-09 02:59Z. The value was never read, printed or written anywhere.

## Decisions

| # | Decision | Reason |
|---|---|---|
| M1 | New `notifications/services/smtp_email_dispatcher.py` (`SmtpEmailDispatcher`, stdlib `smtplib`). Required env: `SMTP_HOST`, `SMTP_FROM_EMAIL`, `SMTP_PASSWORD`. Optional: `SMTP_PORT` (587, or 465 when secure), `SMTP_USERNAME` (defaults to the from address), `SMTP_FROM_NAME`, `SMTP_SECURE`. These are the same names SuperTokens' `Settings.smtp_*` read, so one secret feeds both paths. | Brief 1; provider-neutral. |
| M2 | Same bounded shape as the SendGrid fix. The whole SMTP session (connect, EHLO, STARTTLS, login, send, quit) runs in a dedicated 4-thread `smtp-send` executor under `asyncio.wait_for(SEND_TIMEOUT_SECONDS=10)`. Every socket operation gets the same `timeout`, so a stalled thread frees itself. | Mirrors portal-fixes iteration 2 (dedicated executor, not `to_thread`). |
| M3 | STARTTLS is **required** when `SMTP_SECURE` is false. A relay that doesn't offer it fails the send before login, so the token never crosses a plain connection. `SMTP_SECURE=true` uses `SMTP_SSL` (implicit TLS). Both use `ssl.create_default_context()`, which verifies certificates. | Security default. Mailtrap offers STARTTLS on 587. |
| M4 | Failure reasons and logs carry only the exception type and SMTP code, for example `SMTP authentication failed (SMTPAuthenticationError 535)`. Server reply text is dropped, because it can echo what the client sent. | "Failures logged without credentials." A test sends a reply that contains the token and checks it never reaches the logs. |
| M5 | Selection order is the same in `bootstrap/notifications.py` and `portal/services/portal_email.py`: SMTP when its three settings are set, else SendGrid when `SENDGRID_API_KEY` and `SENDGRID_FROM_EMAIL` are set, else the stub (production still raises, as before). **SendGrid is kept as an optional fallback**: the dispatcher, the `sendgrid` pin and its tests already existed, so keeping them was trivial. Staging no longer sets any `SENDGRID_*`. | Brief 1: "keep SendGrid only if trivial". |
| M6 | `portal_email.email_channel_configured()` and `invite_email.send_invite_email()` now check "SMTP or SendGrid" instead of SendGrid only. `send_portal_email` builds the selected dispatcher. Templates and HTML rendering are unchanged. | Without this, the portal invite and order emails would never send over SMTP. |
| M7 | `staging_aws.sh` edits are localized to the four SendGrid sites. `SECRET_SENDGRID` became `SECRET_MAILTRAP="${PREFIX}/mailtrap-api-token"`. `EMAIL_FROM` defaults to `no-reply@runsheetops.com` (no longer `no-reply@${DOMAIN}`, which would have been the unverified `staging.` subdomain) and `EMAIL_FROM_NAME` to `Runsheet`; both can be overridden. Env: `SMTP_HOST=live.smtp.mailtrap.io`, `SMTP_PORT=587`, `SMTP_USERNAME=api`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_SECURE=false`. One ECS secret: `SMTP_PASSWORD` → the ARN. In `ensure_execution_role`, the ARN joins `runsheet-staging-read-secrets` when the secret exists. If it's missing, deploy warns, no email env is set, and the deploy continues. Only `describe-secret` is used (`secret_exists`, `secret_arn`). | Brief 2. |
| M8 | SuperTokens auth email is unchanged: `_build_email_delivery` already maps `smtp_*` into `SMTPSettings`. I checked supertokens-python 0.31.3's `Transporter._connect`: with `secure=False` it connects without TLS, calls `starttls()`, then `login(username, password)`. A test drives it with a fake `aiosmtplib.SMTP`. | Brief 1: "verify it honours port 587 STARTTLS". |
| M9 | `sendgrid-setup.md` is replaced by `mailtrap-setup.md`: what's done (DNS records, secret name), what deploy sets, token rotation with `read -rs` + `put-secret-value` + redeploy, and a one-line note that SendGrid was dropped by owner decision. | Brief 4. |

## Tests (`tests/unit/test_smtp_email_dispatcher.py`, 18 cases; `smtplib` and `aiosmtplib` mocked)

- Selection: bootstrap picks SMTP when all three settings are set, prefers SMTP over SendGrid, and falls back to the stub when any one is missing (×3) or nothing is set. The init error names the missing settings without their values. With nothing configured, the portal sends nothing and opens no connection.
- Transport: `ehlo → starttls → ehlo → login("api", token) → send → quit` against `live.smtp.mailtrap.io:587` with the 10 s socket timeout. From is `Runsheet <no-reply@runsheetops.com>`. The Message-ID is on `@runsheetops.com` and stored as `provider_message_id`. Without STARTTLS the send fails and login is never called. Secure mode uses `SMTP_SSL` on 465 with no STARTTLS.
- No secret in logs: a 535 reply that contains the token gives a fixed reason, and the token appears in neither the reason nor the captured logs.
- Timeout: the connect hangs and the bound is 0.3 s. Dispatch returns `failed` ("SMTP send timed out after 0.3s") in under 2 s, while a concurrent ticker keeps running (≥ 5 ticks).
- Invite over SMTP uses the invite subject and wording, has the "Choose your password" link in HTML, and says "reset your password" in neither part.
- Order emails (received, confirmed, declined) go out as `multipart/alternative` with exactly `text/plain` (equal to the rendered text) and `text/html` (`lang="en"`).
- Auth email: `_build_email_delivery` with Mailtrap settings gives host/587/`secure=False`/user `api`. The transporter does `connect → starttls → login("api", token)` with `use_tls=False`, and no token appears in the logs.

Existing tests: the three "unconfigured" tests in `tests/portal/` now also clear `SMTP_PASSWORD`, so a developer shell that has SMTP set can't make them flaky. All existing SendGrid tests still pass unchanged.

## Verification (local macOS, backend venv Python 3.11, from `.worktrees/mailtrap/Runsheet-backend`)

Env for the pytest runs, as in CI: `REDIS_URL=redis://localhost:6379 JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 ENVIRONMENT=test`.

| Check | Command | Result |
|---|---|---|
| Targeted | `python -m pytest --no-cov -q -p no:cacheprovider tests/unit/test_smtp_email_dispatcher.py tests/portal/test_portal_fixes.py tests/portal/test_portal_ui_revamp_additions.py tests/unit/test_real_channel_dispatchers.py` | **92 passed** |
| Backend full + coverage gate (CI form) | `python -m pytest --cov=. --cov-report=xml:… --cov-report=term --cov-fail-under=70 -x -q -p no:cacheprovider --deselect tests/unit/test_dispatch_board_perf.py` | **14,828 passed**, 274 skipped (Postgres-backed; Docker is wedged locally), 9 deselected, **coverage 79.69%** (gate 70%), 16 min 03 s. `smtp_email_dispatcher.py` 92.5%, `portal_email.py` 94.1% |
| Board latency budgets (CI's separate untraced step) | `python -m pytest tests/unit/test_dispatch_board_perf.py --no-cov -q -p no:cacheprovider -rs` | **9 passed**, none skipped |
| Changed-file coverage | `python scripts/check_coverage.py --threshold 0` (with the run's `coverage.xml`) | **passed** (309 files) |
| Endpoint registry | `python scripts/generate_endpoint_registry.py`, then `git status` | 394 entries, **no diff** (no endpoints changed) |
| Single alembic head | `ENVIRONMENT=development JWT_SECRET=ci-test-jwt-secret python -m alembic heads \| grep -c '(head)'` | **1** (no migration in this change) |
| `staging_aws.sh` | `bash -n`, then the extracted task-def generator run with and without `MAILTRAP_SECRET_ARN` | Syntax OK. Without the ARN: no `SMTP_*`/`SENDGRID_*` env or secrets. With it: the 6 env values above plus 1 secret `SMTP_PASSWORD` → the ARN. No `SENDGRID_*` either way. |
| Secret hygiene | `git diff` scan of added lines | no token value; the only token-like string is the test fake `mt-fake-token-for-tests-only-7f3a` |
| GitHub CI | draft PR #21 | see the CI section below |

Cleanup: the extracted generator, `coverage.xml` and `.coverage` were deleted. `tmp/mailtrap/` holds only the run log.

## GitHub CI

PENDING. Filled in after the run.

## Next (release step, not done here)

- Deploy from a worktree pinned to the reviewed commit, after CI passes on it: `Runsheet-backend/scripts/staging_aws.sh deploy`. Expect `ok email via Mailtrap SMTP (live.smtp.mailtrap.io:587) from Runsheet <no-reply@runsheetops.com>`.
- After the deploy, check: `SMTP_PASSWORD` resolves (the task starts), the log shows `Registered REAL SMTP email dispatcher`, a portal invite to a QA address arrives with invite wording, a SuperTokens password reset arrives, and the Mailtrap Sending Domain stats show the sends.
