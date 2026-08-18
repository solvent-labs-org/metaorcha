"""OutputNormalizer — converts handler results to AgentState-compatible format.

Plain text / JSON MCP results pass through unchanged. Binary MCP payloads (bytes or
content-list blobs) are uploaded via artifact_store. File paths on disk are NOT
interpreted here — the model must call save_artifact after MCP tools that write files.

Canvas envelopes: if an agent returns {"__canvas__": True, "manifest": {...}, "summary": "..."}
(or a JSON string of that shape), normalize() sets "ui_manifest" in the result dict and
the execute_agent_calls node emits a canvas_manifest SSE event.

Text outputs without an envelope are synthesised into one (layout "single" with a
markdown_card component) so every successful agent run renders as a dashboard card
instead of a chat bubble. Error-prefixed and artifact results are never wrapped.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from typing import Any

from ..artifact_store import persist_agent_output_bytes
from ..graph.state import ArtifactRef


class OutputNormalizer:
    """Normalises handler output into a content string and optional artifact.

    normalize() is async — it may upload binary blobs to S3.
    """

    @staticmethod
    def _detect_canvas_envelope(raw: Any) -> dict[str, Any] | None:
        """Return the UIManifest dict if raw is a canvas envelope, else None.

        A canvas envelope is any dict (or JSON string) with ``__canvas__: true``
        and a ``manifest`` key whose value matches the UIManifest schema.
        """
        candidate = raw
        if isinstance(raw, str):
            try:
                candidate = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
        if isinstance(candidate, dict) and candidate.get("__canvas__") is True:
            manifest = candidate.get("manifest")
            if isinstance(manifest, dict) and manifest.get("version") == "1.0":
                return manifest
        return None

    @staticmethod
    def _synthesise_canvas_envelope(
        result: dict[str, Any], agent_name: str
    ) -> dict[str, Any]:
        """Wrap a plain-text result in a canvas envelope (single markdown_card).

        Only successful, non-artifact text results are wrapped — error-prefixed
        content, empty content, and existing envelopes pass through unchanged.
        """
        content = result.get("content")
        if (
            result.get("ui_manifest") is not None
            or result.get("artifact") is not None
            or not isinstance(content, str)
            or not content.strip()
            or content.startswith(("Error:", "Input error:", "Unsupported protocol:"))
        ):
            return result
        title = agent_name or "Agent output"
        result["content"] = content[:280]
        result["ui_manifest"] = {
            "version": "1.0",
            "title": title,
            "layout": "single",
            "components": [
                {
                    "type": "markdown_card",
                    "id": "agent-output",
                    "title": title,
                    "body": content,
                }
            ],
        }
        return result

    @staticmethod
    async def normalize(
        raw_output: Any,
        protocol: str,
        session_id: str = "",
        user_id: str = "",
        agent_name: str = "",
    ) -> dict[str, Any]:
        """
        Normalise raw handler output.

        Returns dict:
            "content"    — str content for ToolMessage
            "artifact"   — ArtifactRef | None if binary content was stored
            "ui_manifest" — UIManifest dict | None if agent returned a canvas envelope
        """
        ui_manifest = OutputNormalizer._detect_canvas_envelope(raw_output)
        if ui_manifest is not None:
            summary: str
            if isinstance(raw_output, str):
                try:
                    parsed = json.loads(raw_output)
                    summary = str(parsed.get("summary", "Dashboard rendered."))
                except Exception:
                    summary = "Dashboard rendered."
            elif isinstance(raw_output, dict):
                summary = str(raw_output.get("summary", "Dashboard rendered."))
            else:
                summary = "Dashboard rendered."
            return {"content": summary, "artifact": None, "ui_manifest": ui_manifest}

        if isinstance(raw_output, str):
            result = {"content": raw_output, "artifact": None, "ui_manifest": None}
        elif isinstance(raw_output, dict):
            result = await OutputNormalizer._normalise_dict(
                raw_output, session_id, user_id
            )
            result.setdefault("ui_manifest", None)
        elif isinstance(raw_output, bytes):
            result = await persist_agent_output_bytes(
                raw_output,
                "application/octet-stream",
                "output.bin",
                session_id,
                user_id,
            )
            result.setdefault("ui_manifest", None)
        else:
            result = {"content": str(raw_output), "artifact": None, "ui_manifest": None}

        return OutputNormalizer._synthesise_canvas_envelope(result, agent_name)

    @staticmethod
    async def _normalise_dict(
        data: dict[str, Any], session_id: str, user_id: str
    ) -> dict[str, Any]:
        # MCP content list (list of text/image/blob items)
        content_list = data.get("content")
        if isinstance(content_list, list):
            parts: list[str] = []
            artifact: ArtifactRef | None = None
            for item in content_list:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                item_type = item.get("type", "text")
                if item_type == "text":
                    parts.append(item.get("text", ""))
                elif item_type in ("image", "blob"):
                    raw = item.get("data") or item.get("blob")
                    if raw:
                        mime = item.get("mimeType", "application/octet-stream")
                        if isinstance(raw, str):
                            try:
                                raw = base64.b64decode(raw)
                            except Exception:
                                parts.append(raw)
                                continue
                        result = await persist_agent_output_bytes(
                            raw,
                            mime,
                            f"output{_ext_for_mime(mime)}",
                            session_id,
                            user_id,
                        )
                        artifact = result["artifact"]
                        if artifact:
                            parts.append(f"[Artifact: {artifact.artifact_id}]")
                else:
                    parts.append(str(item))
            return {"content": "\n".join(parts), "artifact": artifact}

        # A2A / generic text result
        text = (
            data.get("result")
            or data.get("output")
            or data.get("text")
            or data.get("message")
            or str(data)
        )
        return {"content": str(text), "artifact": None}


def _ext_for_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime) or ""
    return ext if ext else ".bin"
