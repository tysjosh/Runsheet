# Mailtrap email for staging

SendGrid was dropped by owner decision (2026-10-09, no longer free); staging email now goes through Mailtrap SMTP.

## Already done

- **Sending domain** `runsheetops.com` is verified in Mailtrap.
- **DNS** (Route 53 zone `Z0780840W9S0EZVE5W8K`):

  | Type | Name | Value |
  |---|---|---|
  | CNAME | `mt94.runsheetops.com` | `smtp.mailtrap.live` |
  | CNAME | `rwmt1._domainkey.runsheetops.com` | `rwmt1.dkim.smtp.mailtrap.live` |
  | CNAME | `rwmt2._domainkey.runsheetops.com` | `rwmt2.dkim.smtp.mailtrap.live` |
  | TXT | `_dmarc.runsheetops.com` | `v=DMARC1; p=none; rua=mailto:dmarc@smtp-staging.mailtrap.net; ruf=mailto:dmarc@smtp-staging.mailtrap.net; rf=afrf; pct=100` |
  | CNAME | `mt-link.runsheetops.com` | `t.mailtrap.live` |

- **Token**: the Transactional Stream API token is in AWS Secrets Manager as `runsheet-staging/mailtrap-api-token` (us-east-2, account 224535575204). Nothing in the repo or the deploy output contains its value.

## What the deploy does

`Runsheet-backend/scripts/staging_aws.sh deploy` checks the secret with `describe-secret` (ARN only, never the value), then:

- adds the ARN to the execution role's inline policy `runsheet-staging-read-secrets`;
- injects the token as the ECS task secret `SMTP_PASSWORD`;
- sets `SMTP_HOST=live.smtp.mailtrap.io`, `SMTP_PORT=587`, `SMTP_USERNAME=api`, `SMTP_SECURE=false` (STARTTLS), `SMTP_FROM_EMAIL=no-reply@runsheetops.com`, `SMTP_FROM_NAME=Runsheet`.

To send from another address on the verified domain, deploy with `EMAIL_FROM=…` and, optionally, `EMAIL_FROM_NAME=…`.

The same settings drive both email paths: the notifications/portal SMTP dispatcher (portal invite, delivery request received/confirmed/declined) and SuperTokens auth email (password reset).

If the secret is missing, deploy prints a warning, leaves email unconfigured and carries on.

## Rotate the token

1. In Mailtrap: Sending Domains → `runsheetops.com` → Integration → Transactional Stream → SMTP, and create or copy the new API token.
2. In a terminal signed in to account 224535575204, run the commands below. `read -rs` prompts without echoing, so the token stays out of the command line and shell history. Paste it and press Enter.

   ```sh
   read -rs MT_TOKEN
   aws secretsmanager put-secret-value --region us-east-2 \
     --secret-id runsheet-staging/mailtrap-api-token \
     --secret-string "$MT_TOKEN"
   unset MT_TOKEN
   ```

3. ECS reads secrets only when a task starts, so roll the API service: `Runsheet-backend/scripts/staging_aws.sh deploy`, or force a new deployment of `runsheet-staging-api`.
4. Revoke the old token in Mailtrap.

## Turning email off

Never delete the secret to turn email off. A deleted secret stays describable for its 7–30 day recovery window, and older deploy scripts would still put its ARN in the task definition, which stops API tasks from starting.

Instead, deploy with the off switch:

```sh
STAGING_EMAIL_OFF=1 Runsheet-backend/scripts/staging_aws.sh deploy
```

That deploy sets no `SMTP_*` env and no `SMTP_PASSWORD` secret, so the API uses the stub email dispatcher and SuperTokens' built-in auth email. The secret is left as it is. To turn email back on, run `deploy` without the variable.

The switch lasts for one deploy. The next plain `deploy` turns email back on while the secret exists. Since 2026-10-09 the script also treats a secret that is scheduled for deletion as missing (email off, with a warning) rather than failing the deploy. That is only a safety net, not the way to turn email off.
