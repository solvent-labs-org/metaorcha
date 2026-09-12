"""Fan-out StepResult events to Kafka for validator observers."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .observers import StepResult

logger = logging.getLogger(__name__)


def step_result_payload(record: StepResult) -> dict:
    from .observers import StepResult

    if not isinstance(record, StepResult):
        raise TypeError("expected StepResult")
    payload = asdict(record)
    # `args` carries raw post-InputGuard call arguments (user queries,
    # potential PII). It stays on StepResult for in-process observers (the
    # run-attestation observer needs it for the RFC 0003 args_hash) but must
    # never leave the process over Kafka.
    payload.pop("args", None)
    return payload


async def fan_out_step_complete(record: StepResult) -> None:
    """Publish to execution.step_complete when Kafka is enabled (never raises)."""
    if os.getenv("KAFKA_ENABLED", "false").lower() != "true":
        return
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
    if not bootstrap:
        return
    try:
        from common_kafka.producer import KafkaProducer, KafkaProducerConfig
        from common_kafka.topics import KafkaTopics

        config = KafkaProducerConfig(bootstrap_servers=bootstrap)
        producer = KafkaProducer(config)
        await producer.start()
        try:
            await producer.publish(
                KafkaTopics.EXECUTION_STEP_COMPLETE,
                payload=step_result_payload(record),
            )
        finally:
            await producer.stop()
    except Exception:
        logger.exception("fan_out_step_complete failed for call_id=%s", record.call_id)


def schedule_fan_out(record: StepResult) -> None:
    """Fire-and-forget Kafka publish (does not block execution path)."""
    asyncio.create_task(fan_out_step_complete(record))
