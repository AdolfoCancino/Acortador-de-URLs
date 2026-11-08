import os
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from redis.asyncio import Redis
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.main import create_app


@pytest.fixture(scope="session")
def service_urls():
    """Default to disposable real containers; CI may supply equivalent services."""
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if database_url and redis_url:
        yield database_url, redis_url
        return
    if database_url or redis_url:
        raise RuntimeError("Supply both TEST_DATABASE_URL and TEST_REDIS_URL, or neither")
    with (
        PostgresContainer("postgres:16") as postgres,
        RedisContainer("redis:7-alpine") as redis,
    ):
        url = postgres.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        yield url, f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


@dataclass
class Service:
    client: httpx.AsyncClient
    redis: Redis
    settings: Settings
    sessions: object


@pytest_asyncio.fixture
async def service(service_urls):
    database_url, redis_url = service_urls
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        api_keys={"owner-one-key": "owner-one", "owner-two-key": "owner-two"},
        code_secret="test-code-secret-32-characters-long",
        ip_hash_secret="test-ip-secret-32-characters-long",
        base_url="http://short.test",
        rate_limit_capacity=1000,
        rate_limit_refill_per_second=100,
        blocked_domains=["blocked.example"],
    )
    engine = make_engine(database_url)
    redis = Redis.from_url(redis_url, decode_responses=True)
    async with LifespanManager(create_app(settings)) as manager:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE clicks, links RESTART IDENTITY CASCADE"))
        await redis.flushdb()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://short.test",
            follow_redirects=False,
        ) as client:
            yield Service(client, redis, settings, make_session_factory(engine))
    await redis.aclose()
    await engine.dispose()


@pytest.fixture
def auth():
    return {"X-API-Key": "owner-one-key"}
