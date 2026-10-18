"""Domain concepts for document templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

OUTPUT_TYPES = frozenset({"PDF", "EXCEL", "WORD"})


class TemplateError(Exception):
    """Base error for template use cases."""


class TemplateNotFoundError(TemplateError):
    pass


class TemplateInactiveError(TemplateError):
    pass


class TemplateCodeAlreadyExistsError(TemplateError):
    pass


class TemplateValidationError(TemplateError):
    pass


@dataclass(frozen=True)
class DocumentTemplate:
    id: UUID
    code: str
    name: str
    template_body: str
    output_type: str
    version: int
    is_active: bool
    created_at: datetime
    created_by: UUID | None
    updated_at: datetime | None
    updated_by: UUID | None
