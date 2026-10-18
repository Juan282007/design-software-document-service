"""Outbound ports that turn a template and its data into a PDF."""

from __future__ import annotations

from typing import Protocol


class TemplateEngine(Protocol):
    def render(self, template_body: str, data: dict[str, object]) -> str:
        """Render the template with the data; raise NonRetryableRenderError if the template is invalid."""


class PdfRenderer(Protocol):
    def render(self, html: str) -> bytes:
        """Convert HTML into a PDF; raise RenderError if the engine fails."""
