import json
import logging
import math
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import geoip2.database
import geoip2.errors
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.codes import encode_id, validate_alias
from app.config import Settings, get_settings
from app.db import Click, Link, init_db, make_engine, make_session_factory
from app.security import hash_ip, sanitize_referer, validate_target_url

logger = logging.getLogger(__name__)
NEGATIVE = "!"
TOKEN_BUCKET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local capacity, refill = tonumber(ARGV[1]), tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = math.min(capacity, tonumber(state[1] or capacity) +
    math.max(0, now - tonumber(state[2] or now)) * refill)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill * 1000))
return {allowed, math.ceil(math.max(0, 1 - tokens) / refill)}
"""
CACHE_SET = """
if (redis.call('GET', KEYS[2]) or '0') == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1
end
return 0
"""
INVALIDATE = """
redis.call('INCR', KEYS[2]); redis.call('DEL', KEYS[1]); return 1
"""


class LinkCreate(BaseModel):
    target_url: str = Field(min_length=1, max_length=2048)
    alias: str | None = None
    expires_at: datetime | None = None

    @field_validator("alias")
    @classmethod
    def valid_alias(cls, value: str | None) -> str | None:
        return validate_alias(value) if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value <= datetime.now(UTC)):
            raise ValueError("expires_at must be a future timezone-aware timestamp")
        return value


class LinkUpdate(BaseModel):
    target_url: str | None = Field(default=None, min_length=1, max_length=2048)
    expires_at: datetime | None = None
    is_active: bool | None = None

    @field_validator("target_url", "is_active")
    @classmethod
    def cannot_clear(cls, value):
        if value is None:
            raise ValueError("this field cannot be null")
        return value

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: datetime | None) -> datetime | None:
        return LinkCreate.valid_expiry(value)


class LinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    target_url: str
    owner_id: str
    created_at: datetime
    expires_at: datetime | None
    is_active: bool
    short_url: str


def _response(link: Link, settings: Settings) -> dict:
    return {
        "id": link.id,
        "code": link.code,
        "target_url": link.target_url,
        "owner_id": link.owner_id,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "is_active": link.is_active,
        "short_url": settings.base_url.rstrip("/") + "/" + link.code,
    }


async def _rate_limit(request: Request, scope: str, fail_open: bool = False) -> None:
    settings = request.app.state.settings
    ip = request.client.host if request.client else "unknown"
    key = f"rate:{scope}:{hash_ip(ip, settings.ip_hash_secret)}"
    try:
        allowed, retry = await request.app.state.redis.eval(
            TOKEN_BUCKET,
            1,
            key,
            settings.rate_limit_capacity,
            settings.rate_limit_refill_per_second,
        )
    except RedisError:
        if fail_open:
            logger.warning("Redis unavailable: redirect rate limiter bypassed")
            return
        raise HTTPException(503, "rate limiter unavailable") from None
    if not allowed:
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(max(1, retry))})


async def _owner(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
    if x_api_key:
        for key, owner in request.app.state.settings.api_keys.items():
            if secrets.compare_digest(x_api_key.encode(), key.encode()):
                await _rate_limit(request, "api")
                return owner
    raise HTTPException(401, "valid X-API-Key required")


async def _invalidate(request: Request, code: str) -> None:
    try:
        await request.app.state.redis.eval(INVALIDATE, 2, f"link:{code}", f"link-version:{code}")
    except RedisError:
        # Do not silently acknowledge mutations while stale cache entries survive.
        raise HTTPException(
            503, "database updated, but cache invalidation failed; retry update"
        ) from None


async def _resolve(request: Request, code: str) -> dict | None:
    settings = request.app.state.settings
    redis = request.app.state.redis
    version = "0"
    cache_enabled = settings.cache_ttl_seconds > 0
    if cache_enabled:
        try:
            cached, version = await redis.mget(f"link:{code}", f"link-version:{code}")
            version = version or "0"
            if cached == NEGATIVE:
                return None
            if cached:
                data = json.loads(cached)
                if data["expires_at"] is None or data["expires_at"] > datetime.now(UTC).timestamp():
                    return data
                return None
        except (RedisError, ValueError, KeyError, TypeError):
            logger.warning("Cache unavailable or malformed; resolving from PostgreSQL")
    async with request.app.state.sessions() as session:
        link = await session.scalar(select(Link).where(Link.code == code))
        now = datetime.now(UTC)
        data = None
        if link and link.is_active and (link.expires_at is None or link.expires_at > now):
            data = {
                "id": link.id,
                "target_url": link.target_url,
                "expires_at": link.expires_at.timestamp() if link.expires_at else None,
            }
    ttl = settings.cache_ttl_seconds if data else settings.negative_cache_ttl_seconds
    if data and data["expires_at"]:
        ttl = min(ttl, max(0, math.ceil(data["expires_at"] - datetime.now(UTC).timestamp())))
    if cache_enabled and ttl > 0:
        try:
            await redis.eval(
                CACHE_SET,
                2,
                f"link:{code}",
                f"link-version:{code}",
                version,
                json.dumps(data) if data else NEGATIVE,
                ttl,
            )
        except RedisError:
            logger.warning("Could not populate redirect cache")
    return data


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine = make_engine(settings.database_url)
        application.state.settings = settings
        application.state.engine = engine
        application.state.sessions = make_session_factory(engine)
        application.state.redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=0.5
        )
        application.state.geoip = None
        if settings.geoip_database_path:
            application.state.geoip = geoip2.database.Reader(settings.geoip_database_path)
        try:
            await init_db(engine)
            await application.state.redis.ping()
            yield
        finally:
            if application.state.geoip:
                application.state.geoip.close()
            await application.state.redis.aclose()
            await engine.dispose()

    application = FastAPI(title="URL Shortener", lifespan=lifespan)

    @application.get("/healthz")
    async def health():
        return {"status": "ok"}

    @application.get("/readyz")
    async def ready(request: Request):
        try:
            async with request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await request.app.state.redis.ping()
        except Exception:
            raise HTTPException(503, "dependencies unavailable") from None
        return {"status": "ready"}

    @application.post("/api/links", status_code=201, response_model=LinkResponse)
    async def create_link(body: LinkCreate, request: Request, owner: str = Depends(_owner)):
        try:
            target = await validate_target_url(body.target_url, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        async with request.app.state.sessions() as session:
            link_id = await session.scalar(
                text("SELECT nextval(pg_get_serial_sequence('links', 'id'))")
            )
            code = body.alias or encode_id(link_id, settings.code_secret)
            link = Link(
                id=link_id,
                code=code,
                target_url=target,
                owner_id=owner,
                expires_at=body.expires_at,
                is_active=True,
            )
            session.add(link)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise HTTPException(409, "alias already exists") from None
            await session.refresh(link)
        await _invalidate(request, code)
        return _response(link, settings)

    @application.patch("/api/links/{code}", response_model=LinkResponse)
    async def update_link(
        code: str, body: LinkUpdate, request: Request, owner: str = Depends(_owner)
    ):
        fields = body.model_dump(exclude_unset=True)
        if "target_url" in fields:
            try:
                fields["target_url"] = await validate_target_url(fields["target_url"], settings)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        async with request.app.state.sessions() as session:
            link = await session.scalar(
                select(Link).where(Link.code == code, Link.owner_id == owner)
            )
            if link is None:
                raise HTTPException(404, "link not found")
            for field, value in fields.items():
                setattr(link, field, value)
            await session.commit()
            await session.refresh(link)
        await _invalidate(request, code)
        return _response(link, settings)

    @application.get("/api/links/{code}/stats")
    async def stats(code: str, request: Request, owner: str = Depends(_owner)):
        async with request.app.state.sessions() as session:
            link = await session.scalar(
                select(Link).where(Link.code == code, Link.owner_id == owner)
            )
            if link is None:
                raise HTTPException(404, "link not found")
            total = await session.scalar(
                select(func.count()).select_from(Click).where(Click.link_id == link.id)
            )
            result = {"code": code, "total": total}
            for name, expression, label in [
                ("by_day", func.date_trunc("day", func.timezone("UTC", Click.ts)), "day"),
                ("by_country", Click.country, "country"),
                ("by_browser", Click.browser, "browser"),
                ("by_device", Click.device, "device"),
                ("by_referer", Click.referer, "referer"),
            ]:
                rows = await session.execute(
                    select(expression, func.count())
                    .where(Click.link_id == link.id)
                    .group_by(expression)
                    .order_by(expression)
                )
                result[name] = [{label: value, "count": count} for value, count in rows]
        return result

    @application.get("/{code}", status_code=302)
    async def redirect(code: str, request: Request):
        if len(code) > 32:
            raise HTTPException(404, "link not found")
        await _rate_limit(request, "redirect", fail_open=True)
        data = await _resolve(request, code)
        if not data:
            raise HTTPException(404, "link not found")
        ip = request.client.host if request.client else "unknown"
        country = None
        if request.app.state.geoip:
            try:
                country = request.app.state.geoip.country(ip).country.iso_code
            except (ValueError, geoip2.errors.AddressNotFoundError):
                pass
        event = {
            "event_id": str(uuid.uuid4()),
            "link_id": data["id"],
            "ts": datetime.now(UTC).isoformat(),
            "country": country,
            "user_agent": request.headers.get("user-agent", "")[:512],
            "referer": sanitize_referer(request.headers.get("referer")),
            "ip_hash": hash_ip(ip, settings.ip_hash_secret),
        }
        try:
            await request.app.state.redis.xadd(settings.redis_stream, {"event": json.dumps(event)})
        except RedisError:
            # Availability wins over analytics during a Redis outage. No raw IP is logged.
            logger.error("Click event could not be queued; redirect will still be served")
        return RedirectResponse(
            data["target_url"], status_code=302, headers={"Cache-Control": "no-store"}
        )

    return application


app = create_app()
