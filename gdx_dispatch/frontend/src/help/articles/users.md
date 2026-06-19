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
| **Owner** | Everything, including billing | You |
| **Admin** | Everything except your subscription | Office manager |
| **Dispatcher** | Jobs, customers, dispatch, comms | Front desk |
| **Sales** | Estimates, customers, follow-ups | Salesperson |
| **Technician** | Their own jobs and routes | Tech in the field |
| **Accounting** | Invoices, payments, reports | Bookkeeper |
| **Viewer** | Read-only access to most things | Owner's spouse, auditor |

## Inviting someone

1. Go to **Settings → Users → + Invite**.
2. Enter their email and pick a role.
3. They get an email with a one-time signup link.
4. They set their own password the first time they sign in.

The invite link expires in 7 days. You can resend it from the user list.

## Deactivating

When someone leaves the company, **deactivate** them — don't delete. Deactivation:

- Revokes their active session immediately
- Preserves their name on the jobs and invoices they touched (auditability)
- Reassigns their open jobs to a person you pick

Reactivating later is one click if they come back.

## Passwords and security

- Minimum 12 characters, mix of upper/lower/number recommended.
- Users can reset their own password from the login page.
- Owners and admins can force a password reset for any user from the user detail page.
- Two-factor (MFA) is supported — enable per-user under Settings → Security.

## Permissions beyond role

Some shops need finer control — e.g., a dispatcher who can see jobs but not customer payment info. Use the **Role permissions** page (Settings → Role permissions) to fine-tune what each role can see.

## Service accounts (API)

For integrations that need to call your data programmatically, use a **Personal Access Token** (PAT) instead of a user login. Settings → API tokens → + New. PATs can be scoped to read-only or specific endpoints.

## Related
- [Modules & permissions](#) — what each module unlocks
- [Owner getting started](#) — the full setup checklist
