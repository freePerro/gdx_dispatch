# Sprint 5 + 6 — Workflow Map

What a tech, dispatcher, and admin actually do across the new surfaces, and which endpoint backs each step. Updated 2026-05-03 (S92).

---

## Tech workflow — a job, end-to-end

```
clock in (TimeclockView)
   │ POST /api/timeclock/clock-in
   │ ↳ opens TimeclockEntry (tenant-plane, technician_id=user)
   ▼
GPS breadcrumb starts (MobileTodayView)
   │ navigator.geolocation → POST /api/mobile/location every 30s
   │ Server gate: refuses 403 if no open clock-in (S5-C4 privacy)
   ▼
open job (JobDetailView)
   │ GET /api/jobs/{id} → returns is_callback flag (S5-A4)
   │ GET /api/jobs?customer_id=… → past visits (S5-A2)
   │ GET /api/customers/{cid} → customer notes (S5-A5)
   │ GET /api/equipment?customer_id=… → equipment list with warranty/install_date (S5-A1, S5-A3)
   ▼
en route — auto-arrival prompt (future UI)
   │ Frontend polls GET /api/jobs/{id}/arrival-check (S5-C3)
   │ Server: distance to CustomerLocation.lat/lng, dwell time
   │ should_prompt=true → tech taps "Mark arrived?"
   ▼
on site
   │ Diagnosis tab → POST /api/jobs/{id}/diagnosis (S5-B1)
   │   (per-service-type schema from /api/diagnosis/schemas)
   │ Hazards tab → POST /api/jobs/{id}/hazards (S5-B2)
   │   (sticky=true persists hazard on customer for future jobs)
   │ Receipts tab → POST /api/jobs/{id}/receipts (S5-B3)
   ▼
finish job
   │ existing complete-job + signature flows (Sprint 4)
   ▼
log vehicle inspection (TimeclockView, if enabled)
   │ POST /api/vehicle-inspections (S6-B1)
   │ Visibility gated by tech_mobile.vehicle_inspection setting
   │   (off / daily / weekly — read at mount via /api/me/tech-mobile-settings)
   ▼
clock out
   │ POST /api/timeclock/clock-out
   │ ↳ closes TimeclockEntry; GPS breadcrumb stops on next 403
   ▼
end of day review (TimeclockView)
   │ Tech sees today's hours + entry count
   │ POST /api/timeclock/submit-day {date} (S6-A4) — confirmation + count
```

## Dispatcher workflow

```
DispatchView
   │ Existing: jobs board (drag-drop, filters)
   ▼
Live Techs panel (S5-C2)
   │ GET /api/dispatch/locations?minutes=30
   │ ↳ Latest sample per user_id, click row → Google Maps
   │ Polls every 30s
```

## Admin workflow — settings

```
TechMobileSettingsView (admin)
   │ GET /api/admin/feature-settings/tech-mobile
   │   ↳ catalog + tenant overrides + resolved values
   │ PUT /api/admin/feature-settings/tech-mobile
   │   ↳ writes overrides to AppSettings.tenant_mobile_settings
   │
   │ Sprint 5/6 keys (phase 5.3 + 6.1 + 6.2):
   │   tech_mobile.gps_breadcrumb_enabled       (master switch, default true)
   │   tech_mobile.gps_breadcrumb_interval_seconds  (10–600, default 30)
   │   tech_mobile.gps_retention_days           (7–365, default 45)
   │   tech_mobile.gps_arrival_distance_m       (10–1000, default 100)
   │   tech_mobile.gps_arrival_dwell_seconds    (30–600, default 120)
   │   tech_mobile.diagnosis_required           (required/optional)
   │   tech_mobile.hazard_photo_required        (required/optional)
   │   tech_mobile.receipt_photo_required       (required/optional)
   │   tech_mobile.break_tracking               (off/optional/required)
   │   tech_mobile.vehicle_inspection           (off/daily/weekly)
   │   tech_mobile.callback_window_days         (existing, default 90)
```

## Background jobs (Celery beat)

```
tech-locations-prune-daily-3am
   │ gdx_dispatch.tasks.tech_locations_prune.prune_tech_locations_for_all_tenants
   │ For each tenant: read tech_mobile.gps_retention_days,
   │   DELETE FROM tech_locations WHERE recorded_at < NOW() - days
   │ S5-C5
```

## Data model — tenant-plane tables added in Sprint 5/6

| Table                  | Sprint  | Purpose                                                     |
|------------------------|---------|-------------------------------------------------------------|
| `customer_equipments` (extended) | S5-A1, A3 | install_date already present; +warranty_expires_on column   |
| `job_diagnoses`        | S5-B1   | per-service-type structured diagnosis (data JSONB)          |
| `job_hazards`          | S5-B2   | safety hazards, sticky-to-customer optional                  |
| `job_receipts`         | S5-B3   | road-purchase receipts                                       |
| `tech_locations`       | S5-C1   | GPS breadcrumb (lat/lng/accuracy/recorded_at)               |
| `vehicle_inspections`  | S6-B1   | DOT pre/post-trip + fuel log                                |

Three-plane invariant honored: every new table is on the tenant-plane;
isolation is the connection. No `tenant_id` column on any new table.

## Removed / unwired (consolidation)

- `equipment_tracking_router` (legacy `EquipmentAsset` surface) — unwired
  from app.py. The canonical `gdx_dispatch/modules/equipment/router.py` now serves
  every `/api/equipment*` path.
- Legacy `equipment_assets` table is preserved as a read-archive; the
  consolidation migration backfills any rows into `customer_equipments`
  and the table can be dropped in a follow-up sprint after confirming
  zero reads on prod.
- `/api/equipment-tracking` shim in `ui_compat.py` still returns empty
  lists for any historical caller (kept until a frontend audit confirms
  no view depends on it).

See `ai-queue/brainstorm/gap_equipment_router_consolidation.md` for the
audit that produced this plan.
