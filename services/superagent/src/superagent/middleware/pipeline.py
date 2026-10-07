"""ExecutionMiddleware — 7-step pipeline for external agent calls."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from ..config import settings
from ..graph.state import session_credentials_from_config
from .input_guard import InputGuard, InputGuardError
from .observers import StepResult, emit_step_complete
from .output_normalizer import OutputNormalizer
from .preflight import PreFlightManager
from .step_output import call_credentials, redact_output, step_output_preimage

logger = logging.getLogger(__name__)


def _is_control_flow(exc: BaseException) -> bool:
    """Interrupts pause a call; they are not a failed call and record no step.

    Runs inside the dispatch ``except``: if the interrupt types cannot be
    imported, record nothing rather than raise over the original exception.
    """
    try:
        from langgraph.errors import GraphInterrupt

        from ..pricing.guard import PaymentInterrupt
        from .preflight import AuthInterruptRequired
        from .scope_gate import ScopeApprovalRequired
    except Exception:
        return True
    return isinstance(
        exc,
        (
            AuthInterruptRequired,
            ScopeApprovalRequired,
            PaymentInterrupt,
            GraphInterrupt,
        ),
    )


def _structural_verify(content: str, has_canvas: bool) -> tuple[bool, str]:
    """Structural check on agent output. Returns (verified, verdict_reason)."""
    if not content:
        return False, "empty output"
    if content.startswith(("Error:", "Input error:", "Unsupported protocol:")):
        return False, content[:120]
    if has_canvas:
        return True, "canvas output verified"
    return True, "ok"


class ExecutionMiddleware:
    """
    7-step pipeline:
    1. Route & Resolve (caller provides resolved agent_id/capability_id/protocol)
    2. InputGuard — jsonschema validate + pagination defaults
    2.5 PaymentGuard — credit check + Redis soft reserve (A2A/ACP only)
    3. PreFlight — manifest fetch, health, auth cascade
       Raises AuthInterruptRequired (soft) or PreFlightError (hard) — not returned.
    3.5 ScopeGate (connections only, AD-18) — a write with no declared allow
       and every destructive call raise ScopeApprovalRequired (soft) before
       any request reaches the platform.
    4. Handler Dispatch — MCPHandler | A2AHandler | ACPHandler
    5. OutputNormalizer — text vs artifact
    6. Checklist auto-update
    6.5 PaymentSettlement — deduct credits, write Transaction(PENDING) (async task)
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def execute(
        self,
        agent_id: str,
        capability_id: str,
        protocol: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        config: RunnableConfig | None = None,
        scope_approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the pipeline and return {"content": str, "artifact": ...}.

        ``scope_approval`` is ``{"call_id", "approver"}`` from a resumed
        approval card: the gate passes that one call, and the step records
        the approver (AD-21).
        """
        from ..vault.client import VaultClient

        vault = VaultClient()

        logger.info(
            "Pipeline execute | agent=%s capability=%s protocol=%s call_id=%s",
            agent_id,
            capability_id,
            protocol,
            call_id,
        )

        # Step 2: InputGuard
        schema = await self._get_capability_schema(agent_id, capability_id)
        try:
            args = InputGuard.validate(args, schema)
        except InputGuardError as exc:
            err_text = f"Input error: {exc}"
            self._auto_update_checklist(tool_name, call_id, err_text, success=False)
            return {"content": err_text}

        # Step 2.5: PaymentGuard (A2A/ACP only; MCP agents are infrastructure primitives)
        # Raises PaymentInterrupt → caught at execute_agent_calls_node level.
        base_fee = Decimal("0")
        if protocol not in ("MCP",):
            base_fee = await self._resolve_base_fee(agent_id)
            if base_fee > 0:
                from ..pricing.guard import payment_guard

                await payment_guard(
                    user_id=self._state.get("user_id", ""),
                    base_fee=base_fee,
                    session_id=self._state.get("session_id", ""),
                    call_id=call_id,
                )

        # Step 3: PreFlight (includes auth)
        # Raises AuthInterruptRequired (soft) → caught at node level by execute_agent_calls_node
        # Raises PreFlightError (hard) → caught at node level, yields error ToolMessage
        logger.info("Pipeline step 3: PreFlight | agent=%s", agent_id)
        preflight = PreFlightManager(vault)
        # AD-14: session-scoped credentials ride on the run config, never on
        # graph state (state is checkpointed; the config key is not).
        session_credentials = session_credentials_from_config(config)
        result = await preflight.run(
            agent_id=agent_id,
            user_id=self._state.get("user_id", ""),
            capability_id=capability_id,
            tool_name=tool_name,
            state=self._state,
            session_credentials=session_credentials,
        )

        auth_headers: dict[str, str] = result["headers"]
        manifest: dict[str, Any] = result["manifest"]
        # AD-16: the credentials resolved for this call, redacted out of the
        # output before its hash is taken.
        credentials = call_credentials(
            manifest, result.get("resolved_env"), auth_headers
        )

        # Step 3.5: ScopeGate (AD-18). Connections only, and only with the
        # feature on: an agent registered any other way is dispatched exactly
        # as before. Raises ScopeApprovalRequired → caught at node level.
        scope_meta = self._scope_gate(
            agent_id=agent_id,
            capability_id=capability_id,
            args=args,
            call_id=call_id,
            manifest=manifest,
            scope_approval=scope_approval,
        )

        # Step 4: Handler Dispatch
        _call_start = datetime.now(UTC)
        logger.info(
            "Pipeline step 5: dispatch | protocol=%s agent=%s", protocol, agent_id
        )
        transport = manifest.get("transport", {})
        # Inject resolved env for STDIO agents — PreFlight already resolved
        # ${VAR} placeholders from the vault; pass a copy so we don't mutate the cache.
        if result.get("resolved_env") is not None:
            transport = {**transport, "resolved_env": result["resolved_env"]}
        try:
            raw_output = await self._dispatch_with_timeout(
                protocol=protocol,
                agent_id=agent_id,
                capability_id=capability_id,
                args=args,
                auth_headers=auth_headers,
                transport=transport,
                config=config,
                call_id=call_id,
            )
        except Exception as exc:
            if not _is_control_flow(exc):
                await self._emit_failed_dispatch(
                    exc,
                    agent_id=agent_id,
                    capability_id=capability_id,
                    protocol=protocol,
                    tool_name=tool_name,
                    args=args,
                    call_id=call_id,
                    base_fee=base_fee,
                    credentials=credentials,
                    started=_call_start,
                    scope_meta=scope_meta,
                )
            raise
        # AD-16: the step commits to the raw result, not the display copy the
        # normalizer builds next.
        output_preimage = step_output_preimage(raw_output, credentials)
        # AD-14 / story 1.6b: the display copy is redacted too, before the
        # normalizer, the criteria step, the checklist, the ToolMessage and
        # the SSE stream see it — an agent echoing its request headers must
        # not put the token into the chat, the transcript or the run audit.
        raw_output = redact_output(raw_output, credentials)

        # Step 5: OutputNormalizer (async — may upload to S3 for file outputs)
        agent_name = str(manifest.get("name") or "").strip()
        if not agent_name:
            agent_name = agent_id.rsplit(":", 1)[-1].replace("_", " ")
        normalised = await OutputNormalizer.normalize(
            raw_output,
            protocol,
            session_id=self._state.get("session_id", ""),
            user_id=self._state.get("user_id", ""),
            agent_name=agent_name,
        )
        # The normalizer may synthesise text from the raw value (canvas
        # summaries, str() of unknown shapes): redact what it produced as well.
        for key in ("content", "ui_manifest"):
            if key in normalised:
                normalised[key] = redact_output(normalised[key], credentials)
        content = normalised.get("content", "")
        content_str = content if isinstance(content, str) else str(content)
        logger.info(
            "Pipeline complete | agent=%s call_id=%s content_len=%d",
            agent_id,
            call_id,
            len(content_str),
        )

        # Step 5.5: StructuralVerifier + optional declared-acceptance
        has_canvas = normalised.get("ui_manifest") is not None
        verified, verdict_reason = _structural_verify(content_str, has_canvas)
        normalised["verified"] = verified
        normalised["verdict_reason"] = verdict_reason

        declared_meta: dict[str, Any] = {}
        criteria = self._state.get("_declared_criteria")
        if isinstance(criteria, dict) and criteria:
            from .criteria import criteria_digest, evaluate_declared_criteria

            accepted, declared_reason = evaluate_declared_criteria(
                criteria, content_str
            )
            declared_meta["criteria_digest"] = criteria_digest(criteria)
            declared_meta["declared_acceptance"] = {
                "result": "pass" if accepted else "fail",
                "detail": declared_reason,
            }

        # Step 6: Checklist auto-update
        success = not (
            content_str.startswith("Error:")
            or content_str.startswith("Input error:")
            or content_str.startswith("Unsupported protocol:")
        )
        self._auto_update_checklist(tool_name, call_id, content_str, success=success)

        # Open/closed seam: emit the completed step to the installed observer.
        # OSS ships NoOpObserver; hosted deployments inject a recorder
        # server-side. emit_step_complete never raises out to the caller.
        _latency_ms = int((datetime.now(UTC) - _call_start).total_seconds() * 1000)
        await emit_step_complete(
            StepResult(
                call_id=call_id,
                agent_id=agent_id,
                capability_id=capability_id,
                protocol=protocol,
                tool_name=tool_name,
                success=success,
                content=content_str,
                output_preimage=output_preimage,
                user_id=self._state.get("user_id", ""),
                session_id=self._state.get("session_id", ""),
                latency_ms=_latency_ms,
                base_fee=str(base_fee),
                verdict={"verified": verified, "reason": verdict_reason},
                metadata={"goal": self._session_goal(), **declared_meta, **scope_meta},
                args=dict(args),
            )
        )

        # Step 6.5: PaymentSettlement — async, non-blocking for the caller.
        # Only runs for charged agents (A2A/ACP with base_fee > 0).
        if protocol not in ("MCP",) and base_fee > 0:
            elapsed_ms = int((datetime.now(UTC) - _call_start).total_seconds() * 1000)
            from ..pricing.settlement import settle_invocation

            asyncio.create_task(
                settle_invocation(
                    user_id=self._state.get("user_id", ""),
                    agent_id=agent_id,
                    session_id=self._state.get("session_id", ""),
                    call_id=call_id,
                    base_fee=base_fee,
                    latency_ms=elapsed_ms,
                    execution_success=success,
                    platform_tokens=self._state.get("_last_turn_tokens", 0),
                )
            )

        # Surface cost breakdown for SSE / transcript.
        # total_cost_usd = agent base_fee + LLM token cost (PLATFORM_TOKEN_RATE × output tokens)
        try:
            from common_pricing.constants import PLATFORM_TOKEN_RATE

            platform_tokens = int(self._state.get("_last_turn_tokens") or 0)
            llm_cost = PLATFORM_TOKEN_RATE * Decimal(str(platform_tokens))
            total_cost = (base_fee + llm_cost).quantize(Decimal("0.00000001"))
        except Exception:
            llm_cost = Decimal("0")
            total_cost = base_fee

        normalised["base_fee"] = str(base_fee)
        normalised["llm_cost_usd"] = str(llm_cost)
        normalised["total_cost_usd"] = str(total_cost)
        return normalised

    async def _emit_failed_dispatch(
        self,
        exc: Exception,
        *,
        agent_id: str,
        capability_id: str,
        protocol: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        base_fee: Decimal,
        credentials: list[tuple[str, str]],
        started: datetime,
        scope_meta: dict[str, Any] | None = None,
    ) -> None:
        """Record a call whose dispatch raised as a ``success: false`` step.

        The caller re-raises, so retry and error handling are unchanged. A
        retried call re-emits under the same ``call_id``; the run-attestation
        observer keeps the last attempt, so the receipt has one row per call.
        The content matches the ToolMessage the node builds from the error.
        Never raises: it runs inside the caller's ``except``, where a new
        exception would replace the one being re-raised.
        """
        try:
            error_text = f"Error: {exc}"
            record = StepResult(
                call_id=call_id,
                agent_id=agent_id,
                capability_id=capability_id,
                protocol=protocol,
                tool_name=tool_name,
                success=False,
                content=error_text,
                output_preimage=step_output_preimage(error_text, credentials),
                user_id=self._state.get("user_id", ""),
                session_id=self._state.get("session_id", ""),
                latency_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                base_fee=str(base_fee),
                verdict={"verified": False, "reason": error_text[:120]},
                metadata={"goal": self._session_goal(), **(scope_meta or {})},
                args=dict(args),
            )
        except Exception:
            logger.exception("failed-dispatch step not recorded | call_id=%s", call_id)
            return
        await emit_step_complete(record)

    def _scope_gate(
        self,
        *,
        agent_id: str,
        capability_id: str,
        args: dict[str, Any],
        call_id: str,
        manifest: dict[str, Any],
        scope_approval: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The pipeline's dispatch-site call of ``scope_gate`` (AD-18).

        Returns the ``scope_approval`` step metadata when this call carries
        an approval, else ``{}``. With CONNECTIONS_ENABLED off, or for an
        agent that is not a connection, the gate is not invoked.
        """
        from .connections import connections_enabled, is_connection
        from .scope_gate import scope_gate, scope_verdict

        if not connections_enabled() or not is_connection(manifest):
            return {}
        scope_gate(
            agent_id=agent_id,
            capability_id=capability_id,
            args=args,
            call_id=call_id,
            session_id=str(self._state.get("session_id") or ""),
            manifest=manifest,
            scope_approval=scope_approval,
            connection_name=str(manifest.get("name") or ""),
        )
        if (
            isinstance(scope_approval, dict)
            and scope_approval.get("call_id") == call_id
        ):
            return {
                "scope_approval": scope_verdict(
                    approved=True, approver=str(scope_approval.get("approver") or "")
                )
            }
        return {}

    async def emit_declined(
        self,
        *,
        agent_id: str,
        capability_id: str,
        protocol: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        content: str,
    ) -> None:
        """Record a call the user declined at the approval card (AD-21).

        Nothing was dispatched, so there is no output to hash beyond the
        refusal text; the step is ``success: false`` and its verdict is
        ``scope_approval:<call_id> warn`` — never ``fail``. Never raises.
        """
        from .scope_gate import scope_verdict

        try:
            record = StepResult(
                call_id=call_id,
                agent_id=agent_id,
                capability_id=capability_id,
                protocol=protocol,
                tool_name=tool_name,
                success=False,
                content=content,
                output_preimage=step_output_preimage(content, []),
                user_id=self._state.get("user_id", ""),
                session_id=self._state.get("session_id", ""),
                latency_ms=0,
                base_fee="0",
                verdict={"verified": False, "reason": content[:120]},
                metadata={
                    "goal": self._session_goal(),
                    "scope_approval": scope_verdict(approved=False),
                },
                args=dict(args),
            )
        except Exception:
            logger.exception("declined step not recorded | call_id=%s", call_id)
            return
        await emit_step_complete(record)

    async def _resolve_base_fee(self, agent_id: str) -> Decimal:
        """Fetch the agent's base_fee from DB (or manifest cache). Returns Decimal("0") if free."""
        try:
            from src.generated_client import Prisma

            db = Prisma()
            await db.connect()
            try:
                payment = await db.payment.find_unique(where={"agent_id": agent_id})
                if payment and payment.enabled and getattr(payment, "base_fee", None):
                    return Decimal(str(payment.base_fee))
            finally:
                await db.disconnect()
        except Exception:
            logger.debug("_resolve_base_fee: DB unavailable for agent=%s", agent_id)
        return Decimal("0")

    async def _get_capability_schema(
        self, agent_id: str, capability_id: str
    ) -> dict[str, Any] | None:
        from .manifest_cache import MANIFEST_CACHE

        manifest = await MANIFEST_CACHE.get_manifest(agent_id)
        caps = manifest.get("capabilities", [])
        for cap in caps:
            if cap.get("capability_id") == capability_id:
                return cap.get("input_schema")
        return None

    async def _dispatch_with_timeout(
        self, protocol: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Hard per-call ceiling: a hung agent becomes an Error string (retryable),
        never a silent stall. Config: agent_call_timeout_seconds."""
        timeout_s = getattr(settings, "agent_call_timeout_seconds", 60) or 60
        try:
            return await asyncio.wait_for(
                self._dispatch(protocol, *args, **kwargs), timeout=timeout_s
            )
        except TimeoutError:
            logger.error(
                "dispatch timeout after %ds | protocol=%s", timeout_s, protocol
            )
            return f"Error: agent call timed out after {timeout_s}s ({protocol})"

    async def _dispatch(
        self,
        protocol: str,
        agent_id: str,
        capability_id: str,
        args: dict[str, Any],
        auth_headers: dict[str, str],
        transport: dict[str, Any],
        config: RunnableConfig | None,
        call_id: str,
    ) -> Any:
        if protocol == "MCP":
            from ..handlers.mcp_handler import MCPHandler

            handler = MCPHandler(auth_headers=auth_headers)
            return await handler.call_tool(
                agent_id=agent_id,
                capability_id=capability_id,
                args=args,
                transport=transport,
            )
        if protocol in ("A2A", "ACP"):
            from ..handlers.a2a_handler import A2AHandler

            handler = A2AHandler(auth_headers=auth_headers)
            return await handler.send_task(
                agent_id=agent_id,
                task=args.get("task", str(args)),
                transport=transport,
                state=self._state,
                config=config,
                call_id=call_id,
            )
        if protocol == "COMPUTER_USE":
            from ..handlers.computer_use_handler import ComputerUseHandler

            handler = ComputerUseHandler(auth_headers=auth_headers)
            return await handler.execute(
                args=args,
                transport=transport,
                config=config,
                call_id=call_id,
                state=self._state,
            )
        return f"Unsupported protocol: {protocol}"

    def _session_goal(self) -> str:
        """Session goal: checklist goal when present, else first HumanMessage."""
        checklist = self._state.get("task_checklist")
        if checklist is not None:
            goal = getattr(checklist, "goal", "")
            if goal:
                return goal
        for msg in self._state.get("messages", []):
            if isinstance(msg, HumanMessage):
                return str(msg.content or "")
        return ""

    def _auto_update_checklist(
        self, tool_name: str, call_id: str, result_summary: str, success: bool
    ) -> None:
        checklist = self._state.get("task_checklist")
        if not checklist:
            return
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        for step in getattr(checklist, "steps", []):
            # Primary match: exact call_id (most reliable)
            step_call_id = getattr(step, "call_id", None)
            if step_call_id and step_call_id == call_id:
                step.status = "done" if success else "failed"
                step.result_summary = result_summary[:200]
                step.completed_at = now
                break
            # Fallback: agent_id fuzzy match (for steps not yet bound to a call_id)
            agent_id = getattr(step, "agent_id", None)
            if agent_id and agent_id in tool_name and step_call_id is None:
                step.status = "done" if success else "failed"
                step.result_summary = result_summary[:200]
                step.completed_at = now
                break
