# GDX Platform — Admin Guide

## User Management

- Navigate to **Settings** > **Users** tab
- Add users with email, name, and role (admin/dispatcher/tech)
- Deactivate users instead of deleting (preserves audit trail)

## Module Configuration

- Navigate to **Settings** > **Modules** tab
- Toggle modules on/off per your subscription tier
- Available modules: jobs, estimates, invoices, dispatch, communications, equipment_tracking, fleet, campaigns, quickbooks, stripe_connect

## QuickBooks Connection

1. Go to **Settings** > **Integrations**
2. Click **Connect QuickBooks**
3. Authorize the OAuth connection
4. Sync runs automatically every 15 minutes
5. Manual sync: click **Sync Now** for customers, invoices, or items

## Branding

- Go to **Settings** > **Branding** tab
- Set company name, logo, primary color, accent color
- Changes apply immediately to all users

## Plugins

Third-party plugins add full-stack modules (their own screens + backend) without forking the
app. **Owner/superadmin only** — installing a plugin runs operator-vetted code with backend
access, so it sits at the same trust tier as adding a dependency.

1. Go to **Plugins** > **Manage plugins** (`/admin/plugins`).
2. Under **Install a package**, enter the pip package name (and optionally a version), then
   **Install**. This *records intent* — it does not run yet.
3. Click **Restart plugin-host** to apply. The plugin-host container pip-installs the package
   and mounts its routes; this takes ~10s. The rest of the app keeps serving — only plugin-host
   cycles. The page polls until it's back, then the plugin appears under **Running now**.
4. Installed plugins show as their own entries in the **Plugins** nav section.

To remove a plugin, click the trash icon next to it, then **Restart plugin-host** again. Removal
takes effect on the restart (it stays running until then).

> Vetting is your responsibility: only install packages you trust. There is no signing or
> registry allowlist — see ADR-013.

## Subscription Management

- Billing portal accessible at **Settings** > **Billing**
- Manage subscription tier, payment method, view invoices
- Upgrade/downgrade takes effect at next billing cycle
