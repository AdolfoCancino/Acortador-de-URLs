from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://shortener:shortener@localhost:5432/shortener"
    redis_url: str = "redis://localhost:6379/0"
    api_keys: dict[str, str] = Field(default_factory=lambda: {"dev-api-key": "demo"})
    code_secret: str = "development-code-secret-change-before-production"
    ip_hash_secret: str = "development-ip-secret-change-before-production"
    base_url: str = "http://localhost:8000"
    cache_ttl_seconds: int = Field(default=300, ge=0)
    negative_cache_ttl_seconds: int = Field(default=10, ge=0)
    rate_limit_capacity: int = Field(default=100, ge=1)
    rate_limit_refill_per_second: float = Field(default=10, gt=0)
    blocked_domains: list[str] = Field(default_factory=list)
    geoip_database_path: str | None = None
    redis_stream: str = "click-events"
    redis_group: str = "click-writers"
    worker_batch_size: int = Field(default=100, ge=1, le=1000)
    worker_flush_seconds: float = Field(default=1.0, gt=0)
    worker_claim_idle_ms: int = Field(default=30000, ge=1000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
