# Control Plane High Availability

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │           nginx upstream              │
                    │  app-1:8000  │  app-2:8000           │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼───────────────────┐
              │                    │                   │
         PgBouncer             Redis Sentinel       Control DB
        (tx mode)             (3 nodes, HA)        Primary+Replica
         port 6432                                  port 5434
```

## PostgreSQL Primary + Replica

```bash
# On replica server, add to postgresql.conf:
primary_conninfo = 'host=control-db-primary port=5432 user=replicator'
recovery_target_timeline = 'latest'

# On primary, create replication user:
CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD '...';

# pg_hba.conf on primary:
host  replication  replicator  replica-ip/32  scram-sha-256

# Verify replication lag:
SELECT now() - pg_last_xact_replay_timestamp() AS replication_lag;
```

## nginx Multi-Instance Config

```nginx
upstream gdx_app {
    least_conn;
    server app-1:8000 max_fails=3 fail_timeout=30s;
    server app-2:8000 max_fails=3 fail_timeout=30s;
    keepalive 32;
}

server {
    location / {
        proxy_pass http://gdx_app;
        proxy_set_header X-Request-ID $request_id;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
    }
}
```

## FastAPI Instance Config

Each instance must:

1. Set `INSTANCE_ID` env var (used in logs + metrics)
2. Use `NullPool` for tenant engine registry (engine-per-request, no cross-fork issues)
3. Call `engine_registry.dispose_all()` in Gunicorn `post_fork` hook (already in `gdx_dispatch/core/celery_app.py`)

```bash
# Gunicorn with 2 workers per instance:
gunicorn gdx_dispatch.main:app -w 2 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 --timeout 30
```

## Health Check Endpoints

- `GET /health` — returns `{"status": "ok", "db": "ok", "redis": "ok"}`
- Used by nginx `proxy_pass` health checks and Docker healthcheck

## Failover Procedure

### App instance fails

nginx detects failed health check after `fail_timeout=30s`, stops routing to it.
Action: Restart container, nginx auto-recovers.

### Control DB primary fails

1. Promote replica: `pg_ctl promote -D /var/lib/postgresql/data`
2. Update `DATABASE_URL` in environment to point to promoted replica
3. Rolling restart of app instances
4. Provision new replica from promoted primary

### Redis fails

Redis Sentinel automatically elects new primary.
App reconnects on next request (redis-py auto-reconnect).
