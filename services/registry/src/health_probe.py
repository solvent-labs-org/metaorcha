"""When GET health_endpoint is not a useful register gate."""

from __future__ import annotations

from .models.emerge_config import EmergeConfig


def should_skip_health_probe(config: EmergeConfig) -> bool:
    """True when GET health_endpoint is not a useful register gate.

    STDIO agents have no HTTP port. Remote MCP SSE/HTTP servers usually have
    no /health; capability harvest is the connectivity check. A2A HTTP still
    probes health.
    """
    transport = config.protocol.transport.type.lower()
    proto = config.protocol.type.lower()
    if transport == "stdio":
        return True
    return proto == "mcp" and transport in ("sse", "http")
