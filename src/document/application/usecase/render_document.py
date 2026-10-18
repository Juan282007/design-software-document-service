"""Use case for HU-DOC-004: render a document, store it, version it and publish its events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from document.application.port.outbound.document_repository import DocumentRepository
from document.application.port.outbound.event_publisher import EventPublisher
from document.application.port.outbound.object_storage import ObjectStorage
from document.application.port.outbound.pdf_renderer import PdfRenderer, TemplateEngine
from document.application.port.outbound.template_repository import TemplateRepository
from document.domain.model.document import (
    GENERATED_BY,
    SYSTEM_ACTOR_ID,
    Document,
    DocumentVersion,
    DuplicateVersionError,
    NonRetryableRenderError,
    RenderError,
)
from document.domain.model.events import DOCUMENT_GENERATED, VERSION_CREATED, DomainEvent

PDF_MIME_TYPE = "application/pdf"
NOT_RENDERABLE_STATUSES = frozenset({"ARCHIVED", "EXPIRED"})
FAILABLE_STATUSES = frozenset({"GENERATING", "GENERATION_FAILED"})


@dataclass(frozen=True)
class RenderRequest:
    document_id: UUID
    template_code: str
    data_payload: dict[str, object]
    correlation_id: str


@dataclass(frozen=True)
class RenderOutcome:
    version: DocumentVersion
    duplicate: bool


def build_storage_key(document: Document, version_number: int) -> str:
    """<domain>/<year>/<month>/<owner_entity_id>/<document_id>-v<n>.pdf (storage-adapters.md)."""
    created = document.created_at
    return (
        f"{document.domain}/{created:%Y}/{created:%m}/{document.owner_entity_id}/"
        f"{document.id}-v{version_number}.pdf"
    )


def _event_id(version_id: UUID, event_type: str) -> UUID:
    """Deterministic, so a republished event keeps its identity and consumers can deduplicate it."""
    return uuid5(NAMESPACE_URL, f"{version_id}:{event_type}")


class RenderDocumentService:
    def __init__(
        self,
        documents: DocumentRepository,
        templates: TemplateRepository,
        engine: TemplateEngine,
        renderer: PdfRenderer,
        storage: ObjectStorage,
        events: EventPublisher,
    ) -> None:
        self._documents = documents
        self._templates = templates
        self._engine = engine
        self._renderer = renderer
        self._storage = storage
        self._events = events

    def process(self, request: RenderRequest) -> RenderOutcome:
        document = self._documents.get(request.document_id)
        if document is None:
            raise NonRetryableRenderError("el documento no existe")
        if document.status in NOT_RENDERABLE_STATUSES:
            raise NonRetryableRenderError(f"el documento está {document.status} y no se puede generar")

        existing = self._documents.get_version_by_correlation(document.id, request.correlation_id)
        if existing is not None:
            # Redelivered message: nothing is rendered again, but the events are republished
            # (same event_id) in case the previous attempt stopped before publishing them.
            self._publish_events(document, existing, request, document.size_bytes or 0)
            return RenderOutcome(version=existing, duplicate=True)

        template = self._templates.get(request.template_code)
        if template is None or not template.is_active:
            raise NonRetryableRenderError("no existe una plantilla activa para template_code")
        if template.output_type != "PDF":
            raise NonRetryableRenderError(f"output_type {template.output_type} no está soportado por este worker")

        html = self._engine.render(template.template_body, request.data_payload)
        if not html.strip():
            raise NonRetryableRenderError("la plantilla no produjo contenido (revisar bloques sin cerrar)")
        pdf = self._renderer.render(html)
        if not pdf.startswith(b"%PDF"):
            raise RenderError("el motor de PDF no generó un PDF válido")

        version_number = self._documents.next_version_number(document.id)
        storage_key = build_storage_key(document, version_number)
        self._storage.put(storage_key, pdf, PDF_MIME_TYPE)

        version = DocumentVersion(
            id=uuid4(),
            document_id=document.id,
            version_number=version_number,
            storage_key=storage_key,
            notes=None,
            created_at=datetime.now(UTC),
            created_by=SYSTEM_ACTOR_ID,
            correlation_id=request.correlation_id,
        )
        try:
            updated = self._documents.complete_generation(
                document_id=document.id,
                version=version,
                mime_type=PDF_MIME_TYPE,
                size_bytes=len(pdf),
                updated_by=SYSTEM_ACTOR_ID,
            )
        except DuplicateVersionError:
            existing = self._documents.get_version_by_correlation(document.id, request.correlation_id)
            assert existing is not None
            self._publish_events(document, existing, request, len(pdf))
            return RenderOutcome(version=existing, duplicate=True)

        self._publish_events(updated, version, request, len(pdf))
        return RenderOutcome(version=version, duplicate=False)

    def mark_failed(self, request: RenderRequest) -> None:
        """Final failure: a document still waiting for its first file becomes GENERATION_FAILED.

        A document that is already AVAILABLE (a failed regeneration) keeps its current version.
        """
        document = self._documents.get(request.document_id)
        if document is not None and document.status in FAILABLE_STATUSES:
            self._documents.mark_generation_failed(document.id)

    def _publish_events(
        self, document: Document, version: DocumentVersion, request: RenderRequest, size_bytes: int
    ) -> None:
        if version.version_number == 1:
            self._events.publish(
                DomainEvent(
                    event_id=_event_id(version.id, DOCUMENT_GENERATED),
                    event_type=DOCUMENT_GENERATED,
                    timestamp=version.created_at,
                    correlation_id=request.correlation_id,
                    payload={
                        "document_id": str(document.id),
                        "template_code": request.template_code,
                        "domain": document.domain,
                        "owner_entity_id": str(document.owner_entity_id),
                        "owner_service": document.owner_service,
                        "storage_key": version.storage_key,
                        "mime_type": document.mime_type or PDF_MIME_TYPE,
                        "size_bytes": size_bytes,
                        "generated_by": GENERATED_BY,
                        "generated_at": version.created_at.isoformat().replace("+00:00", "Z"),
                    },
                )
            )
        self._events.publish(
            DomainEvent(
                event_id=_event_id(version.id, VERSION_CREATED),
                event_type=VERSION_CREATED,
                timestamp=version.created_at,
                correlation_id=request.correlation_id,
                payload={
                    "version_id": str(version.id),
                    "document_id": str(document.id),
                    "version_number": version.version_number,
                    "storage_key": version.storage_key,
                    "created_by": GENERATED_BY,
                    "created_at": version.created_at.isoformat().replace("+00:00", "Z"),
                },
            )
        )
