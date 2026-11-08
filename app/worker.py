"""Consume click events without adding PostgreSQL writes to redirects.

Redis consumer-group delivery is at least once.  The UUID carried by each event
is unique in PostgreSQL, so a crash between COMMIT and XACK is safe to replay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import socket
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from ua_parser import user_agent_parser

from app.config import get_settings
from app.db import Click, init_db, make_engine, make_session_factory

logger = logging.getLogger(__name__)
Message = tuple[str, Mapping[str, str]]
HEALTH_FILE = Path(os.getenv("WORKER_HEALTH_FILE", "/tmp/shortener-worker-heartbeat"))


def _write_heartbeat() -> None:
    temporary = HEALTH_FILE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    temporary.replace(HEALTH_FILE)


def event_to_row(payload: str) -> dict[str, Any]:
    """Validate a wire event and parse the user agent outside the HTTP path."""
    event = json.loads(payload)
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    raw_event_id = event["event_id"]
    if not isinstance(raw_event_id, str):
        raise ValueError("event_id must be a UUID string")
    event_id = str(UUID(raw_event_id))
    link_id = event["link_id"]
    if isinstance(link_id, bool) or not isinstance(link_id, int) or not 1 <= link_id < 2**63:
        raise ValueError("link_id must be a positive PostgreSQL bigint")
    timestamp = datetime.fromisoformat(event["ts"])
    if timestamp.tzinfo is None:
        raise ValueError("ts must include its timezone")
    ip_hash = event["ip_hash"]
    if not isinstance(ip_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", ip_hash):
        raise ValueError("ip_hash must be a SHA-256 hex digest")
    country = event.get("country")
    if country is not None:
        if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
            raise ValueError("country must be an ISO alpha-2 code")
        country = country.upper()
    user_agent = event.get("user_agent")
    if user_agent is None:
        user_agent = ""
    referer = event.get("referer")
    if not isinstance(user_agent, str) or (referer is not None and not isinstance(referer, str)):
        raise ValueError("user_agent and referer must be strings")
    parsed = user_agent_parser.Parse(user_agent[:2048])
    return {
        "event_id": event_id,
        "link_id": link_id,
        "ts": timestamp.astimezone(UTC),
        "country": country,
        "device": (parsed["device"]["family"] or "Other")[:100],
        "browser": (parsed["user_agent"]["family"] or "Other")[:100],
        "referer": referer[:1024] if referer else None,
        "ip_hash": ip_hash,
    }


class ClickWorker:
    def __init__(self, redis_client, session_factory, settings, consumer: str | None = None):
        self.redis = redis_client
        self.session_factory = session_factory
        self.settings = settings
        self.stream = settings.redis_stream
        self.group = settings.redis_group
        self.consumer = consumer or f"{socket.gethostname()}-{uuid4().hex[:12]}"
        self.stop = asyncio.Event()
        self._claim_cursor = "0-0"
        self._inflight: list[Message] = []

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def process_batch(self, messages: Sequence[Message]) -> int:
        """Persist valid rows, preserve poison events, and only then acknowledge.

        On any PostgreSQL or Redis failure, the caller retains the batch and the
        pending entries remain available for retry/reclaim after process death.
        """
        if not messages:
            return 0
        rows = []
        malformed = []
        for stream_id, fields in messages:
            try:
                rows.append(event_to_row(fields["event"]))
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                malformed.append((stream_id, fields, type(exc).__name__))

        if rows:
            async with self.session_factory() as session, session.begin():
                statement = (
                    insert(Click)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=[Click.event_id])
                )
                await session.execute(statement)

        # A malformed event must not block unrelated metrics.  Preserve it for
        # inspection; do not log event data, user agents, hashes or URLs.
        for stream_id, fields, reason in malformed:
            await self.redis.xadd(
                f"{self.stream}:dead",
                {
                    "original_id": stream_id,
                    "event": fields.get("event", ""),
                    "reason": reason,
                },
            )
            logger.warning("Malformed click event moved to dead-letter stream: %s", stream_id)
        await self.redis.xack(self.stream, self.group, *(stream_id for stream_id, _ in messages))
        return len(messages)

    async def run_once(self) -> int:
        """Collect at most one bounded batch, including abandoned pending work."""
        if not self._inflight:
            result = await self.redis.xautoclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=self.settings.worker_claim_idle_ms,
                start_id=self._claim_cursor,
                count=self.settings.worker_batch_size,
            )
            self._claim_cursor = result[0]
            self._inflight.extend(result[1])
        deadline = time.monotonic() + self.settings.worker_flush_seconds
        while len(self._inflight) < self.settings.worker_batch_size and not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            batches = await self.redis.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=self.settings.worker_batch_size - len(self._inflight),
                block=max(1, min(1000, int(remaining * 1000))),
            )
            for _, messages in batches:
                self._inflight.extend(messages)
        result = await asyncio.wait_for(self.process_batch(self._inflight), timeout=15)
        self._inflight.clear()
        return result

    async def heartbeat(self) -> None:
        # An idle worker must still detect a database outage.
        await self.redis.ping()
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1"))
        await asyncio.to_thread(_write_heartbeat)

    async def run(self) -> None:
        group_ready = False
        next_health = 0.0
        while not self.stop.is_set():
            try:
                if not group_ready:
                    await self.ensure_group()
                    group_ready = True
                await self.run_once()
                if time.monotonic() >= next_health:
                    await asyncio.wait_for(self.heartbeat(), timeout=5)
                    next_health = time.monotonic() + 5
            except Exception as exc:
                if isinstance(exc, ResponseError) and "NOGROUP" in str(exc):
                    group_ready = False
                logger.exception("Click batch failed; pending events will be retried")
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1)
                except TimeoutError:
                    pass
        if self._inflight:
            try:
                await asyncio.wait_for(self.process_batch(self._inflight), timeout=10)
                self._inflight.clear()
            except Exception:
                logger.exception("Final flush failed; pending events remain recoverable")


def healthcheck() -> int:
    try:
        health = json.loads(HEALTH_FILE.read_text())
        os.kill(int(health["pid"]), 0)
        age = time.time() - float(health["ts"])
        return 0 if 0 <= age <= 30 else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 1


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    worker = ClickWorker(client, make_session_factory(engine), settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop.set)
    try:
        await init_db(engine)
        await worker.run()
    finally:
        await client.aclose()
        await engine.dispose()
        await asyncio.to_thread(HEALTH_FILE.unlink, missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(healthcheck())
    asyncio.run(main())
