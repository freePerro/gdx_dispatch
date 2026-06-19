# Secrets Rotation Runbook — GDX Platform

## Overview

All secrets must be rotated on a regular schedule or immediately after suspected compromise.

## Secret Inventory

| Secret | Location | Rotation Period | Impact |
|--------|----------|----------------|--------|
| DB passwords | Docker env, .env | 90 days | All services |
| JWT signing key | JWT_SECRET_KEY in .env | 90 days | All user sessions invalidated |
| Stripe API keys | STRIPE_SECRET_KEY in .env | On compromise only | Billing disrupted |
| Stripe webhook secret | STRIPE_WEBHOOK_SECRET | On key rotation | Webhook verification fails |
| Sentry DSN | SENTRY_DSN in .env | On compromise only | Error reporting |
| Twilio credentials | TWILIO_* in .env | On compromise only | SMS/voice disrupted |
| Google Maps API key | GOOGLE_MAPS_API_KEY | On compromise only | Maps/routing |
| AWS credentials | AWS_ACCESS_KEY_ID/SECRET | 90 days | Backups, SES, S3 |
| Redis password | REDIS_URL in .env | 90 days | Cache, Celery |

## Rotation Procedures

### 1. Database Passwords

```bash
# 1. Generate new password
NEW_PW=$(openssl rand -base64 32)

# 2. Update PostgreSQL
docker exec gdx-control-db psql -U gdx -c "ALTER USER gdx PASSWORD '$NEW_PW';"

# 3. Update .env on VPS
# Edit CONTROL_DB_URL and TENANT_DB_URL with new password

# 4. Restart app containers
docker compose -f gdx_dispatch/docker/docker-compose.yml restart app celery-high celery-low celery-beat

# 5. Verify
curl -sk https://gdx.example.com/health
```

### 2. JWT Signing Key

```bash
# 1. Generate new key
NEW_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")

# 2. Update .env
# Set JWT_SECRET_KEY=$NEW_KEY

# 3. Restart app (all active sessions will be invalidated)
docker compose -f gdx_dispatch/docker/docker-compose.yml restart app

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
# 4. Verify backups: bash gdx/scripts/backup.sh
```

## Emergency Rotation (Suspected Compromise)

1. **Immediately** rotate ALL secrets listed above
2. Invalidate all active sessions (rotate JWT key)
3. Check audit logs for unauthorized access: `docker exec gdx-app python -c "from gdx_dispatch.core.audit import ...; ..."`
4. Review Sentry for unusual errors
5. Check Stripe Dashboard for unauthorized charges
6. Notify affected tenants if data exposure suspected

## Verification Checklist

After any rotation:

- [ ] `curl -sk https://gdx.example.com/health` returns 200
- [ ] Login works for at least one admin account
- [ ] Stripe webhook test event succeeds
- [ ] SMS sending works (test via /api/sms/send)
- [ ] Backup script runs without error
- [ ] Celery Beat is scheduling tasks (check logs)
