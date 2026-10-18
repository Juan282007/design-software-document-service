"""Domain concepts for documents requested to the generation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

DOMAINS = frozenset({"SCHEDULE", "FICHA", "CERTIFICATE", "ACTOR", "REPORT"})
STATUS_GENERATING = "GENERATING"
STATUS_AVAILABLE = "AVAILABLE"
STATUS_GENERATION_FAILED = "GENERATION_FAILED"
STATUSES = frozenset({"GENERATING", "AVAILABLE", "ARCHIVED", "EXPIRED", "GENERATION_FAILED"})
# Reserved system actor (ADR-004) used for automatic actions of the workers.
SYSTEM_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000000")
GENERATED_BY = "pdf-renderer-worker"


class DocumentError(Exception):
    """Base error for document use cases."""


class DocumentValidationError(DocumentError):
    pass


class DocumentNotFoundError(DocumentError):
    pass


class GenerationQueueUnavailableError(DocumentError):
    pass


class DocumentNotAvailableError(DocumentError):
    pass


class StorageUnavailableError(DocumentError):
    pass


class RenderError(DocumentError):
    """Transient failure while rendering, storing or publishing: the message is retried."""


class NonRetryableRenderError(DocumentError):
    """Failure that retrying cannot fix (missing template, invalid message): it goes straight to the DLQ."""


class DuplicateVersionError(DocumentError):
    pass


@dataclass(frozen=True)
class Document:
    id: UUID
    template_id: UUID | None
    title: str
    domain: str
    owner_service: str
    owner_entity_id: UUID
    status: str
    row_version: int
    created_at: datetime
    created_by: UUID
    updated_at: datetime
    updated_by: UUID | None
    mime_type: str | None = None
    size_bytes: int | None = None
    training_center_id: UUID | None = None
    storage_key: str | None = None


@dataclass(frozen=True)
class GenerationRequest:
    """Message published to document-generation-queue."""

    document_id: UUID
    template_code: str
    data_payload: dict[str, object]
    correlation_id: str


@dataclass(frozen=True)
class DocumentVersion:
    id: UUID
    document_id: UUID
    version_number: int
    storage_key: str
    notes: str | None
    created_at: datetime
    created_by: UUID | None
    correlation_id: str | None = None


@dataclass(frozen=True)
class DocumentAccess:
    """Data scope of the caller, resolved from the IAM feature:scope pairs of the token."""

    global_scope: bool
    training_center_id: UUID | None = None

    def allows(self, document: Document) -> bool:
        if self.global_scope:
            return True
        return self.training_center_id is not None and document.training_center_id == self.training_center_id
