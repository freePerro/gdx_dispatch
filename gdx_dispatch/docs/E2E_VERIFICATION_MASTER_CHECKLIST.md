# E2E Verification Master Checklist

**Purpose**: Define what "works" means for every feature in the GDX platform. Not "page loads" -- actual end-to-end functional verification. If every item on this checklist passes, the app is confirmed working.

**Current gap**: The existing `test_33_chrome_devtools_vue_audit.py` only checks HTTP 200 + zero JS console errors. That misses forms that render but don't submit, buttons that exist but don't fire, tables that show but are empty, and dozens of other failure modes.

**Architecture**: Vue 3 + PrimeVue frontend, FastAPI backend, 44 routers (313 endpoints), 21 Vue pages, per-tenant SQLite databases, module gating, WebSocket dispatch, Stripe Connect, SMS/email, PDF generation, file uploads.

---

## Table of Contents

1. [Testing Layers and Tools](#1-testing-layers-and-tools)
2. [Authentication and Authorization](#2-authentication-and-authorization)
3. [Dashboard](#3-dashboard)
4. [Jobs Workflow](#4-jobs-workflow)
5. [Customer Management](#5-customer-management)
6. [Dispatch Board and WebSocket](#6-dispatch-board-and-websocket)
7. [Estimates Workflow](#7-estimates-workflow)
8. [Invoicing and Billing](#8-invoicing-and-billing)
9. [Payments and Stripe](#9-payments-and-stripe)
10. [Timeclock and Labor](#10-timeclock-and-labor)
11. [Equipment Tracking](#11-equipment-tracking)
12. [Communications (SMS/Email)](#12-communications-smsemail)
13. [Campaigns and Marketing](#13-campaigns-and-marketing)
14. [Reports](#14-reports)
15. [Documents and File Uploads](#15-documents-and-file-uploads)
16. [Fleet Management](#16-fleet-management)
17. [Mobile Technician App](#17-mobile-technician-app)
18. [Inventory and Catalog](#18-inventory-and-catalog)
19. [Settings and Configuration](#19-settings-and-configuration)
20. [PDF Generation](#20-pdf-generation)
21. [Module Gating](#21-module-gating)
22. [Multi-Tenant Isolation](#22-multi-tenant-isolation)
23. [Automations](#23-automations)
24. [Segments](#24-segments)
25. [Warranties](#25-warranties)
26. [Technicians](#26-technicians)
27. [Expenses](#27-expenses)
28. [Maps and Geocoding](#28-maps-and-geocoding)
29. [Booking and Customer Portal](#29-booking-and-customer-portal)
30. [Loyalty, Referrals, and Reviews](#30-loyalty-referrals-and-reviews)
31. [Pricing Engine](#31-pricing-engine)
32. [QuickBooks Integration](#32-quickbooks-integration)
33. [Notifications and Push](#33-notifications-and-push)
34. [Job Templates](#34-job-templates)
35. [Recurring Jobs](#35-recurring-jobs)
36. [Search](#36-search)
37. [Audit Trail](#37-audit-trail)
38. [Edge Cases and Failure Modes](#38-edge-cases-and-failure-modes-cross-cutting)
39. [Security (OWASP)](#39-security-owasp)
40. [Accessibility (WCAG 2.1 AA)](#40-accessibility-wcag-21-aa)
41. [Performance and Core Web Vitals](#41-performance-and-core-web-vitals)
42. [Visual Regression](#42-visual-regression)
43. [Chaos and Resilience](#43-chaos-and-resilience)
44. [CI/CD Pipeline Design](#44-cicd-pipeline-design)
45. [Automation Strategy and Tooling](#45-automation-strategy-and-tooling)

---

## 1. Testing Layers and Tools

### The Testing Diamond (recommended for this app)

```
         /  E2E Browser (Playwright)  \        ~50 tests, 10-15 min
        / Visual Regression (Playwright) \     ~20 tests, 5 min
       /  Integration/API Contract (pytest) \  ~300 tests, 3 min
      /     Unit (pytest, Vitest)            \ ~1200 tests, 1 min
```

**Why a diamond, not a pyramid**: For a multi-tenant SaaS with complex UI workflows, integration tests (API contracts) catch more real bugs per test than unit tests. E2E tests are essential because the Vue frontend is where most user-facing bugs hide.

### Tool Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| Unit (Python) | pytest | Model logic, helpers, validators |
| Unit (JS) | Vitest | Vue component logic, stores, composables |
| API Contract | pytest + httpx | Every endpoint: correct status, schema, data |
| E2E Browser | Playwright (Python) | Full user journeys through the Vue app |
| Visual Regression | Playwright screenshots | Pixel-diff every page across deploys |
| Accessibility | @axe-core/playwright | WCAG 2.1 AA on every page |
| Performance | Playwright + Lighthouse | Core Web Vitals (LCP, CLS, INP) |
| Security | pytest + OWASP ZAP | Injection, auth bypass, IDOR, tenant leak |
| Load | Locust or k6 | Concurrent users, WebSocket scale |
| Chaos | pytest + fault injection | External service failures (Stripe, Twilio, Maps) |

---

## 2. Authentication and Authorization

### What "works" means

A user can log in, get a JWT, access protected resources, and be denied access to resources outside their role/tenant.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| AUTH-01 | Login with valid credentials | POST /api/auth/login returns 200, response contains `access_token`, token is valid JWT with `tenant_id`, `user_id`, `role` claims |
| AUTH-02 | Login with wrong password | Returns 401, no token in response, response body contains error message |
| AUTH-03 | Login with non-existent email | Returns 401, generic error (no user enumeration) |
| AUTH-04 | Access protected route without token | Returns 401 or 403, not 200 or 500 |
| AUTH-05 | Access protected route with expired token | Returns 401, not 200 |
| AUTH-06 | Access protected route with malformed token | Returns 401, not 500 |
| AUTH-07 | Token refresh | POST /api/auth/refresh with valid refresh token returns new access token |
| AUTH-08 | Logout | POST /api/auth/logout invalidates session |
| AUTH-09 | Role-based access: admin vs tech vs dispatcher | Admin can access /api/settings, technician cannot (returns 403) |
| AUTH-10 | SSO login flow (Google) | GET /api/auth/google returns redirect URL, callback processes token |
| AUTH-11 | Vue login form | Fill email + password, click Login, redirects to /dashboard, sidebar shows user name |
| AUTH-12 | Vue auth guard | Navigate to /jobs without auth, redirected to /login with ?redirect=/jobs |
| AUTH-13 | Vue post-login redirect | After login, redirected to the originally requested page |
| AUTH-14 | Session timeout | After JWT expiry, next API call returns 401, Vue shows login screen |

### Edge Cases

- SQL injection in email field (`' OR 1=1 --`)
- XSS in email field (`<script>alert(1)</script>`)
- Brute force: 10 failed attempts trigger rate limit (429)
- Concurrent sessions: same user logged in on two browsers
- Token reuse after logout

---

## 3. Dashboard

### What "works" means

Dashboard loads and displays real, current data -- not empty cards, not stale data, not zeros when there are records.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| DASH-01 | Dashboard renders with data | Page loads, all stat cards show numbers (not "0" when data exists), charts render with data points |
| DASH-02 | Today's jobs count | Card shows count matching `GET /api/jobs?status=scheduled&date=today` |
| DASH-03 | Revenue figures | Revenue card matches sum of payments in current period |
| DASH-04 | Open estimates count | Card matches `GET /api/estimates?status=sent` count |
| DASH-05 | Overdue invoices | Card matches invoices where due_date < today and balance_due > 0 |
| DASH-06 | Recent activity feed | Shows real events (job created, payment received), not empty |
| DASH-07 | Quick actions | "New Job" button navigates to job creation, "New Customer" works |
| DASH-08 | Dashboard with zero data (new tenant) | Shows "0" values and empty states, no JS errors, no broken layouts |
| DASH-09 | Dashboard refresh | Data updates after creating a new job (no stale cache) |
| DASH-10 | Charts interactive | Hover on chart shows tooltip with correct values |

---

## 4. Jobs Workflow

### What "works" means

A job can be created, assigned, scheduled, dispatched, tracked through status changes, and completed with photos, signatures, notes, and time entries. The entire lifecycle works end-to-end.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| JOB-01 | Job list loads with data | GET /api/jobs returns array, each job has id, customer_id, status, scheduled_date; Vue table shows rows |
| JOB-02 | Job list pagination | With 50+ jobs, pagination controls appear and work |
| JOB-03 | Job list filtering | Filter by status (scheduled/in_progress/completed) returns correct subset |
| JOB-04 | Job list search | Search by customer name or address returns matching jobs |
| JOB-05 | Create job - form renders | All form fields present: customer dropdown (with options), job type, description, scheduled date/time, assigned technician, address |
| JOB-06 | Create job - customer dropdown populated | Dropdown shows actual customers from GET /api/customers, not empty |
| JOB-07 | Create job - technician dropdown populated | Dropdown shows technicians from GET /api/technicians, not empty |
| JOB-08 | Create job - submit | Fill all required fields, submit, API returns 201 with job data, redirects to job detail |
| JOB-09 | Create job - appears in list | After creation, job appears in jobs list without page reload |
| JOB-10 | Create job - appears on dispatch board | New scheduled job appears on dispatch board calendar |
| JOB-11 | Job detail page | Shows all job fields, customer info, assigned tech, status, notes, photos, time entries |
| JOB-12 | Edit job | Change description, save, API returns updated data, page reflects changes |
| JOB-13 | Status transitions | scheduled -> in_progress -> completed; each transition: button visible, click works, status updates in UI and API |
| JOB-14 | Add job note | Type note, submit, note appears in timeline with timestamp and author |
| JOB-15 | Upload job photo | Select image file, upload succeeds, photo thumbnail appears on job detail |
| JOB-16 | Capture customer signature | Signature pad renders, draw signature, submit, signature saved and displayed |
| JOB-17 | Job time entries | Clock in on job, clock out, time entry recorded with duration |
| JOB-18 | Job parts used | Add part from inventory, quantity deducted, part listed on job |
| JOB-19 | Delete job (soft) | Delete sets deleted_at, job disappears from list, still exists in DB |
| JOB-20 | Job -> Invoice | Create invoice from completed job, invoice has correct customer, line items from job |
| JOB-21 | Job dependencies | Set job B depends on job A, job B cannot start until A is completed |
| JOB-22 | Job follow-up | Create follow-up job, linked to original job |

### Edge Cases

- Create job with no customer selected (should fail validation)
- Create job with past scheduled date (should warn or prevent)
- Very long description (10,000 chars)
- Special characters in address (apostrophes, unicode)
- Concurrent status update (two dispatchers changing same job)
- Job with disabled module (should 403 if jobs module disabled)

---

## 5. Customer Management

### What "works" means

Customers can be created, viewed, edited, searched, and have full history (jobs, invoices, communications) accessible from their detail page.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| CUST-01 | Customer list loads | GET /api/customers returns array with name, phone, email, address; Vue table shows rows |
| CUST-02 | Customer search | Type in search box, results filter in real-time (debounced API call or client-side filter) |
| CUST-03 | Create customer - form | All fields present: name, phone, email, address, notes |
| CUST-04 | Create customer - submit | Fill required fields, submit, 201 response, customer appears in list |
| CUST-05 | Create customer - duplicate detection | Creating customer with same phone/email as existing shows warning |
| CUST-06 | Customer detail page | Shows customer info, job history, invoice history, communication log, documents |
| CUST-07 | Customer detail - jobs tab | Lists all jobs for this customer with status and date |
| CUST-08 | Customer detail - invoices tab | Lists all invoices with amount, status, date |
| CUST-09 | Customer detail - communications tab | Shows SMS/email history |
| CUST-10 | Edit customer | Change name/phone, save, reflected in detail and list |
| CUST-11 | Delete customer (soft) | Soft delete, disappears from list, jobs still reference it |
| CUST-12 | Customer import | POST /import/customers with CSV data, customers created |
| CUST-13 | Customer locations | Add secondary location, location appears in list, selectable on job creation |
| CUST-14 | Customer LTV | GET /customer-ltv returns lifetime value calculation |

---

## 6. Dispatch Board and WebSocket

### What "works" means

The dispatch board shows a real-time view of today's jobs, technician assignments, and updates live when jobs are assigned or status changes. WebSocket connection is active and receiving events.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| DISP-01 | Dispatch page renders | Calendar/board view loads, shows jobs for current day/week |
| DISP-02 | Jobs shown on board | Each scheduled job appears as a card with customer name, address, time, assigned tech |
| DISP-03 | Drag-and-drop assignment | Drag job card to technician column, API call fires, job.technician_id updates |
| DISP-04 | WebSocket connects | On page load, WS connection to /ws/dispatch established (check network tab) |
| DISP-05 | WebSocket receives job_assigned | When another user assigns a job, board updates without refresh |
| DISP-06 | WebSocket receives job_status | When tech marks job in_progress, card color/status changes live |
| DISP-07 | WebSocket receives tech_location | Technician location update reflected on map (if map view active) |
| DISP-08 | WebSocket receives board_refresh | Full board refresh triggered by broadcast |
| DISP-09 | WebSocket reconnect | Kill WS connection, verify automatic reconnect within 5 seconds |
| DISP-10 | WebSocket auth | WS connection without valid token is rejected (code 1008) |
| DISP-11 | WebSocket tenant isolation | Messages from tenant A do not appear on tenant B's board |
| DISP-12 | Dispatch with no jobs | Empty state shown, no JS errors |
| DISP-13 | Dispatch time range | Switch between day/week/month view, jobs update accordingly |

### Edge Cases

- WebSocket disconnects mid-session (network interruption)
- 50+ concurrent WebSocket connections per tenant
- Rapid-fire status updates (10 updates in 1 second)
- Invalid message type sent over WebSocket (should be ignored, not crash)

---

## 7. Estimates Workflow

### What "works" means

An estimate can be created, line items added with correct math, sent to customer, accepted or declined, and converted to an invoice. The entire lifecycle works with correct financial calculations.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| EST-01 | Estimates list | GET /api/estimates returns array, Vue table shows rows with estimate number, customer, total, status |
| EST-02 | Create estimate | POST with customer_id or job_id, returns 201 with estimate_number in format EST-NNNNNN |
| EST-03 | Add line item | POST /{id}/lines with description, qty, unit_price; line_total = qty * unit_price (to the penny) |
| EST-04 | Total recalculation | After adding 3 lines ($100, $250, $75), estimate.total = $425.00 exactly |
| EST-05 | Edit line item | PATCH line, change quantity, line_total and estimate.total recalculate |
| EST-06 | Delete line item | DELETE line, estimate.total recalculates (decreases) |
| EST-07 | Send estimate | POST /{id}/send, status changes to "sent", sent_at timestamp set |
| EST-08 | Accept estimate | POST /{id}/accept, status changes to "accepted", accepted_at set |
| EST-09 | Decline estimate | POST /{id}/decline with reason, status="declined", declined_reason saved |
| EST-10 | Cannot edit accepted estimate | PATCH on accepted estimate returns 409 |
| EST-11 | Cannot accept declined estimate | POST /accept on declined returns 409 |
| EST-12 | Estimate PDF | GET /{id}/pdf returns valid PDF with correct line items, totals, customer info, branding |
| EST-13 | Conversion rate analytics | GET /analytics/conversion-rate returns sent/accepted counts and percentage |
| EST-14 | Expire stale estimates | POST /expire-stale marks past-valid_until estimates as expired |
| EST-15 | Estimate detail Vue page | Shows all fields, line items table with edit/delete buttons, action buttons (Send, Accept, Decline) |
| EST-16 | Add line item via Vue | Click "Add Line", fill description/qty/price, save, line appears in table, total updates |
| EST-17 | Estimate -> Invoice conversion | Accept estimate, create invoice from it, invoice lines match estimate lines |

### Edge Cases

- Line item with quantity 0 (should fail validation, gt=0)
- Unit price with more than 2 decimal places (should round to cents)
- Very large total (1,000,000+)
- Estimate with no lines (total = $0.00)
- Concurrent accept + decline on same estimate
- Empty description (should fail validation)

---

## 8. Invoicing and Billing

### What "works" means

Invoices can be created, line items managed, finalized, sent, and paid. Financial calculations are penny-accurate. Status lifecycle (draft -> sent -> paid/overdue) works correctly.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| INV-01 | Invoice list | Vue billing page shows invoices with number, customer, total, status, due date |
| INV-02 | Create invoice | POST /api/invoices with job_id, returns 201 with invoice_number |
| INV-03 | Add line items | POST /{id}/lines, line_total calculated correctly |
| INV-04 | Invoice totals | subtotal = sum(line_totals), tax_amount calculated, total = subtotal + tax, balance_due = total - payments |
| INV-05 | Finalize invoice | POST /{id}/finalize locks the invoice, prevents further line edits |
| INV-06 | Send invoice | POST /{id}/send, status changes to "sent" |
| INV-07 | Record payment | POST /{id}/payments with amount, balance_due decreases, status becomes "paid" when balance = 0 |
| INV-08 | Partial payment | Pay $50 on $100 invoice, balance_due = $50, status remains "sent" or "partial" |
| INV-09 | Overpayment prevention | Payment > balance_due is rejected or creates credit |
| INV-10 | Invoice PDF | GET /{id}/pdf returns valid PDF with line items, totals, customer, branding |
| INV-11 | Credit memo | POST /{id}/credit-memo creates credit, adjusts balance |
| INV-12 | Refund | POST /{id}/refund processes refund, balance updates |
| INV-13 | Payment plan | POST /{id}/payment-plan creates installment schedule |
| INV-14 | Send receipt | POST /{id}/send-receipt triggers email with receipt |
| INV-15 | Batch invoice creation | POST /batch creates multiple invoices, all valid |
| INV-16 | Overdue detection | Invoice past due_date with balance > 0 shows as "overdue" |
| INV-17 | Invoice detail Vue page | Shows all fields, line items, payment history, action buttons |

### Edge Cases

- Negative line amounts
- Invoice with zero total
- Payment with zero amount
- Invoice for deleted customer
- Currency rounding (test: 3 items at $33.33 = $99.99, not $100.00)

---

## 9. Payments and Stripe

### What "works" means

Stripe Connect is configured per tenant, payment intents are created with correct amounts and platform fees, webhooks process correctly, and the full checkout flow works.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| PAY-01 | Stripe onboarding | POST /api/stripe/connect/onboard returns account_id and onboarding_url |
| PAY-02 | Stripe status check | GET /api/stripe/connect/status returns charges_enabled, payouts_enabled |
| PAY-03 | Create payment intent | POST /api/stripe/connect/payment-intent with amount_cents, returns client_secret |
| PAY-04 | Platform fee calculation | Fee = amount * fee_percent (default 2%), fee_amount in response metadata |
| PAY-05 | Custom fee percent | Pass fee_percent=3.5, verify application_fee_amount = amount * 0.035 |
| PAY-06 | Webhook: payment_intent.succeeded | POST /webhook with valid Stripe signature, payment recorded in DB |
| PAY-07 | Webhook: account.updated | Updates tenant's stripe_connect_account_id |
| PAY-08 | Webhook: invalid signature | Returns 400, not processed |
| PAY-09 | Balance retrieval | GET /api/stripe/connect/balance returns Stripe balance for connected account |
| PAY-10 | Customer portal payment | Invoice with "Pay Now" button -> Stripe Checkout -> payment processes -> invoice status updates |
| PAY-11 | Payment methods | CRUD on saved payment methods |
| PAY-12 | Stripe not configured | When STRIPE_SECRET_KEY is empty, returns 500 with clear error, not crash |

### Edge Cases

- Duplicate webhook delivery (idempotency)
- Webhook for non-existent tenant
- Payment intent with $0 amount (should fail, gt=0)
- Very large payment ($999,999.99)
- Stripe API timeout/failure (graceful degradation)
- Currency other than USD

---

## 10. Timeclock and Labor

### What "works" means

Technicians can clock in/out for the day and for individual jobs. Time entries are recorded accurately with correct durations. The timeclock page shows a usable interface.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| TIME-01 | Timeclock page renders | Shows clock in/out button, today's entries |
| TIME-02 | Clock in (daily) | POST /api/timeclock/clock-in, entry created with clock_in timestamp |
| TIME-03 | Clock out (daily) | POST /api/timeclock/clock-out, entry updated with clock_out, duration calculated |
| TIME-04 | Job clock in | POST /api/timeclock/jobs/{id}/clock-in, time entry linked to job |
| TIME-05 | Job clock out | POST /api/timeclock/jobs/{id}/clock-out, duration calculated |
| TIME-06 | Time entries list | GET /api/timeclock/entries returns entries with dates, durations |
| TIME-07 | Delete time entry | DELETE /api/timeclock/time-entries/{id} removes entry |
| TIME-08 | Timecard view | Shows weekly/biweekly summary with total hours |
| TIME-09 | Double clock-in prevention | Clock in when already clocked in returns error |
| TIME-10 | Vue timeclock page | Clock in button visible, click it, status changes to "Clocked In", timer shows |

### Edge Cases

- Clock out without clock in
- Overnight shift (clock in 11 PM, clock out 2 AM)
- Timezone handling
- Concurrent clock-in on two devices

---

## 11. Equipment Tracking

### What "works" means

Equipment is tracked per customer with service history, warranty info, and predictive maintenance alerts.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| EQUIP-01 | Equipment list | GET /api/equipment returns items with make, model, serial, customer |
| EQUIP-02 | Equipment page renders | Vue shows equipment table with search/filter |
| EQUIP-03 | Add equipment | Create new equipment linked to customer, appears in list |
| EQUIP-04 | Equipment history | GET /{id}/history returns service records |
| EQUIP-05 | Expiring warranties | GET /expiring-warranties returns items with soon-expiring warranties |
| EQUIP-06 | Predictive maintenance | GET /predictive-maintenance returns maintenance predictions |
| EQUIP-07 | Delete equipment | Soft delete, disappears from list |

---

## 12. Communications (SMS/Email)

### What "works" means

SMS and email can be sent to customers, conversations are threaded and viewable, webhook from Twilio records incoming messages.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| COMM-01 | Communications page renders | Shows conversation list, message composer |
| COMM-02 | Send SMS | POST /api/sms/send with phone and message, returns 200, message appears in conversation |
| COMM-03 | SMS conversations list | GET /api/sms/conversations returns conversations grouped by phone |
| COMM-04 | Conversation detail | GET /api/sms/conversations/{phone} returns message thread in order |
| COMM-05 | Incoming SMS webhook | POST /api/sms/webhook (from Twilio), message recorded in conversation |
| COMM-06 | Communication timeline | GET /api/communications/timeline/{customer_id} shows all communications |
| COMM-07 | Do Not Contact | POST /api/communications/dnc/{customer_id}, subsequent sends blocked |
| COMM-08 | Email send | System sends email (estimate, invoice, receipt), delivered to inbox |
| COMM-09 | Inbox view | GET /api/inbox/folders, /api/inbox/unread-count shows correct counts |

### Edge Cases

- SMS to invalid phone number
- SMS with unicode/emoji
- Very long SMS (multi-segment)
- Twilio API down (graceful degradation)
- DNC customer receives no messages

---

## 13. Campaigns and Marketing

### What "works" means

Marketing campaigns can be created, segments targeted, messages sent, and results tracked.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| CAMP-01 | Campaigns page renders | Shows campaign list with status |
| CAMP-02 | Loyalty tiers | GET /api/loyalty/tiers returns tier list |
| CAMP-03 | Customer points | GET /customers/{id}/points returns point balance |
| CAMP-04 | Add points | POST /customers/{id}/points, balance increases |
| CAMP-05 | Customer tier | GET /customers/{id}/tier returns current tier |
| CAMP-06 | Reviews | GET /reviews returns review list |
| CAMP-07 | Referrals | POST /referrals creates referral, GET /referrals lists them |
| CAMP-08 | Referral tracking | Referral converts when referred customer creates job |

---

## 14. Reports

### What "works" means

Reports generate with real data, charts render, exports produce valid files.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| RPT-01 | Reports page renders | Shows report type selector (revenue, jobs, technician performance) |
| RPT-02 | Revenue report | GET /api/reports/dashboard returns revenue data, chart renders |
| RPT-03 | Daily snapshot | GET /api/reports/daily-snapshot returns today's metrics |
| RPT-04 | Export | GET /api/reports/export returns CSV/Excel file with correct data |
| RPT-05 | Date range filter | Filter by custom date range, data reflects only that period |
| RPT-06 | Empty date range | Date range with no data shows "No data" message, not broken chart |

---

## 15. Documents and File Uploads

### What "works" means

Files can be uploaded, stored, downloaded, and deleted. File associations with jobs/customers are maintained. File type validation works.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| DOC-01 | Documents page renders | Shows document list with file names, dates, associations |
| DOC-02 | Upload document | POST /api/documents/upload with file + job_id, returns 201, file in list |
| DOC-03 | Upload job photo | POST /api/jobs/{id}/photos with image, returns 201, thumbnail shown on job |
| DOC-04 | Download document | GET /api/documents/{id}/download returns file with correct content-type and name |
| DOC-05 | Delete document | DELETE /api/documents/{id}, soft delete, file removed from list |
| DOC-06 | File type validation | Upload .exe file as job photo, returns 415 (only jpg/png/webp allowed) |
| DOC-07 | File size limit | Upload 15MB photo, returns 413 (10MB limit) |
| DOC-08 | Signature upload | POST /api/jobs/{id}/signature with base64 PNG, stored correctly |
| DOC-09 | Document folders | GET /api/document-folders returns folder structure |
| DOC-10 | File actually stored | After upload, verify file exists on disk at expected path |
| DOC-11 | File content matches | Download uploaded file, compare bytes to original |

### Edge Cases

- Filename with special characters (spaces, unicode, `../../etc/passwd`)
- Zero-byte file
- File with wrong extension (rename .txt to .jpg)
- Concurrent uploads to same entity
- Upload with no tenant context (should fail 400)
- Path traversal in filename (security)

---

## 16. Fleet Management

### What "works" means

Vehicles are tracked with service schedules, mileage, and maintenance history.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| FLEET-01 | Fleet page renders | Shows vehicle list with make, model, year, mileage |
| FLEET-02 | Add vehicle | POST creates vehicle, appears in list |
| FLEET-03 | Vehicle service log | GET /{id}/service-log returns maintenance records |
| FLEET-04 | Due for service | GET /due-for-service returns vehicles needing maintenance |
| FLEET-05 | Delete vehicle | Soft delete, removed from list |

---

## 17. Mobile Technician App

### What "works" means

Technicians on mobile devices can view their schedule, update job status, clock in/out, take photos, capture signatures, and add notes -- all from the mobile-optimized view.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| MOB-01 | Mobile schedule | GET /api/mobile/schedule returns today's jobs for logged-in tech |
| MOB-02 | Job detail | GET /api/mobile/job/{id} returns full job info |
| MOB-03 | En-route | POST /api/mobile/jobs/{id}/en-route updates status |
| MOB-04 | Arrived | POST /api/mobile/jobs/{id}/arrived updates status |
| MOB-05 | Complete job | POST /api/mobile/jobs/{id}/complete, job marked complete |
| MOB-06 | Mobile clock in/out | POST /api/mobile/clock-in and /clock-out work |
| MOB-07 | Job clock in/out | POST /api/mobile/jobs/{id}/clock-in and /clock-out work |
| MOB-08 | Photo upload (mobile) | POST /api/mobile/jobs/{id}/photos, photo saved |
| MOB-09 | Signature capture (mobile) | POST /api/mobile/jobs/{id}/signature, signature saved |
| MOB-10 | Add note | POST /api/mobile/jobs/{id}/notes, note appears on job |
| MOB-11 | Parts used | POST /api/mobile/jobs/{id}/parts-used records parts |
| MOB-12 | Location tracking | POST /api/mobile/location records GPS coordinates |
| MOB-13 | Offline sync | POST /api/mobile/sync reconciles offline data |
| MOB-14 | Mobile viewport | Mobile schedule page renders correctly at 375px width |
| MOB-15 | Touch targets | All buttons/links at least 44x44px (mobile accessibility) |

---

## 18. Inventory and Catalog

### What "works" means

Parts and materials are tracked with stock levels, custom catalogs can be managed, and items can be associated with jobs.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| INV-01 | Inventory page renders | Shows parts list with name, SKU, quantity, price |
| INV-02 | Custom catalogs | GET /api/catalogs returns catalog list |
| INV-03 | Catalog items | GET /api/catalogs/{id}/items returns items in catalog |
| INV-04 | Add catalog item | POST creates item, appears in catalog |
| INV-05 | Delete catalog item | DELETE removes item from catalog |
| INV-06 | Parts search | Search inventory by name/SKU |

---

## 19. Settings and Configuration

### What "works" means

Tenant settings (company name, branding, modules, users) can be viewed and updated. Changes persist and take effect immediately.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| SET-01 | Settings page renders | Shows settings tabs (general, branding, modules, users) |
| SET-02 | Update company name | Change company name, save, name appears in header/PDFs |
| SET-03 | Update branding | Change colors/logo, save, reflected across app |
| SET-04 | Module enable/disable | POST /api/settings/modules/{key}/enable and /disable toggles module |
| SET-05 | Module gating takes effect | After disabling "estimates" module, /api/estimates returns 403 |
| SET-06 | User management | GET /api/settings/users lists users, POST creates user |
| SET-07 | User permissions | POST /api/settings/permissions updates role permissions |
| SET-08 | API key management | CRUD on API keys for integrations |
| SET-09 | Notification settings | GET/PUT /api/notifications/settings configures notification preferences |
| SET-10 | Branding endpoint | GET /api/settings/branding returns current branding for frontend |

---

## 20. PDF Generation

### What "works" means

PDFs are generated with correct content -- not blank pages, not missing data, not broken formatting.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| PDF-01 | Estimate PDF content | Download PDF, parse with pdf-parse, verify: estimate number present, customer name present, line items listed with correct totals, grand total matches |
| PDF-02 | Invoice PDF content | Same as above for invoices: invoice number, customer, lines, subtotal, tax, total, balance due |
| PDF-03 | PDF branding | Company name, logo, primary/secondary colors from tenant settings appear in PDF |
| PDF-04 | PDF with no lines | Estimate/invoice with zero lines generates PDF without error (shows $0.00 total) |
| PDF-05 | PDF with many lines | 50+ line items all appear, pagination works |
| PDF-06 | PDF with unicode | Customer name "O'Brien & Munoz" renders correctly |
| PDF-07 | PDF file size | Generated PDF is reasonable size (< 5MB for typical invoice) |
| PDF-08 | PDF is valid | PDF passes validation (not corrupted, opens in viewer) |

---

## 21. Module Gating

### What "works" means

When a module is disabled for a tenant, all API endpoints for that module return 403, and the Vue UI hides the corresponding navigation items and pages.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| MOD-01 | All modules enabled | Every API endpoint returns 200 (not 403) |
| MOD-02 | Disable jobs module | POST /modules/jobs/disable, then GET /api/jobs returns 403 |
| MOD-03 | Disable estimates module | GET /api/estimates returns 403, Estimates link hidden in Vue sidebar |
| MOD-04 | Disable invoices module | GET /api/invoices returns 403 |
| MOD-05 | Disable timeclock module | GET /api/timeclock/*and /api/labor/* return 403 |
| MOD-06 | Disable equipment_tracking | GET /api/equipment returns 403 |
| MOD-07 | Disable communications | GET /api/sms/*, GET /api/notifications/* return 403 |
| MOD-08 | Disable fleet | GET /api/fleet/* returns 403 |
| MOD-09 | Disable documents | GET /api/documents/*, file upload endpoints return 403, PDF endpoints return 403 |
| MOD-10 | Disable stripe_connect | GET /api/stripe/connect/* returns 403 |
| MOD-11 | Disable dispatch | GET /api/technicians returns 403, WebSocket connection rejected |
| MOD-12 | Disable loyalty | Loyalty, referrals, reviews endpoints return 403 |
| MOD-13 | Disable google_maps | Maps/geocoding endpoints return 403 |
| MOD-14 | Disable customer_portal | Booking endpoints return 403 |
| MOD-15 | Disable segments | Segments endpoints return 403 |
| MOD-16 | Disable warranties | Warranties endpoints return 403 |
| MOD-17 | Disable mobile | Mobile endpoints return 403 |
| MOD-18 | Disable inventory | Catalog endpoints return 403 |
| MOD-19 | Re-enable module | After disabling and re-enabling, endpoints work again |
| MOD-20 | Vue sidebar reflects modules | Disabled module's nav link not rendered in sidebar |

---

## 22. Multi-Tenant Isolation

### What "works" means

Tenant A's data is completely invisible to Tenant B. No cross-tenant data leakage through any path: API, WebSocket, file storage, caching, search, or exports.

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| TENANT-01 | Data isolation: customers | Tenant A creates customer, Tenant B's GET /api/customers does not return it |
| TENANT-02 | Data isolation: jobs | Tenant A's job not visible to Tenant B |
| TENANT-03 | Data isolation: invoices | Tenant A's invoice not accessible by Tenant B |
| TENANT-04 | Data isolation: documents | Tenant A's uploaded file not downloadable by Tenant B |
| TENANT-05 | Data isolation: estimates | Tenant B cannot access Tenant A's estimates |
| TENANT-06 | IDOR prevention | Tenant B with valid token cannot GET /api/jobs/{tenant_a_job_id} |
| TENANT-07 | WebSocket isolation | Tenant A's dispatch board messages not received by Tenant B |
| TENANT-08 | File storage isolation | Tenant A's files stored under /uploads/tenant_a_id/, not accessible by Tenant B's path |
| TENANT-09 | Search isolation | Search results only return current tenant's data |
| TENANT-10 | Export isolation | Data exports contain only current tenant's data |
| TENANT-11 | Audit log isolation | Tenant A's audit log not visible to Tenant B |
| TENANT-12 | Settings isolation | Tenant A's settings change does not affect Tenant B |
| TENANT-13 | Missing tenant header | Request without x-tenant-id header returns 400/403, not data from random tenant |
| TENANT-14 | Invalid tenant ID | Request with non-existent tenant ID returns 404 or 403 |
| TENANT-15 | Tenant header spoofing | Tenant B sends request with Tenant A's x-tenant-id but Tenant B's JWT: denied |

---

## 23. Automations

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| AUTO-01 | List automations | GET /api/automations returns automation sequences |
| AUTO-02 | Create automation | POST creates sequence with steps |
| AUTO-03 | Add steps | POST /{id}/steps adds action steps (send SMS, wait, send email) |
| AUTO-04 | Pause/resume | POST /{id}/pause and /{id}/resume toggle execution |
| AUTO-05 | Enrollments | POST enrolls customer, GET /{id}/enrollments lists enrolled customers |
| AUTO-06 | Automation execution | Enrolled customer triggers automation steps in sequence with correct delays |

---

## 24. Segments

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| SEG-01 | List segments | GET /api/segments returns segment definitions |
| SEG-02 | Create segment | POST with filter criteria creates segment |
| SEG-03 | Segment membership | Customers matching criteria are in segment, others are not |
| SEG-04 | Delete segment | Soft delete, segment removed from list |

---

## 25. Warranties

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| WARR-01 | List warranties | GET /api/warranties returns warranty records |
| WARR-02 | Create warranty | POST creates warranty linked to equipment/job |
| WARR-03 | Expiring warranties | GET /expiring returns soon-to-expire warranties |
| WARR-04 | File claim | POST /{id}/claim records warranty claim |
| WARR-05 | Delete warranty | Soft delete |

---

## 26. Technicians

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| TECH-01 | List technicians | GET /api/technicians returns tech list with name, skills, availability |
| TECH-02 | Technician detail | GET /{id} returns full profile |
| TECH-03 | Add skills | POST /{id}/skills adds skill tags |
| TECH-04 | Set unavailability | POST /{id}/unavailability blocks time slots |
| TECH-05 | Delete technician | Soft delete, removed from dropdown lists |
| TECH-06 | Technician dashboard | GET /dashboard returns tech's job stats |

---

## 27. Expenses

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| EXP-01 | List expenses | GET /api/expenses returns expense records |
| EXP-02 | Create expense | POST with category, amount, date creates expense |
| EXP-03 | Expense lines | POST /expenses/{id}/lines adds line items |
| EXP-04 | Expense categories | GET /expense-categories returns category list |
| EXP-05 | Delete expense | Soft delete |

---

## 28. Maps and Geocoding

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| MAP-01 | Geocode address | POST /api/maps/geocode with address returns lat/lng |
| MAP-02 | Reverse geocode | POST /api/maps/reverse-geocode with lat/lng returns address |
| MAP-03 | Drive time | POST /api/maps/drive-time with origin/destination returns minutes |
| MAP-04 | Route optimization | POST /api/maps/optimize-route with multiple stops returns optimal order |
| MAP-05 | Service area check | POST /api/maps/check-service-area returns whether address is in service area |
| MAP-06 | Google Maps API down | Graceful degradation, returns error not 500 crash |

---

## 29. Booking and Customer Portal

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| BOOK-01 | Available slots | GET /api/booking/available-slots returns bookable time slots |
| BOOK-02 | Book appointment | POST /api/booking/booking creates appointment request |
| BOOK-03 | Booking requests | GET /api/booking/requests lists pending bookings for admin |
| BOOK-04 | Customer portal login | POST /api/portal/login returns customer session |
| BOOK-05 | Portal invoice payment | POST /api/portal/invoices/{id}/pay processes payment |
| BOOK-06 | Portal branding | GET /api/portal/branding returns tenant-specific branding |

---

## 30. Loyalty, Referrals, and Reviews

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| LOY-01 | Loyalty tiers | CRUD on tiers with thresholds |
| LOY-02 | Points management | Add/deduct points, balance correct |
| LOY-03 | Tier calculation | Customer with enough points auto-assigned to correct tier |
| LOY-04 | Referrals | Create referral, track conversion |
| LOY-05 | Reviews | List reviews with ratings |

---

## 31. Pricing Engine

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| PRICE-01 | Calculate price | GET /api/pricing/calculate with service type returns price |
| PRICE-02 | Bundles | CRUD on pricing bundles, bundle calculation correct |
| PRICE-03 | Customer rates | GET/POST /api/pricing/customer-rates for special pricing |
| PRICE-04 | Seasonal pricing | GET /api/pricing/seasonal returns seasonal adjustments |
| PRICE-05 | Approval rules | Discounts above threshold require approval |
| PRICE-06 | Price lock | POST /api/pricing/lock-prices locks estimate prices |
| PRICE-07 | Markup calculation | POST /api/pricing/markup applies markup to cost |
| PRICE-08 | Price comparison | GET /api/pricing/comparison compares options |
| PRICE-09 | Vendor lists | CRUD on vendor price lists |

---

## 32. QuickBooks Integration

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| QB-01 | QBO sync customers | POST /api/quickbooks/sync/customers syncs, matching customers updated |
| QB-02 | QBO sync invoices | POST /api/quickbooks/sync/invoices syncs invoice data |
| QB-03 | QBO sync items | POST /api/quickbooks/sync/items syncs service items |
| QB-04 | QBO full sync | POST /api/quickbooks/sync/full syncs all entities |
| QB-05 | QBO push invoice | POST /api/quickbooks/push/invoice/{id} pushes single invoice |
| QB-06 | QBO not configured | When QBO credentials missing, returns clear error |

---

## 33. Notifications and Push

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| NOTIF-01 | Send notification | POST /api/notifications/send delivers notification |
| NOTIF-02 | Notification history | GET /api/notifications/history returns past notifications |
| NOTIF-03 | Notification settings | GET/PUT /api/notifications/settings manages preferences |
| NOTIF-04 | Notification templates | CRUD on templates |
| NOTIF-05 | Push notification | Notification delivered to mobile device (via FCM/APNs) |

---

## 34. Job Templates

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| TMPL-01 | List templates | GET /api/job-templates returns template list |
| TMPL-02 | Create template | POST creates template with default fields |
| TMPL-03 | Apply template | POST /{id}/apply creates job from template with pre-filled fields |
| TMPL-04 | Delete template | Soft delete |

---

## 35. Recurring Jobs

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| RECUR-01 | Create recurring schedule | POST /api/recurring-jobs creates schedule (weekly/monthly) |
| RECUR-02 | List schedules | GET returns recurring job schedules |
| RECUR-03 | Schedule generates jobs | At scheduled time, new job instance created automatically |
| RECUR-04 | Delete schedule | Stops future job generation |

---

## 36. Search

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| SRCH-01 | Global search | GET /api/search?q=keyword returns results across customers, jobs, invoices |
| SRCH-02 | Search relevance | Exact match ranks higher than partial match |
| SRCH-03 | Search tenant isolation | Results only from current tenant |
| SRCH-04 | Empty search | Returns empty array, not error |
| SRCH-05 | Command palette | Vue command palette (Ctrl+K) opens, search works, results clickable |

---

## 37. Audit Trail

### Functional Tests

| ID | Test Case | Verification |
|----|-----------|-------------|
| AUDIT-01 | Audit log populated | Every create/update/delete writes audit entry |
| AUDIT-02 | Audit log query | GET /api/audit/audit-log returns entries with user, action, entity, timestamp |
| AUDIT-03 | Audit immutability | Audit entries cannot be modified or deleted via API |
| AUDIT-04 | Audit contains IP | Each entry includes request IP address |
| AUDIT-05 | Audit entity filter | GET /api/audit/entity/{type}/{id} returns entries for specific entity |

---

## 38. Edge Cases and Failure Modes (Cross-Cutting)

### Data Validation

| ID | Test Case | Verification |
|----|-----------|-------------|
| EDGE-01 | Empty required fields | POST with missing required field returns 422 with field name |
| EDGE-02 | Very long strings | 10,000 char description accepted or truncated, not 500 |
| EDGE-03 | Special characters | `O'Brien`, `<script>`, `"quotes"`, `backslash\n`, unicode emojis handled |
| EDGE-04 | SQL injection | `' OR 1=1 --` in search/filter fields returns empty results, not all records |
| EDGE-05 | XSS in stored data | Stored `<script>alert(1)</script>` rendered as text in Vue, not executed |
| EDGE-06 | Negative numbers | Negative quantity/price rejected where gt=0 specified |
| EDGE-07 | Zero values | Zero quantity/price handled correctly (not divide-by-zero) |
| EDGE-08 | Future dates | Scheduled date in year 2099 accepted without error |
| EDGE-09 | Past dates | Appropriate warning or rejection for past dates where relevant |
| EDGE-10 | UUID format | Invalid UUID in path returns 422, not 500 |

### Concurrency

| ID | Test Case | Verification |
|----|-----------|-------------|
| EDGE-11 | Concurrent updates | Two users edit same job simultaneously, last-write-wins with no data corruption |
| EDGE-12 | Concurrent creates | 10 users create jobs simultaneously, all succeed, no duplicate IDs |
| EDGE-13 | Race condition: accept + decline | Simultaneous accept and decline on estimate, one wins, other gets 409 |

### External Service Failures

| ID | Test Case | Verification |
|----|-----------|-------------|
| EDGE-14 | Stripe API down | Payment operations return clear error, app doesn't crash |
| EDGE-15 | Twilio API down | SMS send returns error, message queued or user notified |
| EDGE-16 | Google Maps API down | Geocoding returns error, job creation still works without coordinates |
| EDGE-17 | QBO API down | Sync returns error, local data unaffected |
| EDGE-18 | Database connection lost | Returns 500 with generic error, no stack trace exposed |

### Mobile/Responsive

| ID | Test Case | Verification |
|----|-----------|-------------|
| EDGE-19 | 375px viewport | All pages render without horizontal scroll |
| EDGE-20 | 768px viewport (tablet) | Sidebar collapses, content fills width |
| EDGE-21 | Touch targets | All interactive elements >= 44x44px on mobile |
| EDGE-22 | Slow network (3G) | Pages load within 10s, loading indicators shown |

### State Management

| ID | Test Case | Verification |
|----|-----------|-------------|
| EDGE-23 | Browser back button | Navigate Jobs -> Job Detail -> Back, returns to Jobs list at same scroll position |
| EDGE-24 | Page refresh | Refresh on /jobs/123 reloads job detail (not login screen if token valid) |
| EDGE-25 | Stale data | After updating job in another tab, refreshing shows updated data |

---

## 39. Security (OWASP)

### Based on OWASP ASVS 5.0 and Multi-Tenant Cheat Sheet

| ID | Test Case | Verification |
|----|-----------|-------------|
| SEC-01 | JWT validation | Malformed/expired/wrong-key tokens rejected |
| SEC-02 | CSRF protection | State-changing requests without CSRF token/header rejected |
| SEC-03 | CORS | Only allowed origins can make cross-origin requests |
| SEC-04 | Rate limiting | > 100 requests/min from same IP triggers 429 |
| SEC-05 | Path traversal | `GET /api/documents/../../etc/passwd` returns 404, not file contents |
| SEC-06 | SQL injection (parameterized queries) | `' UNION SELECT * FROM users --` returns no data, query parameterized |
| SEC-07 | NoSQL injection | Not applicable (SQL only), but test JSON injection in filters |
| SEC-08 | SSRF | File upload URL parameter cannot fetch internal network resources |
| SEC-09 | Mass assignment | POST /api/customers with `{"role": "admin"}` does not escalate privileges |
| SEC-10 | IDOR | Authenticated user cannot access other users' data by changing IDs |
| SEC-11 | Error exposure | 500 errors return generic message, not stack trace/SQL query |
| SEC-12 | Security headers | X-Content-Type-Options, X-Frame-Options, CSP, HSTS present |
| SEC-13 | Password storage | Passwords hashed with bcrypt/argon2, not stored plaintext |
| SEC-14 | JWT in httpOnly cookie | If using cookies, they are httpOnly and Secure |
| SEC-15 | Tenant context from token | Tenant ID derived from JWT claims, not just request header |
| SEC-16 | Per-tenant rate limiting | Tenant A's rate limit does not affect Tenant B |
| SEC-17 | API key scoping | API key only accesses authorized endpoints |
| SEC-18 | Webhook signature validation | Invalid Stripe/Twilio signatures rejected |
| SEC-19 | File upload scanning | Uploaded files checked for malicious content |
| SEC-20 | Sensitive data in logs | No passwords, tokens, or PII in application logs |

---

## 40. Accessibility (WCAG 2.1 AA)

### Automated with @axe-core/playwright on every page

| ID | Test Case | Verification |
|----|-----------|-------------|
| A11Y-01 | No critical violations | Axe scan on every page returns 0 critical/serious violations |
| A11Y-02 | Form labels | Every input has associated label (for/id or aria-label) |
| A11Y-03 | Color contrast | Text meets 4.5:1 contrast ratio (normal text), 3:1 (large text) |
| A11Y-04 | Keyboard navigation | Tab through all interactive elements in logical order |
| A11Y-05 | Focus indicators | Focused elements have visible outline |
| A11Y-06 | ARIA landmarks | Main, nav, banner, contentinfo landmarks present |
| A11Y-07 | Alt text | All images have alt text (empty alt="" for decorative) |
| A11Y-08 | Error messages | Form errors announced to screen readers (aria-live or role="alert") |
| A11Y-09 | Modal trap | Focus trapped inside open modal, Escape closes modal |
| A11Y-10 | Skip link | "Skip to content" link present, works |
| A11Y-11 | Heading hierarchy | h1 -> h2 -> h3, no skipped levels |
| A11Y-12 | Table headers | Data tables have th elements with scope |

---

## 41. Performance and Core Web Vitals

### Measured with Playwright + Lighthouse integration

| ID | Test Case | Verification |
|----|-----------|-------------|
| PERF-01 | LCP (Largest Contentful Paint) | < 2.5s on every page |
| PERF-02 | CLS (Cumulative Layout Shift) | < 0.1 on every page |
| PERF-03 | INP (Interaction to Next Paint) | < 200ms for button clicks |
| PERF-04 | Time to Interactive | < 3s on dashboard |
| PERF-05 | API response times | P95 < 500ms for list endpoints, < 200ms for single-entity endpoints |
| PERF-06 | Bundle size | JS bundle < 500KB gzipped |
| PERF-07 | Image optimization | No uncompressed images > 200KB |
| PERF-08 | Lazy loading | Non-critical routes lazy-loaded (already done in router) |
| PERF-09 | Database query performance | No N+1 queries, all list endpoints use JOINs or eager loading |
| PERF-10 | WebSocket latency | Message broadcast < 100ms |

---

## 42. Visual Regression

### Baseline screenshots compared across deploys

| ID | Test Case | Verification |
|----|-----------|-------------|
| VIS-01 | Dashboard screenshot | Pixel-diff < 0.1% from baseline |
| VIS-02 | Jobs list screenshot | Table layout, column widths, row styling match baseline |
| VIS-03 | Job detail screenshot | All sections rendered, no collapsed/hidden elements |
| VIS-04 | Estimate detail screenshot | Line items table, totals section, action buttons visible |
| VIS-05 | Invoice detail screenshot | Financial data layout matches baseline |
| VIS-06 | Mobile view screenshots | Each page at 375px matches mobile baseline |
| VIS-07 | Dark mode screenshots | (If applicable) dark theme renders correctly |
| VIS-08 | Empty state screenshots | Pages with no data show correct empty states |
| VIS-09 | Modal screenshots | Every modal dialog captured and compared |
| VIS-10 | PDF screenshots | Generated PDFs rendered and compared |

---

## 43. Chaos and Resilience

### Fault injection to verify graceful degradation

| ID | Test Case | Verification |
|----|-----------|-------------|
| CHAOS-01 | Redis down | App starts, non-cached features work, cache features degrade gracefully |
| CHAOS-02 | Celery down | Background tasks queued, not lost, sync operations still work |
| CHAOS-03 | Stripe down | Payment pages show "temporarily unavailable", app doesn't crash |
| CHAOS-04 | Twilio down | SMS operations fail gracefully, notification queued |
| CHAOS-05 | Google Maps down | Map features disabled, job creation still works |
| CHAOS-06 | Database slow (1s latency) | App responds slowly but doesn't timeout, no data corruption |
| CHAOS-07 | Disk full | File upload returns 500 with clear error, doesn't corrupt existing files |
| CHAOS-08 | Memory pressure | Under 90% memory, app remains responsive (no OOM kill) |
| CHAOS-09 | High concurrent load | 100 concurrent requests, no 500 errors, p99 < 5s |
| CHAOS-10 | WebSocket flood | 1000 messages/second on WebSocket, server rate-limits, doesn't crash |

---

## 44. CI/CD Pipeline Design

### Pipeline Stages (in order)

```
1. LINT + TYPE CHECK (30s)
   - ruff check (Python)
   - eslint (JavaScript)
   - pyright/mypy (type checking)
   
2. UNIT TESTS (1 min)
   - pytest (Python units)
   - vitest (Vue component tests)
   
3. SCHEMA VALIDATION (30s)
   - Alembic migration check
   - Schema drift check
   
4. API CONTRACT TESTS (3 min)
   - Every endpoint: correct status code, response schema, data types
   - Module gating tests
   - Tenant isolation tests
   
5. SECURITY SCAN (2 min)
   - Semgrep rules
   - Dependency audit (pip-audit, npm audit)
   - OWASP ZAP baseline scan
   
6. E2E BROWSER TESTS (10 min)
   - Playwright: full user journeys
   - Run against ephemeral environment (Docker Compose up)
   - Parallel across 3 workers (Chromium, Firefox, WebKit)
   
7. ACCESSIBILITY AUDIT (3 min)
   - @axe-core/playwright on every page
   - Fail on critical/serious violations
   
8. VISUAL REGRESSION (5 min)
   - Playwright screenshots vs baselines
   - Auto-update baselines on intentional UI changes
   
9. PERFORMANCE BUDGET (2 min)
   - Lighthouse CI on key pages
   - Fail if LCP > 2.5s, CLS > 0.1
   
10. DEPLOY TO STAGING (2 min)
    - Docker build + push
    - Deploy to staging environment
    
11. SMOKE TESTS ON STAGING (1 min)
    - Health check
    - Login flow
    - Create + read one job
    
12. MANUAL APPROVAL GATE
    - For production deploys only
    
13. DEPLOY TO PRODUCTION (2 min)
    - Rolling deploy with health checks
    - Auto-rollback on health check failure
    
14. POST-DEPLOY SMOKE (1 min)
    - Same smoke tests on production
    - Verify WebSocket connectivity
    - Verify Stripe webhook endpoint reachable
```

### Total pipeline time: ~30 minutes

### Parallelization

- Stages 1-3 run in parallel (lint, unit, schema)
- Stage 4-5 run in parallel (API tests, security scan)
- Stages 6-8 run in parallel (E2E, a11y, visual)

### With parallelization: ~15 minutes

---

## 45. Automation Strategy and Tooling

### Test Data Management

```python
# Fixture hierarchy for E2E tests
@pytest.fixture(scope="session")
def test_tenant():
    """Create isolated tenant with fresh database for test run."""
    tenant = provision_test_tenant()
    yield tenant
    teardown_test_tenant(tenant)

@pytest.fixture(scope="session")
def admin_token(test_tenant):
    """Login as admin for this test tenant."""
    return login(test_tenant, role="admin")

@pytest.fixture(scope="session")
def tech_token(test_tenant):
    """Login as technician for this test tenant."""
    return login(test_tenant, role="technician")

@pytest.fixture
def test_customer(test_tenant, admin_token):
    """Create a customer for test, auto-cleanup."""
    customer = create_customer(admin_token)
    yield customer
    delete_customer(admin_token, customer.id)

@pytest.fixture
def test_job(test_tenant, admin_token, test_customer):
    """Create a job linked to test customer."""
    job = create_job(admin_token, customer_id=test_customer.id)
    yield job
    delete_job(admin_token, job.id)
```

### Multi-Tenant Test Strategy (Playwright)

```typescript
// playwright.config.ts
export default defineConfig({
  projects: [
    {
      name: 'tenant-a',
      use: {
        baseURL: 'https://tenant-a.app.com',
        storageState: '.auth/tenant-a.json',
      },
    },
    {
      name: 'tenant-b',
      use: {
        baseURL: 'https://tenant-b.app.com',
        storageState: '.auth/tenant-b.json',
      },
    },
    {
      name: 'cross-tenant',
      dependencies: ['tenant-a', 'tenant-b'],
      // Tests that verify isolation between tenants
    },
  ],
});
```

### Reporting

- **JUnit XML**: For CI integration (GitHub Actions, GitLab CI)
- **HTML Report**: Playwright HTML reporter with screenshots on failure
- **Slack Notification**: On pipeline failure, post to #engineering with:
  - Failed test name
  - Screenshot of failure
  - Link to pipeline logs
- **Trend Dashboard**: Track pass rate, test duration, flaky tests over time

### Flaky Test Management

1. Mark flaky tests with `@pytest.mark.flaky(retries=2)`
2. Track flaky rate per test over 30-day window
3. If flaky rate > 10%, investigate root cause
4. Never disable a flaky test -- fix it or delete it

### Test Maintenance

- **Page Object Model**: Abstract Vue page interactions into reusable classes
- **API Client Wrapper**: Thin wrapper around httpx with auth + tenant headers built-in
- **Shared Assertions**: `assert_api_success(response, expected_status=200)` with schema validation
- **Data Builders**: Factory functions for creating test entities with sensible defaults

---

## Real-World Failures This Checklist Prevents

These are actual failure modes documented in industry post-mortems that only test "loads without errors":

1. **Form renders but submit button has no click handler** -- JOB-08 catches this
2. **Dropdown renders but API returns empty array** -- JOB-06 catches this
3. **Invoice total shows $0 because tax calculation divides by zero** -- INV-04 catches this
4. **PDF generates but is blank because branding query fails silently** -- PDF-01, PDF-03 catch this
5. **Stripe checkout opens but uses wrong API key for tenant** -- PAY-03 catches this
6. **WebSocket connects but tenant_id not set, receives all tenants' data** -- DISP-11 catches this
7. **File upload succeeds (200) but file not written to disk** -- DOC-10 catches this
8. **Search works but returns other tenants' results** -- SRCH-03 catches this
9. **Module disabled in settings but API still serves data** -- MOD-02 through MOD-18 catch this
10. **Estimate accepted twice due to race condition** -- EDGE-13 catches this
11. **Hardcoded tax rate ships instead of configured rate** -- INV-04 catches this (verify against settings)
12. **Mobile page renders at desktop but overflows on mobile** -- EDGE-19 catches this
13. **Clock-out without clock-in creates negative duration** -- TIME-09 catches this
14. **Audit log missing entries because async write fails silently** -- AUDIT-01 catches this
15. **Dashboard shows yesterday's data due to timezone bug** -- DASH-02 catches this (compare to API)

---

## Summary Statistics

| Category | Test Cases |
|----------|-----------|
| Auth & Authorization | 14 |
| Dashboard | 10 |
| Jobs Workflow | 22 |
| Customer Management | 14 |
| Dispatch & WebSocket | 13 |
| Estimates | 17 |
| Invoicing & Billing | 17 |
| Payments & Stripe | 12 |
| Timeclock & Labor | 10 |
| Equipment | 7 |
| Communications | 9 |
| Campaigns & Marketing | 8 |
| Reports | 6 |
| Documents & Uploads | 11 |
| Fleet | 5 |
| Mobile Technician | 15 |
| Inventory & Catalog | 6 |
| Settings | 10 |
| PDF Generation | 8 |
| Module Gating | 20 |
| Multi-Tenant Isolation | 15 |
| Automations | 6 |
| Segments | 4 |
| Warranties | 5 |
| Technicians | 6 |
| Expenses | 5 |
| Maps & Geocoding | 6 |
| Booking & Portal | 6 |
| Loyalty & Referrals | 5 |
| Pricing Engine | 9 |
| QuickBooks | 6 |
| Notifications | 5 |
| Job Templates | 4 |
| Recurring Jobs | 4 |
| Search | 5 |
| Audit Trail | 5 |
| Edge Cases (cross-cutting) | 25 |
| Security (OWASP) | 20 |
| Accessibility (WCAG) | 12 |
| Performance | 10 |
| Visual Regression | 10 |
| Chaos & Resilience | 10 |
| **TOTAL** | **~445** |

---

## Sources and References

- [Optimizing SaaS E2E Testing](https://www.qable.io/blog/saas-e2e-testing)
- [SaaS Testing Guide and Tools in 2026](https://bugbug.io/blog/software-testing/saas-testing-guide-and-tools/)
- [Best Practices for End-to-End Testing in 2026 | Bunnyshell](https://www.bunnyshell.com/blog/best-practices-for-end-to-end-testing-in-2025/)
- [Scaling E2E Tests for Multi-Tenant SaaS with Playwright | CyberArk Engineering](https://medium.com/cyberark-engineering/scaling-e2e-tests-for-multi-tenant-saas-with-playwright-c85f50e6c2ae)
- [Playwright Best Practices](https://playwright.dev/docs/best-practices)
- [Playwright Isolation](https://playwright.dev/docs/browser-contexts)
- [Multi Tenant Security - OWASP Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html)
- [OWASP Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/)
- [SaaS Security Checklist: 50+ Must-Haves | DesignRevision](https://designrevision.com/blog/saas-security-checklist)
- [Unit Tests Passed. The Bug Shipped Anyway. | Optivem](https://journal.optivem.com/p/unit-tests-passed-the-bug-shipped)
- [Automate Accessibility Testing with Playwright and Axe](https://dev.to/subito/how-we-automate-accessibility-testing-with-playwright-and-axe-3ok5)
- [Achieving WCAG Standard with Playwright Accessibility Tests](https://medium.com/@merisstupar11/achieving-wcag-standard-with-playwright-accessibility-tests-f634b6f9e51d)
- [Testing File Uploads, Downloads, and PDFs Using Playwright](https://medium.com/@ayushbhavsar1402/testing-file-uploads-downloads-and-pdfs-using-playwright-cd1de7bb2315)
- [Smoke Test Checklist Documentation | Yuri Kan](https://yrkan.com/blog/smoke-test-checklist-docs/)
- [Functional Testing: A Detailed Guide (2026) | BrowserStack](https://www.browserstack.com/guide/functional-testing)
