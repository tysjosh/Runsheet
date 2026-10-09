# SendGrid setup for staging (owner action)

The code and the deploy script are ready. Email stays off until you do the steps below. I don't create the account, the key or the sender for you, because they're third-party credentials.

What it turns on: the portal invite email (invite wording, with the set-password link), the "request received", "confirmed" and "declined" emails to portal customers, and SuperTokens auth email (password reset) through SendGrid SMTP. One key feeds all of them.

## 1. Create a restricted API key (Mail Send only)

1. Sign in at https://app.sendgrid.com (create a free account if you don't have one).
2. Settings → API Keys → Create API Key.
3. Name: `runsheet-staging-mail-send`. Choose **Restricted Access**, set **Mail Send** to **Full Access**, and leave everything else at **No Access**.
4. Create it and copy the key (it starts with `SG.`). SendGrid shows it once.

## 2. Verify the sender

The deploy script sends from **`no-reply@staging.runsheetops.com`** by default (name "Runsheet"). To use another address, set `EMAIL_FROM=you@yourdomain` (and optionally `EMAIL_FROM_NAME`) when you run the deploy. Verify the address you choose in one of two ways:

- **Domain authentication (recommended):** Settings → Sender Authentication → Authenticate Your Domain → domain `staging.runsheetops.com` (or `runsheetops.com`). SendGrid gives you 3 CNAME records. Add them to the Route 53 hosted zone for that domain, then click Verify in SendGrid.
- **Single sender (quicker):** Settings → Sender Authentication → Verify a Single Sender. Use an inbox you can read. Click the link in the confirmation email. Then deploy with `EMAIL_FROM=<that address>`.

## 3. Store the key in AWS Secrets Manager

Run this in a terminal signed in to AWS account 224535575204. Paste the key in place of `<KEY>`, and don't put it in any file, chat or ticket:

```sh
aws secretsmanager create-secret --region us-east-2 \
  --name runsheet-staging/sendgrid-api-key \
  --secret-string '<KEY>'
```

If the secret already exists, use this instead:

```sh
aws secretsmanager put-secret-value --region us-east-2 \
  --secret-id runsheet-staging/sendgrid-api-key \
  --secret-string '<KEY>'
```

## 4. Tell the orchestrator

Say "SendGrid key stored" (and the from address, if you changed it). The next deploy (`Runsheet-backend/scripts/staging_aws.sh deploy`) then:

- adds the secret's ARN to the task execution role's read-secrets policy;
- injects the key as `SENDGRID_API_KEY` and `SMTP_PASSWORD` (ECS secrets, never printed);
- sets `SENDGRID_FROM_EMAIL`, `SMTP_HOST=smtp.sendgrid.net`, `SMTP_PORT=587`, `SMTP_USERNAME=apikey` and `SMTP_FROM_EMAIL`.

Until the secret exists, deploy prints a warning and leaves email unconfigured. Nothing fails.

To turn email off again, delete the secret and redeploy.
