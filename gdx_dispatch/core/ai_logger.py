from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class AILogRecord:
    tenant_id: str
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float
    latency_ms: int
    created_at: datetime


class AILogger:
    """In-memory AI usage logger for billing/audit and testability.

    In production this can be swapped for a DB-backed implementation.
    """

    def __init__(self) -> None:
        self._records: list[AILogRecord] = []

    async def log(
        self,
        tenant_id: str,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        latency_ms: int,
    ) -> None:
        self._records.append(
            AILogRecord(
                tenant_id=tenant_id,
                task=task,
                model=model,
                input_tokens=max(0, int(input_tokens)),
                output_tokens=max(0, int(output_tokens)),
                cost=round(max(0.0, float(cost)), 8),
                latency_ms=max(0, int(latency_ms)),
                created_at=datetime.now(UTC),
            )
        )

    def usage_for_tenant(self, tenant_id: str) -> dict[str, Any]:
        rows = [r for r in self._records if r.tenant_id == tenant_id]
        input_tokens = sum(r.input_tokens for r in rows)
        output_tokens = sum(r.output_tokens for r in rows)
        cost = round(sum(r.cost for r in rows), 8)
        by_task: dict[str, int] = defaultdict(int)
        for row in rows:
            by_task[row.task] += 1

        return {
            "tenant_id": tenant_id,
            "requests": len(rows),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost": cost,
            "by_task": dict(by_task),
        }

    def clear(self) -> None:
        self._records.clear()
