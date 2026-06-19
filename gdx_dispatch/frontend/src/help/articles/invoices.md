---
title: Invoices
role: all
tags: invoices, billing, payments
related: jobs, customers, billing-subscription
module: invoices
---

# Invoices

An invoice is what your customer pays you. Most invoices in your shop come from completed jobs.

## How an invoice gets created

The usual path: a tech submits a closeout sheet → the job lands in **Ready to Bill** → you open it, review, and send.

You can also create a standalone invoice (no job behind it) from the Billing page — useful for service agreements, deposits, or one-off items.

## Sending

Click **Send invoice**. The customer gets:

- An email with a PDF attached
- A payment link that opens a hosted checkout (Stripe-powered)
- Optionally, an SMS with the same link

You see when they viewed, paid, or didn't — the invoice status updates in real time.

## Payment

Customers can pay by card, ACH, or cash/check (recorded manually). Once paid:

- Status flips to **Paid**
- Customer record shows the balance going to zero
- Payment lands in your **Payments** page with the deposit details
- QuickBooks sync (if connected) pulls the invoice and payment into QBO automatically

## Adjustments

You can edit a sent-but-unpaid invoice. Once paid, you'd issue a credit memo instead — that keeps the audit trail clean for accounting.

## Reminders

Unpaid invoices get automatic reminders on a schedule you set (Settings → Reminders). Default: 7 days, 14 days, 30 days. Each reminder is a fresh email + payment link.

## Statements and balances

The dashboard widget **Open balances** shows total receivable, broken down by age. Click through to see a list of every unpaid invoice with one-click resend.

## Refunds

Issue a refund from the invoice page — partial or full. Card payments refund through Stripe automatically; cash/check needs manual handling.

## Related
- [Jobs](#) — how a job becomes an invoice
- [Billing & subscription](#) — your own bill from us (different page)
