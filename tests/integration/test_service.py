import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db import Click, Link
from app.worker import ClickWorker

pytestmark = pytest.mark.integration

PUBLIC_URL = "https://8.8.8.8/original?source=test"


async def create_link(service, auth, **overrides):
    response = await service.client.post(
        "/api/links", headers=auth, json={"target_url": PUBLIC_URL, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_auth_validation_alias_conflict_and_ownership(service, auth):
    assert (
        await service.client.post("/api/links", json={"target_url": PUBLIC_URL})
    ).status_code == 401
    unicode_auth = await service.client.post(
        "/api/links",
        headers={b"X-API-Key": "incorrect-🔑".encode()},
        json={"target_url": PUBLIC_URL},
    )
    assert unicode_auth.status_code == 401
    for target in [
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://localhost/",
        "https://blocked.example/",
    ]:
        response = await service.client.post(
            "/api/links", headers=auth, json={"target_url": target}
        )
        assert response.status_code in (400, 422), response.text
    link = await create_link(service, auth, alias="custom-123")
    assert link["code"] == "custom-123"
    assert link["owner_id"] == "owner-one"
    assert link["short_url"].endswith("/custom-123")
    duplicate = await service.client.post(
        "/api/links", headers=auth, json={"target_url": PUBLIC_URL, "alias": "custom-123"}
    )
    assert duplicate.status_code == 409
    for endpoint, method, body in [
        ("/api/links/custom-123/stats", "GET", None),
        ("/api/links/custom-123", "PATCH", {"is_active": False}),
    ]:
        response = await service.client.request(
            method, endpoint, headers={"X-API-Key": "owner-two-key"}, json=body
        )
        assert response.status_code in (403, 404)


async def test_redirect_cache_aside_and_patch_invalidation(service, auth):
    link = await create_link(service, auth)
    code = link["code"]
    response = await service.client.get(f"/{code}")
    assert response.status_code == 302
    assert response.headers["location"] == PUBLIC_URL
    assert await service.redis.get(f"link:{code}") is not None
    assert 0 < await service.redis.ttl(f"link:{code}") <= service.settings.cache_ttl_seconds

    # A direct database change deliberately bypasses invalidation: the next
    # request must use the warm cache, proving that this path avoids the query.
    async with service.sessions() as session:
        await session.execute(
            update(Link).where(Link.id == link["id"]).values(target_url="https://1.1.1.1/uncached")
        )
        await session.commit()
    assert (await service.client.get(f"/{code}")).headers["location"] == PUBLIC_URL

    updated_url = "https://1.1.1.1/updated"
    response = await service.client.patch(
        f"/api/links/{code}", headers=auth, json={"target_url": updated_url}
    )
    assert response.status_code == 200, response.text
    assert (await service.client.get(f"/{code}")).headers["location"] == updated_url
    response = await service.client.patch(
        f"/api/links/{code}", headers=auth, json={"is_active": False}
    )
    assert response.status_code == 200
    assert (await service.client.get(f"/{code}")).status_code == 404


async def test_negative_cache_is_short_and_creation_invalidates_it(service, auth):
    code = "not-found-yet"
    assert (await service.client.get(f"/{code}")).status_code == 404
    assert await service.redis.get(f"link:{code}") is not None
    assert (
        0 < await service.redis.ttl(f"link:{code}") <= service.settings.negative_cache_ttl_seconds
    )
    await create_link(service, auth, alias=code)
    assert (await service.client.get(f"/{code}")).status_code == 302


async def test_expired_link_does_not_redirect(service, auth):
    link = await create_link(service, auth)
    async with service.sessions() as session:
        await session.execute(
            update(Link)
            .where(Link.id == link["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert (await service.client.get(f"/{link['code']}")).status_code == 404


async def test_warm_cache_still_enforces_expiration(service, auth, monkeypatch):
    now = datetime.now(UTC)
    link = await create_link(service, auth, expires_at=(now + timedelta(minutes=1)).isoformat())
    code = link["code"]
    assert (await service.client.get(f"/{code}")).status_code == 302
    assert await service.redis.get(f"link:{code}") is not None

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (now + timedelta(minutes=2)).astimezone(tz)

    monkeypatch.setattr("app.main.datetime", FutureDatetime)
    assert (await service.client.get(f"/{code}")).status_code == 404


async def test_click_queue_batch_replay_is_idempotent_and_stats_are_eventual(service, auth):
    link = await create_link(service, auth)
    worker = ClickWorker(
        service.redis, service.sessions, service.settings, consumer="integration-consumer"
    )
    await worker.ensure_group()
    response = await service.client.get(
        f"/{link['code']}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://referrer.example/",
        },
    )
    assert response.status_code == 302
    async with service.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Click)) == 0
    batches = await service.redis.xreadgroup(
        service.settings.redis_group, "integration-consumer", {"click-events": ">"}, count=10
    )
    entries = batches[0][1]
    assert len(entries) == 1
    event = json.loads(entries[0][1]["event"])
    assert len(event["ip_hash"]) == 64
    assert "ip" not in event
    assert "127.0.0.1" not in event.values()
    await worker.process_batch(entries)
    await worker.process_batch(entries)
    assert (await service.redis.xpending("click-events", service.settings.redis_group))[
        "pending"
    ] == 0
    async with service.sessions() as session:
        rows = list((await session.scalars(select(Click))).all())
    assert len(rows) == 1
    assert rows[0].link_id == link["id"]
    assert rows[0].browser == "Chrome"
    stats = await service.client.get(f"/api/links/{link['code']}/stats", headers=auth)
    assert stats.status_code == 200, stats.text
    assert stats.json()["total"] == 1
    assert stats.json()["by_browser"] == [{"browser": "Chrome", "count": 1}]
    assert stats.json()["by_referer"] == [{"referer": "https://referrer.example/", "count": 1}]
    assert sum(day["count"] for day in stats.json()["by_day"]) == 1


async def test_worker_recovers_abandoned_pending_delivery(service, auth):
    link = await create_link(service, auth)
    worker = ClickWorker(
        service.redis, service.sessions, service.settings, consumer="replacement-consumer"
    )
    await worker.ensure_group()
    assert (await service.client.get(f"/{link['code']}")).status_code == 302
    batches = await service.redis.xreadgroup(
        service.settings.redis_group, "crashed-consumer", {"click-events": ">"}, count=1
    )
    event_id = batches[0][1][0][0]
    await service.redis.xclaim(
        "click-events",
        service.settings.redis_group,
        "crashed-consumer",
        min_idle_time=0,
        message_ids=[event_id],
        idle=service.settings.worker_claim_idle_ms + 1000,
    )
    assert await worker.run_once() == 1
    assert (await service.redis.xpending("click-events", service.settings.redis_group))[
        "pending"
    ] == 0
    async with service.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Click)) == 1


async def test_worker_quarantines_invalid_events_and_acks_them(service):
    worker = ClickWorker(
        service.redis, service.sessions, service.settings, consumer="poison-consumer"
    )
    await worker.ensure_group()
    await service.redis.xadd("click-events", {"event": "not valid JSON"})
    batches = await service.redis.xreadgroup(
        service.settings.redis_group, "poison-consumer", {"click-events": ">"}, count=1
    )
    assert await worker.process_batch(batches[0][1]) == 1
    assert (await service.redis.xpending("click-events", service.settings.redis_group))[
        "pending"
    ] == 0
    assert await service.redis.xlen("click-events:dead") == 1
    async with service.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Click)) == 0


async def test_rate_limit_returns_429_after_bucket_is_exhausted(service):
    service.settings.rate_limit_capacity = 1
    service.settings.rate_limit_refill_per_second = 0.000001
    assert (await service.client.get("/does-not-exist")).status_code == 404
    assert (await service.client.get("/does-not-exist")).status_code == 429
