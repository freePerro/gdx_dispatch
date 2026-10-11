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

Click **Send Invoice**. The customer gets an email with a PDF attached and, when online payment is set up, a payment link that opens a hosted checkout (Stripe-powered).

- **Text it too** — if the phone integration is on, **Text Invoice** sends the same link by SMS. It's a separate button.
- **Did they open it?** The **Viewed** column on the Billing list shows when the customer last opened the invoice.

## Payment

Customers can pay by card, ACH, or cash/check (recorded manually). Once paid:

- Status flips to **Paid**
- Customer record shows the balance going to zero
- Payment lands in your **Payments** page with the deposit details

## Adjustments

You can edit a sent-but-unpaid invoice. Once paid, you'd issue a credit memo instead — that keeps the audit trail clean for accounting.

## Reminders

Reminder emails for overdue invoices are set up under **Billing → Reminders** (admins only). Nothing is sent until an admin switches on **Send reminder emails automatically** and saves.

- **Before you save** — the page shows how many invoices would get an email right now, and the first few of them.
- **Once on** — a daily run emails each overdue invoice as it reaches a step in the schedule.
- **Schedule** — 7, 14 and 30 days after the due date by default; change it on the same page.
- **What the customer gets** — the subject and body templates from that page, plus a **Pay Invoice Online** button when online payment is set up.
- **One customer on a payment plan?** Open the invoice and click **Pause reminders**.

## Statements and balances

Open a customer and click **Statement** to see everything that customer owes and what happened on their account over a period.

- **Period** — last 30, 60 or 90 days (the default), year to date, a finished calendar year, or a custom range. Statements start from January 1, 2026: payment records imported from QuickBooks before then are still being repaired.
- **What's on it** — the total unpaid balance split by age, every open invoice with its **Pay online** link, the balance before the period, every invoice and payment in the period, and the ending balance.
- **Warnings** — if an invoice's payment records don't add up, or a payment sits on a draft invoice, the preview says so above the statement. Warnings never appear on the customer's copy. Check the invoice before sending.
- **Download PDF** or **Email statement**. Emailing needs the *Send invoices* permission and a connected email account; if it can't be sent, the dialog says why. Every send — and every attempt that didn't go out — is recorded in the audit log, and every email that was attempted appears in the **Email Log** under *Statements*.

The statement is worked out fresh each time you open it, so a payment recorded today shows up the next time you look.

## Refunds

Record a refund under **Billing → Payments → + Record Payment / Refund**, with entry type **Refund**. Partial or full, it can't be more than what the customer has paid.

Recording a refund doesn't move any money. For cash or check, hand the money back yourself and then record it here.

For a card payment, refund it in your Stripe dashboard **first**:

- **Full refund** — the payment is voided here on its own and the invoice balance re-opens. Don't record it here; that would count it twice.
- **Partial refund** — then record it here; Stripe's notice alone doesn't reach your books.
- **Partial, then the rest later** — the second Stripe refund voids the whole payment here, so the partial refund you recorded is counted twice. The invoice balance won't show it; tell whoever keeps your books.

## Related
- [Jobs](#) — how a job becomes an invoice
