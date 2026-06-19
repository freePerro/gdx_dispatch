---
title: Modules & permissions
role: owner
tags: modules, permissions, roles, access
related: users, owner-getting-started, billing-subscription
---

# Modules & permissions

Two separate concepts share the "what can someone see" question:

- **Modules** — which areas of the app are turned on for your *shop*
- **Permissions** — which areas a *specific user* can see, given the modules that are on

Both are set under Settings.

## Modules (per shop)

Your plan unlocks a set of modules. You can turn on or off any module your plan includes — useful when you're not ready for a feature yet.

Examples:

| Module | What it unlocks |
|---|---|
| **Jobs** | Job creation, lifecycle, closeout |
| **Dispatch** | Drag-and-drop dispatch board |
| **Invoices** | Customer invoicing + Stripe payments |
| **Inventory** | Truck stock tracking |
| **Equipment** | Customer equipment registry (model/serial) |
| **Phone.com** | Inbound call routing + call-to-job |
| **AI Assistant** | In-app conversational helper |
| **QuickBooks** | Two-way sync with QBO |

When a module is off:
- The sidebar entry is hidden from everyone
- The related tour steps are skipped
- The related help articles still show up in search but link to "this module isn't enabled"

## Permissions (per user / role)

Once a module is on, **role** decides who in the shop can use it. The defaults work for most shops:

- **Owner / Admin** → everything
- **Dispatcher** → jobs, customers, dispatch, comms
- **Sales** → estimates, customers, follow-ups
- **Technician** → their own jobs, mobile view
- **Accounting** → invoices, payments, reports
- **Viewer** → read-only

You can fine-tune what each role sees under **Settings → Role permissions**. Useful when a dispatcher needs to see jobs but not customer payment info, or a salesperson needs read-only access to dispatch.

## Tenant-level vs user-level

| Concern | Where to change it |
|---|---|
| "Turn off Inventory for the whole shop" | Settings → Modules |
| "Hide payments from this one dispatcher" | Settings → Role permissions |
| "This tech shouldn't see other techs' jobs" | Settings → Role permissions (Technician → restrict to own jobs) |
| "This person is leaving the company" | Settings → Users → Deactivate |

## When a permission feels wrong

- Someone seeing more than they should: tighten role permissions, then test by logging in as them ("Impersonate" — admins only).
- Someone seeing less than they should: confirm the module is on AND their role has the permission. The combination has to be true.

## Related
- [Users & teams](#)
- [Owner getting started](#)
- [Billing & subscription](#)
