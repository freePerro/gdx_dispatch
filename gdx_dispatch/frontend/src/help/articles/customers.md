---
title: Customers
role: all
tags: customers, contacts, history
related: jobs, invoices, dispatcher-daily-flow
module: customers
---

# Customers

The Customers page is where every contact lives — homeowners, property managers, builders, and anyone else you do work for.

## Finding a customer

- **Search by name, phone, address, or email** in the search box at the top of the list.
- Use the filter chips to narrow by tag, segment, or last-job date.
- `Ctrl+K` from anywhere opens the global search — type a name and jump straight to the customer record.

## Adding a customer

1. Click **+ New Customer** in the top right.
2. Fill in name and at least one contact method (phone or email).
3. Add the service address — this is what shows up on the dispatch board and on invoices.
4. Save. The customer is now searchable and ready to schedule against.

You can also **bulk import** customers from QuickBooks Online or a CSV. Look for "Import" in the Customers page menu — the importer matches columns to fields and previews changes before applying.

## What the customer record shows

Each customer page has tabs:

- **Overview** — contact info, billing address, payment method on file.
- **Jobs** — every job ever scheduled for this customer, with status and total.
- **Invoices** — billed work, payments, and balance.
- **Notes** — internal notes (not visible to the customer).
- **Files** — uploaded photos, signed estimates, warranty documents.

## Tags and segments

Use **tags** for ad-hoc grouping ("VIP", "Holiday list", "Spring service due").
Use **segments** for rule-based grouping ("Last service > 12 months ago"). Segments update automatically as job data changes.

## Merging duplicates

If the same customer was entered twice, open the duplicate detector at `/customers/duplicates`. It groups likely duplicates by name+phone and lets you merge them — all jobs, invoices, and notes from both records consolidate onto the survivor.

## Soft delete

Deleting a customer is a soft-delete — the record is hidden but their job and invoice history is preserved for accounting. An admin can restore a soft-deleted customer within 30 days.

## Related
- [Jobs](#) — how to schedule work for a customer
- [Invoices](#) — how customer billing flows
