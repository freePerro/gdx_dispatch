# Twilio removal — the SMS provider that was never configured

Status: PLAN 2026-09-06, audited and built the same day on `chore/remove-twilio`; not merged. Owner decision "twilio can be removed" (2026-09-06). Update to MERGED #N when the PR lands.

## What already exists (do not rebuild)

- **Phone.com is the SMS and voice system.** `modules/phone_com/` owns 8
  tables; prod holds 165 rows in `phone_com_messages`. Its client exposes
  `send_message` (`modules/phone_com/client.py`), called from that module's
  router (`POST /api/phone-com/messages`). Nothing in this plan touches it.
- **The inbound-email webhook is real and secured** (#603): `POST
  /api/inbound-email/webhook` with `INBOUND_EMAIL_WEBHOOK_SECRET`, plus four
  admin routes over `inbound_emails`. It shared `routers/inbound_comms.py`
  with the Twilio SMS half and stays.
- **A user-facing "text the customer" toggle already exists** and is the
  right target for any future SMS work: Settings → Workflow → "Text customer
  'Tech is on the way'" (`SettingsView.vue`, `workflow_sms_arrival_notify` in
  `core/tenant_settings.py`). Its only consumer, `routers/jobs.py` inside
  `POST /jobs/{id}/start`, logs an intent and sends nothing. Found by the
  audit of this plan; filed as a follow-up issue, not fixed here.

## Why delete rather than fix (#600)

Every Twilio path was dead on prod, measured 2026-09-06:

| Fact | Prod |
|---|---|
| `app_settings.integrations.twilio` | `false` |
| `integration_configs` rows of any type | 0 |
| `inbound_sms` rows | 0 (demo: 0) |
| `TWILIO_*` env in any container | absent |
| Frontend callers of any Twilio route | 0 |
| `twilio` package in requirements | absent (imported lazily; would have failed) |

The sender returned `{"sent": False, "reason": "not configured"}` on every
call; the signature gate (#600) protected two webhooks no provider was
configured to call. Fixing the gate would harden a door that opens onto
nothing.

## What the adversarial audit changed (2026-09-06, before any code)

1. **Migration 091 is a guarded `DROP TABLE` and nothing else.** The first
   draft also stripped the `"twilio": false` key from
   `app_settings.integrations` with a Python-side `json.loads`. On Postgres
   the `JSON` column arrives as a `dict` (psycopg2 typecaster), so that would
   have raised `TypeError` inside `alembic upgrade head` and stopped prod
   from booting. It was also unnecessary: `routers/settings.py::
   _canonical_integrations` rebuilds the dict from `_ALLOWED_INTEGRATIONS` on
   every read and write, so the stale key is invisible now and rewritten out
   on the next save.
2. **`POST /api/jobs/{job_id}/on-my-way` is deleted, not kept honest.** It
   had zero frontend callers: the appointment card posts to
   `/api/appointments/{id}/on-my-way` and mobile posts to
   `/api/mobile/jobs/{id}/en-route`, neither of which ever sent SMS. Keeping
   an audit row that says `no_sms_transport` on a route nobody can reach is
   theater. Its tests (`test_sms_send_honesty.py` <!-- link-ok: deleted -->) go with it.
3. **The follow-up targets the toggle users can see**, not the orphan route
   (see "What already exists").
4. Eight doc lines in archived plans and one live plan
   (`contact-opt-out-suppression-plan.md`) named the deleted sender; the
   dead-reference scanner (`tests/test_doc_link_scan.py`) refuses a growing
   baseline, so each line carries `link-ok` and the live plan carries an
   amendment paragraph. The pg fixture `tests/fixtures/structure.sql` lost
   the table and its four indexes.

## Scope — what was removed

| Surface | Action |
|---|---|
| `core/twilio_signature.py` <!-- link-ok: deleted --> | deleted |
| `core/sms.py` <!-- link-ok: deleted --> | deleted (Twilio-only sender; both callers below) |
| `routers/voice.py` | `POST /api/communications/missed-call` deleted; `POST /api/mobile/voice-note` kept (0 callers, its own class: the never-wired PR) |
| `routers/inbound_comms.py` | SMS half deleted: `InboundSMS` import, `_serialize_sms`, `GET /api/inbound-sms`, `GET /api/inbound-sms/{id}`, `POST /api/inbound-sms/{id}/link`, `POST /api/inbound-sms/webhook`; docstring rewritten |
| `routers/dispatch_scheduling.py` | `POST /api/jobs/{job_id}/on-my-way` and `OnMyWayIn` deleted (audit item 2) |
| `models/tenant_models.py`, `models/__init__.py` | `InboundSMS` deleted |
| migration `091_drop_inbound_sms` | `has_table` guard + `DROP TABLE inbound_sms`; `downgrade()` recreates it empty |
| `core/integrations.py` | Twilio catalogue entry removed; `"twilio"` out of `_API_KEY_TYPES` |
| `routers/settings.py` | `"twilio"` out of `_ALLOWED_INTEGRATIONS` |
| `.env.template`, `docker/docker-compose.yml` | `TWILIO_*` block and the `TWILIO_AUTH_TOKEN` passthrough removed |
| `tests/authz_sweep.py` | `verify_twilio_signature` out of the recognised-gate list |
| `openapi_routes.txt` | regenerated: 6 operations gone, 1312 remain |
| `tests/fixtures/structure.sql` | `inbound_sms` table, pkey and four indexes removed |
| comments | `core/scheduler.py`, `core/inbound_email_auth.py`, `tests/test_celery_tasks.py`, `tests/test_inbound_comms.py` no longer name deleted files |
| docs (present tense) | `docs/SECRETS_ROTATION_RUNBOOK.md` (row + one sentence), `docs/E2E_VERIFICATION_MASTER_CHECKLIST.md` (seven rows → Phone.com) |

Left alone on purpose: `modules/workflows/engine.py` `SUPPORTED_ACTIONS`
`send_sms` and the `automation_action_type` enum value (Event Rules' future
executor, #349, provider-agnostic); `routers/jobs.py` arrival-text intent log
(the follow-up's target); archived design docs (annotated, not rewritten).

## Tests

- Deleted: `test_sms.py` <!-- link-ok: deleted -->, `test_sms_send_honesty.py` <!-- link-ok: deleted -->.
- `test_inbound_comms.py`: four SMS tests deleted; two mixed tests keep their
  email half.
- `test_33_integrations.py`: `twilio` → `zapier` (same API-key class); the
  catalogue count 7 → 6.
- `test_settings.py`: expected dicts lose the `twilio` key.
- `test_communications_shell_removed.py`: `SURVIVING_PREFIX_PATHS` is empty;
  the two "must still exist" guards for the missed-call route and the sender
  are inverted into absence guards in the new file.
- New `test_twilio_retired.py`, each guard naming its counterfactual: the two
  modules not importable; the six routes not registered (via
  `conftest.app_route_paths`); `InboundSMS` absent from `tenant_models` and
  `inbound_sms` absent from the metadata; no `TWILIO_` env read or `import
  twilio` in non-test source; and a self-test proving the source scan can go
  red.

## Verification

1. Local suite via the split runner (7 shards) — every FAIL/SKIP named.
2. Lint ratchet vs `.ruff_baseline`.
3. Throwaway container from this branch, real browser, light + dark: Settings
   → Integrations renders (QuickBooks card unchanged); `/openapi.json` has no
   `inbound-sms`, `missed-call` or `on-my-way` job path; `POST
   /api/inbound-sms/webhook` → 404.
4. After deploy: the same checks on prod, plus `alembic current` = 091 and
   `inbound_sms` absent from `information_schema.tables`.

## Sibling sweep (declared before the build)

Shape: **an env-gated third-party transport whose configuration check no-ops
silently when unset, plus routes or catalogue entries for a provider with
zero configuration on prod.** Surface searched: every `getenv(` naming a
credential in `gdx_dispatch/core`, `routers`, `modules`, `tasks`; every
"not configured" branch; the integrations catalogue. Instances found and NOT
fixed here (reported for the never-wired PR):

- `core/log_shipper.py` — S3 log shipping, `LOG_S3_BUCKET` never delivered,
  zero callers.
- `routers/auth/sso.py` — Google and Microsoft SSO, wired in `app.py`, no
  login button reaches it, seven env vars never delivered.
- Integrations catalogue entries `mailchimp` and `google_calendar` — no code
  behind them anywhere; `zapier` backs only the generic webhooks router.
- `POST /api/mobile/voice-note` — not Twilio, but 0 callers in the same file.

## Issues

Closes #600. Moots the Twilio row in #598. Follow-up filed: #629, the
"Text customer 'Tech is on the way'" toggle saves but never sends.
