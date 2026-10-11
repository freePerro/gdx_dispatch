---
title: Users & teams
role: owner
tags: users, roles, permissions, invites
related: modules-permissions, owner-getting-started
---

# Users & teams

Every person who logs into your shop has their own user account. Their role determines what they can see and do.

## Roles

| Role | Sees | Typical user |
|---|---|---|
| **Owner** | Everything, including role and permission changes | You |
| **Admin** | Everything except granting, changing or removing admin and owner roles | Office manager |
| **Dispatcher** | Jobs, customers, dispatch, comms | Front desk |
| **Sales** | Estimates, customers, follow-ups | Salesperson |
| **Technician** | Their own jobs and routes | Tech in the field |
| **Accounting** | Invoices, payments, reports | Bookkeeper |
| **Viewer** | Read-only access to most things | Owner's spouse, auditor |

## Inviting someone

1. Go to **Settings → Users → Invite User**.
2. Enter their email and pick a role.
3. Click **Send Invite**. No email goes out — copy the one-time link the app shows you and send it to them yourself.
4. They open the link and set their own password.

The link expires in 7 days and is shown only once. If they lose it, they can use **Forgot your password?** on the login page to get a reset email.

## When someone leaves

Go to **Admin → Users** and click **Lock Out** on their row — don't delete them. You give a reason, and locking out:

- Signs them out everywhere and stops them signing in
- Keeps their name on the jobs and invoices they touched (auditability)

Their open jobs stay assigned to them, so move those to someone else on the dispatch board. If they come back, click **Unlock**.

## Passwords and security

- Minimum 8 characters; longer, with a mix of upper/lower/number, is better.
- Users can reset their own password from the login page.
- Owners and admins can set a new password for any user with **Reset Password** on their row in **Admin → Users**.

## Permissions beyond role

Some shops need finer control — e.g., a dispatcher who can see jobs but not customer payment info. Use **Admin → Roles & Permissions** in the sidebar to fine-tune what each role can see.

## Service accounts (API)

For integrations that need to call your data programmatically, use a scoped **API key** (sent as the `X-API-Key` header) instead of a user login. There's no screen for API keys yet. They are created through the API (`/api/developer/keys`), so ask whoever runs your install.

## Related
- [Modules & permissions](#) — what each module unlocks
- [Owner getting started](#) — the full setup checklist
