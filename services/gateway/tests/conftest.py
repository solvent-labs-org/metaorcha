"""Shared test fixtures for Gateway tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

# Add the project root to sys.path so `common.*` namespace packages are importable
# when running pytest from within services/gateway/
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set required env vars before importing settings
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")


class FakeJson:
    """Stands in for prisma's ``Json`` column wrapper (``.data`` holds the value)."""

    def __init__(self, data) -> None:
        self.data = data


@pytest.fixture(autouse=True)
def prisma_json_stub(monkeypatch):
    """CI has no generated Prisma client; routes import ``Json`` at call time.

    Every Gateway test sees this stub instead, so a run reads the same with or
    without the generated client on disk.
    """
    fields = ModuleType("common.database.src.generated_client.fields")
    fields.Json = FakeJson
    monkeypatch.setitem(
        sys.modules, "common.database.src.generated_client.fields", fields
    )


def _make_db_mock() -> AsyncMock:
    """Build an AsyncMock that mimics the Prisma client."""
    db = AsyncMock()
    db.user = AsyncMock()
    db.refreshtoken = AsyncMock()
    db.workflowtemplate = AsyncMock()
    return db


def _make_redis_mock() -> AsyncMock:
    redis = AsyncMock()
    redis.sismember = AsyncMock(return_value=False)
    redis.sadd = AsyncMock()
    redis.expire = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock()
    redis.ping = AsyncMock()
    return redis


@pytest.fixture
def mock_db() -> AsyncMock:
    return _make_db_mock()


@pytest.fixture
def mock_redis() -> AsyncMock:
    return _make_redis_mock()


@pytest.fixture
def mock_superagent() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_registry() -> AsyncMock:
    return AsyncMock()


@pytest_asyncio.fixture
async def client(mock_db, mock_redis, mock_superagent, mock_registry):
    """ASGI test client with mocked app state."""
    from gateway.main import app

    app.state.db = mock_db
    app.state.redis = mock_redis
    app.state.superagent = mock_superagent
    app.state.registry = mock_registry

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
