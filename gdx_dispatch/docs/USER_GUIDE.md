# GDX Platform — User Guide

## Getting Started

### Login

Navigate to your company's URL (e.g., `acme.example.com`) and enter your email and password. Use Ctrl+K to access the global search from any page.

### Dashboard

Your dashboard shows key metrics: revenue, active jobs, overdue invoices, and upcoming appointments. Click any KPI card to drill into the detail.

## Core Workflows

### Jobs

1. Click **Jobs** in the sidebar
2. Click **New Job** to create a job
3. Fill in customer, title, job type, and schedule
4. Assign a technician from the dispatch board

### Customers

1. Click **Customers** in the sidebar
2. Search by name, phone, or email
3. Click a customer to see their history (jobs, invoices, notes)
4. Click **New Customer** to add one

### Estimates

1. Click **Estimates** in the sidebar
2. Click **New Estimate**, select the customer/job
3. Add line items with description, quantity, and price
4. Click **Send** to email the estimate to the customer
5. Customer can accept or decline via the public link

### Invoices

1. Create from an accepted estimate (click **Convert to Invoice**)
2. Or create directly from a job
3. Send to customer for online payment
4. Record payments as they come in

### Dispatch Board

1. Click **Dispatch** in the sidebar
2. Drag unassigned jobs onto technician columns
3. Jobs update in real-time via WebSocket
4. Use the date picker to view different days

### Catalogs

Catalogs hold the products and services you sell.

1. Click **Catalogs** in the sidebar
2. Click **+ New Catalog** and choose a type:
   - **Parts** — SKUs, accessories, miscellaneous
   - **Doors** — garage doors with full install specs
   - **Custom…** — define your own fields for any industry (HVAC, electrical, …)
3. For **Custom…**, add your fields (label + type) before creating
4. Add items, or bulk-load with **Import CSV** / **AI Import**

See [Custom Catalogs](CUSTOM_CATALOGS.md) for the full guide to custom types.

## Tips

- **Ctrl+K** — Global search across jobs, customers, invoices
- **Dark/Light mode** — Click the sun/moon icon in the topbar
- **Mobile** — Visit `/mobile` for the technician mobile schedule
