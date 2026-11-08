"""A bijection over unsigned 64-bit values, encoded in base62.

This hides adjacent sequence numbers; it is not cryptographic encryption.
The ~ prefix makes generated codes disjoint from the custom alias namespace.
"""

import hashlib
import re

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
MASK = (1 << 64) - 1
RESERVED = {"api", "docs", "redoc", "openapi.json", "health", "healthz", "readyz"}
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def encode_base62(value: int) -> str:
    if value < 0:
        raise ValueError("value must be nonnegative")
    if value == 0:
        return "0"
    result = []
    while value:
        value, remainder = divmod(value, 62)
        result.append(ALPHABET[remainder])
    return "".join(reversed(result))


def decode_base62(value: str) -> int:
    if not value:
        raise ValueError("empty base62 code")
    result = 0
    for char in value:
        try:
            digit = ALPHABET.index(char)
        except ValueError:
            raise ValueError("invalid base62 code") from None
        result = result * 62 + digit
    return result


def _key(secret: str) -> tuple[int, int]:
    digest = hashlib.sha256(secret.encode()).digest()
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big") | 1


def encode_id(value: int, secret: str) -> str:
    if not 1 <= value <= (1 << 63) - 1:
        raise ValueError("id outside BIGSERIAL positive range")
    xor, multiplier = _key(secret)
    return "~" + encode_base62(((value ^ xor) * multiplier) & MASK)


def decode_id(code: str, secret: str) -> int:
    if not code.startswith("~"):
        raise ValueError("not a generated code")
    value = decode_base62(code[1:])
    if value > MASK:
        raise ValueError("code outside 64-bit range")
    xor, multiplier = _key(secret)
    result = ((value * pow(multiplier, -1, 1 << 64)) & MASK) ^ xor
    if not 1 <= result <= (1 << 63) - 1:
        raise ValueError("decoded id outside BIGSERIAL positive range")
    return result


def validate_alias(alias: str) -> str:
    if not ALIAS_PATTERN.fullmatch(alias) or alias.lower() in RESERVED:
        raise ValueError("alias must contain 3-32 letters, digits, _ or - and not be reserved")
    return alias
