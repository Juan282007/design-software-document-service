"""FastAPI adapter for document-api: request generation (HU-DOC-002) and query/download (HU-DOC-003)."""

import os
from collections.abc import Callable, Generator
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from document.adapter.inbound.http.security import Principal, require_feature, resolve_document_access
from document.adapter.outbound.messaging.document_generation_publisher import RabbitMqDocumentGenerationPublisher
from document.adapter.outbound.persistence.document_repository import SqlAlchemyDocumentRepository
from document.adapter.outbound.persistence.template_repository import SqlAlchemyTemplateRepository
from document.adapter.outbound.storage.s3_object_storage import S3ObjectStorage
from document.application.port.outbound.document_generation_publisher import DocumentGenerationPublisher
from document.application.port.outbound.object_storage import ObjectStorage
from document.application.usecase.query_documents import DEFAULT_SIGNED_URL_TTL_SECONDS, DocumentQueryService
from document.application.usecase.request_document_generation import (
    DocumentGenerationService,
    GenerateDocumentCommand,
)
from document.domain.model.document import (
    Document,
    DocumentNotAvailableError,
    DocumentNotFoundError,
    DocumentValidationError,
    DocumentVersion,
    GenerationQueueUnavailableError,
    StorageUnavailableError,
)
from document.domain.model.template import TemplateNotFoundError

GENERATE_FEATURE = "DOC_CREATE"
VIEW_FEATURE = "DOC_VIEW"
DOWNLOAD_FEATURE = "DOC_DOWNLOAD"
CORRELATION_HEADER = "X-Correlation-Id"
DOMAIN_PATTERN = r"^(SCHEDULE|FICHA|CERTIFICATE|ACTOR|REPORT)$"
STATUS_PATTERN = r"^(GENERATING|AVAILABLE|ARCHIVED|EXPIRED|GENERATION_FAILED)$"


class GenerateDocumentBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    template_code: str = Field(min_length=1, max_length=50)
    domain: str = Field(pattern=DOMAIN_PATTERN)
    owner_service: str = Field(min_length=1, max_length=100)
    owner_entity_id: UUID
    title: str = Field(min_length=1, max_length=300)
    data: dict[str, Any] = Field(default_factory=dict)


def _error(code: str, message: str, http_status: int, details: list[dict[str, Any]] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message, "details": details or [], "trace_id": None}},
    )


def _document_response(document: Document) -> dict[str, Any]:
    """Document metadata. storage_key and download URLs are never exposed here."""
    return {
        "id": str(document.id),
        "template_id": str(document.template_id) if document.template_id else None,
        "title": document.title,
        "domain": document.domain,
        "owner_service": document.owner_service,
        "owner_entity_id": str(document.owner_entity_id),
        "mime_type": document.mime_type,
        "size_bytes": document.size_bytes,
        "status": document.status,
        "row_version": document.row_version,
        "created_by": str(document.created_by),
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }


def _version_response(version: DocumentVersion) -> dict[str, Any]:
    """Version metadata. storage_key is never exposed to the client."""
    return {
        "id": str(version.id),
        "document_id": str(version.document_id),
        "version_number": version.version_number,
        "notes": version.notes,
        "created_by": str(version.created_by) if version.created_by else None,
        "created_at": version.created_at.isoformat(),
    }


def _pagination(page: int, page_size: int, total: int) -> dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


def _database_engine() -> Engine:
    database_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DB_URL debe configurarse para document-api")
    return create_engine(database_url, pool_pre_ping=True)


def _signed_url_ttl() -> int:
    return int(os.getenv("SIGNED_URL_TTL_SECONDS", str(DEFAULT_SIGNED_URL_TTL_SECONDS)))


def create_document_app(
    *,
    engine: Engine | None = None,
    authorizer: Callable[[str], Callable[..., Principal]] = require_feature,
    publisher: DocumentGenerationPublisher | None = None,
    storage: ObjectStorage | None = None,
    signed_url_ttl_seconds: int | None = None,
) -> FastAPI:
    """Build the app; the optional arguments make API integration tests self-contained."""
    app = FastAPI(title="document_api", version="1.0.0")
    session_factory = sessionmaker(bind=engine or _database_engine(), autoflush=False, expire_on_commit=False)
    generation_publisher = publisher or RabbitMqDocumentGenerationPublisher()
    object_storage = storage or S3ObjectStorage()
    ttl = signed_url_ttl_seconds if signed_url_ttl_seconds is not None else _signed_url_ttl()

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        details = [{key: item[key] for key in ("loc", "msg", "type") if key in item} for item in error.errors()]
        return _error("VALIDATION_ERROR", "La solicitud no cumple el contrato", 422, details)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, error: HTTPException) -> JSONResponse:
        if isinstance(error.detail, dict):
            return _error(
                error.detail.get("code", "REQUEST_ERROR"),
                error.detail.get("message", "Solicitud rechazada"),
                error.status_code,
            )
        return _error("REQUEST_ERROR", str(error.detail), error.status_code)

    def get_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def generation_service(session: Annotated[Session, Depends(get_session)]) -> DocumentGenerationService:
        return DocumentGenerationService(
            SqlAlchemyDocumentRepository(session),
            SqlAlchemyTemplateRepository(session),
            generation_publisher,
        )

    def query_service(session: Annotated[Session, Depends(get_session)]) -> DocumentQueryService:
        return DocumentQueryService(SqlAlchemyDocumentRepository(session), object_storage, ttl)

    router = APIRouter(prefix="/api/v1", tags=["documents"])
    generate_access = authorizer(GENERATE_FEATURE)
    view_access = authorizer(VIEW_FEATURE)
    download_access = authorizer(DOWNLOAD_FEATURE)

    not_found = {"code": "DOCUMENT_NOT_FOUND", "message": "El documento no existe"}

    @router.post("/documents/generate", status_code=status.HTTP_202_ACCEPTED)
    def generate_document(
        body: GenerateDocumentBody,
        response: Response,
        use_case: DocumentGenerationService = Depends(generation_service),
        principal: Principal = Depends(generate_access),
        correlation_id: Annotated[str | None, Header(alias=CORRELATION_HEADER)] = None,
    ) -> dict[str, Any]:
        correlation = correlation_id or str(uuid4())
        try:
            document = use_case.request_generation(
                GenerateDocumentCommand(
                    template_code=body.template_code,
                    domain=body.domain,
                    owner_service=body.owner_service,
                    owner_entity_id=body.owner_entity_id,
                    title=body.title,
                    data=body.data,
                    actor_id=principal.subject,
                    correlation_id=correlation,
                    training_center_id=principal.training_center_id,
                )
            )
        except TemplateNotFoundError:
            raise HTTPException(404, {"code": "TEMPLATE_NOT_FOUND", "message": "No existe una plantilla activa para template_code"})
        except DocumentValidationError as error:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(error)})
        except GenerationQueueUnavailableError:
            raise HTTPException(
                503,
                {"code": "GENERATION_QUEUE_UNAVAILABLE", "message": "No fue posible encolar la generación del documento"},
            )
        response.headers[CORRELATION_HEADER] = correlation
        return {"document_id": str(document.id), "status": document.status}

    @router.get("/documents")
    def list_documents(
        use_case: DocumentQueryService = Depends(query_service),
        principal: Principal = Depends(view_access),
        owner_service: Annotated[str | None, Query(max_length=100)] = None,
        owner_entity_id: UUID | None = None,
        domain: Annotated[str | None, Query(pattern=DOMAIN_PATTERN)] = None,
        document_status: Annotated[str | None, Query(alias="status", pattern=STATUS_PATTERN)] = None,
        sort: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        access = resolve_document_access(principal, VIEW_FEATURE)
        try:
            documents, total = use_case.list_documents(
                access=access,
                owner_service=owner_service,
                owner_entity_id=owner_entity_id,
                domain=domain,
                status=document_status,
                sort=sort,
                page=page,
                page_size=page_size,
            )
        except DocumentValidationError as error:
            raise HTTPException(400, {"code": "INVALID_SORT", "message": str(error)})
        return {
            "data": [_document_response(document) for document in documents],
            "pagination": _pagination(page, page_size, total),
        }

    @router.get("/documents/{document_id}")
    def get_document(
        document_id: UUID,
        use_case: DocumentQueryService = Depends(query_service),
        principal: Principal = Depends(view_access),
    ) -> dict[str, Any]:
        access = resolve_document_access(principal, VIEW_FEATURE)
        try:
            return _document_response(use_case.get_document(document_id, access))
        except DocumentNotFoundError:
            raise HTTPException(404, not_found)

    @router.get("/documents/{document_id}/versions")
    def list_versions(
        document_id: UUID,
        use_case: DocumentQueryService = Depends(query_service),
        principal: Principal = Depends(view_access),
    ) -> list[dict[str, Any]]:
        access = resolve_document_access(principal, VIEW_FEATURE)
        try:
            versions = use_case.list_versions(document_id, access)
        except DocumentNotFoundError:
            raise HTTPException(404, not_found)
        return [_version_response(version) for version in versions]

    @router.get("/documents/{document_id}/versions/{version_id}")
    def get_version(
        document_id: UUID,
        version_id: UUID,
        use_case: DocumentQueryService = Depends(query_service),
        principal: Principal = Depends(view_access),
    ) -> dict[str, Any]:
        access = resolve_document_access(principal, VIEW_FEATURE)
        try:
            return _version_response(use_case.get_version(document_id, version_id, access))
        except DocumentNotFoundError:
            raise HTTPException(404, {"code": "DOCUMENT_VERSION_NOT_FOUND", "message": "La versión no existe"})

    @router.get("/documents/{document_id}/download-url")
    def get_download_url(
        document_id: UUID,
        use_case: DocumentQueryService = Depends(query_service),
        principal: Principal = Depends(download_access),
    ) -> dict[str, Any]:
        access = resolve_document_access(principal, DOWNLOAD_FEATURE)
        try:
            document, url, expires_in = use_case.get_download_url(document_id, access)
        except DocumentNotFoundError:
            raise HTTPException(404, not_found)
        except DocumentNotAvailableError:
            raise HTTPException(
                409, {"code": "DOCUMENT_NOT_AVAILABLE", "message": "El documento aún no está disponible para descarga"}
            )
        except StorageUnavailableError:
            raise HTTPException(
                503, {"code": "STORAGE_UNAVAILABLE", "message": "No fue posible generar la URL de descarga"}
            )
        return {"document_id": str(document.id), "url": url, "expires_in": expires_in}

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
