---
title: Invoices
role: all
tags: invoices, billing, payments
related: jobs, customers
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

Open a customer and click **Statement** to see everything that customer owes and what happened on their account over a period.

- **Period** — last 30, 60 or 90 days (the default), year to date, a finished calendar year, or a custom range. Statements start from January 1, 2026: payment records imported from QuickBooks before then are still being repaired.
- **What's on it** — the total unpaid balance split by age, every open invoice with its **Pay online** link, the balance before the period, every invoice and payment in the period, and the ending balance.
- **Warnings** — if an invoice's payment records don't add up, or a payment sits on a draft invoice, the preview says so above the statement. Warnings never appear on the customer's copy. Check the invoice before sending.
- **Download PDF** or **Email statement**. Emailing needs the *Send invoices* permission and a connected email account; if it can't be sent, the dialog says why. Every send — and every attempt that didn't go out — is recorded in the audit log, and every email that was attempted appears in the **Email Log** under *Statements*.

The statement is worked out fresh each time you open it, so a payment recorded today shows up the next time you look.

## Refunds

Issue a refund from the invoice page — partial or full. Card payments refund through Stripe automatically; cash/check needs manual handling.

## Related
- [Jobs](#) — how a job becomes an invoice
