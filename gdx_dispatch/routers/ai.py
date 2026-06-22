import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = timezone.utc

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth_capabilities import derive_ai_worker_caps
from gdx_dispatch.core.auth_capabilities import caps_for_role
from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.llm.anthropic_client import get_client
from gdx_dispatch.core.llm.key_storage import get_key
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import list_tools_for_principal
# Side-effect import: registers all built-in MCP tools so list_tools_for_principal
# sees a populated registry. Without this the AI loop has nothing to call.
import gdx_dispatch.core.mcp_tools  # noqa: F401
from gdx_dispatch.core.unified_principal import Principal, principal_tenant_uuid


router = APIRouter(prefix="/api/ai", tags=["ai"])

# --- Rate Limiting ---

_BUCKET: dict[str, list[float]] = {}
_RATE_LIMIT_PER_MIN = 30
_RATE_LIMIT_WINDOW_S = 60


def _check_rate_limit(tenant_id: str) -> tuple[bool, int]:
    """
    Checks if the tenant has exceeded the rate limit.
    Returns (allowed, retry_after_s).
    """
    now = time.monotonic()
    timestamps = _BUCKET.get(tenant_id, [])

    # Prune timestamps older than the window
    timestamps = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW_S]

    if len(timestamps) < _RATE_LIMIT_PER_MIN:
        timestamps.append(now)
        _BUCKET[tenant_id] = timestamps
        return True, 0
    else:
        # Calculate retry_after based on the oldest timestamp in the window
        oldest = timestamps[0]
        retry_after = int(_RATE_LIMIT_WINDOW_S - (now - oldest))
        # Ensure retry_after is at least 1
        retry_after = max(1, retry_after)
        return False, retry_after


def _reset_rate_limit() -> None:
    """Clears the rate limit bucket. Used for testing."""
    _BUCKET.clear()


# --- End Rate Limiting ---


def get_current_principal_for_ai(
    user: dict = Depends(get_current_user),
) -> Principal:
    """Wrapper that synthesizes a Principal from the SPA-compatible
    ``get_current_user`` dict. Routed through SS-7's auth path (not the
    auth_dispatcher OAuth path) so SPA-Bearer JWTs work.

    Tests still override this dep via ``app.dependency_overrides``.
    """
    from uuid import UUID, uuid4

    role = (user.get("role") or "user").lower()
    caps = caps_for_role(role)
    raw_id = user.get("id") or user.get("sub") or str(uuid4())
    try:
        identity_id = UUID(str(raw_id))
    except (ValueError, TypeError):
        identity_id = uuid4()
    return Principal(
        identity_id=identity_id,
        tenant_id=str(user.get("tenant_id") or ""),
        principal_role=role,
        capabilities=caps,
        auth_kind="session",
        actor_type="human",
    )


def get_db_for_ai(db: Session = Depends(get_db)) -> Session:
    """Wrapper around get_db so tests can override this dep
    via app.dependency_overrides without touching the database path.

    Used for: TenantSettings reads (the LLM key + last_validated_at),
    audit log writes (control-plane).
    """
    return db


def get_db_for_ai(db: Session = Depends(get_db)) -> Session:
    """Tenant-plane DB session for AI tool invocations.

    The control-plane db (above) holds the LLM key + audit logs; the
    tenant-plane db holds the actual business data (customers, jobs,
    invoices, etc). Tools registered in ``gdx_dispatch/core/mcp_tools/`` query
    the tenant-plane tables, so ``invoke_tool`` must receive THIS
    session, not the control-plane one.
    """
    return db


class AskHistoryEntry(BaseModel):
    """One prior Q/A turn supplied by the client for multi-turn context."""
    question: str = Field(..., max_length=4000)
    answer: str = Field(..., max_length=20000)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    approval_ref: str | None = None
    tool: str | None = None
    payload: dict | None = None
    history: list[AskHistoryEntry] = Field(
        default_factory=list,
        description="Prior turns from the same chat session (most recent last).",
    )

    @model_validator(mode="after")
    def validate_approval_ref(self) -> "AskRequest":
        if self.approval_ref is not None and (self.tool is None or self.payload is None):
            raise ValueError("approval_ref requires tool + payload")
        return self


class AskResponse(BaseModel):
    answer: str | None = None
    tools_used: list[str] = []
    disabled: bool = False
    reason: str | None = None
    pending_action: dict | None = None
    result: dict | None = None


@router.post("/ask", response_model=AskResponse)
async def ask(
    request: AskRequest,
    principal: Principal = Depends(get_current_principal_for_ai),
    db: Session = Depends(get_db_for_ai),
    tenant_db: Session = Depends(get_db_for_ai),
) -> AskResponse:
    """
    Exposes the AI ask endpoint. Implements a Claude Haiku tool-use loop
    with all tools the AIWorker is authorized to use, or a direct tool
    invocation if an approval_ref is provided.
    """
    tenant_uuid = principal_tenant_uuid(principal)
    key = get_key(db, tenant_uuid)

    if key is None:
        return AskResponse(disabled=True, reason="no_key")

    # Rate limit check
    allowed, retry_after_s = _check_rate_limit(tenant_uuid)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "rate_limit_exceeded", "retry_after_s": retry_after_s},
        )

    # Mint AIWorker principal
    derived_caps = derive_ai_worker_caps(principal.capabilities)
    ai_worker = replace(
        principal,
        actor_type="ai_worker",
        delegated_by_user_id=str(principal.identity_id),
        capabilities=derived_caps,
    )

    # --- Branch: Confirm/Apply Flow ---
    if request.approval_ref:
        # Note: Pydantic model_validator handles the 422 error if tool/payload are missing.
        # We proceed with the direct tool call.
        result_obj = await invoke_tool(
            request.tool,  # type: ignore
            request.payload,  # type: ignore
            principal=ai_worker,
            db=tenant_db,
            approval_ref=request.approval_ref,
        )

        if result_obj.ok:
            return AskResponse(
                answer=f"Applied {request.tool}.",
                tools_used=[request.tool],  # type: ignore
                result=result_obj.result,  # type: ignore
                disabled=False,
                reason=None,
                pending_action=None,
            )
        else:
            # If the tool call fails even with approval, we return the error state.
            # This is a fallback; usually, an approved tool should succeed.
            return AskResponse(
                answer=None,
                tools_used=[request.tool],  # type: ignore
                disabled=False,
                reason=f"tool_error: {result_obj.error_type}",
                pending_action=None,
                result=None,
            )

    # --- Branch: Standard Haiku Loop ---
    client = get_client(db, tenant_uuid)

    # Prepare tools for Anthropic.
    #
    # Anthropic requires tool names to match ``^[a-zA-Z0-9_-]{1,128}$``
    # (no dots). GDX tool names use dots (``customers.detail``,
    # ``jobs.list``, etc.) so we expose them to Anthropic with dots
    # replaced by underscores and reverse-map when handling the
    # ``tool_use`` response.
    tools_payload = []
    anthropic_to_tgd: dict[str, str] = {}

    def _to_anthropic_name(gdx_name: str) -> str:
        return gdx_name.replace(".", "_")

    try:
        for descriptor in list_tools_for_principal(ai_worker):
            anthropic_name = _to_anthropic_name(descriptor.name)
            anthropic_to_tgd[anthropic_name] = descriptor.name
            tools_payload.append(
                {
                    "name": anthropic_name,
                    "description": descriptor.description,
                    "input_schema": descriptor.input_schema,
                }
            )
    except Exception:
        # If tool enumeration fails, we proceed with no tools.
        pass

    # Compose a date-aware system prompt. Without this the model would
    # (and did) ask the user for today's date before calling tools
    # that take date filters. Use America/New_York since GDX (and most
    # current tenants) operate on Eastern Time — UTC dates flip a day
    # earlier from a local-time perspective.
    today_local = datetime.now(_ET)
    today_iso = today_local.date().isoformat()
    today_human = today_local.strftime("%A, %B %-d, %Y")
    iso_week = today_local.isocalendar()
    tz_label = "Eastern Time (America/New_York)" if _ET is not timezone.utc else "UTC"

    # User identity. We don't have name/email on Principal (only role +
    # identity_id) so we surface what we have.
    user_role = (principal.principal_role or "user").lower()
    user_handle = str(principal.identity_id)[:8]

    system_prompt = (
        "You are a helpful AI assistant embedded in GDX, a field-service "
        "platform for garage-door companies.\n\n"
        f"Today's date is {today_human} ({today_iso}, {tz_label}). "
        f"It is ISO week {iso_week.week} of {iso_week.year}.\n\n"
        f"You are talking to a user whose role is '{user_role}' "
        f"(id prefix: {user_handle}). Personalise responses where natural — "
        "say 'your jobs', 'your customers' rather than 'the company's'.\n\n"
        "Whenever a question references a relative time window — 'today', "
        "'this week', 'last 30 days', 'this month', 'this quarter' — compute "
        "the absolute date range yourself from the date above and pass it to "
        "the appropriate tool. Do NOT ask the user for today's date.\n\n"
        "When a question refers to data (customers, jobs, invoices, schedules, "
        "technicians, revenue, etc.), call a tool. Do not invent data. If a "
        "tool returns an error, summarise the error briefly and suggest the "
        "next step the user could take."
    )

    # Build multi-turn message history. The client (Vue popup / route view)
    # passes the most-recent N prior {question, answer} pairs in
    # request.history. We replay them so follow-up questions like
    # 'what about hers?' or 'show me more' have the prior context.
    # Hard-cap at 20 turns so prompt size stays bounded.
    messages: list[dict[str, Any]] = []
    for prior in request.history[-20:]:
        messages.append({"role": "user", "content": prior.question})
        messages.append({"role": "assistant", "content": prior.answer})
    messages.append({"role": "user", "content": request.question})
    tools_used_list: list[str] = []
    max_iterations = 5
    iteration = 0

    last_text_answer = ""
    pending_action_result: dict | None = None

    while iteration < max_iterations:
        # Anthropic API rejects `tools=null` with
        # "tools: Input should be a valid list". Omit the kwarg
        # entirely when no tools are available.
        create_kwargs: dict[str, Any] = {
            "model": "claude-haiku-4-5",
            "max_tokens": 1024,
            "messages": messages,
            "system": system_prompt,
        }
        if tools_payload:
            create_kwargs["tools"] = tools_payload
        response = client.messages.create(**create_kwargs)

        iteration += 1

        if response.stop_reason == "end_turn":
            # Extract text content
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            last_text_answer = "".join(text_parts)
            break

        elif response.stop_reason == "tool_use":
            # Process tool use blocks
            tool_use_blocks = [
                block
                for block in response.content
                if block.type == "tool_use"
            ]

            # Append the assistant's tool use message to conversation
            messages.append({"role": "assistant", "content": response.content})

            for block in tool_use_blocks:
                # Anthropic returns the underscored name we registered;
                # reverse-map back to the GDX-canonical dotted name.
                tool_name = anthropic_to_tgd.get(block.name, block.name)
                tool_input = block.input
                tool_use_id = block.id
                tools_used_list.append(tool_name)

                # Invoke the tool (GDX name) on the tenant-plane DB.
                result_obj = await invoke_tool(
                    tool_name,
                    tool_input,
                    principal=ai_worker,
                    db=tenant_db,
                )

                if result_obj.ok:
                    # Serialise the tool result as JSON for the
                    # tool_result block. JSON parses cleanly model-side;
                    # falling back to str() only when JSON serialisation
                    # fails (non-serialisable values like Decimal,
                    # datetime, etc. — default=str handles those).
                    try:
                        content_str = json.dumps(result_obj.result, default=str)
                    except (TypeError, ValueError):
                        content_str = str(result_obj.result)
                elif result_obj.error_type == "approval_required":
                    # Handle Yellow tool pending action
                    error_body = result_obj.error_body or {}
                    res_data = error_body.get("result") or {}

                    pending_action_result = {
                        "tool": tool_name,
                        "payload": tool_input,
                        "approval_token": res_data.get("approval_token"),
                        "trace_id": error_body.get("trace_id"),
                        "diff": res_data if res_data else None,
                    }
                    # Stop the loop immediately
                    break
                else:
                    content_str = f"Error: {result_obj.error_type} - {result_obj.error_body}"

                # Append the tool result to messages
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": content_str,
                            }
                        ],
                    }
                )

            if pending_action_result:
                break
        else:
            # Other stop reasons (max_tokens, etc.)
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            last_text_answer = "".join(text_parts)
            break

    if iteration >= max_iterations and not last_text_answer and not pending_action_result:
        last_text_answer = "I hit my tool-use cap; I couldn't complete the request."
    elif iteration >= max_iterations and not pending_action_result:
        last_text_answer = (
            f"I hit my tool-use cap; here's what I found so far: {last_text_answer}"
        )

    return AskResponse(
        answer=last_text_answer or None,
        tools_used=tools_used_list,
        disabled=False,
        reason=None,
        pending_action=pending_action_result,
        result=None,
    )
