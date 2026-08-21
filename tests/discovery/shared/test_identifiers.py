import pytest

from discovery.shared.identifiers import (
    IdentifierEstablishmentError,
    establish_identifier,
    validate_internal_identifier,
)


def test_identifier_is_alphanumeric_deterministic_and_untruncated() -> None:
    provider_id = "provider identifier with punctuation and a very long suffix"

    first = establish_identifier("win", provider_id, set())
    second = establish_identifier("win", provider_id, set())

    assert first == second
    assert first.isalnum()
    assert first == "winprovideridentifierwithpunctuationandaverylongsuffix"
    assert len(first) > 16


def test_collision_suffix_is_appended_without_truncation_and_is_deterministic() -> None:
    first = establish_identifier("ska", "a-b", set())
    collision = establish_identifier("ska", "ab", {first})

    assert collision == establish_identifier("ska", "ab", {first})
    assert collision != first
    assert collision.isalnum()
    assert collision.startswith("skaab")
    assert len(collision) == len(first) + 10


@pytest.mark.parametrize("identifier", ["", "has-dash"])
def test_existing_identifier_must_follow_contract(identifier: str) -> None:
    with pytest.raises(IdentifierEstablishmentError):
        validate_internal_identifier(identifier)


def test_existing_long_alphanumeric_identifier_is_valid() -> None:
    identifier = "x" * 100
    assert validate_internal_identifier(identifier) == identifier
