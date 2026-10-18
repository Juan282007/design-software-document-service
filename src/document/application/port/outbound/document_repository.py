"""Outbound port for the document aggregate."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from document.domain.model.document import Document, DocumentAccess, DocumentVersion


class DocumentRepository(Protocol):
    def add(self, document: Document) -> Document: ...

    def get(self, identifier: UUID) -> Document | None: ...

    def mark_generation_failed(self, identifier: UUID) -> None: ...

    def get_version_by_correlation(self, document_id: UUID, correlation_id: str) -> DocumentVersion | None: ...

    def next_version_number(self, document_id: UUID) -> int: ...

    def complete_generation(
        self,
        *,
        document_id: UUID,
        version: DocumentVersion,
        mime_type: str,
        size_bytes: int,
        updated_by: UUID,
    ) -> Document:
        """Store the version and point the document to it, as AVAILABLE, in one transaction.

        Raise DuplicateVersionError if the same correlation_id was already stored for the document.
        """

    def list(
        self,
        *,
        access: DocumentAccess,
        owner_service: str | None,
        owner_entity_id: UUID | None,
        domain: str | None,
        status: str | None,
        sort_field: str,
        sort_direction: str,
        page: int,
        page_size: int,
    ) -> tuple[list[Document], int]: ...

    def list_versions(self, document_id: UUID) -> list[DocumentVersion]: ...

    def get_version(self, document_id: UUID, version_id: UUID) -> DocumentVersion | None: ...
