---
title: Jobs
role: all
tags: jobs, scheduling, closeout
related: customers, dispatch, invoices, tech-mobile-flow
module: jobs
---

# Jobs

A job is one visit by one technician (or crew) to do work for a customer. It moves through a lifecycle from booked to billed.

## Creating a job

1. Click **+ Job** in the top bar (or from a customer's page).
2. Pick the customer. Existing customer? Search. New? Add them on the fly.
3. Choose a **service** from your catalog (spring replacement, opener install, etc.).
4. Set a date, time window, and technician.
5. Save. The job appears on the dispatch board and on the assigned tech's mobile route.

## Job lifecycle

| Status | What it means |
|---|---|
| **Booked** | Scheduled but not yet today's work |
| **Dispatched** | On the tech's route today |
| **En route** | Tech tapped "On my way" |
| **On site** | Tech tapped "Arrived" |
| **In progress** | Work happening now |
| **Complete** | Closeout sheet submitted — parts, labor, signature |
| **Invoiced** | Invoice generated, ready to send |

## Closeout

When work is done, the tech submits a **closeout sheet** that captures:

- Parts used (drawn from inventory or added as snapshot)
- Hours worked (rolled into payroll + job costing)
- Customer signature
- Notes for office or for next visit

Your shop can set required fields (parts, hours, signature) under Settings → Workflow gates. Until those fields are filled, the job won't flip to Complete.

## From job to invoice

A completed job lands in the **Ready to Bill** queue on the dashboard. Open it, review the lines, adjust if needed, then send. The invoice shows up on the customer's record and the dashboard's open-balances widget.

## Rescheduling and reassigning

- **Drag** a job on the dispatch board to move it between techs or times.
- **Right-click** on a job for quick actions: reschedule, reassign, duplicate, cancel.
- Customer gets an automatic SMS confirmation when the time changes.

## Related
- [Dispatch](#) — assigning and moving jobs in real time
- [Invoices](#) — billing a completed job
- [Customers](#) — pulling a customer's job history
