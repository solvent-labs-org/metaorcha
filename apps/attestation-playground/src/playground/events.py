"""Kafka bridge: execution.step_complete -> per-run SSE queues.

Consumes the harness's EXISTING step-event topic (published by superagent's
step_events.py). No new event channel is introduced. Live steps are a
narrative feed only — the attested chain comes from the signed envelope.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from common_kafka import BaseKafkaConsumer, KafkaConsumerConfig, KafkaTopics

logger = logging.getLogger(__name__)


class StepEventRouter(BaseKafkaConsumer):
    """One app-level consumer routing step events to run queues by session_id."""

    def __init__(self, bootstrap_servers: str) -> None:
        config = KafkaConsumerConfig(
            bootstrap_servers=bootstrap_servers,
            group_id="attestation-playground",
            topics=[KafkaTopics.EXECUTION_STEP_COMPLETE],
            auto_offset_reset="latest",
            enable_auto_commit=True,
        )
        super().__init__(config)
        self._routes: dict[str, asyncio.Queue] = {}

    def register(self, session_id: str, queue: asyncio.Queue) -> None:
        self._routes[session_id] = queue

    async def handle_message(self, message: dict[str, Any]) -> None:
        queue = self._routes.get(message.get("session_id") or "")
        if queue is None:
            return
        await queue.put(
            {
                "type": "live_step",
                "tool": message.get("tool_name"),
                "success": message.get("success"),
                "latency_ms": message.get("latency_ms"),
                "call_id": message.get("call_id"),
            }
        )
