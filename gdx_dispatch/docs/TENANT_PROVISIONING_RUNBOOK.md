# Tenant Provisioning Runbook

**Status: HISTORICAL — do not follow these steps.** Describes the
multi-tenant SaaS control plane (Stripe Checkout at `/signup` → `provision_tenant()`
→ a per-tenant database `gdx_{slug}`). **None of it exists.** `provision_tenant`
returns zero hits repo-wide and `gdx/provisioning/state_machine.py` is not in the
tree. GDX Dispatch is single-tenant and self-hosted — one tenant per database,
isolation is the connection (see `CLAUDE.md`). Kept as the record of the
architecture that was rejected, not as an operating procedure.
_Marked 2026-09-01 by the doc audit._

## Automated Flow (Stripe Checkout)

1. Customer visits `/signup` and fills in company name, email, plan
2. Stripe Checkout session created → customer enters payment
3. Stripe webhook `checkout.session.completed` fires
4. `provision_tenant()` in `gdx/provisioning/state_machine.py` runs:
   - Creates tenant record in control DB
   - Creates tenant database (`gdx_{slug}`)
   - Runs schema migrations
   - Seeds demo data via `core/onboarding.py`
   - Grants default modules based on plan tier
5. Welcome email sent to tenant admin

## Manual Provisioning

```bash
# 1. Create tenant in control DB
docker exec gdx-app python -c "
from gdx.provisioning.state_machine import provision_tenant
import asyncio
asyncio.run(provision_tenant(slug='acme', name='Acme Doors', email='admin@acme.com', plan='professional'))
"

# 2. Verify
curl -sk https://acme.example.com/health
```

## Troubleshooting

- **DB creation fails**: Check PostgreSQL disk space and max_connections
- **Module grants missing**: Run `POST /api/admin/modules/enable` with module keys
- **DNS not resolving**: Add Cloudflare DNS record for `{slug}.example.com`
