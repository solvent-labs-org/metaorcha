import asyncio

from playground.events import StepEventRouter


def test_router_routes_by_session_id():
    router = StepEventRouter("localhost:9092")
    queue: asyncio.Queue = asyncio.Queue()
    router.register("sess-1", queue)
    asyncio.run(
        router.handle_message(
            {
                "session_id": "sess-1",
                "tool_name": "search_docs",
                "success": True,
                "latency_ms": 5,
                "call_id": "call-1",
            }
        )
    )
    assert queue.get_nowait() == {
        "type": "live_step",
        "tool": "search_docs",
        "success": True,
        "latency_ms": 5,
        "call_id": "call-1",
    }


def test_router_drops_unknown_session():
    router = StepEventRouter("localhost:9092")
    queue: asyncio.Queue = asyncio.Queue()
    router.register("sess-1", queue)
    asyncio.run(router.handle_message({"session_id": "other", "tool_name": "x"}))
    assert queue.empty()
