# Secrets Rotation Runbook — GDX Platform

**Status: CURRENT** — container and env names corrected 2026-09-06 (they named a control-plane database this install never had). Newer of the two secrets-rotation docs (2026-08-31); see
also `SECRETS_ROTATION.md`, which overlaps and has not been reconciled with
this one.


## Overview

All secrets must be rotated on a regular schedule or immediately after suspected compromise.

## Secret Inventory

| Secret | Location | Rotation Period | Impact |
|--------|----------|----------------|--------|
| DB passwords | Docker env, .env | 90 days | All services |
| JWT signing key | JWT_SECRET_KEY in .env | 90 days | All user sessions invalidated |
| Stripe API keys | STRIPE_SECRET_KEY in .env | On compromise only | Billing disrupted |
| Stripe webhook secret | STRIPE_WEBHOOK_SECRET | On key rotation | Webhook verification fails |
| Google Maps API key | GOOGLE_MAPS_API_KEY | On compromise only | Maps/routing |
| AWS credentials | AWS_ACCESS_KEY_ID/SECRET | 90 days | Backups, SES, S3 |
| Redis password | REDIS_URL in .env | 90 days | Cache, Celery |

## Rotation Procedures

### 1. Database Passwords

```bash
# The production stack is compose project `gdx`: docker-compose.yml plus
# docker-compose.prod.yml, named by the COMPOSE_FILE line in .env (update.sh
# reads the same line). Without `-p gdx` compose addresses a project that does
# not exist: `stop` stops nothing and `up` creates a SECOND stack. Never add
# `-f …docker-compose.yml` alone: it drops the prod overlay and its volumes,
# and GDX_REQUIRE_EXISTING_DATA=1 then refuses to boot. Run from the checkout
# root, where .env lives:
COMPOSE=(docker compose -p gdx --env-file ./.env)

# 1. Generate a new password. secrets-init accepts only letters, digits and
#    _.:/@%+=,- ; base64 stays inside that set.
NEW_PW=$(openssl rand -base64 32)

# 2. Change it in PostgreSQL. The old password stops working here, so do
#    steps 2-5 back to back.
"${COMPOSE[@]}" exec -T db psql -U gdx -d gdx -c "ALTER USER gdx PASSWORD '$NEW_PW';"

# 3. Put it in .env as DB_PASSWORD=<new>. DATABASE_URL is not set in .env:
#    secrets-init derives it from DB_PASSWORD.

# 4. Re-mint the stored copy. secrets-init keeps the password it stored in the
#    gdx_secrets volume (runtime.env) over any .env value, because postgres
#    fixed it at first init. Editing .env alone therefore changes nothing and
#    the app keeps the old, now-refused password. Delete the stored line, then
#    run secrets-init: it takes the .env value and rewrites runtime.env
#    (DB_PASSWORD, DATABASE_URL) and db_password (read by the backup service).
"${COMPOSE[@]}" run --rm --no-deps --entrypoint sh secrets-init \
  -c "sed -i '/^DB_PASSWORD=/d' /secrets/runtime.env"
"${COMPOSE[@]}" run --rm --no-deps secrets-init

# 5. Recreate everything that reads runtime.env or db_password at start (the
#    app container carries the Celery workers and beat).
"${COMPOSE[@]}" up -d --force-recreate app plugin-host backup

# 6. Verify
"${COMPOSE[@]}" exec -T app curl -sf http://localhost:8000/health
```

### 2. JWT Signing Key

```bash
# 1. Generate new key
NEW_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")

# 2. Update .env
# Set JWT_SECRET_KEY=$NEW_KEY

# 3. Restart app (all active sessions will be invalidated)
"${COMPOSE[@]}" restart app   # COMPOSE as defined in step 1

# 4. Verify login works
curl -sk -X POST https://gdx.example.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@test.com","password":"..."}'
```

**Note:** All users will need to re-login after JWT key rotation.

### 3. Stripe API Keys

```bash
# 1. Generate new keys in Stripe Dashboard → Developers → API Keys
# 2. Update STRIPE_SECRET_KEY in .env
# 3. Update STRIPE_WEBHOOK_SECRET for new webhook endpoint
# 4. Restart app
# 5. Verify: Create a test payment
```

### 4. AWS Credentials

```bash
# 1. Create new IAM access key in AWS Console
# 2. Update AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env
# 3. Delete old key in AWS Console
# 4. Verify backups: see RESTORE_RUNBOOK.md — nothing in this repo ships
#    backups to S3 (the script that did was removed 2026-09-06)
```

## Emergency Rotation (Suspected Compromise)

1. **Immediately** rotate ALL secrets listed above
2. Invalidate all active sessions (rotate JWT key)
3. Check audit logs for unauthorized access: `docker exec gdx-app python -c "from gdx_dispatch.core.audit import ...; ..."`
4. Review Server Logs (`/server-errors`, the in-app error sink) for unusual errors
5. Check Stripe Dashboard for unauthorized charges
6. Notify affected tenants if data exposure suspected

## Verification Checklist

After any rotation:

- [ ] `curl -sk https://gdx.example.com/health` returns 200
- [ ] Login works for at least one admin account
- [ ] Stripe webhook test event succeeds
- [ ] SMS sending works — send a text from Phone → SMS (`POST /api/phone-com/messages`, modules/phone_com/router.py; Phone.com is the live SMS line). The job "on-my-way" text and the Twilio sender behind it were removed 2026-09-06 (Twilio was never configured here); the Settings toggle "Text customer 'Tech is on the way'" still only logs an intent. (`/api/sms/send` was a fake-success stub, removed in #350.)
- [ ] Backup script runs without error
- [ ] Celery Beat is scheduling tasks (check logs)
