"""Google Workspace A2A orchestrator — JSON-RPC surface + workspace-mcp bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import mcp_http_url, settings
from .mcp_client import WorkspaceMCPClient
from .oauth_routes import router as oauth_router
from .orchestrator import run_workspace_task
from .token_store import get_tokens, log_ref, put_tokens

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()

_mcp_client: WorkspaceMCPClient | None = None
_mcp_proc: asyncio.subprocess.Process | None = None
_task_store: dict[str, dict[str, Any]] = {}


async def _wait_for_mcp(
    client: WorkspaceMCPClient,
    mcp_url: str,
    bearer: str | None,  # kept for API compatibility; unused when external OAuth is active
    timeout: float = 90.0,
    *,
    subprocess: asyncio.subprocess.Process | None = None,
) -> None:
    """Wait until workspace-mcp HTTP server is reachable.

    With EXTERNAL_OAUTH21_PROVIDER=true every MCP endpoint (including initialize)
    requires a bearer token, so we cannot perform a full tools/list probe at startup.
    Instead we ping an unauthenticated discovery endpoint that workspace-mcp always
    exposes — any sub-500 response means the server is ready.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    start = loop.time()
    last_stall_log = start
    while loop.time() < deadline:
        if subprocess is not None and subprocess.returncode is not None:
            raise RuntimeError(
                f"workspace-mcp subprocess exited with code {subprocess.returncode} "
                "(see container stderr above)"
            )
        try:
            if await client.ping():
                return
        except Exception as exc:
            logger.warning("mcp_probe_failed", error=str(exc))
        now = loop.time()
        if now - last_stall_log >= 25.0:
            logger.warning(
                "mcp_still_waiting",
                url=mcp_url,
                elapsed_s=round(now - start, 1),
                hint=(
                    "uvx can spend several minutes downloading on first run; "
                    "use an image with workspace-mcp pre-installed (see Dockerfile)"
                ),
            )
            last_stall_log = now
        await asyncio.sleep(2.0)
    raise RuntimeError(f"MCP not ready at {mcp_url} within {timeout}s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _mcp_proc
    url = mcp_http_url(settings)
    spawned_subprocess = False
    if settings.mcp_spawn_enabled:
        mcp_bin = shutil.which("workspace-mcp")
        uvx = shutil.which("uvx")
        tier = settings.workspace_mcp_tool_tier
        transport_args = ["--transport", "streamable-http", "--tool-tier", tier]
        cmd: list[str] | None
        if mcp_bin:
            cmd = [mcp_bin, *transport_args]
        elif uvx:
            cmd = [uvx, "workspace-mcp", *transport_args]
        else:
            cmd = None
            logger.error(
                "mcp_spawn_missing_executable",
                hint=(
                    "Install workspace-mcp on PATH (recommended in Docker) or uvx, "
                    "or set MCP_SPAWN_ENABLED=0 and WORKSPACE_MCP_URL to an external server"
                ),
            )
        if cmd is not None:
            env = os.environ.copy()
            # Force MCP child to bind its own HTTP port; parent agent may run with PORT=3011.
            env["PORT"] = str(settings.workspace_mcp_port)
            env["WORKSPACE_MCP_PORT"] = str(settings.workspace_mcp_port)
            # workspace-mcp reads OAuth from its own environment — not from Pydantic-loaded
            # defaults in the parent. Without these, the child exits before binding HTTP.
            if settings.google_oauth_client_id:
                env["GOOGLE_OAUTH_CLIENT_ID"] = settings.google_oauth_client_id
            if settings.google_oauth_client_secret:
                env["GOOGLE_OAUTH_CLIENT_SECRET"] = settings.google_oauth_client_secret
            elif not env.get("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY"):
                if settings.fastmcp_server_auth_google_jwt_signing_key:
                    env["FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY"] = (
                        settings.fastmcp_server_auth_google_jwt_signing_key
                    )
                else:
                    # Public OAuth 2.1 clients: upstream requires this when client secret is omitted.
                    env["FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY"] = secrets.token_hex(32)
                    logger.info(
                        "mcp_spawn_ephemeral_jwt_signing_key",
                        hint="Set GOOGLE_OAUTH_CLIENT_SECRET or FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY for a stable key across restarts",
                    )
            if settings.google_oauth_redirect_uri:
                env.setdefault("GOOGLE_OAUTH_REDIRECT_URI", settings.google_oauth_redirect_uri)
            # External OAuth provider mode: workspace-mcp validates Google access tokens
            # (ya29.*) from the Authorization header via Google's userinfo API rather than
            # running its own OAuth flow or reading from disk credentials.
            env["MCP_ENABLE_OAUTH21"] = "true"
            env["EXTERNAL_OAUTH21_PROVIDER"] = "true"
            env["WORKSPACE_MCP_STATELESS_MODE"] = "true"
            _mcp_proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=None,
            )
            spawned_subprocess = True
            logger.info(
                "mcp_subprocess_started",
                pid=_mcp_proc.pid,
                cmd=" ".join(cmd),
                port=settings.workspace_mcp_port,
                url=url,
                launcher="workspace-mcp" if mcp_bin else "uvx",
            )
    _mcp_client = WorkspaceMCPClient(url)
    # Only probe MCP when something should be listening: spawned child or explicit URL.
    should_probe_mcp = spawned_subprocess or (settings.workspace_mcp_url is not None)
    mcp_probe_task: asyncio.Task[None] | None = None
    if should_probe_mcp and settings.mcp_readiness_timeout > 0:

        async def _mcp_readiness_probe() -> None:
            assert _mcp_client is not None
            try:
                await _wait_for_mcp(
                    _mcp_client,
                    url,
                    None,
                    settings.mcp_readiness_timeout,
                    subprocess=_mcp_proc,
                )
                logger.info("mcp_ready", url=url)
            except Exception as exc:
                rc = _mcp_proc.returncode if _mcp_proc else None
                logger.error(
                    "mcp_startup_warning",
                    error=str(exc),
                    url=url,
                    mcp_subprocess_returncode=rc,
                    hint=(
                        "If returncode is set, workspace-mcp exited — check stderr in this log stream. "
                        "If launcher was uvx, first-run download can exceed MCP_READINESS_TIMEOUT; "
                        "rebuild the image so workspace-mcp is on PATH (see Dockerfile)."
                    ),
                )

        mcp_probe_task = asyncio.create_task(_mcp_readiness_probe())
    elif should_probe_mcp:
        logger.info(
            "mcp_readiness_skipped",
            url=url,
            reason="MCP_READINESS_TIMEOUT is 0",
        )
    else:
        logger.info(
            "mcp_probe_skipped",
            url=url,
            hint="MCP spawn disabled and no WORKSPACE_MCP_URL — MCP calls will fail until you configure one",
        )
    try:
        yield
    finally:
        if mcp_probe_task and not mcp_probe_task.done():
            mcp_probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await mcp_probe_task
        if _mcp_proc and _mcp_proc.returncode is None:
            _mcp_proc.terminate()
            try:
                await asyncio.wait_for(_mcp_proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                _mcp_proc.kill()
            logger.info("mcp_subprocess_stopped")


app = FastAPI(title="Google Workspace Orchestrator (A2A)", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(oauth_router)

_PORT = int(os.getenv("PORT", str(settings.port)))


def _auth_bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or request.headers.get("Authorization")
    if raw and raw.lower().startswith("bearer "):
        return raw.split(" ", 1)[1].strip()
    return None


def _session_key(params: dict[str, Any]) -> str:
    meta = params.get("metadata") or {}
    if isinstance(meta, dict):
        return str(meta.get("oauth_state") or meta.get("session_id") or "default")
    return "default"


async def _try_refresh(key: str) -> str | None:
    """Refresh an expired Google access token using the stored refresh token.

    Returns the new access token on success, None if refresh is not possible or fails.
    """
    tok = get_tokens(key)
    if not tok or not tok.refresh_token:
        return None
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        logger.warning("oauth_refresh_skipped key=%s reason=no_client_credentials", log_ref(key))
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tok.refresh_token,
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.warning(
                "oauth_refresh_failed key=%s status=%d body=%s",
                log_ref(key),
                resp.status_code,
                resp.text[:300],
            )
            return None
        data = resp.json()
        new_access = data.get("access_token")
        if not new_access:
            return None
        put_tokens(key, new_access, tok.refresh_token, data.get("expires_in"))
        logger.info("oauth_token_refreshed key=%s", log_ref(key))
        # Reset the MCP session so the next call re-initializes with the new token.
        if _mcp_client is not None:
            _mcp_client._reset_session()
        return new_access
    except Exception as exc:
        logger.warning("oauth_refresh_error key=%s error=%s", log_ref(key), exc)
        return None


async def _bearer_for_session(request: Request, params: dict[str, Any]) -> str | None:
    direct = _auth_bearer(request)
    if direct:
        logger.info("oauth_bearer_source source=request_header")
        return direct
    key = _session_key(params)
    tok = get_tokens(key)
    if not tok:
        logger.warning("oauth_bearer_missing key=%s", log_ref(key))
        return None
    if tok.is_expired():
        logger.info("oauth_token_expired key=%s refreshing=true", log_ref(key))
        refreshed = await _try_refresh(key)
        if refreshed:
            return refreshed
        logger.warning("oauth_token_expired_unrefreshable key=%s", log_ref(key))
        return None
    logger.info("oauth_bearer_source source=token_store key=%s", log_ref(key))
    return tok.access_token


AGENT_CARD: dict[str, Any] = {
    "schemaVersion": "1.0",
    "name": "Google Workspace Orchestrator",
    "description": (
        "Plans and runs multi-step Google Workspace tasks via workspace-mcp "
        "(Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Chat, Apps Script, "
        "Tasks, Contacts, Custom Search). OpenRouter-backed ADK agents."
    ),
    "url": f"http://localhost:{_PORT}",
    "version": "0.2.0",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "skills": [
        {
            "id": "workspace_orchestrator",
            "name": "Workspace orchestration",
            "description": "Full DAG across all workspace-mcp services from one goal.",
            "tags": ["google", "workspace", "mcp", "orchestration"],
            "examples": [
                "Find the Q3 PDF in Drive, summarize it, post to Chat, and add a Calendar follow-up"
            ],
        },
        {
            "id": "workspace_drive",
            "name": "Drive",
            "description": "Drive files, folders, sharing, permissions.",
            "tags": ["google", "drive"],
            "examples": ["Search my Drive for PDFs about onboarding"],
        },
        {
            "id": "workspace_gmail",
            "name": "Gmail",
            "description": "Gmail search, threads, labels, drafts, send.",
            "tags": ["google", "gmail"],
            "examples": ["Search my unread mail about invoices"],
        },
        {
            "id": "workspace_calendar",
            "name": "Calendar",
            "description": "Calendars, events, free/busy, OOO, focus time.",
            "tags": ["google", "calendar"],
            "examples": ["What meetings do I have tomorrow morning?"],
        },
        {
            "id": "workspace_docs",
            "name": "Google Docs",
            "description": "Docs content, formatting, comments, export.",
            "tags": ["google", "docs"],
            "examples": ["Summarize the content of my project spec doc"],
        },
        {
            "id": "workspace_sheets",
            "name": "Google Sheets",
            "description": "Sheets ranges, tables, formatting, comments.",
            "tags": ["google", "sheets"],
            "examples": ["Append a row to my expenses table"],
        },
        {
            "id": "workspace_slides",
            "name": "Google Slides",
            "description": "Presentations, slides, thumbnails, comments.",
            "tags": ["google", "slides"],
            "examples": ["Create a deck titled Roadmap Review"],
        },
        {
            "id": "workspace_forms",
            "name": "Google Forms",
            "description": "Forms, responses, publish settings.",
            "tags": ["google", "forms"],
            "examples": ["List responses for my feedback form"],
        },
        {
            "id": "workspace_tasks",
            "name": "Google Tasks",
            "description": "Task lists and tasks.",
            "tags": ["google", "tasks"],
            "examples": ["Add a task due Friday to my Today list"],
        },
        {
            "id": "workspace_contacts",
            "name": "Google Contacts",
            "description": "People, groups, batch operations.",
            "tags": ["google", "contacts"],
            "examples": ["Search contacts at acme.com"],
        },
        {
            "id": "workspace_chat",
            "name": "Google Chat",
            "description": "Spaces, messages, search, reactions.",
            "tags": ["google", "chat"],
            "examples": ["Send a standup summary to the engineering space"],
        },
        {
            "id": "workspace_search",
            "name": "Programmable Search",
            "description": "Custom Search Engine (search_custom).",
            "tags": ["google", "search"],
            "examples": ["Search the web for workspace MCP streamable HTTP"],
        },
        {
            "id": "workspace_apps_script",
            "name": "Apps Script",
            "description": "Script projects, deployments, execution.",
            "tags": ["google", "apps-script"],
            "examples": ["List my Apps Script projects"],
        },
    ],
}


@app.get("/.well-known/agent.json")
async def agent_card():
    return AGENT_CARD


@app.get("/health")
async def health():
    mcp_ok = False
    if _mcp_client:
        try:
            mcp_ok = await _mcp_client.ping()
        except Exception:
            mcp_ok = False
    return {
        "status": "healthy",
        "agent": "google-workspace-orchestrator",
        "version": "0.2.0",
        "mcp_reachable": mcp_ok,
        "openrouter_configured": bool(settings.openrouter_api_key),
        "google_oauth_callback_configured": bool(
            settings.google_oauth_client_id and settings.google_oauth_redirect_uri
        ),
    }


def _task_failed(task_id: str, error: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": {
            "state": "failed",
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": f"Error: {error}"}],
            },
        },
    }


async def _execute_task(
    task_id: str,
    query: str,
    request: Request,
    params: dict[str, Any],
) -> dict[str, Any]:
    if not query.strip():
        return _task_failed(task_id, "No text message provided")
    if _mcp_client is None:
        return _task_failed(task_id, "MCP client not initialized")

    message = (params.get("message") or {}) if isinstance(params, dict) else {}
    resume_task_id = message.get("taskId")
    user_lower = query.strip().lower()
    resume_hitl: dict[str, Any] | None = None
    if resume_task_id and resume_task_id in _task_store:
        prev = _task_store[resume_task_id]
        meta = prev.get("metadata") or {}
        if meta.get("pending_tool"):
            if "deny" in user_lower and "approve" not in user_lower:
                return _task_failed(task_id, "Action denied by user.")
            resume_hitl = {
                "pending_tool": meta.get("pending_tool"),
                "pending_args": meta.get("pending_args") or {},
                "user_said_approve": "approve" in user_lower and "deny" not in user_lower,
            }
            if not resume_hitl["user_said_approve"]:
                return {
                    "id": task_id,
                    "status": {
                        "state": "input-required",
                        "message": {
                            "role": "agent",
                            "parts": [
                                {
                                    "kind": "text",
                                    "text": "Reply **approve** to run the pending action or **deny** to cancel.",
                                }
                            ],
                        },
                    },
                    "metadata": {
                        "pending_tool": meta.get("pending_tool"),
                        "pending_args": meta.get("pending_args"),
                    },
                }

    bearer = await _bearer_for_session(request, params)
    if not bearer and not resume_hitl:
        # No Google OAuth token available for this session.  Tell SuperAgent to
        # trigger the OAuth flow (preflight will surface the auth URL) rather than
        # silently proceeding with null bearer, which would produce degraded or
        # empty tool results from workspace-mcp.
        logger.warning(
            "auth_required_no_bearer task_id=%s — returning auth-required task state",
            task_id,
        )
        return {
            "id": task_id,
            "status": {
                "state": "auth-required",
                "message": {
                    "role": "agent",
                    "parts": [
                        {
                            "kind": "text",
                            "text": (
                                "Google Workspace authentication is required. "
                                "Please complete the OAuth flow to grant access."
                            ),
                        }
                    ],
                },
            },
            "metadata": {"interrupt_type": "auth"},
        }

    try:
        result = await run_workspace_task(
            settings,
            _mcp_client,
            bearer,
            query,
            resume_hitl=resume_hitl,
        )
        if result.get("hitl"):
            h = result["hitl"]
            return {
                "id": task_id,
                "status": {
                    "state": "input-required",
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": h.get("message", "Approval required")}],
                    },
                },
                "metadata": {
                    "pending_tool": h.get("pending_tool"),
                    "pending_args": h.get("pending_args"),
                },
            }
        text = str(result.get("text") or "")
        return {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": text}]}],
        }
    except Exception as exc:
        logger.exception("task_failed", task_id=task_id)
        return _task_failed(task_id, str(exc))


@app.post("/")
async def jsonrpc_root(request: Request, body: dict[str, Any]):
    rpc_id = body.get("id", "")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "message/send":
        message = params.get("message") or {}
        task_id = params.get("taskId") or message.get("taskId") or str(uuid.uuid4())
        parts = message.get("parts") or []
        text_parts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and (p.get("kind") == "text" or p.get("type") == "text")
        ]
        query = " ".join(text_parts).strip()
        logger.info("jsonrpc_message_send", task_id=task_id, query_preview=query[:200])
        task_result = await _execute_task(task_id, query, request, params)
        _task_store[task_id] = task_result
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task_result}

    if method == "tasks/get":
        tid = params.get("id", "")
        task = _task_store.get(tid)
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": f"Task {tid!r} not found"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method!r}"},
    }


@app.post("/a2a/tasks/send")
async def legacy_tasks_send(request: Request, body: dict[str, Any]):
    task_id = body.get("id", f"task_{uuid.uuid4().hex[:12]}")
    message = body.get("message") or {}
    parts = message.get("parts") or []
    text_parts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and (p.get("type") == "text" or p.get("kind") == "text")
    ]
    query = " ".join(text_parts).strip()
    meta = body.get("metadata") or {}
    params = {"message": message, "metadata": meta}
    return await _execute_task(task_id, query, request, params)
