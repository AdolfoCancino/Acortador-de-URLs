import pytest

from app.codes import decode_base62, decode_id, encode_base62, encode_id, validate_alias


def test_generated_codes_have_no_collisions_in_one_million_ids():
    """Exercise the same permutation and representation used by POST /api/links."""
    secret = "unit-test-permutation-secret"
    codes = {encode_id(identifier, secret) for identifier in range(1, 1_000_001)}
    assert len(codes) == 1_000_000
    assert all(code.startswith("~") for code in codes)
    for identifier in range(1, 1_000_001, 9_973):
        assert decode_id(encode_id(identifier, secret), secret) == identifier


@pytest.mark.parametrize("identifier", [1, 61, 62, 3844, 2**32, 2**63 - 1])
def test_id_encoding_round_trip(identifier):
    assert decode_id(encode_id(identifier, "round-trip"), "round-trip") == identifier


@pytest.mark.parametrize("number", [0, 1, 61, 62, 62**10, 2**64 - 1])
def test_base62_round_trip(number):
    assert decode_base62(encode_base62(number)) == number


def test_secret_changes_generated_codes():
    assert encode_id(1234, "first-secret") != encode_id(1234, "second-secret")


@pytest.mark.parametrize("alias", ["abc", "My-link_123", "A" * 32])
def test_valid_custom_aliases(alias):
    assert validate_alias(alias) == alias


@pytest.mark.parametrize(
    "alias",
    [
        "a",
        "aa",
        "a" * 33,
        "white space",
        "~generated",
        "nested/path",
        "ábc",
        "api",
        "docs",
        "healthz",
    ],
)
def test_invalid_or_reserved_aliases(alias):
    with pytest.raises(ValueError):
        validate_alias(alias)
