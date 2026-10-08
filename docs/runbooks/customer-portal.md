# Runbook: customer portal (OI-06)

Design: `.kiro/specs/customer-portal/design.md`. Backend code lives in `Runsheet-backend/portal/`.

## Feature flag

- `CUSTOMER_PORTAL_ENABLED` (default `false`) turns on every `/api/portal/*` route and the portal-user admin routes.
- The portal also needs `COMMERCE_BACKBONE_ENABLED=true`. With the backbone off, every portal route answers 404 `PORTAL_DISABLED`, and startup logs a WARN from `config.settings` saying so.
- Invoices and payments also need `COMMERCE_INVOICING_ENABLED=true`. With invoicing off, the invoice and payment routes answer 404 `INVOICING_DISABLED`, and `/api/portal/me` reports `invoices_available=false` and `payments_available=false`.
- Staging sets `CUSTOMER_PORTAL_ENABLED=true` in `Runsheet-backend/scripts/staging_aws.sh`. `.env.example` keeps it `false`.

## Inviting and revoking portal users

Only `admin` sessions can use these routes. They live under the customer's commerce record:

| Action | Route |
|---|---|
| List users (active, invited, revoked) | `GET /api/commerce/customers/{customer_id}/portal-users` |
| Invite (creates the identity and returns a password-set link) | `POST /api/commerce/customers/{customer_id}/portal-users` with `{"email": "..."}` |
| Resend the password-set link | `POST /api/commerce/customers/{customer_id}/portal-users/{grant_id}/resend` |
| Revoke | `DELETE /api/commerce/customers/{customer_id}/portal-users/{grant_id}` |

- A customer can have at most 10 active portal users (`PORTAL_MAX_USERS_PER_CUSTOMER`). The 11th invite answers 409 `PORTAL_USER_LIMIT_REACHED`.
- An email that already belongs to staff, a driver, or another customer answers 409 `PORTAL_EMAIL_IN_USE`. The response doesn't say which; the WARN log line carries the reason.
- Revoke deletes the user's SuperTokens identity and its `auth_users` row, and revokes its sessions. The grant row stays, as `revoked`, for history. Inviting the same email again creates a fresh identity.
- Revoke window: the grant/customer check is cached per backend process for `PORTAL_PRINCIPAL_CACHE_SECONDS` (default 60, the R2.11 bound). The task that handles the revoke clears its cache, so the user's next call there fails. Another backend task, including the old and new tasks overlapping during a rolling deploy, can keep serving that user's portal calls until its cached entry expires, up to 60 s. Staging runs one backend task. If production runs more than one and needs a shorter window, lower `PORTAL_PRINCIPAL_CACHE_SECONDS`; each lower value costs one more grant/customer read per user per window.
- Archiving the customer (`PATCH /api/commerce/customers/{id}` with `status=archived`) revokes every portal session for that customer. Its grants stay active, so un-archiving restores access.

## Stripe webhook events

ACH payments use the tenant's Stripe integration (an **enabled** Stripe instance in the Integration Marketplace). Without one, `payments_available` is `false` and payment create answers 409 `PORTAL_PAYMENTS_UNAVAILABLE`.

The tenant's Stripe webhook endpoint, `https://<api-host>/webhooks/stripe/{tenant_id}`, must subscribe to these events:

- `payment_intent.processing`
- `payment_intent.succeeded`
- `payment_intent.payment_failed`
- `payment_intent.canceled`
- `charge.refunded`
- `charge.dispute.created`

Portal PaymentIntents carry `metadata.source = runsheet_portal` plus `tenant_id`, `customer_id`, `invoice_id` and `payment_attempt_id`. Events without that source take the existing reconciliation path unchanged.

Log lines to watch:

| Line | Level | Meaning |
|---|---|---|
| `portal_audit` with `outcome=webhook_mismatch` | WARN | Event metadata didn't match a payment attempt in this tenant. Nothing was written; Stripe got 200. Repeated mismatches suggest a misrouted webhook endpoint. |
| `portal_audit` with `action=payment_refund_or_dispute` | WARN | A refund or dispute on a portal payment. No commerce record changes (PD14). Handle it in the AR process. |
| `portal_payment_provider_error` | ERROR | A Stripe call failed or timed out during payment create, replay or cancel. The customer saw 502 `PAYMENT_PROVIDER_ERROR` and can retry. |
| `portal_payment_late_success` | WARN | `succeeded` arrived for an attempt the portal had marked `failed` or `canceled`. The payment was recorded anyway, because money moved. |
| `portal_payment_unapplied` | ERROR | The payment was recorded but not applied to the invoice. Follow the procedure below. |
| `StripeConnector: key mode check` | WARN | The tenant's secret and publishable keys are in different modes (the Payment Element will fail in the browser), or a non-production environment holds a live secret key. Staging must use test-mode keys. Nothing is blocked; fix the keys in the Integration Marketplace. |
| `StripeConnector: live-mode webhook event in a non-production environment` | WARN | A live-mode event reached staging or dev. The event is still processed. Check which Stripe account the webhook endpoint belongs to. |

## Unapplied-payment procedure

`portal_payment_unapplied` means Stripe collected the money, but the portal didn't apply it to the invoice: the invoice was no longer payable, or the amount was more than its `remaining_cents` (for example, a staff payment landed in between). The attempt row is `succeeded` with `payment_id` NULL and `failure_code = apply_rejected:<code>`.

1. Find the affected attempts:

   ```sql
   SELECT payment_attempt_id, tenant_id, customer_id, invoice_id, account_id,
          amount_cents, stripe_payment_intent_id, failure_code, updated_at
     FROM portal_payment_attempts
    WHERE status = 'succeeded' AND failure_code LIKE 'apply_rejected:%';
   ```

2. Find the commerce Payment: `GET /api/commerce/payments?invoice_id=<invoice_id>` and match `external_id` to `stripe_payment_intent_id` (source `stripe`, method `ach`). With `apply_rejected:VALIDATION_ERROR` or another code raised before recording, there may be no Payment at all.
3. **Check for an earlier credit before applying anything.** When the invoice had nothing left to pay at ingest time, `PaymentService.ingest` already accrued the whole amount to the account's credit balance. Look in the account's event log (`account_events`) for a `credit_balance_applied` event (the `account_credit_balance_applied` event in the commerce design) whose payload names this `payment_id`. If one exists, the money is already on the account as credit: don't apply it again.
4. Otherwise, choose one:
   - **Refund in Stripe** when the customer paid an invoice that was voided or already settled.
   - **Apply by hand** when the invoice is still open: apply the existing Payment's amount (capped at the invoice's remaining balance) to the invoice with that `payment_id`, so the invoice's `payment_applied` event names it. There's no staff UI for applying an existing Payment yet. Don't record a second manual payment for the same money, because that double-counts it.
5. Record what you did on the invoice's ticket. Don't edit the attempt row: it's the audit trail.

## Before merging a migration

The portal adds one Alembic revision (`0012_customer_portal`, on top of `0011_margin_feed`). Before merging any branch that touches `alembic/versions/`, run:

```bash
cd Runsheet-backend && alembic heads
```

There must be exactly one head. If another revision landed on the same parent, re-parent and renumber this branch's revision. Never add a merge revision.
