from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.security import hash_ip, sanitize_referer, validate_target_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://10.1.2.3/",
        "http://172.16.1.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://localhost/",
        "http://api.localhost/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "https://user:password@example.com/",
        "https://example.com/\r\nInjected: yes",
        "https://example.com/" + "a" * 2048,
        "http://example.com:99999/",
        "http://example.com\\@127.0.0.1/",
    ],
)
async def test_rejects_nonpublic_and_malformed_destinations(url):
    with pytest.raises(ValueError):
        await validate_target_url(url, Settings())


@pytest.mark.parametrize("host", ["blocked.example", "sub.blocked.example", "BLOCKED.EXAMPLE."])
async def test_domain_blocklist_covers_subdomains_and_case(host):
    with pytest.raises(ValueError, match="blocked"):
        await validate_target_url(f"https://{host}/", Settings(blocked_domains=["blocked.example"]))


async def test_rejects_dns_with_any_private_answer(monkeypatch):
    async def mixed_answers(host, port):
        return {"8.8.8.8", "10.0.0.2"}

    monkeypatch.setattr("app.security.resolve_addresses", mixed_answers)
    with pytest.raises(ValueError, match="non-public"):
        await validate_target_url("https://mixed.example/path", Settings())


async def test_allows_public_domain_and_preserves_path_query_fragment(monkeypatch):
    async def public_answers(host, port):
        assert host == "public.example"
        assert port == 8443
        return {"8.8.8.8", "2001:4860:4860::8888"}

    monkeypatch.setattr("app.security.resolve_addresses", public_answers)
    url = "https://public.example:8443/path?q=value#fragment"
    assert await validate_target_url(url, Settings()) == url


async def test_dns_failure_is_validation_error(monkeypatch):
    async def failed_lookup(host, port):
        raise OSError("name resolution failed")

    monkeypatch.setattr("app.security.resolve_addresses", failed_lookup)
    with pytest.raises(ValueError, match="DNS"):
        await validate_target_url("https://unresolvable.example/", Settings())


def test_ip_hash_is_daily_keyed_and_does_not_contain_the_ip():
    day = datetime(2026, 1, 3, 12, tzinfo=UTC)
    value = hash_ip("203.0.113.1", "private-secret", day)
    assert len(value) == 64
    assert "203.0.113.1" not in value
    assert value == hash_ip("203.0.113.1", "private-secret", day + timedelta(hours=2))
    assert value != hash_ip("203.0.113.1", "private-secret", day + timedelta(days=1))
    assert value != hash_ip("203.0.113.2", "private-secret", day)
    assert value != hash_ip("203.0.113.1", "different-secret", day)


def test_referer_removes_credentials_query_and_fragment():
    assert (
        sanitize_referer("https://username:password@site.example/path?token=secret#fragment")
        == "https://site.example/path"
    )
    assert sanitize_referer("javascript:alert(1)") is None
    assert sanitize_referer(None) is None
