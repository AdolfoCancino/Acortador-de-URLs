from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    target_url: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Click(Base):
    __tablename__ = "clicks"
    __table_args__ = (Index("ix_clicks_link_id_ts", "link_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    device: Mapped[str | None] = mapped_column(String(128))
    browser: Mapped[str | None] = mapped_column(String(128))
    referer: Mapped[str | None] = mapped_column(String(1024))
    ip_hash: Mapped[str] = mapped_column(String(64))


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        # Serialize startup DDL when API and worker start simultaneously.
        from sqlalchemy import text

        await connection.execute(text("SELECT pg_advisory_xact_lock(734892374)"))
        await connection.run_sync(Base.metadata.create_all)
