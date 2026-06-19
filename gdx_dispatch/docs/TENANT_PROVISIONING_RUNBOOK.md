# Tenant Provisioning Runbook

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
