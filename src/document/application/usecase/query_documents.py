"""Use cases for HU-DOC-003: query documents, their versions and get a signed download URL."""

from __future__ import annotations

import re
from uuid import UUID

from document.application.port.outbound.document_repository import DocumentRepository
from document.application.port.outbound.object_storage import ObjectStorage
from document.domain.model.document import (
    STATUS_AVAILABLE,
    Document,
    DocumentAccess,
    DocumentNotAvailableError,
    DocumentNotFoundError,
    DocumentValidationError,
    DocumentVersion,
)

DEFAULT_SIGNED_URL_TTL_SECONDS = 300


class DocumentQueryService:
    _SORT_PATTERN = re.compile(r"^(created_at|updated_at|title|status|domain|owner_service):(asc|desc)$")

    def __init__(
        self,
        documents: DocumentRepository,
        storage: ObjectStorage,
        signed_url_ttl_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS,
    ) -> None:
        self._documents = documents
        self._storage = storage
        self._ttl = signed_url_ttl_seconds

    def list_documents(
        self,
        *,
        access: DocumentAccess,
        owner_service: str | None,
        owner_entity_id: UUID | None,
        domain: str | None,
        status: str | None,
        sort: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Document], int]:
        sort_field, sort_direction = self._parse_sort(sort)
        return self._documents.list(
            access=access,
            owner_service=owner_service,
            owner_entity_id=owner_entity_id,
            domain=domain,
            status=status,
            sort_field=sort_field,
            sort_direction=sort_direction,
            page=page,
            page_size=page_size,
        )

    def get_document(self, identifier: UUID, access: DocumentAccess) -> Document:
        document = self._documents.get(identifier)
        if document is None or not access.allows(document):
            # A document outside the caller's scope is indistinguishable from a missing one.
            raise DocumentNotFoundError()
        return document

    def list_versions(self, identifier: UUID, access: DocumentAccess) -> list[DocumentVersion]:
        self.get_document(identifier, access)
        return self._documents.list_versions(identifier)

    def get_version(self, identifier: UUID, version_id: UUID, access: DocumentAccess) -> DocumentVersion:
        self.get_document(identifier, access)
        version = self._documents.get_version(identifier, version_id)
        if version is None:
            raise DocumentNotFoundError()
        return version

    def get_download_url(self, identifier: UUID, access: DocumentAccess) -> tuple[Document, str, int]:
        document = self.get_document(identifier, access)
        if document.status != STATUS_AVAILABLE or not document.storage_key:
            raise DocumentNotAvailableError()
        url = self._storage.presign_download(document.storage_key, self._ttl)
        return document, url, self._ttl

    @classmethod
    def _parse_sort(cls, sort: str | None) -> tuple[str, str]:
        value = sort or "created_at:desc"
        match = cls._SORT_PATTERN.fullmatch(value)
        if match is None:
            raise DocumentValidationError(
                "sort debe usar <campo>:<asc|desc>; campos permitidos: "
                "created_at, updated_at, title, status, domain, owner_service"
            )
        return match.group(1), match.group(2)
