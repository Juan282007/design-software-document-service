"""Use case for HU-DOC-002: request the asynchronous generation of a document."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from document.application.port.outbound.document_generation_publisher import DocumentGenerationPublisher
from document.application.port.outbound.document_repository import DocumentRepository
from document.application.port.outbound.template_repository import TemplateRepository
from document.domain.model.document import (
    DOMAINS,
    STATUS_GENERATING,
    Document,
    DocumentValidationError,
    GenerationQueueUnavailableError,
    GenerationRequest,
)
from document.domain.model.template import TemplateNotFoundError


@dataclass(frozen=True)
class GenerateDocumentCommand:
    template_code: str
    domain: str
    owner_service: str
    owner_entity_id: UUID
    title: str
    data: dict[str, object]
    actor_id: UUID
    correlation_id: str
    training_center_id: UUID | None = None


class DocumentGenerationService:
    def __init__(
        self,
        documents: DocumentRepository,
        templates: TemplateRepository,
        publisher: DocumentGenerationPublisher,
    ) -> None:
        self._documents = documents
        self._templates = templates
        self._publisher = publisher

    def request_generation(self, command: GenerateDocumentCommand) -> Document:
        if command.domain not in DOMAINS:
            raise DocumentValidationError("domain debe ser SCHEDULE, FICHA, CERTIFICATE, ACTOR o REPORT")
        template = self._templates.get(command.template_code)
        if template is None or not template.is_active:
            raise TemplateNotFoundError()

        now = datetime.now(UTC)
        document = self._documents.add(
            Document(
                id=uuid4(),
                template_id=template.id,
                title=command.title,
                domain=command.domain,
                owner_service=command.owner_service,
                owner_entity_id=command.owner_entity_id,
                status=STATUS_GENERATING,
                row_version=0,
                created_at=now,
                created_by=command.actor_id,
                updated_at=now,
                updated_by=command.actor_id,
                training_center_id=command.training_center_id,
            )
        )
        try:
            self._publisher.publish(
                GenerationRequest(
                    document_id=document.id,
                    template_code=template.code,
                    data_payload=command.data,
                    correlation_id=command.correlation_id,
                )
            )
        except GenerationQueueUnavailableError:
            self._documents.mark_generation_failed(document.id)
            raise
        return document
