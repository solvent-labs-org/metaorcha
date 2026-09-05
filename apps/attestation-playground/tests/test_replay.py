"""SSE replay: late subscribers (e.g. mid-run browser refresh) get the full
buffered event history, then live events — exactly once, in order."""

import asyncio
import json

import httpx
from playground.main import app
from playground.state import run_store


def test_subscribe_returns_history_then_live_only():
    run_store.create("replay-1", "sim")
    history, q = run_store.subscribe("replay-1")
    assert history == []
    run_store.emit("replay-1", {"type": "step", "step_index": 0})
    run_store.emit("replay-1", {"type": "envelope_ready", "run_id": "replay-1"})
    assert q.get_nowait()["type"] == "step"
    assert q.get_nowait()["type"] == "envelope_ready"
    # a later subscriber sees both as history
    later_history, _ = run_store.subscribe("replay-1")
    assert [e["type"] for e in later_history] == ["step", "envelope_ready"]


def test_late_subscriber_history_excludes_nothing_and_duplicates_nothing():
    run_store.create("replay-2", "sim")
    run_store.emit("replay-2", {"type": "step", "step_index": 0})
    history, q = run_store.subscribe("replay-2")
    assert [e["step_index"] for e in history] == [0]
    # events after subscribe arrive live, without replaying the buffered one
    run_store.emit("replay-2", {"type": "step", "step_index": 1})
    assert q.get_nowait()["step_index"] == 1
    assert q.empty()


def test_sse_endpoint_replays_full_run_for_late_subscriber():
    asyncio.run(_run_to_completion_and_collect("replay-e2e-1"))


def test_sse_endpoint_replays_for_second_subscriber_after_first_disconnects():
    """The real refresh bug: first viewer drained the feed, then refreshed.
    The re-attached subscriber must still get the full run, exactly once."""
    events = asyncio.run(
        _run_to_completion_and_collect("replay-e2e-2", first_subscriber=True)
    )
    types = [e["type"] for e in events]
    assert types.count("step") == 3
    assert types.count("envelope_ready") == 1
    assert types[-1] == "envelope_ready"


async def _run_to_completion_and_collect(
    run_label: str, first_subscriber: bool = False
):
    app.state.step_delay = 0.0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/runs", json={"prompt": run_label, "mode": "sim"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        if first_subscriber:
            # original viewer: drains the whole feed, then "closes the tab"
            async with asyncio.timeout(5):
                async with client.stream("GET", f"/runs/{run_id}/events") as stream:
                    assert stream.status_code == 200
                    async for line in stream.aiter_lines():
                        if '"envelope_ready"' in line or '"run_failed"' in line:
                            break

        for _ in range(200):
            r = await client.get(f"/runs/{run_id}/envelope")
            if r.status_code == 200:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("run never completed")

        events = []
        async with asyncio.timeout(5):
            async with client.stream("GET", f"/runs/{run_id}/events") as stream:
                assert stream.status_code == 200
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[len("data: ") :]))
        return events
