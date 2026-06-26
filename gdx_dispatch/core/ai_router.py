from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from enum import Enum
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.ai_logger import AILogger
from gdx_dispatch.core.ai_usage_logger import log_ai_usage
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db

logger = logging.getLogger(__name__)


class AIProvider(str, Enum):
    CLOUD = "cloud"
    LOCAL = "local"
    HYBRID = "hybrid"


class AITask(str, Enum):
    PHOTO_ANALYSIS = "photo_analysis"
    DISPATCH_SUGGESTION = "dispatch_suggestion"
    ESTIMATE_GENERATION = "estimate_generation"
    COMMUNICATION_DRAFT = "communication_draft"
    SENTIMENT_ANALYSIS = "sentiment_analysis"
    GENERAL = "general"


class AIRouter:
    def __init__(self, logger: AILogger | None = None):
        self.provider = os.getenv("AI_PROVIDER", "cloud").strip().lower()
        self.cloud_endpoint = os.getenv("AI_CLOUD_ENDPOINT", "https://api.anthropic.com/v1")
        self.cloud_api_key = os.getenv("AI_CLOUD_API_KEY", "")
        self.local_endpoint = os.getenv("AI_LOCAL_ENDPOINT", "http://localhost:8000/v1")
        self.cloud_model = os.getenv("AI_CLOUD_MODEL", "claude-sonnet-4-20250514")
        self.cloud_model_fast = os.getenv("AI_CLOUD_MODEL_FAST", "claude-haiku-4-5-20251001")
        self.local_model = os.getenv("AI_LOCAL_MODEL", "qwen2.5-coder-32b")
        self.logger = logger or get_ai_logger()

    def _get_endpoint(self, task: AITask) -> tuple[str, str, str]:
        if self.provider == AIProvider.CLOUD.value:
            model = (
                self.cloud_model_fast
                if task in (AITask.SENTIMENT_ANALYSIS, AITask.COMMUNICATION_DRAFT)
                else self.cloud_model
            )
            return self.cloud_endpoint, self.cloud_api_key, model
        if self.provider == AIProvider.LOCAL.value:
            return self.local_endpoint, "", self.local_model

        if task in (
            AITask.PHOTO_ANALYSIS,
            AITask.DISPATCH_SUGGESTION,
            AITask.SENTIMENT_ANALYSIS,
        ):
            return self.local_endpoint, "", self.local_model
        return self.cloud_endpoint, self.cloud_api_key, self.cloud_model

    def _cloud_fallback(self, task: AITask) -> tuple[str, str, str]:
        model = (
            self.cloud_model_fast
            if task in (AITask.SENTIMENT_ANALYSIS, AITask.COMMUNICATION_DRAFT)
            else self.cloud_model
        )
        return self.cloud_endpoint, self.cloud_api_key, model

    @staticmethod
    def _chat_url(endpoint: str) -> str:
        return f"{endpoint.rstrip('/')}/chat/completions"

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _estimate_prompt_tokens(prompt: str, system_prompt: str | None) -> int:
        joined = f"{system_prompt or ''} {prompt}".strip()
        return max(1, len(joined.split()))

    @staticmethod
    def _estimate_output_tokens(text: str) -> int:
        return max(1, len(text.split()))

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        # USD pricing per 1M tokens.
        pricing = {
            "claude-sonnet-4-20250514": (3.0, 15.0),
            "claude-haiku-4-5-20251001": (1.0, 5.0),
            "qwen2.5-coder-32b": (0.0, 0.0),
        }
        in_per_million, out_per_million = pricing.get(model, (2.0, 8.0))
        return round(((input_tokens * in_per_million) + (output_tokens * out_per_million)) / 1_000_000, 8)

    async def _request_chat_completion(
        self,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.perf_counter()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._chat_url(endpoint),
                headers=headers,
                json=payload,
                timeout=30.0,
            )

        if response.status_code >= 400:
            response.raise_for_status()

        latency_ms = int((time.perf_counter() - started) * 1000)
        return response.json(), latency_ms

    async def _generate_internal(
        self,
        task: AITask,
        prompt: str,
        tenant_id: str,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        endpoint, api_key, model = self._get_endpoint(task)
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "metadata": {"tenant_id": tenant_id, "task": task.value},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        response_data: dict[str, Any] | None = None
        latency_ms = 0

        try:
            response_data, latency_ms = await self._request_chat_completion(endpoint, api_key, payload)
        except Exception as exc:
            last_error = exc
            should_retry_cloud = endpoint.rstrip("/") == self.local_endpoint.rstrip("/")
            if should_retry_cloud:
                fallback_endpoint, fallback_key, fallback_model = self._cloud_fallback(task)
                payload["model"] = fallback_model
                response_data, latency_ms = await self._request_chat_completion(
                    fallback_endpoint,
                    fallback_key,
                    payload,
                )
                model = fallback_model
            else:
                raise

        if response_data is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("AI response missing")

        content = self._extract_content(response_data)
        usage = response_data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or self._estimate_prompt_tokens(prompt, system_prompt))
        output_tokens = int(usage.get("completion_tokens") or self._estimate_output_tokens(content))
        tokens_used = input_tokens + output_tokens
        cost = self.calculate_cost(model, input_tokens=input_tokens, output_tokens=output_tokens)

        await self.logger.log(
            tenant_id=tenant_id,
            task=task.value,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            latency_ms=latency_ms,
        )

        return {
            "content": content,
            "model": model,
            "tokens_used": tokens_used,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
        }

    async def generate(
        self,
        task: AITask,
        prompt: str,
        tenant_id: str,
        system_prompt: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        return await self._generate_internal(
            task=task,
            prompt=prompt,
            tenant_id=tenant_id,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=None,
        )

    async def generate_with_tools(
        self,
        task: AITask,
        prompt: str,
        tenant_id: str,
        tools: list[dict[str, Any]],
        system_prompt: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        return await self._generate_internal(
            task=task,
            prompt=prompt,
            tenant_id=tenant_id,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )


class _TenantRateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute))
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, tenant_id: str) -> bool:
        now = time.time()
        window_start = now - 60.0
        bucket = self._calls[tenant_id]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(now)
        return True

    def reset(self) -> None:
        self._calls.clear()


_router_singleton: AIRouter | None = None
_logger_singleton: AILogger | None = None
_limiter_singleton: _TenantRateLimiter | None = None


def get_ai_logger() -> AILogger:
    global _logger_singleton
    if _logger_singleton is None:
        _logger_singleton = AILogger()
    return _logger_singleton


def get_ai_router() -> AIRouter:
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = AIRouter()
    return _router_singleton


def get_ai_rate_limiter() -> _TenantRateLimiter:
    global _limiter_singleton
    if _limiter_singleton is None:
        _limiter_singleton = _TenantRateLimiter(int(os.getenv("AI_RATE_LIMIT_PER_MIN", "60")))
    return _limiter_singleton


def reset_ai_singletons() -> None:
    global _router_singleton, _logger_singleton, _limiter_singleton
    if _logger_singleton is not None:
        _logger_singleton.clear()
    if _limiter_singleton is not None:
        _limiter_singleton.reset()
    _router_singleton = None
    _logger_singleton = None
    _limiter_singleton = None


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    task: AITask = AITask.GENERAL
    system_prompt: str | None = None
    max_tokens: int = 1000
    temperature: float = 0.3


class ToolGenerateRequest(GenerateRequest):
    tools: list[dict[str, Any]] = Field(default_factory=list)


class AnalyzePhotoRequest(BaseModel):
    image_base64: str = Field(..., min_length=8)
    prompt: str = "Analyze this photo"
    system_prompt: str | None = None


class DraftMessageRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    system_prompt: str | None = None


class DispatchSuggestionRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    system_prompt: str | None = None


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tenant_id = str(tenant.get("id") or request.headers.get("x-tenant-id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tenant_id


def _enforce_rate_limit(tenant_id: str) -> None:
    limiter = get_ai_rate_limiter()
    if not limiter.allow(tenant_id):
        raise HTTPException(status_code=429, detail="AI rate limit exceeded")


router = APIRouter(prefix="/api/ai", tags=["ai-router"])


def _persist_usage(
    db: Session,
    request: Request,
    user: dict[str, Any],
    tenant_id: str,
    task: AITask,
    result: dict[str, Any],
) -> None:
    log_ai_usage(
        db,
        tenant_id=tenant_id,
        user_id=str(user.get("user_id") or ""),
        task=task.value,
        model=str(result.get("model") or ""),
        input_tokens=int(result.get("input_tokens") or 0),
        output_tokens=int(result.get("output_tokens") or 0),
        cost_usd=float(result.get("cost") or 0.0),
        latency_ms=0,
        request_id=str(getattr(request.state, "request_id", request.headers.get("x-request-id", "-"))),
    )


@router.post("/generate")
async def generate_ai(
    request: Request,
    body: GenerateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_rate_limit(tenant_id)
    result = await get_ai_router().generate(
        task=body.task,
        prompt=body.prompt,
        tenant_id=tenant_id,
        system_prompt=body.system_prompt,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    _persist_usage(db, request, user, tenant_id, body.task, result)
    return result


@router.post("/generate-with-tools")
async def generate_ai_with_tools(
    request: Request,
    body: ToolGenerateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_rate_limit(tenant_id)
    result = await get_ai_router().generate_with_tools(
        task=body.task,
        prompt=body.prompt,
        tenant_id=tenant_id,
        tools=body.tools,
        system_prompt=body.system_prompt,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    _persist_usage(db, request, user, tenant_id, body.task, result)
    return result


@router.post("/analyze-photo")
async def analyze_photo(
    request: Request,
    body: AnalyzePhotoRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_rate_limit(tenant_id)

    try:
        image_bytes = base64.b64decode(body.image_base64, validate=True)
    except Exception as exc:  # pragma: no cover - validation branch
        raise HTTPException(status_code=422, detail="Invalid base64 image") from exc

    prompt = (
        f"{body.prompt}\n"
        f"Photo bytes length: {len(image_bytes)}\n"
        "Return JSON with fields: damage_level, confidence, recommendation, notable_findings."
    )

    result = await get_ai_router().generate(
        task=AITask.PHOTO_ANALYSIS,
        prompt=prompt,
        tenant_id=tenant_id,
        system_prompt=body.system_prompt,
        max_tokens=700,
        temperature=0.2,
    )
    _persist_usage(db, request, user, tenant_id, AITask.PHOTO_ANALYSIS, result)

    parsed: dict[str, Any]
    try:
        parsed = json.loads(result.get("content", ""))
        if not isinstance(parsed, dict):
            parsed = {"summary": result.get("content", "")}
    except Exception:
        logging.getLogger(__name__).exception("analyze_photo caught exception")
        parsed = {"summary": result.get("content", "")}

    return {
        "analysis": parsed,
        "model": result.get("model"),
        "tokens_used": result.get("tokens_used", 0),
    }


@router.post("/draft-message")
async def draft_message(
    request: Request,
    body: DraftMessageRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_rate_limit(tenant_id)
    result = await get_ai_router().generate(
        task=AITask.COMMUNICATION_DRAFT,
        prompt=body.prompt,
        tenant_id=tenant_id,
        system_prompt=body.system_prompt,
        max_tokens=600,
        temperature=0.4,
    )
    _persist_usage(db, request, user, tenant_id, AITask.COMMUNICATION_DRAFT, result)
    return result


@router.post("/suggest-dispatch")
async def suggest_dispatch(
    request: Request,
    body: DispatchSuggestionRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _enforce_rate_limit(tenant_id)
    result = await get_ai_router().generate(
        task=AITask.DISPATCH_SUGGESTION,
        prompt=body.prompt,
        tenant_id=tenant_id,
        system_prompt=body.system_prompt,
        max_tokens=700,
        temperature=0.2,
    )
    _persist_usage(db, request, user, tenant_id, AITask.DISPATCH_SUGGESTION, result)
    return result


@router.get("/usage")
async def ai_usage(request: Request) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    return get_ai_logger().usage_for_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Smart Estimate (#266) — suggest price from historical data
# ---------------------------------------------------------------------------

@router.post("/smart-estimate")
async def smart_estimate(
    request: Request,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Suggest price based on job description + historical data."""
    tenant_id = _tenant_id(request)
    body = await request.json()
    job_type = body.get("job_type", "")
    try:
        row = db.execute(
            text(
                """
                SELECT AVG(CAST(total AS FLOAT)) AS avg_price, COUNT(*) AS cnt
                FROM invoices
                WHERE company_id = :tid AND deleted_at IS NULL
                  AND job_id IN (SELECT id FROM jobs WHERE company_id = :tid AND job_type = :jt AND status IN ('Completed','completed'))
                """
            ),
            {"tid": tenant_id, "jt": job_type},
        ).mappings().first()
        avg_price = round(float(row["avg_price"] or 0), 2) if row else 0
        cnt = int(row["cnt"] or 0) if row else 0
        confidence = min(round(cnt / 20, 2), 1.0)
        return {"suggested_price": avg_price, "confidence": confidence, "similar_jobs_count": cnt, "job_type": job_type}
    except Exception:
        logger.exception("smart_estimate_failed")
        raise HTTPException(status_code=500, detail="Failed to generate estimate suggestion") from None


# ---------------------------------------------------------------------------
# Dispatch Optimization (#268) — rank technicians for a job
# ---------------------------------------------------------------------------

@router.post("/dispatch-optimize")
async def dispatch_optimize(
    request: Request,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Score and rank available technicians for a job."""
    tenant_id = _tenant_id(request)
    body = await request.json()
    job_type = body.get("job_type", "")
    try:
        techs = db.execute(
            text(
                """
                SELECT id, full_name, field_skills, route_start_lat, route_start_lng
                FROM users
                WHERE company_id = :tid AND role IN ('technician', 'tech') AND active = true AND deleted_at IS NULL
                """
            ),
            {"tid": tenant_id},
        ).mappings().all()

        scored = []
        for t in techs:
            skills = str(t.get("field_skills") or "").lower()
            skill_score = 40 if job_type.lower() in skills else 0
            scored.append({
                "tech_id": str(t["id"]),
                "name": t["full_name"] or "Unknown",
                "score": skill_score + 30,  # base proximity score
                "reason": "Skills match" if skill_score > 0 else "Available",
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored
    except Exception:
        logger.exception("dispatch_optimize_failed")
        raise HTTPException(status_code=500, detail="Failed to optimize dispatch") from None


# ---------------------------------------------------------------------------
# Sentiment Scoring (#271) — score customer happiness
# ---------------------------------------------------------------------------

@router.post("/sentiment")
async def customer_sentiment(
    request: Request,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Score customer sentiment from communication history."""
    tenant_id = _tenant_id(request)
    body = await request.json()
    body.get("customer_id", "")
    positive = {"thank", "great", "excellent", "happy", "recommend", "awesome", "perfect", "love", "fantastic"}
    negative = {"complaint", "unhappy", "terrible", "worst", "disappointed", "horrible", "angry", "frustrated", "broken"}
    try:
        rows = db.execute(
            text("SELECT body FROM job_notes WHERE company_id = :tid"),
            {"tid": tenant_id},
        ).mappings().all()
        pos_count = 0
        neg_count = 0
        for r in rows:
            words = set(str(r.get("body", "")).lower().split())
            pos_count += len(words & positive)
            neg_count += len(words & negative)
        total = pos_count + neg_count
        score = round((pos_count / max(total, 1)) * 100) if total else 50
        return {"score": score, "positive_count": pos_count, "negative_count": neg_count, "total_messages": len(rows)}
    except Exception:
        logger.exception("sentiment_scoring_failed")
        raise HTTPException(status_code=500, detail="Failed to score sentiment") from None


# ---------------------------------------------------------------------------
# Auto-Categorize (#275) — tag job type from notes
# ---------------------------------------------------------------------------

@router.post("/auto-categorize")
async def auto_categorize(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Read job notes and auto-tag job type, parts, issues."""
    body = await request.json()
    notes = str(body.get("notes", "")).lower()
    patterns = {
        "Spring Replacement": ["torsion spring", "extension spring", "spring broke", "broken spring"],
        "Opener Repair": ["opener", "motor", "remote", "keypad", "liftmaster", "chamberlain"],
        "Cable Repair": ["cable", "cable broke", "frayed cable"],
        "Panel Replacement": ["panel", "dented panel", "damaged section"],
        "Roller Replacement": ["roller", "noisy roller", "worn roller"],
        "Seal Replacement": ["weather seal", "bottom seal", "astragal"],
        "Track Repair": ["track", "bent track", "misaligned"],
        "General Service": ["tune-up", "maintenance", "lubricate", "inspect"],
    }
    detected = []
    parts = []
    for job_type, keywords in patterns.items():
        for kw in keywords:
            if kw in notes:
                detected.append(job_type)
                parts.append(kw)
                break
    primary = detected[0] if detected else "General Service"
    confidence = min(round(len(detected) / 3, 2), 1.0) if detected else 0.1
    return {"detected_type": primary, "parts_mentioned": list(set(parts)), "all_matches": detected, "confidence": confidence}


# ---------------------------------------------------------------------------
# Demand Forecast (#272) — predict job volume
# ---------------------------------------------------------------------------

@router.get("/demand-forecast")
async def demand_forecast(
    request: Request,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Predict busy/slow periods from historical job data."""
    tenant_id = _tenant_id(request)
    try:
        by_dow = db.execute(
            text(
                """
                SELECT EXTRACT(DOW FROM created_at) AS dow, COUNT(*) AS cnt
                FROM jobs WHERE company_id = :tid AND deleted_at IS NULL
                GROUP BY dow ORDER BY dow
                """
            ),
            {"tid": tenant_id},
        ).mappings().all()

        by_month = db.execute(
            text(
                """
                SELECT EXTRACT(MONTH FROM created_at) AS mon, COUNT(*) AS cnt
                FROM jobs WHERE company_id = :tid AND deleted_at IS NULL
                GROUP BY mon ORDER BY mon
                """
            ),
            {"tid": tenant_id},
        ).mappings().all()

        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        return {
            "by_day_of_week": [{"day": days[int(r["dow"])], "jobs": int(r["cnt"])} for r in by_dow if r["dow"] is not None],
            "by_month": [{"month": months[int(r["mon"]) - 1], "jobs": int(r["cnt"])} for r in by_month if r["mon"] is not None],
        }
    except Exception:
        logger.exception("demand_forecast_failed")
        with contextlib.suppress(Exception):
            db.rollback()
        return {"by_day_of_week": [], "by_month": []}
