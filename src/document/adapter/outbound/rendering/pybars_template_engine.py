"""Handlebars template engine (pybars3), the same engine template-api uses for previews."""

from __future__ import annotations

from pybars import Compiler, PybarsError

from document.domain.model.document import NonRetryableRenderError


class PybarsTemplateEngine:
    def __init__(self) -> None:
        self._compiler = Compiler()

    def render(self, template_body: str, data: dict[str, object]) -> str:
        try:
            return str(self._compiler.compile(template_body)(data))
        except PybarsError as error:
            raise NonRetryableRenderError("la plantilla contiene sintaxis Handlebars inválida") from error
