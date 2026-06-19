from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis, from_url
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

UUID4_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$", re.I
)
TTL_SECONDS = 24 * 60 * 60


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    return from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method == "GET" or request.method not in {"POST", "PATCH", "DELETE"}:
            return await call_next(request)
        key = request.headers.get("Idempotency-Key")
        if not key:
            return await call_next(request)
        if not UUID4_RE.fullmatch(key):
            return JSONResponse(status_code=400, content={"detail": "Malformed Idempotency-Key"})
        tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
        if not tenant_id:
            return JSONResponse(status_code=400, content={"detail": "Missing tenant context"})
        redis = get_redis_client()
        redis_key = f"idempotency:{tenant_id}:{key}"
        if not await redis.set(redis_key, "in_flight", ex=TTL_SECONDS, nx=True):
            cached = await redis.get(redis_key)
            if cached == "in_flight":
                return JSONResponse(status_code=409, content={"detail": "Request already in flight"})
            if cached:
                payload = json.loads(cached)
                return Response(
                    content=payload["body"],
                    status_code=payload["status_code"],
                    headers=payload["headers"],
                )
            return JSONResponse(status_code=409, content={"detail": "Request already in flight"})
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        await redis.set(
            redis_key,
            json.dumps(
                {
                    "status_code": response.status_code,
                    "body": body.decode("utf-8", errors="replace"),
                    "headers": dict(response.headers),
                }
            ),
            ex=TTL_SECONDS,
        )
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
