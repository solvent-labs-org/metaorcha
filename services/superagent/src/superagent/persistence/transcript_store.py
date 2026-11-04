"""Conversation transcript persistence — LangChain messages ↔ Postgres rows."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_messages,
    messages_from_dict,
)

from ..tool_call_parsing import normalize_openai_api_tool_calls

logger = logging.getLogger(__name__)

# ToolMessage.additional_kwargs — persisted to SessionTranscriptEntry.tool_inputs (JSON)
TRANSCRIPT_TOOL_META_KEY = "transcript_meta"

DEFAULT_TITLE = "New chat"
_MAX_PAGE_SIZE = 50


def _content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content) if content is not None else ""


def _assistant_model(message: AIMessage) -> dict[str, Any] | None:
    """``{"model": "<route>/<model id>"}`` from the message, or None."""
    from ..turn_model import TURN_MODEL_KEY

    metadata = getattr(message, "response_metadata", None)
    model = metadata.get(TURN_MODEL_KEY) if isinstance(metadata, dict) else None
    return {"model": model} if isinstance(model, str) and model else None


def _tool_calls_to_json(msg: AIMessage) -> list[dict[str, Any]] | None:
    raw = getattr(msg, "tool_calls", None) or []
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            out.append(
                {
                    "id": str(tc.get("id", "")),
                    "name": str(tc.get("name", "")),
                    "args": tc.get("args", {})
                    if isinstance(tc.get("args"), dict)
                    else {},
                }
            )
    return out or None


def _sanitize_for_prisma_json(obj: Any) -> Any:
    """Recursively coerce values for Prisma ``Json`` fields (must be JSON-serialisable)."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_prisma_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_prisma_json(v) for v in obj]
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return _sanitize_for_prisma_json(model_dump())
    return str(obj)


def _tool_status_from_content(content: str) -> str | None:
    if content.startswith("Error:") or content.startswith("Input error:"):
        return "error"
    if content.startswith("Unsupported protocol:"):
        return "error"
    return "success"


def _preview_message_raw(m: Any, *, max_len: int = 400) -> str:
    """Compact, log-safe description of a checkpoint message slot."""
    if isinstance(m, BaseMessage):
        tc = getattr(m, "type", None) or type(m).__name__
        content = _content_to_str(getattr(m, "content", None))[:80]
        return f"BaseMessage type={tc!r} content_preview={content!r}"
    if isinstance(m, dict):
        keys = list(m.keys())[:20]
        role = m.get("role")
        typ = m.get("type")
        hint = f"keys={keys}"
        if isinstance(role, str):
            hint += f" role={role!r}"
        if isinstance(typ, str):
            hint += f" lc_type={typ!r}"
        raw = repr(m)
        if len(raw) > max_len:
            raw = raw[:max_len] + "…"
        return f"dict {hint} repr={raw}"
    raw = repr(m)
    if len(raw) > max_len:
        raw = raw[:max_len] + "…"
    return f"{type(m).__name__} repr={raw}"


def _normalize_openai_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    """OpenAI API shape → LangChain AIMessage tool_calls list."""
    return normalize_openai_api_tool_calls(raw)


def _openai_style_dict_to_messages(d: dict[str, Any]) -> list[BaseMessage] | None:
    """
    Best-effort: ``{"role": "...", "content": ...}`` (and tool/tool_calls variants)
    as returned by some serializers / providers.
    """
    role = d.get("role")
    if not isinstance(role, str):
        return None
    r = role.lower()
    content = d.get("content")
    if r in ("user", "human"):
        return [HumanMessage(content=_content_to_str(content))]
    if r in ("assistant", "ai"):
        tcalls = _normalize_openai_tool_calls(d.get("tool_calls"))
        if tcalls:
            return [
                AIMessage(
                    content=_content_to_str(content) if content is not None else "",
                    tool_calls=tcalls,
                )
            ]
        return [
            AIMessage(content=_content_to_str(content) if content is not None else "")
        ]
    if r == "tool":
        tcid = str(d.get("tool_call_id", "") or "")
        name = d.get("name")
        name_s = str(name) if name is not None else None
        kwargs: dict[str, Any] = {
            "content": _content_to_str(content),
            "tool_call_id": tcid,
        }
        if name_s:
            kwargs["name"] = name_s
        return [ToolMessage(**kwargs)]
    return None


def _coerce_one_checkpoint_message(m: Any) -> list[BaseMessage]:
    """Best-effort: one checkpoint item → zero or more BaseMessage instances."""
    if isinstance(m, BaseMessage):
        return [m]
    if isinstance(m, dict):
        if "data" in m and isinstance(m.get("type"), str):
            try:
                got = list(messages_from_dict([m]))
                if got:
                    logger.debug(
                        "transcript: messages_from_dict ok type=%r",
                        m.get("type"),
                    )
                    return got
            except (KeyError, TypeError, ValueError):
                logger.info(
                    "transcript: messages_from_dict failed type=%r preview=%s",
                    m.get("type"),
                    _preview_message_raw(m),
                    exc_info=True,
                )
        oai = _openai_style_dict_to_messages(m)
        if oai:
            logger.debug(
                "transcript: coerced via openai-style dict keys=%s", list(m.keys())[:12]
            )
            return oai
        try:
            got = list(convert_to_messages([m]))
            if got:
                logger.debug(
                    "transcript: convert_to_messages ok keys=%s",
                    list(m.keys())[:12],
                )
            return got
        except (TypeError, ValueError, NotImplementedError):
            logger.warning(
                "transcript: could not coerce message dict preview=%s",
                _preview_message_raw(m),
            )
            return []
    logger.warning(
        "transcript: skipping persist for non-dict message %s",
        _preview_message_raw(m),
    )
    return []


def coerce_checkpoint_messages(messages: list[Any]) -> list[BaseMessage]:
    """
    LangGraph / Redis checkpoints often deserialize ``messages`` as plain dicts.
    ``messages_to_entry_dicts`` only recognises BaseMessage instances, so we
    normalise here before persisting.
    """
    out: list[BaseMessage] = []
    for m in messages:
        out.extend(_coerce_one_checkpoint_message(m))
    return out


def _unparsed_slot_placeholder(raw: Any) -> AIMessage:
    """One DB row per checkpoint slot when coercion fails (keeps counts aligned)."""
    return AIMessage(
        content=(
            "[Transcript: could not parse checkpoint message — check superagent logs. "
            f"Preview: {_preview_message_raw(raw, max_len=280)}]"
        )
    )


def coerce_checkpoint_messages_for_persist(raw_tail: list[Any]) -> list[BaseMessage]:
    """
    Like ``coerce_checkpoint_messages`` but guarantees **one** ``BaseMessage`` per
    raw checkpoint slot so ``persisted_message_count`` stays aligned with Postgres
    rows and LangGraph's ``messages`` list indices.
    """
    out: list[BaseMessage] = []
    for raw in raw_tail:
        cms = _coerce_one_checkpoint_message(raw)
        if not cms:
            logger.warning(
                "transcript: empty coercion for checkpoint slot preview=%s",
                _preview_message_raw(raw),
            )
            out.append(_unparsed_slot_placeholder(raw))
            continue
        if len(cms) > 1:
            logger.warning(
                "transcript: %d messages from one checkpoint slot; using first only preview=%s",
                len(cms),
                _preview_message_raw(raw),
            )
        out.append(cms[0])
    return out


def messages_to_entry_dicts(
    messages: list[BaseMessage],
    start_sequence: int,
) -> list[dict[str, Any]]:
    """Map LangChain messages to Prisma SessionTranscriptEntry create payloads (no id)."""
    from src.generated_client import enums

    rows: list[dict[str, Any]] = []
    seq = start_sequence
    for m in messages:
        if isinstance(m, HumanMessage):
            ak = getattr(m, "additional_kwargs", None) or {}
            att = ak.get("artifact_attachments")
            user_tool_inputs = None
            if isinstance(att, list) and len(att) > 0:
                user_tool_inputs = {
                    "artifact_attachments": _sanitize_for_prisma_json(att)
                }
            rows.append(
                {
                    "sequence_num": seq,
                    "role": enums.TranscriptRole.USER,
                    "content": _content_to_str(m.content),
                    "tool_calls": None,
                    "tool_call_id": None,
                    "tool_name": None,
                    "tool_inputs": user_tool_inputs,
                    "tool_status": None,
                }
            )
            seq += 1
        elif isinstance(m, AIMessage):
            rows.append(
                {
                    "sequence_num": seq,
                    "role": enums.TranscriptRole.ASSISTANT,
                    "content": _content_to_str(m.content),
                    "tool_calls": _tool_calls_to_json(m),
                    "tool_call_id": None,
                    "tool_name": None,
                    # Story 3.3: the model that produced this message, so a
                    # turn with no steps (no receipt) still records it.
                    "tool_inputs": _assistant_model(m),
                    "tool_status": None,
                }
            )
            seq += 1
        elif isinstance(m, ToolMessage):
            name = getattr(m, "name", None) or None
            content = _content_to_str(m.content)
            ak = getattr(m, "additional_kwargs", None) or {}
            meta = ak.get(TRANSCRIPT_TOOL_META_KEY) if isinstance(ak, dict) else None
            tool_inputs = meta if isinstance(meta, dict) else None
            rows.append(
                {
                    "sequence_num": seq,
                    "role": enums.TranscriptRole.TOOL,
                    "content": content,
                    "tool_calls": None,
                    "tool_call_id": m.tool_call_id,
                    "tool_name": name,
                    "tool_inputs": tool_inputs,
                    "tool_status": _tool_status_from_content(content),
                }
            )
            seq += 1
        else:
            logger.info(
                "transcript: skipping persist for unsupported message type %s preview=%s",
                type(m).__name__,
                _preview_message_raw(m),
            )
    return rows


def rows_to_langchain(rows: list[Any]) -> list[BaseMessage]:
    """Hydrate LangChain messages from Prisma SessionTranscriptEntry records."""
    from src.generated_client import enums

    out: list[BaseMessage] = []
    for r in rows:
        role = r.role
        if role == enums.TranscriptRole.USER:
            user_kw: dict[str, Any] = {}
            ti = r.tool_inputs
            if isinstance(ti, dict):
                att = ti.get("artifact_attachments")
                if isinstance(att, list) and att:
                    user_kw["artifact_attachments"] = att
            out.append(HumanMessage(content=r.content, additional_kwargs=user_kw))
        elif role == enums.TranscriptRole.ASSISTANT:
            tc = r.tool_calls
            tool_calls: list[dict[str, Any]] = []
            if isinstance(tc, list) and tc:
                tool_calls = [
                    {
                        "id": str(x.get("id", "")),
                        "name": str(x.get("name", "")),
                        "args": x.get("args", {})
                        if isinstance(x.get("args"), dict)
                        else {},
                    }
                    for x in tc
                    if isinstance(x, dict)
                ]
            if tool_calls:
                out.append(AIMessage(content=r.content or "", tool_calls=tool_calls))
            else:
                out.append(AIMessage(content=r.content or ""))
        elif role == enums.TranscriptRole.TOOL:
            kwargs: dict[str, Any] = {
                "content": r.content,
                "tool_call_id": r.tool_call_id or "",
            }
            if r.tool_name:
                kwargs["name"] = r.tool_name
            ti = r.tool_inputs
            if isinstance(ti, dict) and ti:
                kwargs["additional_kwargs"] = {
                    TRANSCRIPT_TOOL_META_KEY: dict(ti),
                }
            out.append(ToolMessage(**kwargs))
    return out


def prisma_transcript_entry_create_data(
    session_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """
    Build Prisma create payload. Optional Json? fields must be omitted when null —
    passing Python None triggers MissingRequiredValueError in prisma-client-py.

    ``tool_calls`` / ``tool_inputs`` must use ``fields.Json(...)`` — raw dict/list
    values are rejected by the query engine (see MissingRequiredValueError on Json).
    """
    from src.generated_client.fields import Json

    data: dict[str, Any] = {
        "session_id": session_id,
        "sequence_num": row["sequence_num"],
        "role": row["role"],
        "content": row["content"],
    }
    for key in ("tool_call_id", "tool_name", "tool_status"):
        val = row.get(key)
        if val is not None:
            data[key] = val
    for key in ("tool_calls", "tool_inputs"):
        val = row.get(key)
        if val is not None:
            data[key] = Json(_sanitize_for_prisma_json(val))
    return data


async def upsert_conversation_session(
    session_id: str,
    user_id: str,
    title: str | None,
    office_id: str | None = None,
) -> None:
    """Create the session, or retitle it. The office is fixed at creation
    (story 2.0): an update never moves a session to another office."""
    from src.generated_client import Prisma

    t = (title or "").strip() or DEFAULT_TITLE
    create: dict[str, Any] = {"id": session_id, "user_id": user_id, "title": t}
    if office_id:
        create["office_id"] = office_id
    db = Prisma()
    await db.connect()
    try:
        await db.conversationsession.upsert(
            where={"id": session_id},
            data={
                "create": create,
                "update": {"title": t},
            },
        )
    except Exception:
        logger.exception("upsert_conversation_session failed for %s", session_id)
        raise
    finally:
        await db.disconnect()


async def persist_new_messages(
    session_id: str,
    messages: list[Any],
    previous_persisted_count: int,
) -> int:
    """
    Append transcript rows for messages[previous_persisted_count:].
    Returns new persisted_message_count (total LangChain messages processed, not skipped).
    """
    if previous_persisted_count >= len(messages):
        return previous_persisted_count

    tail_raw = messages[previous_persisted_count:]
    if not tail_raw:
        return previous_persisted_count

    logger.info(
        "transcript: persist_new_messages session=%s prev_count=%d total_msgs=%d tail=%d",
        session_id,
        previous_persisted_count,
        len(messages),
        len(tail_raw),
    )
    for i, raw in enumerate(tail_raw):
        logger.debug(
            "transcript: tail[%d/%d] %s",
            i + 1,
            len(tail_raw),
            _preview_message_raw(raw),
        )

    tail = coerce_checkpoint_messages_for_persist(tail_raw)

    from src.generated_client import Prisma

    # Next sequence = max existing + 1 or 1
    db = Prisma()
    await db.connect()
    try:
        last = await db.sessiontranscriptentry.find_first(
            where={"session_id": session_id},
            order={"sequence_num": "desc"},
        )
        start_seq = (last.sequence_num + 1) if last else 1
        payloads = messages_to_entry_dicts(tail, start_seq)
        if not payloads:
            if tail_raw:
                logger.error(
                    "transcript: zero rows after coercion (unexpected) session=%s tail_raw=%d",
                    session_id,
                    len(tail_raw),
                )
            return previous_persisted_count

        logger.info(
            "transcript: inserting %d row(s) for session=%s sequence_start=%d",
            len(payloads),
            session_id,
            start_seq,
        )

        for p in payloads:
            await db.sessiontranscriptentry.create(
                data=prisma_transcript_entry_create_data(session_id, p),
            )

        # Advance by raw checkpoint indices consumed (matches messages[prev:] slice).
        new_count = previous_persisted_count + len(tail_raw)
        await db.conversationsession.update(
            where={"id": session_id},
            data={
                "persisted_message_count": new_count,
                "updated_at": datetime.now(UTC),
            },
        )
        return new_count
    except Exception:
        logger.exception("persist_new_messages failed for session %s", session_id)
        return previous_persisted_count
    finally:
        await db.disconnect()


async def load_transcript_rows(session_id: str) -> list[Any]:
    from src.generated_client import Prisma

    db = Prisma()
    await db.connect()
    try:
        return await db.sessiontranscriptentry.find_many(
            where={"session_id": session_id},
            order={"sequence_num": "asc"},
        )
    finally:
        await db.disconnect()


async def get_session_meta(session_id: str) -> tuple[int, str | None] | None:
    """Return (persisted_message_count, user_id) or None."""
    from src.generated_client import Prisma

    db = Prisma()
    await db.connect()
    try:
        row = await db.conversationsession.find_unique(where={"id": session_id})
        if row is None:
            return None
        return (row.persisted_message_count, row.user_id)
    finally:
        await db.disconnect()


async def get_session_office(session_id: str) -> tuple[str | None, str | None] | None:
    """Return (user_id, office_id) or None. A NULL office is the owner's
    personal office; the Gateway resolves that, not this service."""
    from src.generated_client import Prisma

    db = Prisma()
    await db.connect()
    try:
        row = await db.conversationsession.find_unique(where={"id": session_id})
        if row is None:
            return None
        return (row.user_id, getattr(row, "office_id", None))
    finally:
        await db.disconnect()


async def verify_session_owner(session_id: str, user_id: str) -> bool:
    meta = await get_session_meta(session_id)
    if meta is None:
        return False
    return meta[1] == user_id


async def list_sessions_paginated(
    user_id: str,
    page: int,
    page_size: int,
    office_id: str | None = None,
    include_unassigned: bool = False,
) -> tuple[list[Any], int]:
    """List a user's sessions, optionally in one office. ``include_unassigned``
    also returns sessions with no office (created before story 2.0), which
    belong to the owner's personal office."""
    from src.generated_client import Prisma

    ps = min(max(1, page_size), _MAX_PAGE_SIZE)
    p = max(1, page)
    skip = (p - 1) * ps

    where: dict[str, Any] = {"user_id": user_id}
    if office_id:
        if include_unassigned:
            where["OR"] = [{"office_id": office_id}, {"office_id": None}]
        else:
            where["office_id"] = office_id

    db = Prisma()
    await db.connect()
    try:
        total = await db.conversationsession.count(where=where)
        rows = await db.conversationsession.find_many(
            where=where,
            order={"updated_at": "desc"},
            skip=skip,
            take=ps,
        )
        return rows, total
    finally:
        await db.disconnect()


def _role_name(row: Any) -> str:
    r = row.role
    if hasattr(r, "name"):
        return str(r.name)
    s = str(r)
    return s.rsplit(".", 1)[-1] if "." in s else s


def entry_to_dto(row: Any) -> dict[str, Any]:
    """Prisma row → JSON-serializable dict for API."""
    tool_calls = row.tool_calls
    if isinstance(tool_calls, list):
        tc_out = [
            {
                "id": str(x.get("id", "")),
                "name": str(x.get("name", "")),
                "args": x.get("args", {}) if isinstance(x.get("args"), dict) else {},
            }
            for x in tool_calls
            if isinstance(x, dict)
        ]
    else:
        tc_out = None
    if tc_out is not None and len(tc_out) == 0:
        tc_out = None

    role = _role_name(row)
    if role not in ("USER", "ASSISTANT", "TOOL"):
        role = "USER"

    tool_status = row.tool_status
    if tool_status not in ("success", "error", None):
        tool_status = None

    return {
        "sequence_num": row.sequence_num,
        "role": role,
        "content": row.content,
        "tool_calls": tc_out,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "tool_inputs": row.tool_inputs if isinstance(row.tool_inputs, dict) else None,
        "tool_status": tool_status,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }
