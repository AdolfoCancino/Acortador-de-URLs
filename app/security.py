import asyncio
import hashlib
import hmac
import ipaddress
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings

MAX_URL_LENGTH = 2048


def hash_ip(ip: str, secret: str, now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    salt = hmac.new(secret.encode(), day.encode(), hashlib.sha256).digest()
    return hashlib.sha256(ip.encode() + salt).hexdigest()


async def resolve_addresses(host: str, port: int) -> set[str]:
    results = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=3
    )
    return {result[4][0] for result in results}


async def validate_target_url(value: str, settings: Settings) -> str:
    if len(value) > MAX_URL_LENGTH or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("URL too long or contains whitespace/control characters")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        raise ValueError("invalid URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username is not None:
        raise ValueError("only absolute http/https URLs without credentials are allowed")
    if "\\" in value or "%" in host:
        raise ValueError("invalid hostname")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid hostname") from None
    if ascii_host == "localhost" or ascii_host.endswith(".localhost"):
        raise ValueError("local destinations are blocked")
    if any(
        ascii_host == domain.lower().rstrip(".")
        or ascii_host.endswith("." + domain.lower().rstrip("."))
        for domain in settings.blocked_domains
    ):
        raise ValueError("destination domain is blocked")
    try:
        addresses = {str(ipaddress.ip_address(ascii_host))}
    except ValueError:
        try:
            addresses = await resolve_addresses(ascii_host, port)
        except (OSError, TimeoutError):
            raise ValueError("destination DNS could not be resolved") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("private, loopback and other non-public destinations are blocked")
    authority = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if parsed.port is not None:
        authority += f":{parsed.port}"
    return urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, parsed.fragment)
    )


def sanitize_referer(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", ""))[:1024]
    except ValueError:
        return None
