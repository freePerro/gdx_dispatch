# Flask → FastAPI Migration: Phase 3 Canary Runbook

## Status: READY TO ACTIVATE (pending 7-day shadow verification window)

## Prerequisites

- Phase 2 shadow mode running on gdx.example.com (nginx mirror active)
- FastAPI receiving shadow copies of all requests at gdx.example.com/gdx/
- 7-day shadow window started 2026-03-26 — activate Phase 3 on 2026-04-02

## What Phase 3 Does

- 5% of NEW signups only are routed to FastAPI (isolated tenants)
- Zero risk to existing GDX customers
- FastAPI tenants are auto-provisioned into separate DBs

## nginx Configuration (to activate on 2026-04-02)

Add to nginx.conf inside the server block for gdx.example.com:

```nginx
# Phase 3: Route 5% of new signups to FastAPI
# Only applies to /signup and /api/signup — all other routes unaffected
split_clients "${remote_addr}${date_local}" $signup_backend {
    5%    gdx_backend;
    *     gdx_backend;
}

upstream gdx_backend {
    server 127.0.0.1:8001;  # FastAPI GDX
}

upstream gdx_backend {
    server 127.0.0.1:8000;  # Flask GDX
}

location ~ ^/(signup|api/signup) {
    proxy_pass http://$signup_backend;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Monitoring During Phase 3 (14 days)

- Check daily: docker logs gdx-dispatch --since 24h | grep ERROR | wc -l
- Check daily: curl -sk <https://gdx.example.com/health>
- Check daily: curl -sk <https://gdx.example.com/health>
- Watch: Sentry for any FastAPI tenant errors
- Success criteria: 0 data corruption incidents, <1% error rate on FastAPI tenants

## Phase 3 → Phase 4 Trigger

After 14 days with success criteria met, proceed to Phase 4:

- Begin migrating one existing tenant at a time to FastAPI
- 48h verification between each batch
- Document in MIGRATION_PHASE4_TENANT_MIGRATION.md

## Rollback

To instantly roll back Phase 3, remove the split_clients block from nginx.conf:

```bash
docker exec gdx-nginx nginx -s reload
```

All new signups return to Flask immediately.
