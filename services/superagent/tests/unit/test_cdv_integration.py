"""Unit tests for CDV verification integration (flag-gated)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from superagent.middleware.observers import StepResult
from superagent.verification.cdv_integration import (
    CDVObserver,
    get_stopper,
    stopper_should_stop,
)


def _record(content: str = "Useful agent output about the goal") -> StepResult:
    return StepResult(
        call_id="c1",
        agent_id="did:orcha:agent:a",
        capability_id="search",
        protocol="MCP",
        tool_name="a__search",
        success=True,
        content=content,
        session_id="sess-1",
        metadata={},
    )


class TestCDVObserver:
    @pytest.mark.asyncio
    async def test_step_enriched_with_cdv_score(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        observer = CDVObserver()
        record = _record()
        await observer.on_step_complete(record)
        assert "cdv" in record.metadata
        assert 0.0 <= record.metadata["cdv"]["score"] <= 1.0
        assert record.metadata["cdv"]["source"] == "channel_a_only"

    @pytest.mark.asyncio
    async def test_per_run_sqlite_store_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        observer = CDVObserver()
        await observer.on_step_complete(_record())
        assert (tmp_path / "sess-1.db").exists()

    @pytest.mark.asyncio
    async def test_a_platform_tool_step_is_not_scored(self, tmp_path, monkeypatch):
        # story 3.1: a system tool's step reaches the seam; it is nobody's
        # answer to a goal, so no score and no prior under its DID
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        observer = CDVObserver()
        record = StepResult(
            call_id="c-clock",
            agent_id="did:orcha:system:tools",
            capability_id="get_datetime",
            protocol="SYSTEM",
            tool_name="get_datetime",
            success=True,
            content="2026-10-05T07:00:00+00:00",
            session_id="sess-1",
            metadata={"goal": ""},
        )
        await observer.on_step_complete(record)
        assert "cdv" not in record.metadata
        assert not (tmp_path / "sess-1.db").exists()

    @pytest.mark.asyncio
    async def test_failed_step_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        observer = CDVObserver()
        record = _record()
        record = StepResult(
            **{**record.__dict__, "success": False, "content": "Error: boom"}
        )
        await observer.on_step_complete(record)
        assert "cdv" not in record.metadata

    @pytest.mark.asyncio
    async def test_goal_from_metadata_passed_to_scorer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        captured: dict = {}

        def fake_score_channel_a(content, goal, evaluator):
            captured["goal"] = goal
            return MagicMock(score=0.5)

        import cdv.step_scorer

        monkeypatch.setattr(cdv.step_scorer, "score_channel_a", fake_score_channel_a)
        observer = CDVObserver()
        record = StepResult(
            **{**_record().__dict__, "metadata": {"goal": "specific goal"}}
        )
        await observer.on_step_complete(record)
        assert captured["goal"] == "specific goal"


class TestStopperGuard:
    def test_stopper_cached_per_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        s1 = get_stopper("sess-a", "goal")
        s2 = get_stopper("sess-a", "goal")
        s3 = get_stopper("sess-b", "goal")
        assert s1 is s2
        assert s1 is not s3

    def test_stopper_should_stop_false_on_fresh_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        assert stopper_should_stop({"session_id": "fresh", "messages": []}) is False

    def test_stopper_should_stop_true_when_stopper_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "superagent.verification.cdv_integration.settings",
            MagicMock(cdv_store_dir=str(tmp_path)),
        )
        stopper = get_stopper("trip", "goal")
        monkeypatch.setattr(stopper, "should_continue", lambda state: False)
        assert stopper_should_stop({"session_id": "trip", "messages": []}) is True
