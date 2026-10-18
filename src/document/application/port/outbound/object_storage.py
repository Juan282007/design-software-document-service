"""Outbound port for the S3-compatible object storage that holds the document binaries."""

from __future__ import annotations

from typing import Protocol


class ObjectStorage(Protocol):
    def put(self, storage_key: str, content: bytes, content_type: str) -> None:
        """Upload the object; raise StorageUnavailableError on failure."""

    def presign_download(self, storage_key: str, expires_in_seconds: int) -> str:
        """Return a signed URL to download the object; raise StorageUnavailableError on failure."""
