# Email Integration Notes

> Salvaged 2026-04-10 from the pre-lift-and-shift `road map` file (old lot). Original was scoped against the Flask dispatch app (`dispatch/gdx_app.py` + Flask-Mail). These notes are re-framed for the FastAPI GDX app but the underlying SMTP / Microsoft Graph options apply regardless of framework.

## Current state

In the Flask dispatch app:
- Email was integrated via Flask-Mail with config loading and setup in `dispatch/gdx_app.py`.
- A shared `send_email` helper was used by invoice, reminders, portal, and campaigns.

In the FastAPI GDX app:
- Email integration should live in `gdx_dispatch/core/` (or `gdx_dispatch/integrations/`) with a single `send_email` helper that all routers use.
- Outbound email from FastAPI uses `aiosmtplib` for SMTP (async) or direct HTTP calls for Microsoft Graph. No Flask-Mail equivalent is needed.

## Option A — SMTP AUTH via Outlook (fastest path, minimal code change)

Set environment variables:

```
MAIL_SERVER=smtp.office365.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=dispatch@yourdomain.com
MAIL_PASSWORD=<password-or-app-password>
MAIL_FROM=dispatch@yourdomain.com
```

Restart the GDX app container and validate by sending an invoice email end-to-end. Works if Microsoft 365 SMTP AUTH is still enabled for the target tenant — Microsoft has been deprecating it, so Option B is the long-term path.

## Option B — Microsoft Graph `sendMail` (recommended if SMTP AUTH is disabled)

1. Create an Azure AD app registration for the GDX tenant.
2. Grant the `Mail.Send` *application* permission (not delegated) and get admin consent.
3. Add environment variables:

```
MS_TENANT_ID=<azure-tenant-guid>
MS_CLIENT_ID=<app-registration-client-id>
MS_CLIENT_SECRET=<app-registration-secret>
MS_SENDER_UPN=dispatch@yourdomain.com
```

4. Update the `send_email` helper to authenticate with MSAL (client credentials flow), get a token, and POST to `https://graph.microsoft.com/v1.0/users/{MS_SENDER_UPN}/sendMail`.
5. Keep SMTP (Option A) wired as a fallback during rollout so a Graph outage doesn't silence outbound email.

## Production hardening checklist

- **Startup validation**: on app boot, verify email config is present and the configured credential can authenticate. Fail fast on misconfiguration — silent email failures are the worst kind.
- **Retry/backoff**: transient SMTP/Graph failures should retry with exponential backoff. Persistent failures go to a dead-letter log.
- **Admin test endpoint**: `POST /api/admin/test-email` that sends a canary message to a configured operations address. Role-gate it to owners/admins.
- **Per-message audit**: every outbound email gets a row in an `email_audit_log` table with `message_id`, `recipient`, `template_name`, `status`, `sent_at`, `error` (nullable). Required for compliance and for debugging "did that reminder actually send?" tickets.

## Per-tenant implications

Email config could eventually be made **per-installation** configurable rather than global:

- Each tenant configures their own SMTP credentials or Azure app registration.
- The `send_email` helper reads credentials from the tenant's config at request time, not from the global app environment.
- This avoids "all tenants blast email from `dispatch@gdx.com`" which is wrong as soon as there's a second tenant.

Until Command Center is managing tenants, global env vars are acceptable — but design the helper so the config source is a function argument or injected dependency, not hardcoded `os.environ`.
