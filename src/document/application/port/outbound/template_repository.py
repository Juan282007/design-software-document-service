"""Outbound port for the document-template aggregate."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from document.domain.model.template import DocumentTemplate


class TemplateRepository(Protocol):
    def list(
        self,
        *,
        page: int,
        page_size: int,
        output_type: str | None,
        code: str | None,
        is_active: bool | None,
        sort_field: str,
        sort_direction: str,
    ) -> tuple[list[DocumentTemplate], int]: ...

    def get(self, identifier: UUID | str) -> DocumentTemplate | None: ...

    def get_by_id(self, identifier: UUID) -> DocumentTemplate | None: ...

    def exists_by_code(self, code: str) -> bool: ...

    def save(self, template: DocumentTemplate) -> DocumentTemplate: ...
