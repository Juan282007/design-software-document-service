"""FastAPI adapter for HU-DOC-001."""

import os
from collections.abc import Callable, Generator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from document.adapter.inbound.http.security import Principal, require_feature
from document.adapter.outbound.persistence.template_repository import SqlAlchemyTemplateRepository
from document.application.usecase.manage_templates import (
    CreateTemplateCommand,
    TemplateService,
    UpdateTemplateCommand,
)
from document.domain.model.template import (
    TemplateCodeAlreadyExistsError,
    TemplateError,
    TemplateInactiveError,
    TemplateNotFoundError,
    TemplateValidationError,
)


class TemplateCreateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    template_body: str = Field(min_length=1)
    output_type: str = Field(pattern=r"^(PDF|EXCEL|WORD)$")


class TemplateUpdateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    template_body: str | None = Field(default=None, min_length=1)
    output_type: str | None = Field(default=None, pattern=r"^(PDF|EXCEL|WORD)$")
    is_active: bool | None = None


class PreviewBody(BaseModel):
    sample_data: dict[str, Any]


def _error(code: str, message: str, http_status: int, details: list[dict[str, Any]] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message, "details": details or [], "trace_id": None}},
    )


def _template_response(template: Any, include_body: bool = True) -> dict[str, Any]:
    payload = {
        "id": str(template.id),
        "code": template.code,
        "name": template.name,
        "output_type": template.output_type,
        "version": template.version,
        "is_active": template.is_active,
    }
    if include_body:
        payload["template_body"] = template.template_body
    return payload


def _database_engine() -> Engine:
    database_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DB_URL debe configurarse para template-api")
    return create_engine(database_url, pool_pre_ping=True)


def create_template_app(
    *,
    engine: Engine | None = None,
    authorizer: Callable[[str], Callable[..., Principal]] = require_feature,
) -> FastAPI:
    """Build an app; the optional arguments make API integration tests self-contained."""
    app = FastAPI(title="template-api", version="1.0.0")
    session_factory = sessionmaker(bind=engine or _database_engine(), autoflush=False, expire_on_commit=False)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        return _error("VALIDATION_ERROR", "La solicitud no cumple el contrato", 422, error.errors())

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, error: HTTPException) -> JSONResponse:
        if isinstance(error.detail, dict):
            return _error(error.detail.get("code", "REQUEST_ERROR"), error.detail.get("message", "Solicitud rechazada"), error.status_code)
        return _error("REQUEST_ERROR", str(error.detail), error.status_code)

    @app.exception_handler(IntegrityError)
    async def integrity_exception_handler(_: Request, __: IntegrityError) -> JSONResponse:
        return _error("TEMPLATE_CODE_ALREADY_EXISTS", "Ya existe una plantilla con ese code", 409)

    def get_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def service(session: Annotated[Session, Depends(get_session)]) -> TemplateService:
        return TemplateService(SqlAlchemyTemplateRepository(session))

    router = APIRouter(prefix="/api/v1", tags=["templates"])
    read_access = authorizer("DOC_TEMPLATE_VIEW")
    manage_access = authorizer("DOC_TEMPLATE_MANAGE")

    @router.get("/document-templates")
    def list_templates(
        use_case: TemplateService = Depends(service),
        _: Principal = Depends(read_access),
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        output_type: str | None = None,
        code: str | None = None,
        is_active: bool | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        try:
            templates, total = use_case.list_templates(
                page=page,
                page_size=page_size,
                output_type=output_type,
                code=code,
                is_active=is_active,
                sort=sort,
            )
        except TemplateValidationError as error:
            raise HTTPException(400, {"code": "INVALID_SORT", "message": str(error)})
        return {
            "data": [_template_response(template, include_body=False) for template in templates],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }

    @router.post("/document-templates", status_code=status.HTTP_201_CREATED)
    def create_template(
        body: TemplateCreateBody,
        use_case: TemplateService = Depends(service),
        principal: Principal = Depends(manage_access),
    ) -> dict[str, Any]:
        try:
            template = use_case.create_template(
                CreateTemplateCommand(actor_id=principal.subject, **body.model_dump())
            )
            return _template_response(template)
        except TemplateCodeAlreadyExistsError:
            raise HTTPException(409, {"code": "TEMPLATE_CODE_ALREADY_EXISTS", "message": "Ya existe una plantilla con ese code"})
        except TemplateValidationError as error:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(error)})

    @router.get("/document-templates/{identifier}")
    def get_template(
        identifier: str,
        use_case: TemplateService = Depends(service),
        _: Principal = Depends(read_access),
    ) -> dict[str, Any]:
        parsed_identifier: UUID | str
        try:
            parsed_identifier = UUID(identifier)
        except ValueError:
            parsed_identifier = identifier
        try:
            return _template_response(use_case.get_template(parsed_identifier))
        except TemplateNotFoundError:
            raise HTTPException(404, {"code": "TEMPLATE_NOT_FOUND", "message": "La plantilla no existe"})

    @router.put("/document-templates/{template_id}")
    def update_template(
        template_id: UUID,
        body: TemplateUpdateBody,
        use_case: TemplateService = Depends(service),
        principal: Principal = Depends(manage_access),
    ) -> dict[str, Any]:
        try:
            template = use_case.update_template(
                UpdateTemplateCommand(id=template_id, actor_id=principal.subject, **body.model_dump())
            )
            return _template_response(template)
        except TemplateNotFoundError:
            raise HTTPException(404, {"code": "TEMPLATE_NOT_FOUND", "message": "La plantilla no existe"})
        except TemplateValidationError as error:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(error)})

    @router.delete("/document-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
    def deactivate_template(
        template_id: UUID,
        use_case: TemplateService = Depends(service),
        principal: Principal = Depends(manage_access),
    ) -> Response:
        try:
            use_case.deactivate_template(template_id, principal.subject)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except TemplateNotFoundError:
            raise HTTPException(404, {"code": "TEMPLATE_NOT_FOUND", "message": "La plantilla no existe"})
        except TemplateInactiveError:
            raise HTTPException(409, {"code": "TEMPLATE_INACTIVE", "message": "La plantilla ya está desactivada"})

    @router.post("/document-templates/{template_id}/preview")
    def preview_template(
        template_id: UUID,
        body: PreviewBody,
        use_case: TemplateService = Depends(service),
        _: Principal = Depends(manage_access),
    ) -> dict[str, Any]:
        try:
            template, rendered_html, missing = use_case.preview_template(template_id, body.sample_data)
            return {"output_type": template.output_type, "rendered_html": rendered_html, "missing_placeholders": missing}
        except TemplateNotFoundError:
            raise HTTPException(404, {"code": "TEMPLATE_NOT_FOUND", "message": "La plantilla no existe"})
        except TemplateInactiveError:
            raise HTTPException(409, {"code": "TEMPLATE_INACTIVE", "message": "La plantilla está desactivada"})
        except TemplateValidationError as error:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(error)})

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
