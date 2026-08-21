"""Stable alphanumeric internal identifiers shared by discovery providers."""

from __future__ import annotations

from collections.abc import Collection
import hashlib
import re
from typing import TypeVar


_ALPHANUMERIC = re.compile(r"^[A-Za-z0-9]+$")
_ERROR = TypeVar("_ERROR", bound=Exception)


class IdentifierEstablishmentError(Exception):
    """Marker for any failure to establish a valid internal identifier."""


def establish_identifier(
    prefix: str,
    provider_id: str,
    used: Collection[str],
    *,
    error_type: type[_ERROR] = IdentifierEstablishmentError,
) -> str:
    """Return a deterministic, untruncated alphanumeric identifier."""
    if not prefix or not _ALPHANUMERIC.fullmatch(prefix):
        raise error_type("internal identifier prefix must be alphanumeric")

    sanitized = re.sub(r"[^A-Za-z0-9]", "", provider_id)
    if not sanitized:
        sanitized = hashlib.sha256(provider_id.encode()).hexdigest()
    readable = f"{prefix}{sanitized}"
    if readable not in used:
        return readable

    # Preserve a readable prefix while adding a deterministic collision suffix.
    # Further attempts matter only in the exceptionally unlikely event of a hash
    # collision with an already allocated internal identifier.
    for attempt in range(100):
        digest_input = provider_id if attempt == 0 else f"{provider_id}:{attempt}"
        suffix = hashlib.sha256(digest_input.encode()).hexdigest()[:10]
        candidate = f"{readable}{suffix}"
        if candidate not in used:
            return candidate
    raise error_type(f"cannot assign a unique identifier for {provider_id}")


def validate_internal_identifier(
    identifier: str,
    *,
    error_type: type[_ERROR] = IdentifierEstablishmentError,
) -> str:
    """Validate an existing or provider-specialized internal identifier."""
    if not identifier or _ALPHANUMERIC.fullmatch(identifier) is None:
        raise error_type(
            f"internal identifier must be non-empty and alphanumeric: {identifier!r}"
        )
    return identifier
