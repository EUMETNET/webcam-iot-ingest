import pytest

from discovery.shared.identifiers import (
    IdentifierEstablishmentError,
    compact_identifier,
    validate_internal_identifier,
)


def test_identifier_is_alphanumeric_deterministic_and_bounded() -> None:
    provider_id = "provider identifier with punctuation and a very long suffix"

    first = compact_identifier("win", provider_id, set())
    second = compact_identifier("win", provider_id, set())

    assert first == second
    assert first.isalnum()
    assert len(first) <= 16


def test_collision_suffix_remains_inside_limit_and_is_deterministic() -> None:
    first = compact_identifier("ska", "a-b", set())
    collision = compact_identifier("ska", "ab", {first})

    assert collision == compact_identifier("ska", "ab", {first})
    assert collision != first
    assert collision.isalnum()
    assert len(collision) <= 16


@pytest.mark.parametrize("identifier", ["", "has-dash", "x" * 17])
def test_existing_identifier_must_follow_contract(identifier: str) -> None:
    with pytest.raises(IdentifierEstablishmentError):
        validate_internal_identifier(identifier)
