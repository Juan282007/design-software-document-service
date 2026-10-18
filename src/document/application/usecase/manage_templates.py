"""Use cases for HU-DOC-001: manage document templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from uuid import UUID, uuid4

from pybars import Compiler, PybarsError

from document.application.port.outbound.template_repository import TemplateRepository
from document.domain.model.template import (
    OUTPUT_TYPES,
    DocumentTemplate,
    TemplateCodeAlreadyExistsError,
    TemplateInactiveError,
    TemplateNotFoundError,
    TemplateValidationError,
)


@dataclass(frozen=True)
class CreateTemplateCommand:
    code: str
    name: str
    template_body: str
    output_type: str
    actor_id: UUID


@dataclass(frozen=True)
class UpdateTemplateCommand:
    id: UUID
    name: str | None
    template_body: str | None
    output_type: str | None
    is_active: bool | None
    actor_id: UUID


class TemplateService:
    _SORT_PATTERN = re.compile(r"^(code|name|output_type|version|is_active|created_at|updated_at):(asc|desc)$")
    _PLACEHOLDER_PATTERN = re.compile(r"{{{?\s*([A-Za-z_][\w.]*)\s*}?}}")

    def __init__(self, repository: TemplateRepository) -> None:
        self._repository = repository
        self._handlebars = Compiler()

    def list_templates(
        self,
        *,
        page: int,
        page_size: int,
        output_type: str | None,
        code: str | None,
        is_active: bool | None,
        sort: str | None,
    ) -> tuple[list[DocumentTemplate], int]:
        sort_field, sort_direction = self._parse_sort(sort)
        return self._repository.list(
            page=page,
            page_size=page_size,
            output_type=output_type,
            code=code,
            is_active=is_active,
            sort_field=sort_field,
            sort_direction=sort_direction,
        )

    def get_template(self, identifier: UUID | str) -> DocumentTemplate:
        template = self._repository.get(identifier)
        if template is None:
            raise TemplateNotFoundError()
        return template

    def create_template(self, command: CreateTemplateCommand) -> DocumentTemplate:
        self._validate(command.code, command.name, command.template_body, command.output_type)
        if self._repository.exists_by_code(command.code):
            raise TemplateCodeAlreadyExistsError()
        now = datetime.now(UTC)
        return self._repository.save(
            DocumentTemplate(
                id=uuid4(),
                code=command.code,
                name=command.name,
                template_body=command.template_body,
                output_type=command.output_type,
                version=1,
                is_active=True,
                created_at=now,
                created_by=command.actor_id,
                updated_at=now,
                updated_by=command.actor_id,
            )
        )

    def update_template(self, command: UpdateTemplateCommand) -> DocumentTemplate:
        current = self._repository.get_by_id(command.id)
        if current is None:
            raise TemplateNotFoundError()
        name = command.name if command.name is not None else current.name
        body = command.template_body if command.template_body is not None else current.template_body
        output_type = command.output_type if command.output_type is not None else current.output_type
        self._validate(current.code, name, body, output_type)
        active = command.is_active if command.is_active is not None else current.is_active
        return self._repository.save(
            DocumentTemplate(
                id=current.id,
                code=current.code,
                name=name,
                template_body=body,
                output_type=output_type,
                version=current.version + 1,
                is_active=active,
                created_at=current.created_at,
                created_by=current.created_by,
                updated_at=datetime.now(UTC),
                updated_by=command.actor_id,
            )
        )

    def deactivate_template(self, identifier: UUID, actor_id: UUID) -> None:
        current = self._repository.get_by_id(identifier)
        if current is None:
            raise TemplateNotFoundError()
        if not current.is_active:
            raise TemplateInactiveError()
        self._repository.save(
            DocumentTemplate(
                id=current.id,
                code=current.code,
                name=current.name,
                template_body=current.template_body,
                output_type=current.output_type,
                version=current.version,
                is_active=False,
                created_at=current.created_at,
                created_by=current.created_by,
                updated_at=datetime.now(UTC),
                updated_by=actor_id,
            )
        )

    def preview_template(self, identifier: UUID, sample_data: dict[str, object]) -> tuple[DocumentTemplate, str, list[str]]:
        template = self._repository.get_by_id(identifier)
        if template is None:
            raise TemplateNotFoundError()
        if not template.is_active:
            raise TemplateInactiveError()
        try:
            compiled = self._handlebars.compile(template.template_body)
            missing = sorted(
                {
                    placeholder
                    for placeholder in self._PLACEHOLDER_PATTERN.findall(template.template_body)
                    if not self._has_value(sample_data, placeholder)
                }
            )
            rendered = str(compiled(sample_data))
        except PybarsError as error:
            raise TemplateValidationError("template_body contiene sintaxis Handlebars inválida") from error
        return template, rendered, missing

    def _validate(self, code: str, name: str, template_body: str, output_type: str) -> None:
        if not code.strip() or not name.strip() or not template_body.strip():
            raise TemplateValidationError("code, name y template_body son obligatorios")
        if output_type not in OUTPUT_TYPES:
            raise TemplateValidationError("output_type debe ser PDF, EXCEL o WORD")
        try:
            self._handlebars.compile(template_body)
        except PybarsError as error:
            raise TemplateValidationError("template_body contiene sintaxis Handlebars inválida") from error

    @classmethod
    def _parse_sort(cls, sort: str | None) -> tuple[str, str]:
        value = sort or "created_at:desc"
        match = cls._SORT_PATTERN.fullmatch(value)
        if match is None:
            raise TemplateValidationError(
                "sort debe usar <campo>:<asc|desc>; campos permitidos: "
                "code, name, output_type, version, is_active, created_at, updated_at"
            )
        return match.group(1), match.group(2)

    @staticmethod
    def _has_value(sample_data: dict[str, object], placeholder: str) -> bool:
        current: object = sample_data
        for segment in placeholder.split("."):
            if not isinstance(current, dict) or segment not in current:
                return False
            current = current[segment]
        return True
