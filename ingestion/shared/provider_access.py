"""Provider-neutral image-access contracts used by source processing."""

from __future__ import annotations

from typing import Protocol


class ProviderImageAccessError(RuntimeError):
    """A controlled provider freshness or image-access failure."""

    def __init__(self, message: str, *, throttled: bool = False) -> None:
        super().__init__(message)
        self.throttled = throttled


class ProviderImageReference(Protocol):
    """Minimum reference shape; provider-specific metadata is optional."""

    image_url: str


class ProviderImageClient(Protocol):
    def get_current_image(
        self,
        provider_id: str,
        selected_rendition: str,
        source_metadata: dict[str, object] | None = None,
    ) -> ProviderImageReference: ...

    def download(self, image_url: str) -> bytes: ...
